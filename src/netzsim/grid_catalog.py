"""Catalog of importable grid models served by the ``/grids`` API.

Scans an archive (the *European Archetype* zip) for grid workbooks, exposes them
as selectable entries, and converts a chosen one to netzsim inputs on demand
(cached). Conversion uses :mod:`netzsim.grid_import`.

The archive is read lazily and is optional: if it is absent the catalog is simply
empty and ``/grids`` returns ``available: false``.
"""
from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .grid_import.xlsx import GridInputs, convert_workbook

_CATEGORIES = (("031_Urban", "Urban"), ("032_Semi-urban", "Semi-urban"),
               ("033_Rural", "Rural"))


@dataclass
class GridEntry:
    id: str
    name: str
    category: str
    member: str                       # ding0 grid dir (source CSVs), or workbook path
    thumbnail_member: str | None = None
    source: str = "library"           # "library" (curated ding0) | "ding0" (raw) | "archetype"
    voltage: str | None = None        # "MV" | "LV"
    character: str | None = None      # "rural" | "suburban" | "urban"
    nodes: int | None = None          # node count for this scope
    scope: str = "full"               # convert_ding0_csv scope: full | mv | lv
    lv_grid_id: str | None = None
    osm_grid: str | None = None        # path to an OSM-routed LV grid JSON (overrides scope)


class GridCatalog:
    """Lists importable grids and converts a chosen one to netzsim inputs on demand.

    Prefers a curated **library manifest** (``grid_library.json``) of characterized
    MV/LV ding0 grids (type · size · rural/suburban/urban). Without a manifest it
    falls back to listing raw ding0 grid directories. The legacy LV-archetype
    archive is no longer scanned.
    """

    def __init__(self, archive: str | Path | None = None, filter_substring: str = "",
                 ding0_dir: str | Path | None = None,
                 library_manifest: str | Path | None = None):
        self.archive = Path(archive) if archive else None
        self.filter = filter_substring
        self.ding0_dir = Path(ding0_dir) if ding0_dir else None
        self.manifest = Path(library_manifest) if library_manifest else None
        self._entries: dict[str, GridEntry] = {}
        self._cache: dict[tuple[str, int], GridInputs] = {}
        if self.manifest and self.manifest.exists():
            self._load_manifest()
        elif self.ding0_dir and self.ding0_dir.exists():
            self._scan_ding0()

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

    def _scan_ding0(self) -> None:
        for sub in sorted(self.ding0_dir.iterdir()):
            if sub.is_dir() and (sub / "buses.csv").exists():
                self._entries[sub.name] = GridEntry(
                    id=sub.name, name=sub.name, category="ding0 · geographic",
                    member=str(sub), source="ding0",
                )

    @property
    def available(self) -> bool:
        return bool(self._entries)

    def _scan(self) -> None:
        with zipfile.ZipFile(self.archive) as zf:
            names = zf.namelist()
        workbooks = sorted(
            n for n in names if n.endswith(".xlsx") and self.filter in n
        )
        pngs = [n for n in names if n.lower().endswith(".png") and self.filter in n]
        for member in workbooks:
            stem = Path(member).stem                # e.g. network_10_1_1137
            token = stem.replace("network_", "", 1)  # 10_1_1137
            thumb = next((p for p in pngs if token in Path(p).stem), None)
            self._entries[stem] = GridEntry(
                id=stem, name=stem, category=_category(member),
                member=member, thumbnail_member=thumb,
            )

    def has(self, grid_id: str) -> bool:
        return grid_id in self._entries

    def list(self) -> list[dict]:
        """Lightweight listing (counts filled in only once a grid is cached)."""
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
                "geo": e.source in ("library", "ding0"),  # real lat/lon → map-capable
                "thumbnail": f"/grids/{e.id}/thumbnail" if e.thumbnail_member else None,
            }
            cached = next((g for (gid, _), g in self._cache.items() if gid == e.id), None)
            if cached is not None:
                item.update(_counts(cached))
            out.append(item)
        return out

    def get_inputs(self, grid_id: str, *, steps: int = 1440) -> GridInputs:
        if grid_id not in self._entries:
            raise KeyError(grid_id)
        key = (grid_id, steps)
        if key not in self._cache:
            e = self._entries[grid_id]
            if e.osm_grid:
                from .osm_lv_import import convert_osm_lv
                self._cache[key] = convert_osm_lv(e.osm_grid, name=e.name, steps=steps)
            elif e.source in ("library", "ding0"):
                from .ding0_import import convert_ding0_csv
                self._cache[key] = convert_ding0_csv(
                    e.member, name=e.name, steps=steps,
                    scope=e.scope, lv_grid_id=e.lv_grid_id)
            else:
                with zipfile.ZipFile(self.archive) as zf:
                    raw = zf.read(e.member)
                with pd.ExcelFile(io.BytesIO(raw)) as xl:
                    self._cache[key] = convert_workbook(xl, name=e.name, steps=steps)
        return self._cache[key]

    def thumbnail_bytes(self, grid_id: str) -> bytes | None:
        e = self._entries.get(grid_id)
        if not e or not e.thumbnail_member:
            return None
        with zipfile.ZipFile(self.archive) as zf:
            return zf.read(e.thumbnail_member)


def _category(member: str) -> str:
    for key, label in _CATEGORIES:
        if key in member:
            return label
    return "LV"


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
