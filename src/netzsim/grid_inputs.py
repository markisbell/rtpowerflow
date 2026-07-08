"""The neutral netzsim grid model — the in-memory form every importer produces.

``GridInputs`` is the five pandapower-native input documents as plain dicts: the
contract between a grid *importer* and the simulator. ``_daily`` builds the
synthetic placeholder load shapes importers use before the LPG library is layered
on. Importers (:mod:`netzsim.ding0_import`, :mod:`netzsim.osm_lv_import`) produce
``GridInputs``; the catalog caches them; the simulator consumes them. Grids are
*generated* by the separate ``gridgen`` project — no generation code lives here.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass
class GridInputs:
    """The five netzsim input documents as plain dicts.

    ``cells`` is the vertical MV/LV structure (docs/VERTIKALE_INTEGRATION.md,
    Phase 0): one entry per MV/LV secondary substation (ONS) cell, as plain
    dicts ``{id, name, buses, lv_busbar, mv_bus, station_trafos, lumped}``.
    ``buses`` are the cell's LV bus indices (empty for a *lumped* station whose
    LV grid is folded into one aggregate load at ``mv_bus``); ``lv_busbar`` is
    the LV busbar bus, ``station_trafos`` the indices into
    ``lines["transformers"]`` (several = parallel units). A standalone LV grid
    is exactly one cell; an MV-only or legacy grid may have none. Cells are
    runtime metadata — they are NOT part of the five-file contract.
    """

    grid_structure: dict[str, Any]
    lines: dict[str, Any]
    load: dict[str, Any]
    generation: dict[str, Any]
    substation: dict[str, Any]
    notes: list[str] = field(default_factory=list)
    cells: list[dict[str, Any]] = field(default_factory=list)

    def as_files(self) -> dict[str, dict[str, Any]]:
        return {
            "grid_structure.json": self.grid_structure,
            "lines.json": self.lines,
            "load.json": self.load,
            "generation.json": self.generation,
            "substation.json": self.substation,
        }


def _daily(steps: int, base: float, amp: float, peak_hour: float, floor: float = 0.0) -> list[float]:
    """A smooth sinusoidal daily shape peaking at ``peak_hour`` (clock hours)."""
    peak_step = peak_hour / 24.0 * steps
    return [
        round(max(floor, base + amp * math.cos(2 * math.pi * (t - peak_step) / steps)), 6)
        for t in range(steps)
    ]
