"""Electrical E-Check: every committed LV grid solves within voltage/loading limits.

Complements the *structural* E-Check (gridgen/gridformat): here we actually solve
each grid's power flow at the evening peak and assert it is electrically sound —
converges, voltages within EN 50160-style ±10 %, no line overloaded.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from netzsim.data_loader import input_data_from_dicts
from netzsim.osm_lv_import import convert_osm_lv
from netzsim.simulator import Simulator

ROOT = Path(__file__).resolve().parents[1]
LV_GRIDS = sorted((ROOT / "data" / "lv_osm").glob("*.json"))


@pytest.mark.skipif(not LV_GRIDS, reason="no OSM-LV grids in the dataset")
@pytest.mark.parametrize("path", LV_GRIDS, ids=lambda p: p.stem)
def test_lv_grid_solves_within_limits(path):
    g = convert_osm_lv(path, steps=96)
    data = input_data_from_dicts(g.grid_structure, g.lines, g.load,
                                 g.generation, g.substation)
    res = Simulator(data).run_step(76)            # ~19:00, the evening peak
    assert res.converged, f"{path.stem}: power flow did not converge"
    s = res.summary
    assert s["vm_pu_min"] >= 0.90, f"{path.stem}: vmin={s['vm_pu_min']:.3f} < 0.90"
    assert s["vm_pu_max"] <= 1.10, f"{path.stem}: vmax={s['vm_pu_max']:.3f} > 1.10"
    assert s["max_line_loading_percent"] <= 100.0, \
        f"{path.stem}: max line loading={s['max_line_loading_percent']:.0f}% > 100%"
