"""Tests for the OSM-routed LV grids (street-following cables with geometry)."""
from __future__ import annotations

from pathlib import Path

import pytest

from netzsim.data_loader import input_data_from_dicts
from netzsim.osm_lv_import import convert_osm_lv
from netzsim.simulator import Simulator

ROOT = Path(__file__).resolve().parents[1]
GRID = ROOT / "data" / "lv_osm" / "lv_urban_83_189499.json"


@pytest.mark.skipif(not GRID.exists(), reason="OSM-LV grid not built")
def test_osm_lv_grid_has_line_geometry_and_solves():
    g = convert_osm_lv(GRID, steps=96)
    buses = g.grid_structure["buses"]
    assert all(b["vn_kv"] <= 0.4 for b in buses)        # a 0.4 kV grid
    assert all(b.get("geo") for b in buses)             # every node geo-located
    # every line carries a [lon,lat] polyline of >= 2 points (cable along streets)
    specs = g.lines["lines"]
    assert specs and all(s.get("geometry") and len(s["geometry"]) >= 2 for s in specs)

    data = input_data_from_dicts(g.grid_structure, g.lines, g.load,
                                 g.generation, g.substation)
    sim = Simulator(data)
    topo = sim.topology()
    assert topo["has_geo"] is True
    # geometry survives all the way to the /network topology, mapped by line id
    assert all(ln.get("geometry") for ln in topo["lines"])
    # cable cabinets (green circles) are exposed for the map to draw
    assert len(topo.get("cabinet_buses", [])) >= 1

    res = sim.run_step(76)
    assert res.converged
    assert res.summary["vm_pu_min"] > 0.85             # realistic voltage (sized cables)
