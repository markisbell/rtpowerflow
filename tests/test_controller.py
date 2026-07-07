"""Overload controllers (netzdienliche Steuerung).

A placed controller must remove an overload within a few control steps by
throttling the right lever (EV charging on import overloads, PV feed-in on
export overloads), release again with hysteresis once the grid is healthy,
and — in bus scope — only touch the DERs of its own node.

The controller is deliberately fed ONLY from the operator's view (meter
readings + WLS state estimate): the regulation tests therefore meter the
grid fully (exact estimate → behaves like the physics), while the blindness
test proves that WITHOUT meters the controller does nothing at all.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from netzsim.data_loader import input_data_from_dicts
from netzsim.osm_lv_import import convert_osm_lv
from netzsim.simulator import Simulator

ROOT = Path(__file__).resolve().parents[1]
LV_GRID = ROOT / "data" / "lv_osm" / "lv_rural_3150_300266.json"

pytestmark = pytest.mark.skipif(not LV_GRID.exists(), reason="no committed LV grid")


def _sim() -> Simulator:
    g = convert_osm_lv(LV_GRID, steps=96)
    data = input_data_from_dicts(g.grid_structure, g.lines, g.load,
                                 g.generation, g.substation)
    return Simulator(data)


def _meter_all(sim: Simulator) -> None:
    """Full observability: every node + every trafo metered → the state
    estimate is (near) exact and the controller sees the true loadings."""
    sim.meters.apply_preset("all_nodes", sim.net)
    sim.meters.apply_preset("all_trafos", sim.net)


def _run(sim: Simulator, step: int, n: int = 1):
    """run_step, forcing a FRESH estimate every step — the wall-clock throttle
    on the estimator is meant for the live loop, not back-to-back test steps
    (a stale estimate would let the release ramp starve)."""
    res = None
    for _ in range(n):
        sim._est_wall = 0.0
        res = sim.run_step(step)
    return res


def _max_loading(res) -> float:
    vals = [res.summary.get("max_line_loading_percent"),
            res.summary.get("max_trafo_loading_percent")]
    return max(v for v in vals if v is not None)


def test_station_controller_curtails_ev_overload_and_releases():
    sim = _sim()
    _meter_all(sim)
    buses = list(sim._loads_at)[:6]
    for bus in buses:                       # brutal simultaneous fast charging
        sim.add_ev(bus, kw=22.0, start_min=17 * 60, dur_min=240)
    res = _run(sim, 76)                     # 19:00 — everything charges
    assert _max_loading(res) > 100, "test premise: the grid must be overloaded"

    sim.add_controller("station", limit_pct=100.0)
    res = _run(sim, 76, n=12)               # closed loop: factors act next step
    c = sim.controllers[0]
    assert _max_loading(res) <= 100.0 + 1e-6
    assert c.ev_factor < 1.0                # the LOAD lever was pulled
    assert c.pv_factor == 1.0               # generation untouched (import case)
    assert res.controllers[0]["active"] is True
    # the controller acted on observed data, not on the truth
    assert res.controllers[0]["seen_pct"] is not None
    assert res.controllers[0]["seen_src"] in ("meter", "estimate")

    res = _run(sim, 12, n=25)               # 03:00 — nothing charges, healthy
    assert sim.controllers[0].ev_factor == 1.0   # fully released again
    assert res.controllers[0]["active"] is False


def test_station_controller_curtails_pv_export():
    sim = _sim()
    _meter_all(sim)
    for bus, kwp in ((24, 75.0), (22, 75.0), (19, 75.0)):
        sim.add_pv(bus, kwp=kwp)            # 225 kWp on a small rural grid
    res = _run(sim, 48)                     # noon
    assert _max_loading(res) > 100

    sim.add_controller("station", limit_pct=100.0)
    res = _run(sim, 48, n=12)
    c = sim.controllers[0]
    assert _max_loading(res) <= 100.0 + 1e-6
    assert c.pv_factor < 1.0                # the GENERATION lever was pulled
    assert c.ev_factor == 1.0


def test_controller_blind_without_meters_holds_despite_overload():
    """No meters → no estimate → the controller sees nothing and must NOT act,
    even though the (invisible) grid is massively overloaded. This is the
    core didactic point: control quality equals observability."""
    sim = _sim()
    for bus in list(sim._loads_at)[:6]:
        sim.add_ev(bus, kw=22.0, start_min=17 * 60, dur_min=240)
    sim.add_controller("station", limit_pct=100.0)
    res = _run(sim, 76, n=8)
    assert _max_loading(res) > 100          # the overload persists ...
    c = res.controllers[0]
    assert c["ev_factor"] == 1.0 and c["pv_factor"] == 1.0   # ... untouched
    assert c["active"] is False
    assert c["seen_pct"] is None and c["seen_src"] is None   # blind


def test_bus_controller_only_touches_its_node():
    sim = _sim()
    buses = list(sim._loads_at)[:2]
    for bus in buses:
        sim.add_ev(bus, kw=22.0, start_min=17 * 60, dur_min=240)
    sim.add_controller("bus", bus=buses[0], limit_pct=100.0)
    # force the local lever down regardless of loading
    sim.controllers[0].ev_factor = 0.5
    sim.run_step(76)
    net = sim.net
    ev_p = {int(net.load.at[li, "bus"]): float(net.load.at[li, "p_mw"])
            for li in net.load.index if "EV_" in str(net.load.at[li, "name"] or "")}
    assert ev_p[buses[0]] == pytest.approx(0.5 * 0.022, rel=1e-6)   # curtailed
    assert ev_p[buses[1]] == pytest.approx(0.022, rel=1e-6)         # untouched


def test_controller_management():
    sim = _sim()
    c = sim.add_controller("station", limit_pct=110.0)
    assert sim.set_controller(c.cid, 90.0)
    assert sim.controllers[0].limit_pct == 90.0
    assert sim.controllers[0].release_pct <= 85.0     # band stays below the limit
    assert sim.remove_controller(c.cid)
    assert sim.remove_controller(999) is False
    with pytest.raises(KeyError):
        sim.add_controller("bus", bus=99999)
    with pytest.raises(ValueError):
        sim.add_controller("fleet")
