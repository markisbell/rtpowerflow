"""Import a pre-generated ding0 grid (eDisGo CSV export) into netzsim inputs.

ding0 (https://github.com/openego/ding0) generates synthetic German distribution
grids on **real OSM geography**; eDisGo exports them as PyPSA-style CSVs whose
``buses.csv`` carries WGS84 ``x``=longitude / ``y``=latitude. We carry those real
coordinates onto each bus (``BusSpec.geo``) so the Live view can render the grid
on an actual map.

A grid folder holds: ``buses.csv``, ``lines.csv``, ``loads.csv``,
``transformers.csv`` (MV/LV), ``transformers_hvmv.csv`` (HV/MV), ``generators.csv``,
``switches.csv``. The HV/MV transformer marks the slack boundary: we feed the grid
at the MV station busbar (its LV side) and don't model the HV grid.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pandas as pd

from .grid_inputs import GridInputs, _daily


def _num(v, default=0.0):
    try:
        f = float(v)
        return default if math.isnan(f) else f
    except (TypeError, ValueError):
        return default


def _real(busname: Any) -> str:
    """eDisGo creates 'virtual_X' buses for open switches; merge them into X."""
    s = str(busname)
    return s[len("virtual_"):] if s.startswith("virtual_") else s


def _solarish(steps: int, peak: float, subtype: str) -> list[float]:
    """A rough daily generation shape: PV → midday bell, else ~constant."""
    if "solar" in subtype.lower() or "pv" in subtype.lower():
        peak_step, width = steps / 2.0, steps / 7.0
        return [round(peak * math.exp(-((t - peak_step) ** 2) / (2 * width ** 2))
                      if steps * 0.25 <= t <= steps * 0.75 else 0.0, 6)
                for t in range(steps)]
    return [round(peak * 0.3, 6)] * steps  # dispatchable: steady part-load


def _lvg(v: Any) -> str:
    """Normalize an lv_grid_id cell to a clean string ('' when absent)."""
    if v is None:
        return ""
    s = str(v).strip()
    if s == "" or s.lower() == "nan":
        return ""
    return s.split(".")[0]


def trafo_spec_from_row(r, hv_bus: int, lv_bus: int, vn_hv: float, vn_lv: float) -> dict[str, Any]:
    """A netzsim TransformerSpec dict from a ding0 ``transformers.csv`` row
    (PyPSA-style: r/x in pu on s_nom → vk/vkr in percent)."""
    rr, xx = _num(r.get("r")), _num(r.get("x"))
    vk = max(min(100.0 * math.sqrt(rr * rr + xx * xx), 15.0), 1.0)
    return {
        "name": str(r["name"]),
        "hv_bus": hv_bus, "lv_bus": lv_bus,
        "sn_mva": _num(r["s_nom"], 0.4) or 0.4,
        "vn_hv_kv": vn_hv, "vn_lv_kv": vn_lv,
        "vk_percent": round(vk, 4), "vkr_percent": round(min(100.0 * rr, vk), 4),
        "pfe_kw": 0.0, "i0_percent": 0.0,
    }


def _apply_scope(buses, lines, loads, trafos, hvmv, gens, scope: str, lv_grid_id,
                 exclude_lv: set[str] | None = None):
    """Reduce a full ding0 district to a single voltage level.

    ``scope="mv"`` keeps the MV graph (v_nom > 1 kV, no ``lv_grid_id``) and folds
    each downstream LV grid into one lumped load at its feeding MV bus — except
    grids in ``exclude_lv``, whose loads/gens are dropped entirely (the district
    importer splices those in as full street-routed LV subgrids instead).
    ``scope="lv"`` extracts one standalone LV grid (``lv_grid_id``), fed at its
    busbar (the LV side of its MV/LV transformer). Returns the filtered frames
    plus a forced-slack bus name (or None) and notes.
    """
    notes: list[str] = []
    lvof: dict[str, str] = {}
    vnof: dict[str, float] = {}
    for _, r in buses.iterrows():
        nm = str(r["name"])
        lvof[nm] = _lvg(r.get("lv_grid_id"))
        vnof[nm] = _num(r.get("v_nom"), 0.4)
    empty_tr = trafos.iloc[0:0]

    if scope == "lv":
        target = _lvg(lv_grid_id)
        lvset = {nm for nm, g in lvof.items() if g == target}
        if not lvset:
            raise ValueError(f"LV grid '{target}' not found in district")
        busbar = None
        for _, r in trafos.iterrows():
            a, b = _real(r["bus0"]), _real(r["bus1"])
            if a in lvset and b not in lvset:
                busbar = a
            elif b in lvset and a not in lvset:
                busbar = b
            if busbar:
                break
        if busbar is None:
            busbar = max(lvset, key=lambda n: vnof.get(n, 0.0))
        buses_f = buses[buses["name"].astype(str).isin(lvset)]
        lines_f = lines[lines["bus0"].map(_real).isin(lvset) & lines["bus1"].map(_real).isin(lvset)]
        loads_f = loads[loads["bus"].map(_real).isin(lvset)] if not loads.empty else loads
        gens_f = gens[gens["bus"].map(_real).isin(lvset)] if not gens.empty else gens
        notes.append(f"extracted standalone LV grid {target}: {len(buses_f)} buses, slack at {busbar}")
        return buses_f, lines_f, loads_f, empty_tr, empty_tr, gens_f, busbar, notes

    if scope == "mv":
        from collections import defaultdict

        excl = exclude_lv or set()
        mvset = {nm for nm, g in lvof.items() if g == "" and vnof.get(nm, 0.0) > 1.0}
        lvg_mvbus: dict[str, str] = {}  # lv grid id -> feeding MV bus
        for _, r in trafos.iterrows():
            a, b = _real(r["bus0"]), _real(r["bus1"])
            if a in mvset and b not in mvset:
                lvg_mvbus[lvof.get(b, "")] = a
            elif b in mvset and a not in mvset:
                lvg_mvbus[lvof.get(a, "")] = b
        buses_f = buses[buses["name"].astype(str).isin(mvset)]
        lines_f = lines[lines["bus0"].map(_real).isin(mvset) & lines["bus1"].map(_real).isin(mvset)]

        agg: dict[str, float] = defaultdict(float)
        kept = []
        for _, r in loads.iterrows():
            b = _real(r["bus"])
            if b in mvset:
                kept.append({"name": str(r["name"]), "bus": b, "peak_load": _num(r.get("peak_load"))})
            elif lvof.get(b, "") not in excl:
                agg[lvof.get(b, "")] += _num(r.get("peak_load"))
        for g, peak in agg.items():
            mvb = lvg_mvbus.get(g)
            if mvb and peak > 0:
                kept.append({"name": f"lv_{g}", "bus": mvb, "peak_load": round(peak, 6)})
        loads_f = pd.DataFrame(kept) if kept else loads.iloc[0:0]

        gens_f = gens
        if not gens.empty:
            gagg: dict[str, float] = defaultdict(float)
            keptg = []
            for _, r in gens.iterrows():
                b = _real(r["bus"])
                if b in mvset:
                    keptg.append({"name": str(r["name"]), "bus": b, "p_nom": _num(r.get("p_nom")),
                                  "subtype": r.get("subtype"), "type": r.get("type")})
                elif lvof.get(b, "") not in excl:
                    gagg[lvof.get(b, "")] += _num(r.get("p_nom"))
            for g, p in gagg.items():
                mvb = lvg_mvbus.get(g)
                if mvb and p > 0:
                    keptg.append({"name": f"lvgen_{g}", "bus": mvb, "p_nom": round(p, 6), "subtype": "", "type": ""})
            gens_f = pd.DataFrame(keptg) if keptg else gens.iloc[0:0]

        notes.append(f"MV-only graph: {len(buses_f)} MV buses, {len(lines_f)} MV lines, "
                     f"{len(agg)} LV grids folded into lumped loads"
                     + (f", {len(excl)} LV grids excluded for splicing" if excl else ""))
        return buses_f, lines_f, loads_f, empty_tr, hvmv, gens_f, None, notes

    return buses, lines, loads, trafos, hvmv, gens, None, notes


def convert_ding0_csv(grid_dir: str | Path, *, name: str | None = None,
                      steps: int = 1440, power_factor: float = 0.95,
                      scope: str = "full", lv_grid_id: str | int | None = None,
                      exclude_lv: set[str] | None = None) -> GridInputs:
    d = Path(grid_dir)
    name = name or d.name

    def _read(fn):
        p = d / fn
        return pd.read_csv(p) if p.exists() else pd.DataFrame()

    buses = _read("buses.csv")
    lines = _read("lines.csv")
    loads = _read("loads.csv")
    trafos = _read("transformers.csv")
    hvmv = _read("transformers_hvmv.csv")
    gens = _read("generators.csv")

    notes: list[str] = []
    forced_slack: str | None = None
    if scope in ("mv", "lv"):
        buses, lines, loads, trafos, hvmv, gens, forced_slack, snotes = _apply_scope(
            buses, lines, loads, trafos, hvmv, gens, scope, lv_grid_id, exclude_lv)
        notes.extend(snotes)

    # --- buses (skip 'virtual_' switch duplicates; index by row order) -------- #
    bus_index: dict[str, int] = {}
    bus_specs: list[dict[str, Any]] = []
    vn: dict[str, float] = {}
    for _, r in buses.iterrows():
        nm = str(r["name"])
        if nm.startswith("virtual_") or nm in bus_index:
            continue
        v = _num(r["v_nom"], 0.4) or 0.4
        x, y = r.get("x"), r.get("y")
        geo = [round(float(x), 6), round(float(y), 6)] if pd.notna(x) and pd.notna(y) else None
        bus_index[nm] = len(bus_specs)
        vn[nm] = v
        bus_specs.append({"name": nm, "vn_kv": v, "type": "b",
                          "zone": "MV" if v > 1.0 else "LV", "in_service": True, "geo": geo})

    # --- lines (PyPSA r/x are totals in ohm → per-km; s_nom → max_i_ka) ------ #
    line_specs: list[dict[str, Any]] = []
    for _, r in lines.iterrows():
        b0, b1 = _real(r["bus0"]), _real(r["bus1"])
        if b0 not in bus_index or b1 not in bus_index or b0 == b1:
            continue  # unknown bus or self-loop (from a merged switch)
        length = max(_num(r["length"]), 1.0e-4)
        s_nom = _num(r.get("s_nom"))
        line_specs.append({
            "name": str(r["name"]),
            "from_bus": bus_index[b0], "to_bus": bus_index[b1],
            "length_km": round(length, 6),
            "r_ohm_per_km": round(_num(r["r"]) / length, 6),
            "x_ohm_per_km": round(_num(r["x"]) / length, 6),
            "c_nf_per_km": 0.0,
            "max_i_ka": round(s_nom / (math.sqrt(3) * vn[b0]), 6) if s_nom > 0 and vn[b0] else 1.0,
            "parallel": int(_num(r.get("num_parallel"), 1)) or 1,
        })

    # --- transformers (MV/LV) ------------------------------------------------- #
    trafo_specs: list[dict[str, Any]] = []
    for _, r in trafos.iterrows():
        b0, b1 = _real(r["bus0"]), _real(r["bus1"])
        if b0 not in bus_index or b1 not in bus_index:
            continue
        hv, lv = (b0, b1) if vn[b0] >= vn[b1] else (b1, b0)
        trafo_specs.append(trafo_spec_from_row(
            r, bus_index[hv], bus_index[lv], max(vn[b0], vn[b1]), min(vn[b0], vn[b1])))

    # --- coordinates for buses ding0 leaves un-geo-referenced --------------- #
    # ding0's eDisGo export gives WGS84 coords to MV buses / stations but NOT to
    # LV buses (every LV bus inherits its station's single point). Scattering them
    # randomly collapses the feeder into a ~40 m blob with crossing lines and no
    # visible radial structure. Instead we lay the coordinate-less buses out as a
    # RADIAL TREE that follows the actual line topology, fanning out from their
    # geo-having anchor (the LV busbar / MV station) so the map shows real feeders.
    from collections import defaultdict, deque
    adj: dict[int, list[int]] = defaultdict(list)
    for ln in line_specs:
        adj[ln["from_bus"]].append(ln["to_bus"])
        adj[ln["to_bus"]].append(ln["from_bus"])
    for tr in trafo_specs:
        adj[tr["hv_bus"]].append(tr["lv_bus"])
        adj[tr["lv_bus"]].append(tr["hv_bus"])

    STEP_M = 12.0                              # metres of radius per tree level
    anchors = {i for i, b in enumerate(bus_specs) if b["geo"]}
    # rooted tree over the coordinate-less buses, grown from the geo anchors
    children: dict[int, list[int]] = defaultdict(list)
    depth: dict[int, int] = {a: 0 for a in anchors}
    anchor_of: dict[int, int] = {a: a for a in anchors}
    visited = set(anchors)
    dq = deque(sorted(anchors))
    while dq:
        u = dq.popleft()
        for v in sorted(adj[u]):
            if v not in visited:
                visited.add(v)
                children[u].append(v)
                depth[v] = depth[u] + 1
                anchor_of[v] = anchor_of[u]
                dq.append(v)
    # leaf-count per subtree (process deepest first), then split each node's
    # angular sector among its children in proportion to their leaf counts
    by_depth = sorted(visited, key=lambda x: depth[x])
    leaves: dict[int, int] = {}
    for u in reversed(by_depth):
        leaves[u] = 1 if not children[u] else sum(leaves[c] for c in children[u])
    sector: dict[int, tuple[float, float]] = {a: (0.0, 2.0 * math.pi) for a in anchors}
    angle: dict[int, float] = {}
    for u in by_depth:
        lo, hi = sector[u]
        angle[u] = (lo + hi) / 2.0
        tot = sum(leaves[c] for c in children[u]) or 1
        cur = lo
        for c in children[u]:
            w = (hi - lo) * leaves[c] / tot
            sector[c] = (cur, cur + w)
            cur += w
    filled = 0
    for u in by_depth:
        if u in anchors:
            continue
        ax, ay = bus_specs[anchor_of[u]]["geo"]
        r = depth[u] * STEP_M
        mlon = 111320.0 * max(math.cos(math.radians(ay)), 1e-6)
        bus_specs[u]["geo"] = [round(ax + r * math.cos(angle[u]) / mlon, 6),
                               round(ay + r * math.sin(angle[u]) / 110540.0, 6)]
        filled += 1
    if filled:
        notes.append(f"laid out {filled} coordinate-less bus(es) as a radial tree from their station")

    lines_doc = {"lines": line_specs, "transformers": trafo_specs}

    # --- slack: forced (LV busbar) > HV/MV station busbar > best guess ------- #
    slack = None
    if forced_slack and forced_slack in bus_index:
        slack = forced_slack
    if slack is None and not hvmv.empty:
        cand = _real(hvmv.iloc[0]["bus1"])
        slack = cand if cand in bus_index else None
    if slack is None:
        mv = [n for n in bus_index if "MVStation" in n] or \
             [max(bus_index, key=lambda n: vn[n])]
        slack = mv[0]
    substation_doc = {
        "resolution_minutes": 1440 // steps, "steps": steps,
        "substations": [{"name": slack, "bus": bus_index[slack],
                         "vm_pu": [1.0] * steps, "va_degree": [0.0] * steps}],
    }

    # --- loads (daily curve scaled to each load's peak) ---------------------- #
    tan_phi = math.tan(math.acos(max(min(power_factor, 1.0), 1e-3)))
    load_specs: list[dict[str, Any]] = []
    for i, r in loads.reset_index(drop=True).iterrows():
        b = _real(r["bus"])
        if b not in bus_index:
            notes.append(f"dropped load '{r['name']}': bus '{b}' not found")
            continue
        peak = _num(r.get("peak_load"))
        p = _daily(steps, base=peak * 0.5, amp=peak * 0.5, peak_hour=19.0 + (i % 4) * 0.3)
        load_specs.append({"name": str(r["name"]), "bus": bus_index[b],
                           "p_mw": p, "q_mvar": [round(v * tan_phi, 6) for v in p]})
    load_doc = {"resolution_minutes": 1440 // steps, "steps": steps, "loads": load_specs}

    # --- generators → sgen --------------------------------------------------- #
    gen_specs: list[dict[str, Any]] = []
    for _, r in gens.iterrows():
        b = _real(r["bus"])
        if b not in bus_index:
            continue
        p_nom = _num(r.get("p_nom"))
        subtype = str(r.get("subtype") or r.get("type") or "")
        gen_specs.append({"name": str(r["name"]), "bus": bus_index[b],
                          "p_mw": _solarish(steps, p_nom, subtype), "q_mvar": [0.0] * steps})
    generation_doc = {"resolution_minutes": 1440 // steps, "steps": steps,
                      "generation": gen_specs}

    notes.append(f"imported ding0 grid '{name}': {len(bus_specs)} buses, "
                 f"{len(line_specs)} lines, {len(trafo_specs)} trafos, "
                 f"{len(load_specs)} loads, {len(gen_specs)} sgen; slack at {slack}")

    return GridInputs(
        grid_structure={"name": name, "f_hz": 50.0, "buses": bus_specs},
        lines=lines_doc, load=load_doc, generation=generation_doc,
        substation=substation_doc, notes=notes,
    )
