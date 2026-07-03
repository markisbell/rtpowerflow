"""State estimation: the operator's calculated view from meters + grid model.

The estimator (estimator.py) runs WLS on the placed measurements, structural
zero-injection knowledge and profile-based pseudo-loads. It must reproduce the
truth closely under full metering, stay sane under sparse metering, appear
only while meters are placed, and survive strict mode without its truth-based
error metric.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from netzsim.data_loader import input_data_from_dicts, load_inputs
from netzsim.osm_lv_import import convert_osm_lv
from netzsim.simulator import Simulator
from netzsim.state import StateStore

ROOT = Path(__file__).resolve().parents[1]
LV_GRID = ROOT / "data" / "lv_osm" / "lv_rural_3150_300266.json"


@pytest.fixture(scope="module")
def lv_sim() -> Simulator:
    g = convert_osm_lv(LV_GRID, steps=96)
    data = input_data_from_dicts(g.grid_structure, g.lines, g.load,
                                 g.generation, g.substation)
    return Simulator(data)


def test_no_meters_no_estimate():
    sim = Simulator(load_inputs(ROOT / "data"))
    res = sim.run_step(50)
    assert res.converged and res.estimated is None


@pytest.mark.skipif(not LV_GRID.exists(), reason="no committed LV grid")
def test_full_metering_reproduces_truth(lv_sim):
    lv_sim.meters.apply_preset("all_nodes", lv_sim.net)
    res = lv_sim.run_step(76)                      # evening peak
    assert res.converged and res.estimated is not None
    err = res.estimated["error"]
    assert err["max_dv_pu"] < 0.002, f"max dV = {err['max_dv_pu']}"
    assert len(res.estimated["buses"]) == len(res.buses)
    assert len(res.estimated["lines"]) == len(res.lines)


@pytest.mark.skipif(not LV_GRID.exists(), reason="no committed LV grid")
def test_sparse_metering_stays_sane(lv_sim):
    lv_sim.meters.clear()
    lv_sim.meters.apply_preset("substation_trafos", lv_sim.net)  # trafo + busbar only
    res = lv_sim.run_step(76)
    assert res.converged and res.estimated is not None
    err = res.estimated["error"]
    # sparse meters + pseudo-loads: still a usable estimate (well under 1 % V)
    assert err["max_dv_pu"] < 0.01, f"max dV = {err['max_dv_pu']}"
    # the estimate covers the WHOLE grid, not just metered elements
    assert all(b["vm_pu"] is not None for b in res.estimated["buses"])


@pytest.mark.skipif(not LV_GRID.exists(), reason="no committed LV grid")
def test_standard_metering_p_only_degrades_estimate(lv_sim):
    """Standard metering (Lastgang): meters deliver only the 15-min mean active
    power — no voltage, no reactive power. The estimate must still work (from
    P readings + pseudo knowledge), just with degraded quality."""
    lv_sim.meters.clear()
    lv_sim.meters.apply_preset("all_nodes", lv_sim.net)
    lv_sim.meters.set_mode("standard")
    try:
        res = lv_sim.run_step(76)
        assert res.converged
        readings = [n for n in res.measurements["nodes"] if n["p_mw"] is not None]
        assert readings, "standard meters delivered no P at all"
        assert all(n["vm_pu"] is None and n["q_mvar"] is None
                   for n in res.measurements["nodes"])
        assert res.estimated is not None
        err = res.estimated["error"]
        assert err["max_dv_pu"] < 0.05, f"max dV = {err['max_dv_pu']}"

        # full mode at the same placement is strictly more informative
        lv_sim.meters.set_mode("full")
        res_full = lv_sim.run_step(76)
        assert res_full.estimated["error"]["max_dv_pu"] <= err["max_dv_pu"] + 1e-9
    finally:
        lv_sim.meters.set_mode("full")


@pytest.mark.skipif(not LV_GRID.exists(), reason="no committed LV grid")
def test_daily_curves_include_estimate(lv_sim):
    """The daily sweep overlays the operator's estimate: under full metering it
    matches the true current; clearing the meters drops the estimated curve
    (the sweep cache is keyed on the placement)."""
    lv_sim.meters.clear()
    lv_sim.meters.apply_preset("all_nodes", lv_sim.net)
    lp = lv_sim.line_profiles(0)
    assert lp["est_current"] is not None
    pairs = [(a, b) for a, b in zip(lp["current"], lp["est_current"])
             if a is not None and b is not None]
    assert len(pairs) > 10, "estimate present at too few samples"
    assert max(abs(a - b) for a, b in pairs) < 1e-4   # full metering ≈ truth

    lv_sim.meters.clear()
    assert lv_sim.line_profiles(0)["est_current"] is None


@pytest.mark.skipif(not LV_GRID.exists(), reason="no committed LV grid")
def test_strict_mode_keeps_estimate_strips_error(lv_sim):
    lv_sim.meters.clear()
    lv_sim.meters.apply_preset("substation_trafos", lv_sim.net)
    res = lv_sim.run_step(30)
    store = StateStore(history_size=4, expose_ground_truth=False)
    wire = store._project(__import__("dataclasses").asdict(res))
    assert "buses" not in wire and "summary" not in wire   # truth stripped
    assert wire["estimated"] is not None                   # estimate survives
    assert "error" not in wire["estimated"]                # ...without the metric
    assert wire["estimated"]["buses"], "estimated buses missing on the wire"
