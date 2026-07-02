"""Observability layer: measurement placement + projection of reality to the
observed subset, and strict-mode stripping of ground truth from the wire."""
from __future__ import annotations

import asyncio
import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory):
    sys.path.insert(0, str(ROOT / "scripts"))
    import generate_sample_data as g  # type: ignore

    out = tmp_path_factory.mktemp("data")
    g.DATA = out
    g.main()
    return out


def _sim(data_dir):
    from netzsim.data_loader import load_inputs
    from netzsim.simulator import Simulator

    return Simulator(load_inputs(data_dir))


# --- node smart meters ------------------------------------------------------ #
def test_no_meters_means_nothing_observed(data_dir):
    res = _sim(data_dir).run_step(720)
    assert res.converged
    # truth is fully present...
    assert len(res.buses) == 5
    # ...but nothing is observed until a meter is placed
    assert res.measurements["nodes"] == []
    assert res.measurements["trafos"] == []
    assert res.measurements["coverage"]["n_node_meter"] == 0
    assert res.observed_summary["vm_pu_min"] is None
    assert res.observed_summary["vm_pu_max"] is None


def test_node_meter_reveals_only_its_bus(data_dir):
    sim = _sim(data_dir)
    assert sim.place_node_meter(2) is True
    assert sim.place_node_meter(2) is False  # idempotent
    res = sim.run_step(720)

    nodes = res.measurements["nodes"]
    assert [n["bus"] for n in nodes] == [2]
    m = nodes[0]
    # a smart meter reads V, P, Q and the derived current
    for k in ("vm_pu", "v_ll_kv", "p_mw", "q_mvar", "s_mva", "i_ka"):
        assert m[k] is not None
    # current is the balanced 3-phase relation I = S / (sqrt(3) * V_LL)
    expected_i = m["s_mva"] / (math.sqrt(3.0) * m["v_ll_kv"])
    assert m["i_ka"] == pytest.approx(expected_i, rel=1e-4)
    # the reading matches reality at that bus (this is a faithful meter, no noise)
    truth = next(b for b in res.buses if b["index"] == 2)
    assert m["vm_pu"] == pytest.approx(truth["vm_pu"], rel=1e-6)
    assert m["p_mw"] == pytest.approx(truth["p_mw"], rel=1e-6)
    assert res.measurements["coverage"]["n_node_meter"] == 1
    assert res.measurements["phases"] == 3 and res.measurements["balanced"] is True


def test_observed_summary_spans_only_metered_nodes(data_dir):
    sim = _sim(data_dir)
    sim.place_node_meter(1)
    sim.place_node_meter(3)
    res = sim.run_step(720)
    vms = [n["vm_pu"] for n in res.measurements["nodes"]]
    assert res.observed_summary["vm_pu_min"] == pytest.approx(min(vms))
    assert res.observed_summary["vm_pu_max"] == pytest.approx(max(vms))
    assert res.observed_summary["n_node_meter"] == 2
    assert res.observed_summary["n_bus"] == 5


def test_remove_and_preset(data_dir):
    sim = _sim(data_dir)
    sim.apply_meter_preset("all_nodes")
    assert sim.measurement_placement()["coverage"]["n_node_meter"] == 5
    sim.remove_node_meter(0)
    assert sim.measurement_placement()["coverage"]["n_node_meter"] == 4
    sim.apply_meter_preset("clear")
    assert sim.measurement_placement()["coverage"]["n_node_meter"] == 0


def test_unknown_bus_rejected(data_dir):
    with pytest.raises(KeyError):
        _sim(data_dir).place_node_meter(999)


# --- transformer meters (inline net, since the sample grid has no trafo) ---- #
def test_trafo_meter_observation():
    import pandapower as pp

    from netzsim.measurements import MeasurementSet

    net = pp.create_empty_network()
    hv = pp.create_bus(net, vn_kv=20.0)
    lv = pp.create_bus(net, vn_kv=0.4)
    pp.create_ext_grid(net, hv, vm_pu=1.0)
    pp.create_transformer(net, hv, lv, std_type="0.4 MVA 20/0.4 kV")
    pp.create_load(net, lv, p_mw=0.1, q_mvar=0.03)
    pp.runpp(net)

    ms = MeasurementSet()
    assert ms.add_trafo(0) is True
    obs = ms.observe(net)
    assert obs["nodes"] == []            # no node meters placed
    assert len(obs["trafos"]) == 1
    tr = obs["trafos"][0]
    assert tr["trafo"] == 0
    assert tr["loading_percent"] is not None and tr["loading_percent"] > 0
    assert tr["p_hv_mw"] is not None
    summ = ms.observed_summary(obs)
    assert summ["max_trafo_loading_percent"] == pytest.approx(tr["loading_percent"])
    assert summ["n_trafo_meter"] == 1


# --- strict-mode wire stripping --------------------------------------------- #
def _publish_and_get_latest(store, result):
    async def run():
        await store.publish(result)
        return store.latest

    return asyncio.run(run())


def test_state_store_strips_truth_when_not_exposing(data_dir):
    from netzsim.state import StateStore

    sim = _sim(data_dir)
    sim.place_node_meter(2)
    res = sim.run_step(720)

    exposed = _publish_and_get_latest(StateStore(expose_ground_truth=True), res)
    assert "buses" in exposed and "summary" in exposed
    assert exposed["measurements"]["nodes"][0]["bus"] == 2

    strict = _publish_and_get_latest(StateStore(expose_ground_truth=False), res)
    for k in ("buses", "lines", "trafos", "ext_grids", "summary"):
        assert k not in strict, f"{k} leaked in strict mode"
    # the observed projection + scalar fields survive
    assert strict["measurements"]["nodes"][0]["bus"] == 2
    assert strict["observed_summary"]["n_node_meter"] == 1
    assert strict["step"] == 720 and strict["converged"] is True
