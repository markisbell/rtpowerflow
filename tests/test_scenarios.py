"""Scenarios: the configured live setup as a replayable recipe.

The DER journal must capture runtime mutations (bus-addressed, coalesced),
and replaying journal + batteries + meters on a FRESH simulator must
reproduce the setup: same per-bus DER configs, batteries and placement.
"""
from __future__ import annotations

from pathlib import Path

from netzsim.data_loader import load_inputs
from netzsim.scenarios import ScenarioStore
from netzsim.simulator import Simulator

ROOT = Path(__file__).resolve().parents[1]


def _sim() -> Simulator:
    return Simulator(load_inputs(ROOT / "data"))


def test_der_journal_records_and_coalesces():
    sim = _sim()
    sim.add_pv(3, kwp=5.0)
    sim.set_pv_kwp(sim.node_der(3)["pv"]["sgen"], 9.0)   # folds into the add
    sim.add_ev(2, kw=11.0, start_min=19 * 60, dur_min=120)
    sim.set_ev(sim.node_der(2)["ev"]["load"], 21 * 60, 180)
    ops = {(e["op"], e["bus"]): e for e in sim.der_log}
    assert ops[("add_pv", 3)]["kwp"] == 9.0
    assert ops[("add_ev", 2)]["start_min"] == 21 * 60
    assert ops[("add_ev", 2)]["dur_min"] == 180
    assert len(sim.der_log) == 2                          # coalesced

    # runtime add + remove cancels out of the journal entirely
    sim.add_pv(4, kwp=3.0)
    sim.remove_pv(sim.node_der(4)["pv"]["sgen"])
    assert not any(e["bus"] == 4 for e in sim.der_log)


def test_battery_resize_in_place():
    """A deployed battery is resizable (energy + power); the SOC keeps its
    fraction, the storage table follows, and a scenario saved afterwards
    carries the new size."""
    sim = _sim()
    b = sim.add_battery(4, 10.0, 5.0, "self", soc0=0.8)
    assert sim.set_battery_size(b.storage_idx, 100.0, 50.0)
    b = sim.batteries[0]
    assert b.capacity_mwh == 0.1 and b.power_mw == 0.05
    assert abs(b.soc_frac() - 0.8) < 1e-9                 # fraction preserved
    assert float(sim.net.storage.iloc[0]["max_e_mwh"]) == 0.1
    assert sim.set_battery_size(999, 5.0, 2.5) is False   # unknown index


def test_scenario_roundtrip_reproduces_setup():
    a = _sim()
    a.add_pv(3, kwp=8.0)
    a.add_ev(2, kw=11.0, start_min=22 * 60, dur_min=180)
    a.add_battery(4, 10.0, 5.0, "peak")
    a.meters.add_node(2)
    a.meters.add_trafo(0) if len(a.net.trafo) else None
    a.meters.set_mode("standard")

    # replay journal + snapshots on a fresh simulator (what /scenarios/{id}/load does)
    b = _sim()
    for op in a.der_log:
        assert b.apply_der_op(op)
    for bat in a.batteries:
        b.add_battery(bat.bus, bat.capacity_mwh * 1000, bat.power_mw * 1000, bat.mode)
    for bus in a.meters.node_buses:
        b.meters.add_node(bus)
    b.meters.set_mode(a.meters.mode)

    assert b.node_der(3)["pv"]["kwp"] == a.node_der(3)["pv"]["kwp"] == 8.0
    assert b.node_der(2)["ev"] == a.node_der(2)["ev"]
    assert [(x.bus, x.mode) for x in b.batteries] == [(4, "peak")]
    assert b.meters.node_buses == a.meters.node_buses
    assert b.meters.mode == "standard"
    assert b.run_step(76).converged


def test_store_write_read_list_delete(tmp_path):
    store = ScenarioStore(tmp_path / "scen")
    sid = store.write({"name": "PV Überlast! (Demo #1)", "grid_id": None,
                       "der_ops": [], "batteries": []})
    assert sid == "pv-berlast-demo-1"
    assert store.read(sid)["name"] == "PV Überlast! (Demo #1)"
    listing = store.list()
    assert len(listing) == 1 and listing[0]["id"] == sid
    # same name overwrites (iterating on a scenario), different name adds
    store.write({"name": "PV Überlast! (Demo #1)", "grid_id": "x"})
    store.write({"name": "Zweites Szenario"})
    assert len(store.list()) == 2
    assert store.read(sid)["grid_id"] == "x"
    assert store.delete(sid) and not store.delete(sid)
    assert store.read(sid) is None
