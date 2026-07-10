"""Regression net for the sweeps.py extraction: the day-sweep layer moved out
of Simulator (thin delegates remain). Pins the pieces the existing suite did
not cover directly — battery day curves, the measured layer's TAF rastering,
and that a deep-copied Simulator (the bulk exporter's path) sweeps
independently of the original's caches."""
from __future__ import annotations

import copy
from pathlib import Path

from netzsim.data_loader import load_inputs
from netzsim.simulator import Simulator

ROOT = Path(__file__).resolve().parents[1]


def _sim() -> Simulator:
    return Simulator(load_inputs(ROOT / "data"))


def test_battery_profiles_full_day_curve():
    sim = _sim()
    b = sim.add_battery(3, 10.0, 5.0, "self", 0.5)
    prof = sim.battery_profiles(b.storage_idx)
    assert prof is not None and prof["bus"] == 3 and prof["mode"] == "self"
    assert len(prof["soc"]) == sim.steps_per_day
    assert len(prof["power"]) == sim.steps_per_day
    assert abs(prof["capacity_kwh"] - 10.0) < 1e-6
    assert all(0.0 <= s <= 100.0 for s in prof["soc"] if s is not None)
    assert sim.battery_profiles(9999) is None


def test_measured_curves_placement_and_taf_raster():
    sim = _sim()
    assert sim.measured_curves(0) == {"nodes": {}, "trafos": {}}  # unmetered grid

    sim.place_node_meter(2)
    sim.set_node_meter_mode(2, "standard")        # TAF 7: 15-min P means only
    m = sim.measured_curves(0)
    node = m["nodes"][2]
    assert node["vm"] is None and node["raster_min"] == 15
    first_window = node["p_mw"][:15]
    assert len(set(first_window)) == 1            # window mean, held flat

    sim.set_node_meter_mode(2, "full")            # TAF 9/10/14: pass-through
    node = sim.measured_curves(0)["nodes"][2]
    assert node["vm"] is not None and node["raster_min"] == 1
    truth = sim.daily_curves(0)
    assert node["p_mw"] == truth["bus_p"][2]      # full mode = the truth row


def test_deepcopied_simulator_sweeps_independently():
    """The bulk exporter deep-copies the LIVE Simulator and drives sweeps on
    the copy — the extraction must keep that path intact, with cache
    isolation between original and copy."""
    sim = _sim()
    twin = copy.deepcopy(sim)
    curves = twin.daily_curves(0)
    assert curves["n"] == twin.steps_per_day
    assert any(v is not None for v in curves["bus_vm"][1])
    assert 0 not in sim._daily_by_day             # original cache untouched
    # and the original still sweeps on its own afterwards
    assert sim.daily_curves(0)["n"] == sim.steps_per_day
