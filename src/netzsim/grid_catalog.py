"""Catalog of importable grid models served by the ``/grids`` API.

netzsim is a pure *consumer*: it lists grids from a committed dataset and converts
a chosen one to :class:`~netzsim.grid_inputs.GridInputs` on demand (cached). The
grids themselves are produced by the separate ``gridgen`` project; nothing here
generates them.

It prefers a curated **library manifest** (``grid_library.json``) of characterized
MV/LV ding0 grids (type · size · rural/suburban/urban). Without a manifest it
falls back to listing raw ding0 grid directories.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .grid_inputs import GridInputs


@dataclass
class GridEntry:
    id: str
    name: str
    category: str
    member: str                       # ding0 grid dir (source CSVs)
    source: str = "library"           # "library" (curated ding0) | "ding0" (raw)
    voltage: str | None = None        # "MV" | "LV"
    character: str | None = None      # "rural" | "suburban" | "urban"
    nodes: int | None = None          # node count for this scope
    scope: str = "full"               # convert_ding0_csv scope: full | mv | lv
    lv_grid_id: str | None = None
    osm_grid: str | None = None        # path to an OSM-routed LV grid JSON (overrides scope)
    lv_subgrids: list[dict] | None = None  # MV only: OSM LV grids of the same district
                                           # -> build the interconnected MV+LV network


class GridCatalog:
    """Lists importable grids and converts a chosen one to netzsim inputs on demand."""

    def __init__(self, ding0_dir: str | Path | None = None,
                 library_manifest: str | Path | None = None,
                 user_dir: str | Path | None = None):
        self.ding0_dir = Path(ding0_dir) if ding0_dir else None
        self.manifest = Path(library_manifest) if library_manifest else None
        self.user_dir = Path(user_dir) if user_dir else None
        self._entries: dict[str, GridEntry] = {}
        self._cache: dict[tuple[str, int], GridInputs] = {}
        if self.manifest and self.manifest.exists():
            self._load_manifest()
        elif self.ding0_dir and self.ding0_dir.exists():
            self._scan_ding0()
        self._scan_user()

    def _load_manifest(self) -> None:
        data = json.loads(self.manifest.read_text())
        base = self.ding0_dir or (self.manifest.parent / "ding0_grids")
        data_dir = self.manifest.parent
        for g in data.get("grids", []):
            src = base / g["source_dir"]
            osm = str(data_dir / g["osm_grid"]) if g.get("osm_grid") else None
            self._entries[g["id"]] = GridEntry(
                id=g["id"], name=g.get("name", g["id"]),
                category=f"{g.get('character', '')} · {g.get('voltage', '')}".strip(" ·"),
                member=str(src), source="library",
                voltage=g.get("voltage"), character=g.get("character"),
                nodes=g.get("nodes"), scope=g.get("scope", "full"),
                lv_grid_id=g.get("lv_grid_id"), osm_grid=osm,
            )
        # An MV district entry subsumes the street-routed LV grids of the same
        # source district: picking it builds the interconnected MV+LV network
        # (LV grids without an OSM version stay lumped at their station).
        for e in self._entries.values():
            if e.voltage != "MV":
                continue
            subs = [{"lv_grid_id": s.lv_grid_id, "path": s.osm_grid, "id": s.id}
                    for s in self._entries.values()
                    if s.voltage == "LV" and s.osm_grid and s.member == e.member]
            if subs:
                e.lv_subgrids = subs
                e.nodes = (e.nodes or 0) + sum(
                    s.nodes or 0 for s in self._entries.values()
                    if s.voltage == "LV" and s.osm_grid and s.member == e.member)

    def _scan_ding0(self) -> None:
        for sub in sorted(self.ding0_dir.iterdir()):
            if sub.is_dir() and (sub / "buses.csv").exists():
                self._entries[sub.name] = GridEntry(
                    id=sub.name, name=sub.name, category="ding0 · geographic",
                    member=str(sub), source="ding0",
                )

    def _scan_user(self) -> None:
        """User-drawn grids (gridformat JSON from the gridedit editor). Rescanned
        on every listing so a fresh export appears without restarting netzsim;
        entries whose file vanished are dropped."""
        if not self.user_dir:
            return
        seen: set[str] = set()
        if self.user_dir.exists():
            for f in sorted(self.user_dir.glob("*.json")):
                gid = f"user_{f.stem}"
                seen.add(gid)
                try:
                    doc = json.loads(f.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                self._entries[gid] = GridEntry(
                    id=gid, name=doc.get("name", f.stem),
                    category="user · LV", member=str(f), source="user",
                    voltage="LV", character="user",
                    nodes=len(doc.get("buses", [])), osm_grid=str(f),
                )
        for gid in [g for g, e in self._entries.items()
                    if e.source == "user" and g not in seen]:
            del self._entries[gid]
            self._cache = {k: v for k, v in self._cache.items() if k[0] != gid}

    @property
    def available(self) -> bool:
        return bool(self._entries)

    def has(self, grid_id: str) -> bool:
        if grid_id not in self._entries and grid_id.startswith("user_"):
            self._scan_user()
        return grid_id in self._entries

    def list(self) -> list[dict]:
        """Lightweight listing (counts filled in only once a grid is cached)."""
        self._scan_user()
        out: list[dict] = []
        for e in self._entries.values():
            item: dict = {
                "id": e.id,
                "name": e.name,
                "category": e.category,
                "source": e.source,
                "voltage": e.voltage,
                "character": e.character,
                "nodes": e.nodes,
                "geo": e.source in ("library", "ding0", "user"),  # real lat/lon → map-capable
                "thumbnail": None,
            }
            cached = next((g for (gid, _), g in self._cache.items() if gid == e.id), None)
            if cached is not None:
                item.update(_counts(cached))
            out.append(item)
        return out

    def get_inputs(self, grid_id: str, *, steps: int = 1440) -> GridInputs:
        if grid_id not in self._entries and grid_id.startswith("user_"):
            self._scan_user()
        if grid_id not in self._entries:
            raise KeyError(grid_id)
        key = (grid_id, steps)
        if key not in self._cache:
            e = self._entries[grid_id]
            if e.osm_grid:
                from .osm_lv_import import convert_osm_lv
                self._cache[key] = convert_osm_lv(e.osm_grid, name=e.name, steps=steps)
            elif e.lv_subgrids:
                from .district_import import convert_district
                self._cache[key] = convert_district(
                    e.member, e.lv_subgrids, name=e.name, steps=steps)
            else:
                from .ding0_import import convert_ding0_csv
                self._cache[key] = convert_ding0_csv(
                    e.member, name=e.name, steps=steps,
                    scope=e.scope, lv_grid_id=e.lv_grid_id)
        return self._cache[key]


def _counts(g: GridInputs) -> dict[str, int]:
    return {
        "n_bus": len(g.grid_structure["buses"]),
        "n_line": len(g.lines["lines"]),
        "n_trafo": len(g.lines["transformers"]),
        "n_load": len(g.load["loads"]),
    }


def preview(g: GridInputs) -> dict:
    """A net-free topology preview built straight from the converted dicts."""
    buses = [
        {"id": i, "name": b["name"], "vn_kv": b["vn_kv"], "zone": b.get("zone")}
        for i, b in enumerate(g.grid_structure["buses"])
    ]
    lines = [
        {"name": ln.get("name"), "from_bus": ln["from_bus"],
         "to_bus": ln["to_bus"], "length_km": ln["length_km"]}
        for ln in g.lines["lines"]
    ]
    trafos = [
        {"name": t.get("name"), "hv_bus": t["hv_bus"],
         "lv_bus": t["lv_bus"], "sn_mva": t.get("sn_mva")}
        for t in g.lines["transformers"]
    ]
    return {
        "name": g.grid_structure["name"],
        **_counts(g),
        "buses": buses,
        "lines": lines,
        "trafos": trafos,
        "notes": g.notes,
    }
