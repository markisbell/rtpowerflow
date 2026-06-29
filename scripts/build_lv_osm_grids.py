"""Build geographic, street-routed LV grids for every LV entry in the grid
library, from real OpenStreetMap data, and point the manifest at them.

ding0 0.2.1 does NOT geo-reference LV grids (its LV builder is a statistical
cable-string model with no coordinates). This script reconstructs each LV grid
geographically instead: it takes the LV station location and the load count from
the committed ding0 grid, then with OSM (osmnx) places the loads at building
footprints, routes a cable backbone along the street network (a shortest-path
tree from the station, sized for the downstream load with parallel cables) and
taps each building onto the nearest point of the road. Each line carries a
``geometry`` polyline so the live map draws cables along the actual streets.

Output: ``data/lv_osm/<entry_id>.json`` per LV grid, and the LV entries in
``data/grid_library.json`` get ``osm_grid`` + an updated ``nodes`` count.

Run with the Python-3.9 ding0 conda env (it has osmnx + geopandas):
    C:/Users/bell/ding0mamba/python.exe scripts/build_lv_osm_grids.py
Requires internet (OSM via overpass). Re-run any time to refresh.
"""
from __future__ import annotations

import json
import math
import sys
import traceback
from collections import defaultdict, deque
from pathlib import Path

import warnings
warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd
import networkx as nx
import osmnx as ox

REPO = Path(__file__).resolve().parents[1]
GRIDS = REPO / "data" / "ding0_grids"
OUT = REPO / "data" / "lv_osm"
MANIFEST = REPO / "data" / "grid_library.json"

# cable types: backbone NAYY 4x150SE, service NAYY 4x50SE (ohm/km, kA)
BACK = dict(r=0.206, x=0.080, imax=0.275)
SERV = dict(r=0.642, x=0.083, imax=0.142)


def _real(n):
    n = str(n)
    return n[8:] if n.startswith("virtual_") else n


def _hav(a, b):
    R = 6371000.0; (x1, y1), (x2, y2) = a, b
    p1, p2 = math.radians(y1), math.radians(y2)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(x2 - x1) / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(h)))


