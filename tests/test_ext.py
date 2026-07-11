"""External nodes (ext.py): the live P/Q feed for individual buses.
Pins the v1 semantics from docs/EXTERNAL_NODES.md — mailbox latest-wins,
sample-and-hold with per-node staleness policy (hold | zero), value bound
p_max_kw, the full-day history ring, swap reset, the exporter's
deterministic replay reset, and that the estimator keeps working with an
externally driven (profile-less) bus."""
from __future__ import annotations

import copy
import time
from pathlib import Path

import pytest

from netzsim.data_loader import load_inputs
from netzsim.ext import reset_ext_values
from netzsim.simulator import Simulator

ROOT = Path(__file__).resolve().parents[1]


def _sim() -> Simulator:
    return Simulator(load_inputs(ROOT / "data"))


def test_add_apply_negative_and_remove():
    sim = _sim()
    n_loads = len(sim.net.load)
    x = sim.add_ext_node(3, name="Labor")
    assert len(sim.net.load) == n_loads + 1
    assert str(sim.net.load.at[x.load_idx, "name"]) == "EXT_3"

    # never fed: stale, applied value 0 (hold policy holds the 0-kW initial)
    res = sim.run_step(720)
    assert res.converged
    e = res.ext_nodes[0]
    assert e["stale"] is True and e["p_kw"] == 0.0 and e["age_s"] is None

    # consumption
    sim.set_ext_value(x.eid, 5.0)
    res = sim.run_step(721)
    e = res.ext_nodes[0]
    assert e["stale"] is False and abs(e["p_kw"] - 5.0) < 1e-9
    assert abs(float(sim.net.load.at[x.load_idx, "p_mw"]) - 0.005) < 1e-12

    # feed-in = signed negative P
    sim.set_ext_value(x.eid, -3.0, q_kvar=1.0)
    sim.run_step(722)
    assert abs(float(sim.net.load.at[x.load_idx, "p_mw"]) + 0.003) < 1e-12
    assert abs(float(sim.net.load.at[x.load_idx, "q_mvar"]) - 0.001) < 1e-12

    assert sim.remove_ext_node(x.eid)
    assert len(sim.net.load) == n_loads
    assert len(sim.prof.load_idx) == len(sim.prof.load_p)  # rows consistent
    assert sim.run_step(723).converged                     # still solves
    assert not sim.remove_ext_node(x.eid)                  # idempotent


def test_mailbox_latest_wins():
    sim = _sim()
    x = sim.add_ext_node(2)
    sim.set_ext_value(x.eid, 2.0)
    sim.set_ext_value(x.eid, 7.0)                          # latest wins
    sim.run_step(600)
    assert abs(float(sim.net.load.at[x.load_idx, "p_mw"]) - 0.007) < 1e-12


def test_value_bound_and_duplicate_bus():
    sim = _sim()
    x = sim.add_ext_node(2, p_max_kw=10.0)
    with pytest.raises(ValueError):
        sim.set_ext_value(x.eid, 10.5)                     # beyond the bound
    with pytest.raises(ValueError):
        sim.add_ext_node(2)                                # one per bus
    with pytest.raises(KeyError):
        sim.add_ext_node(9999)                             # unknown bus


def test_staleness_hold_vs_zero():
    sim = _sim()
    hold = sim.add_ext_node(2, on_timeout="hold", hold_s=30.0)
    zero = sim.add_ext_node(3, on_timeout="zero", hold_s=30.0)
    sim.set_ext_value(hold.eid, 5.0)
    sim.set_ext_value(zero.eid, 4.0)
    # age the telegrams beyond hold_s
    hold.t_received = time.monotonic() - 100.0
    zero.t_received = time.monotonic() - 100.0
    res = sim.run_step(700)
    by_bus = {e["bus"]: e for e in res.ext_nodes}
    assert by_bus[2]["stale"] and abs(by_bus[2]["p_kw"] - 5.0) < 1e-9   # held
    assert by_bus[3]["stale"] and by_bus[3]["p_kw"] == 0.0             # zeroed
    assert abs(float(sim.net.load.at[hold.load_idx, "p_mw"]) - 0.005) < 1e-12
    assert float(sim.net.load.at[zero.load_idx, "p_mw"]) == 0.0


def test_history_ring_records_applied_kw():
    sim = _sim()
    x = sim.add_ext_node(4)
    sim.set_ext_value(x.eid, 6.0)
    sim.run_step(100)
    sim.set_ext_value(x.eid, -2.0)
    sim.run_step(101)
    assert abs(x.history[100] - 6.0) < 1e-9
    assert abs(x.history[101] + 2.0) < 1e-9
    assert x.history[102] is None                          # untouched slot


def test_swap_resets_placements():
    sim = _sim()
    sim.add_ext_node(2)
    fresh = _sim()                                          # engine.reconfigure builds a new Simulator
    assert fresh.ext_nodes == []


def test_exporter_reset_makes_replay_deterministic():
    sim = _sim()
    x = sim.add_ext_node(3)
    sim.set_ext_value(x.eid, 9.0)
    twin = copy.deepcopy(sim)                               # the exporter's path
    reset_ext_values(twin)                                  # exporter._prepare does this
    twin_node = twin.ext_nodes[0]
    assert twin_node.t_received is None and twin_node.p_mw == 0.0
    twin.run_step(500)
    assert float(twin.net.load.at[twin_node.load_idx, "p_mw"]) == 0.0
    # the LIVE sim keeps its mailbox untouched
    sim.run_step(500)
    assert abs(float(sim.net.load.at[x.load_idx, "p_mw"]) - 0.009) < 1e-12


def test_scenario_persists_placements(tmp_path, monkeypatch):
    """A scenario recipe carries the PLACEMENT (bus, name, hold_s, policy,
    bound) — never the live mailbox: after a load the node is fresh (silent,
    stale, 0 kW) exactly like a newly attached one."""
    from fastapi.testclient import TestClient

    from netzsim.config import settings

    settings.autostart = False
    monkeypatch.setattr(settings, "scenarios_dir", tmp_path)
    from netzsim.api import app

    with TestClient(app) as client:
        x = client.post("/ext", json={"bus": 2, "name": "Labor", "hold_s": 45,
                                      "on_timeout": "zero", "p_max_kw": 20}).json()
        client.put(f"/ext/{x['id']}/value", json={"p_kw": 5.0})
        sid = client.post("/scenarios",
                          json={"name": "ext roundtrip"}).json()["id"]
        assert client.post(f"/scenarios/{sid}/load").status_code == 200
        nodes = client.get("/ext").json()["ext_nodes"]
        assert len(nodes) == 1
        n = nodes[0]
        assert (n["bus"], n["name"], n["hold_s"], n["on_timeout"], n["p_max_kw"]) \
            == (2, "Labor", 45.0, "zero", 20.0)
        assert n["stale"] is True and n["p_kw"] == 0.0 and n["age_s"] is None
        client.delete(f"/ext/{n['id']}")


def test_estimation_survives_external_node():
    """A driven, profile-less bus must not break the WLS: the node's
    p_max_kw widens its pseudo (like battery buses), so the estimator
    stays solvable and delivers a result."""
    sim = _sim()
    x = sim.add_ext_node(3, p_max_kw=50.0)
    sim.set_ext_value(x.eid, 25.0)
    sim.place_node_meter(0)
    sim._est_wall = 0.0                                     # bypass the wall-clock throttle
    res = sim.run_step(720)
    assert res.converged and res.estimated is not None
    assert res.estimated.get("error") is None or "max_dv_pu" in res.estimated.get("error", {})
