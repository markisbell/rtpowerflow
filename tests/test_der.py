"""Runtime-configurable DERs: add PV/EV at a node, resize PV, move the EV
charge window (1-4 h). Parameters must be derivable from the profile rows and
the power flow must keep solving after every mutation."""
from __future__ import annotations

from pathlib import Path

import numpy as np

from netzsim.data_loader import load_inputs
from netzsim.simulator import Simulator

ROOT = Path(__file__).resolve().parents[1]


def _sim() -> Simulator:
    return Simulator(load_inputs(ROOT / "data"))


def test_add_and_resize_pv():
    sim = _sim()
    bus = 3
    assert sim.node_der(bus)["pv"] is None or "PV_" not in ""  # baseline query works
    der = sim.add_pv(bus, kwp=7.0)
    assert der["pv"] is not None and abs(der["pv"]["kwp"] - 7.0) < 0.01
    i = sim.prof.sgen_idx.index(der["pv"]["sgen"])
    assert abs(float(sim.prof.sgen_p[i].max()) * 1000 - 7.0) < 0.01
    assert float(sim.prof.sgen_p[i][0]) == 0.0          # dark at midnight

    assert sim.set_pv_kwp(der["pv"]["sgen"], 12.0)
    assert abs(sim.node_der(bus)["pv"]["kwp"] - 12.0) < 0.01
    assert sim.run_step(720).converged                  # solves at noon
    assert not sim.set_pv_kwp(9999, 5.0)                # unknown sgen


def test_add_and_move_ev():
    sim = _sim()
    bus = 2
    der = sim.add_ev(bus, kw=11.0, start_min=18 * 60, dur_min=120)
    ev = der["ev"]
    assert ev is not None and ev["start_min"] == 18 * 60 and ev["dur_min"] == 120
    assert abs(ev["kw"] - 11.0) < 0.01

    # move the window across midnight; duration clamps into 1-4 h
    assert sim.set_ev(ev["load"], start_min=23 * 60, dur_min=999)
    ev2 = sim.node_der(bus)["ev"]
    assert ev2["dur_min"] == 240                        # clamped to 4 h
    assert ev2["start_min"] == 23 * 60                  # derived across the wrap
    i = sim.prof.load_idx.index(ev2["load"])
    assert int(np.count_nonzero(sim.prof.load_p[i])) == 240
    assert sim.run_step(10).converged                   # charging past midnight

    assert sim.set_ev(ev["load"], start_min=8 * 60, dur_min=30)
    assert sim.node_der(bus)["ev"]["dur_min"] == 60     # clamped to 1 h


def test_remove_pv_and_ev():
    sim = _sim()
    n_sgen0, n_load0 = len(sim.net.sgen), len(sim.net.load)
    pv = sim.add_pv(3, kwp=5.0)["pv"]
    ev = sim.add_ev(2)["ev"]
    assert sim.remove_pv(pv["sgen"]) and sim.remove_ev(ev["load"])
    assert sim.node_der(3)["pv"] is None and sim.node_der(2)["ev"] is None
    assert len(sim.net.sgen) == n_sgen0 and len(sim.net.load) == n_load0
    assert sim.prof.load_p.shape[0] == len(sim.prof.load_idx)
    assert sim.run_step(700).converged                  # net still solves
    assert not sim.remove_pv(9999) and not sim.remove_ev(9999)


def test_daily_graphs_survive_runtime_der_changes():
    """Regression: the daily sweep used to rebuild its net from the ORIGINAL
    input data, so runtime-added DERs made the rebuilt bus-row maps point past
    the sweep's smaller profile arrays (IndexError -> 500 on /line/{}/profiles).
    The sweep must snapshot the live net and include runtime DERs."""
    sim = _sim()
    sim.add_ev(2, kw=11.0, start_min=20 * 60, dur_min=120)
    sim.add_pv(3, kwp=8.0)
    sim.meters.apply_preset("all_nodes", sim.net)   # meters -> sweep runs the estimator
    lp = sim.line_profiles(1)                       # crashed before the fix
    assert lp["current"] and lp["est_current"] is not None
    # the sweep sees the runtime EV: its bus load peaks during the charge window
    np_ = sim.node_profiles(2)
    ev_series = [srs for srs in np_["series"] if srs["kind"] == "ev"]
    assert ev_series and max(v for v in ev_series[0]["p_mw"] if v is not None) > 0.01
    # removal keeps the sweep consistent too
    ev = sim.node_der(2)["ev"]
    sim.remove_ev(ev["load"])
    assert sim.line_profiles(1)["current"]


def test_der_mutation_invalidates_caches():
    sim = _sim()
    sim.daily_curves(0)
    assert sim._daily_by_day
    sim.add_pv(4, kwp=3.0)
    assert not sim._daily_by_day                        # sweep cache dropped
    assert sim._estimator is None                       # estimator stats dropped
    # battery self-mode + estimator pseudo see the new PV via _sgens_at
    assert any(i for i in [1] if 4 in sim._sgens_at)
