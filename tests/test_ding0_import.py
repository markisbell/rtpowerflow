"""Tests for the pre-generated ding0 (eDisGo CSV) grid importer."""
from __future__ import annotations

from pathlib import Path

import pytest

from netzsim.data_loader import input_data_from_dicts
from netzsim.ding0_import import convert_ding0_csv
from netzsim.simulator import Simulator

ROOT = Path(__file__).resolve().parents[1]
GRID = ROOT / "data" / "ding0_grids" / "ding0_1"


@pytest.mark.skipif(not (GRID / "buses.csv").exists(), reason="ding0 grid not committed")
def test_ding0_import_has_geo_and_solves():
    g = convert_ding0_csv(GRID, steps=96)
    buses = g.grid_structure["buses"]
    assert len(buses) > 100
    # every bus is geo-located (MV from eDisGo, LV propagated from its station)
    assert all(b.get("geo") for b in buses)
    lon, lat = buses[0]["geo"]
    assert 7.0 < lon < 9.0 and 47.0 < lat < 49.0  # Freiburg / Black Forest

    data = input_data_from_dicts(g.grid_structure, g.lines, g.load,
                                 g.generation, g.substation)
    sim = Simulator(data)
    topo = sim.topology()
    assert topo["has_geo"] is True
    assert topo["buses"][0]["geo"] is not None     # raw lon/lat exposed
    assert 0.0 <= topo["buses"][0]["x"] <= 1.0      # normalized layout from geo

    res = sim.run_step(48)
    assert res.converged
    assert res.summary["n_trafo"] >= 1