def build_lv_grid(district_dir: Path, lvid: str) -> dict | None:
    b = pd.read_csv(district_dir / "buses.csv")
    b["lvg"] = b["lv_grid_id"].astype(str).str.split(".").str[0]
    lv = b[b["lvg"] == lvid]
    bus = lv[lv["name"].str.startswith("BusBar_")]
    if bus.empty:
        return None
    busbar = bus["name"].iloc[0]
    lon0, lat0 = float(lv[lv.name == busbar]["x"]), float(lv[lv.name == busbar]["y"])
    loads = pd.read_csv(district_dir / "loads.csv")
    lvset = set(lv["name"])
    peaks = sorted([float(r.peak_load) for r in loads.itertuples() if _real(r.bus) in lvset], reverse=True)
    N = len(peaks)
    if N == 0:
        return None

    G = ox.graph_from_point((lat0, lon0), dist=600, network_type="all")
    G = ox.utils_graph.get_undirected(G)
    bld = ox.geometries_from_point((lat0, lon0), tags={"building": True}, dist=600)
    bld = bld[bld.geom_type.isin(["Polygon", "MultiPolygon"])].copy()
    bcent = [(g.centroid.x, g.centroid.y) for g in bld.geometry]
    if len(bcent) < N:
        N = len(bcent)
    if N == 0:
        return None

    gnodes = list(G.nodes); gx = np.array([G.nodes[n]["x"] for n in gnodes]); gy = np.array([G.nodes[n]["y"] for n in gnodes])
    def near(lon, lat): return gnodes[int(((gx - lon) ** 2 + (gy - lat) ** 2).argmin())]
    station_node = near(lon0, lat0)
    dist, paths = nx.single_source_dijkstra(G, station_node, weight="length")

    cand = [(c, near(*c)) for c in bcent]
    cand = [(c, nd) for (c, nd) in cand if nd in dist]
    cand.sort(key=lambda t: dist[t[1]])
    served = cand[:N]
    if not served:
        return None

    def egeom(u, v):
        data = G.get_edge_data(u, v)
        if data:
            d = data[list(data)[0]]
            if "geometry" in d:
                return [[x, y] for x, y in d["geometry"].coords]
        return [[G.nodes[u]["x"], G.nodes[u]["y"]], [G.nodes[v]["x"], G.nodes[v]["y"]]]
    def elen(u, v):
        data = G.get_edge_data(u, v); d = data[list(data)[0]] if data else {}
        return max(d.get("length", 5.0), 1.0)

    cosl = math.cos(math.radians(lat0))
    def mxy(lon, lat): return (lon * 111320 * cosl, lat * 110540)

    # every street edge with geometry pre-projected to metres, for fast queries
    street, seen_e = [], set()
    for a, b in G.edges():
        key = frozenset((a, b))
        if key in seen_e:
            continue
        seen_e.add(key)
        geom = egeom(a, b)
        street.append((a, b, geom, [mxy(x, y) for x, y in geom]))

    def nearest_edge(c):
        pm = mxy(*c); best = None
        for (u, v, geom, gm) in street:
            for k in range(len(gm) - 1):
                ax, ay = gm[k]; bx, by = gm[k + 1]
                dx, dy = bx - ax, by - ay; L2 = dx * dx + dy * dy
                t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((pm[0] - ax) * dx + (pm[1] - ay) * dy) / L2))
                cx, cy = ax + t * dx, ay + t * dy; dd = (pm[0] - cx) ** 2 + (pm[1] - cy) ** 2
                if best is None or dd < best[0]:
                    tap = (geom[k][0] + t * (geom[k + 1][0] - geom[k][0]),
                           geom[k][1] + t * (geom[k + 1][1] - geom[k][1]))
                    best = (dd, u, v, tap)
        return best

    # tap each served building onto its nearest road; record the road and the
    # connection node (its nearer endpoint), which the backbone must reach
    # the main line runs along the road each served building sits on
    bld_edge, used = [], set()
    for (c, _nd) in served:
        _, u, v, _tap = nearest_edge(c)
        bld_edge.append(c); used.add(frozenset((u, v)))
    # connectors: shortest street paths joining those roads to the substation
    conn = set()
    for node in {n for e in used for n in e}:
        p = paths.get(node, [])
        for i in range(len(p) - 1):
            conn.add(frozenset((p[i], p[i + 1])))
    used = {e for e in used if len(e) == 2}     # drop self-loop streets (size-1)
    conn = {e for e in conn if len(e) == 2}
    # backbone = spanning tree that KEEPS every house-street (added first), plus
    # the shortest connectors — so the main line always follows the houses
    uf = {}
    def find(x):
        uf.setdefault(x, x); r = x
        while uf[r] != r:
            r = uf[r]
        while uf[x] != r:
            uf[x], x = r, uf[x]
        return r
    back_edges = set()
    for e in list(used) + sorted(conn, key=lambda e: elen(*tuple(e))):
        a, b = tuple(e)
        if find(a) != find(b):
            uf[find(a)] = find(b); back_edges.add(e)
    buses, bus_id = [], {}
    def addbus(name, lon, lat, role):
        bus_id[name] = len(buses)
        buses.append({"name": name, "vn_kv": 0.4, "geo": [round(lon, 6), round(lat, 6)], "role": role})
    # the substation IS the backbone root node (main feeders branch out of it);
    # backbone junction buses are created lazily so pruned branches leave none behind
    sj = G.nodes[station_node]
    addbus("LV_station", sj["x"], sj["y"], "slack")
    def bn(nd):
        if nd == station_node:
            return "LV_station"
        nm = f"j{nd}"
        if nm not in bus_id:
            addbus(nm, G.nodes[nd]["x"], G.nodes[nd]["y"], "backbone")
        return nm

    lines = []
    def addline(a, c, length_m, cab, geom):
        lines.append({"from": bus_id[a], "to": bus_id[c], "length_km": round(length_m / 1000.0, 6),
                      "r_ohm_per_km": cab["r"], "x_ohm_per_km": cab["x"], "c_nf_per_km": 0.0,
                      "max_i_ka": cab["imax"], "parallel": 1,
                      "geometry": [[round(x, 6), round(y, 6)] for x, y in geom]})

    # per backbone edge: geometry + cumulative length (metres) for tapping
    edge_geom = {}
    for e in back_edges:
        u, v = tuple(e); geom = egeom(u, v); gm = [mxy(x, y) for x, y in geom]
        cums = [0.0]
        for k in range(len(gm) - 1):
            cums.append(cums[-1] + math.hypot(gm[k + 1][0] - gm[k][0], gm[k + 1][1] - gm[k][1]))
        edge_geom[e] = (geom, cums)
    def nearest_back(c):
        pm = mxy(*c); best = None
        for e in back_edges:
            geom, cums = edge_geom[e]
            for k in range(len(geom) - 1):
                a = mxy(*geom[k]); b = mxy(*geom[k + 1])
                dx, dy = b[0] - a[0], b[1] - a[1]; L2 = dx * dx + dy * dy
                t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((pm[0] - a[0]) * dx + (pm[1] - a[1]) * dy) / L2))
                cx, cy = a[0] + t * dx, a[1] + t * dy; dd = (pm[0] - cx) ** 2 + (pm[1] - cy) ** 2
                if best is None or dd < best[0]:
                    best = (dd, e, cums[k] + t * (cums[k + 1] - cums[k]))
        return best
    taps_on = defaultdict(list)                # backbone edge -> [(pos_m, building_idx)]
    for i, c in enumerate(bld_edge):
        _, e, pos = nearest_back(c)
        taps_on[e].append((pos, i))

    # prune whole backbone branches whose subtree carries no cabinet (the empty
    # connector roads that reach no houses), keeping the tree rooted at the station
    adjB = defaultdict(list)
    for e in back_edges:
        a, b = tuple(e); adjB[a].append(b); adjB[b].append(a)
    parent = {station_node: None}; order = [station_node]; dq = deque([station_node])
    while dq:
        u = dq.popleft()
        for w in adjB[u]:
            if w not in parent:
                parent[w] = u; order.append(w); dq.append(w)
    children = defaultdict(list)
    for node in order:
        if parent[node] is not None:
            children[parent[node]].append(node)
    subtree_tap = defaultdict(bool)
    for node in reversed(order):
        for c in children[node]:
            if subtree_tap[c] or taps_on.get(frozenset((node, c))):
                subtree_tap[node] = True
    back_edges = {frozenset((parent[n], n)) for n in order if parent[n] is not None
                  and (subtree_tap[n] or taps_on.get(frozenset((parent[n], n))))}

    # split each backbone edge into segments through CABLE CABINETS — taps within
    # 30 m merge into one cabinet that serves those houses (their service cables)
    def point_at(geom, cums, pos):
        for k in range(len(cums) - 1):
            if cums[k] <= pos <= cums[k + 1]:
                seg = cums[k + 1] - cums[k]; t = 0.0 if seg == 0 else (pos - cums[k]) / seg
                return (geom[k][0] + t * (geom[k + 1][0] - geom[k][0]),
                        geom[k][1] + t * (geom[k + 1][1] - geom[k][1]))
        return tuple(geom[-1])
    def slice_geom(geom, cums, a, b):
        pts = [list(point_at(geom, cums, a))]
        for k in range(len(cums)):
            if a < cums[k] < b:
                pts.append(list(geom[k]))
        pts.append(list(point_at(geom, cums, b)))
        return pts
    load_specs = []; cab_n = 0
    for e in back_edges:
        u, v = tuple(e); geom, cums = edge_geom[e]; total = cums[-1]
        cabinets = []                          # (pos, [building_idx,...])
        for pos, bi in sorted(taps_on.get(e, [])):
            if cabinets and pos - cabinets[-1][0] <= 30.0:
                cabinets[-1][1].append(bi)
            else:
                cabinets.append([pos, [bi]])
        prev, prev_pos = bn(u), 0.0
        for pos, bis in cabinets:
            cab = f"cab{cab_n}"; cab_n += 1; cp = point_at(geom, cums, pos)
            addbus(cab, cp[0], cp[1], "cabinet")
            addline(prev, cab, max(pos - prev_pos, 1.0), BACK, slice_geom(geom, cums, prev_pos, pos))
            for bi in bis:                     # houses served by this cabinet
                c = bld_edge[bi]; ln = f"load{bi}"; addbus(ln, c[0], c[1], "load")
                addline(ln, cab, _hav(c, cp) + 1, SERV, [[c[0], c[1]], [cp[0], cp[1]]])
                load_specs.append({"bus": bus_id[ln], "peak_mw": round(peaks[bi], 6)})
            prev, prev_pos = cab, pos
        addline(prev, bn(v), max(total - prev_pos, 1.0), BACK, slice_geom(geom, cums, prev_pos, total))

    # size cables by downstream peak load (parallel cables), rooted at the station
    adj = defaultdict(list)
    for li, l in enumerate(lines):
        adj[l["from"]].append((l["to"], li)); adj[l["to"]].append((l["from"], li))
    root = bus_id["LV_station"]; depth = {root: 0}; parent = {root: None}; pline = {}; order = [root]
    dq = deque([root])
    while dq:
        u = dq.popleft()
        for v, li in adj[u]:
            if v not in depth:
                depth[v] = depth[u] + 1; parent[v] = u; pline[v] = li; order.append(v); dq.append(v)
    load_at = defaultdict(float)
    for ls in load_specs:
        load_at[ls["bus"]] += ls["peak_mw"]
    sub = defaultdict(float)
    for u in reversed(order):
        sub[u] += load_at[u]
        if parent[u] is not None:
            sub[parent[u]] += sub[u]
    for v in order:
        if parent[v] is None:
            continue
        li = pline[v]; I_ka = sub[v] / (math.sqrt(3) * 0.4)
        lines[li]["parallel"] = max(1, int(math.ceil(I_ka / (lines[li]["max_i_ka"] * 0.6))))

    return {"name": f"osm_lv_{district_dir.name}_{lvid}",
            "station": [round(sj["x"], 6), round(sj["y"], 6)],
            "buses": buses, "lines": lines, "loads": load_specs, "slack_bus": bus_id["LV_station"]}


def main() -> int:
    man = json.loads(MANIFEST.read_text())
    OUT.mkdir(parents=True, exist_ok=True)
    lv_entries = [g for g in man["grids"] if g.get("voltage") == "LV"]
    print(f"building {len(lv_entries)} OSM-routed LV grids", flush=True)
    ok = 0
    for g in lv_entries:
        gid = g["id"]
        try:
            grid = build_lv_grid(GRIDS / g["source_dir"], str(g["lv_grid_id"]))
            if grid is None:
                print(f"  SKIP {gid}: no buildings/station", flush=True); continue
            (OUT / f"{gid}.json").write_text(json.dumps(grid))
            g["osm_grid"] = f"lv_osm/{gid}.json"
            g["nodes"] = len(grid["buses"])
            ok += 1
            print(f"  {gid}: {len(grid['buses'])} buses, {len(grid['lines'])} lines", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL {gid}: {type(e).__name__}: {e}", flush=True)
            traceback.print_exc()
    MANIFEST.write_text(json.dumps(man, indent=2))
    print(f"\ndone: {ok}/{len(lv_entries)} LV grids built; manifest updated", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
