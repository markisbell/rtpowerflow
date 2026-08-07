"""Regression net for the sweeps.py extraction: the day-sweep layer moved out
of Simulator (thin delegates remain). Pins the pieces the existing suite did
not cover directly — battery day curves, the measured layer's TAF rastering,
and that a deep-copied Simulator (the bulk exporter's path) sweeps
independently of the original's caches."""
from __future__ import annotations

import copy
import threading
from pathlib import Path

from netzsim import sweeps
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


def test_daily_est_runs_the_wls_only_at_the_pinned_raster():
    """The estimated day layer samples at the per-grid raster tier that the
    FIRST estimate's cost pins (here: 600 ms -> 120-min tier -> 12 samples).
    The WLS must RUN exactly at those 12 sample steps — the sweep may not
    solve estimates it then throws away (on a district that was 96 runs for
    12 kept samples, ~160 s instead of ~20 s).

    The meters are read at the FINE 15-min cadence regardless, because a
    TAF-7 device publishes the mean of the last COMPLETED window: the
    reading handed to the WLS at step 120 is the window of step 105, not a
    two-hour-old value. That coupling is what makes this delicate."""
    sim = _sim()
    sim.place_node_meter(2)
    sim.set_node_meter_mode(2, "standard")        # TAF 7 -> 15-min windows
    truth = sim.daily_curves(0)

    runs: list[tuple[int, float | None]] = []

    class CountingEstimator:
        """Stand-in for the WLS: records when it is asked to estimate and
        what the meters delivered at that moment."""

        def __init__(self, net):
            self.net = net
            self.t = 0

        def run(self, net, observed, sgen_day_mean, battery_buses=None, **kw):
            nodes = observed.get("nodes", [])
            runs.append((self.t, nodes[0]["p_mw"] if nodes else None))
            return {"buses": [{"index": int(b), "vm_pu": 1.0} for b in net.bus.index],
                    "lines": [{"index": int(l), "i_ka": 0.1} for l in net.line.index],
                    "trafos": [{"index": int(tr), "p_hv_mw": 0.0} for tr in net.trafo.index],
                    "solve_ms": 600.0}

    est_holder = CountingEstimator(sim.net)

    # the sweep asks the Simulator for its estimator; hand it the counter and
    # let it know the step being estimated (sweeps drives them in order)
    orig_observe = sweeps.MeasurementSet.observe

    def observe(self, net, t: int = 0):
        est_holder.t = t
        return orig_observe(self, net, t)

    sim._make_estimator = lambda net: est_holder                 # type: ignore[assignment]
    sweeps.MeasurementSet.observe = observe                      # type: ignore[assignment]
    try:
        out = sim.daily_est(0)
    finally:
        sweeps.MeasurementSet.observe = orig_observe             # type: ignore[assignment]

    spd = sim.steps_per_day
    assert sim._est_sweep_min == 120                             # tier from 600 ms
    sample_steps = [t for t in range(spd) if out["est_bus_vm"][2][t] is not None]
    assert sample_steps == list(range(0, spd, 120))              # 12 kept samples

    # ... and the WLS ran exactly there — no discarded runs
    assert [t for t, _ in runs] == sample_steps

    # the TAF-7 reading fed into the WLS is the last COMPLETED 15-min window
    at_120 = dict(runs)[120]
    assert at_120 == truth["bus_p"][2][105]


def test_concurrent_requests_share_one_sweep():
    """Several side-panel sections refetch at once (view switch, day wrap).
    They must SHARE the day sweep — before this was serialized, three
    concurrent cold requests on the 475-bus district each ran their own full
    sweep and took 144 s instead of the 18 s one costs."""
    sim = _sim()
    sweeps_run = 0
    orig = sweeps._daily_curves_uncached

    def counting(s, d):
        nonlocal sweeps_run
        sweeps_run += 1
        return orig(s, d)

    sweeps._daily_curves_uncached = counting                     # type: ignore[assignment]
    try:
        threads = [threading.Thread(target=sim.daily_curves, args=(0,)) for _ in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
    finally:
        sweeps._daily_curves_uncached = orig                     # type: ignore[assignment]

    assert sweeps_run == 1
    assert sim.daily_curves(0)["n"] == sim.steps_per_day


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
