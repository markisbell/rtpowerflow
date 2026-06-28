"""Catalog of importable grid models served by the ``/grids`` API.

Scans an archive (the *European Archetype* zip) for grid workbooks, exposes them
as selectable entries, and converts a chosen one to netzsim inputs on demand
(cached). Conversion uses :mod:`netzsim.grid_import`.

The archive is read lazily and is optional: if it is absent the catalog is simply
empty and ``/grids`` returns ``available: false``.
"""
from __future__ import annotations

import io
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
    member: str                    # workbook path inside the archive
    thumbnail_member: str | None   # matching PNG path, if any


class GridCatalog:
    """Lists grid workbooks in an archive and converts them on demand."""

    def __init__(self, archive: str | Path | None, filter_substring: str = ""):
        self.archive = Path(archive) if archive else None
        self.filter = filter_substring
        self._entries: dict[str, GridEntry] = {}
        self._cache: dict[tuple[str, int], GridInputs] = {}
        if self.archive and self.archive.exists():
            self._scan()

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
