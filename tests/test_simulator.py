"""Smoke tests: data loads, network builds, and steps solve and wrap a day."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory):
    # Generate the sample dataset into a temp dir via the script logic.
    sys.path.insert(0, str(ROOT / "scripts"))
    import generate_sample_data as g  # type: ignore

    out = tmp_path_factory.mktemp("data")
    g.DATA = out
    g.main()
    return out


def test_load_and_build(data_dir):
    from netzsim.data_loader import load_inputs
    from netzsim.simulator import Simulator

    data = load_inputs(data_dir)
    assert data.steps_per_day == 1440
    sim = Simulator(data)
    assert len(sim.net.bus) == 5
    assert len(sim.net.line) == 4
    assert len(sim.net.ext_grid) == 1


def test_step_solves(data_dir):
    from netzsim.data_loader import load_inputs
    from netzsim.simulator import Simulator

    sim = Simulator(load_inputs(data_dir))
    res = sim.run_step(720)  # midday (1440 / 2)
    assert res.converged
    assert res.time_of_day == "12:00"
    assert res.summary["n_bus"] == 5
    assert 0.9 < res.summary["vm_pu_min"] <= res.summary["vm_pu_max"] < 1.1

    # sample grid has no transformer -> trafo results are empty, not missing
    assert res.trafos == []
    assert res.summary["n_trafo"] == 0
    assert res.summary["max_trafo_loading_percent"] is None


def test_topology_carries_both_layouts(data_dir):
    from netzsim.data_loader import load_inputs
    from netzsim.simulator import Simulator

    topo = Simulator(load_inputs(data_dir)).topology()
    buses = topo["buses"]
    assert len(buses) == 5
    for b in buses:
        for key in ("x", "y", "tx", "ty"):  # geographic (x,y) + tree (tx,ty)
            assert key in b and 0.0 <= b[key] <= 1.0
    # geographic positions are all distinct (no two buses on top of each other)
    assert len({(b["x"], b["y"]) for b in buses}) == 5
    # load buses exposed for the map underlay (sample has 4 loads on buses 1–4)
    assert topo["load_buses"] == [1, 2, 3, 4]
    assert topo["sgen_buses"] == [3, 4]  # sample PV on buses 3 & 4


def test_result_is_strict_json_safe():
    # Non-finite power-flow values (e.g. isolated buses) must serialize as null,
    # not the JSON-invalid literal NaN/Infinity that strict parsers reject.
    import json
    import math

    from netzsim.simulator import _r

    assert _r(float("nan")) is None
    assert _r(float("inf")) is None
    assert _r(-math.inf) is None
    assert _r(1.23456789) == 1.234568

    payload = {"vm_pu": _r(float("nan")), "loading": _r(42.0)}
    # allow_nan=False raises if any NaN/Infinity slipped through
    assert json.dumps(payload, allow_nan=False) == '{"vm_pu": null, "loading": 42.0}'


def test_day_wraps(data_dir):
    from netzsim.data_loader import load_inputs
    from netzsim.simulator import Simulator

    sim = Simulator(load_inputs(data_dir))
    first = sim.run_step(0)
    wrapped = sim.run_step(1440)  # 1440 % 1440 == 0 -> same profile values
    assert wrapped.time_of_day == first.time_of_day == "00:00"
    assert wrapped.summary["total_load_mw"] == pytest.approx(
        first.summary["total_load_mw"], rel=1e-6
    )
