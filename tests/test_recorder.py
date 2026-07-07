"""Session recorder: every published step lands as tidy CSVs on disk.

The recorder consumes the store's projected wire payload, so it must write
exactly the layers the wire carries (strict mode → no truth files), dedupe
double publishes by (day, step), write estimate blocks only when a NEW
estimate arrived, and finalize into a self-describing directory with
metadata.json that packs into a ZIP.
"""
from __future__ import annotations

import asyncio
import csv
import json
import zipfile
from dataclasses import asdict
from pathlib import Path

import pytest

from netzsim.data_loader import input_data_from_dicts
from netzsim.engine import RealtimeEngine
from netzsim.osm_lv_import import convert_osm_lv
from netzsim.recorder import Recorder
from netzsim.simulator import Simulator
from netzsim.state import StateStore

ROOT = Path(__file__).resolve().parents[1]
LV_GRID = ROOT / "data" / "lv_osm" / "lv_rural_3150_300266.json"

pytestmark = pytest.mark.skipif(not LV_GRID.exists(), reason="no committed LV grid")


def _lv_sim() -> Simulator:
    g = convert_osm_lv(LV_GRID, steps=96)
    data = input_data_from_dicts(g.grid_structure, g.lines, g.load,
                                 g.generation, g.substation)
    return Simulator(data)


def _rows(d: Path, name: str) -> list[dict]:
    with (d / name).open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_recorder_writes_tidy_csvs_and_dedupes(tmp_path):
    sim = _lv_sim()
    sim.meters.apply_preset("substation_trafos", sim.net)
    rec = Recorder(tmp_path)
    rec.start({"grid": {"name": "testnetz"}}, name="probe lauf")
    for t in (40, 41, 41, 42):                  # 41 doubled -> must dedupe
        sim._est_wall = 0.0                     # fresh estimate every step
        rec.record(asdict(sim.run_step(t)))
    out = rec.stop()

    assert out["steps"] == 3
    assert "probe-lauf" in out["id"]
    d = tmp_path / out["id"]
    meta = json.loads((d / "metadata.json").read_text(encoding="utf-8"))
    assert meta["steps_recorded"] == 3
    assert meta["grid"]["name"] == "testnetz"
    assert "summary.csv" in meta["files"]

    summary = _rows(d, "summary.csv")
    assert [r["step"] for r in summary] == ["40", "41", "42"]
    assert "vm_pu_min" in summary[0] and "converged" in summary[0]

    n_bus, n_line = len(sim.net.bus), len(sim.net.line)
    assert len(_rows(d, "buses.csv")) == 3 * n_bus
    assert len(_rows(d, "lines.csv")) == 3 * n_line
    assert len(_rows(d, "trafos.csv")) == 3 * len(sim.net.trafo)

    # the Gemessen layer: only placed devices (busbar meter + trafo meter)
    mn = _rows(d, "measurements_nodes.csv")
    assert len(mn) == 3 * len(sim.meters.node_buses)
    assert {r["bus"] for r in mn} == {str(b) for b in sim.meters.node_buses}
    assert len(_rows(d, "measurements_trafos.csv")) == 3

    # one estimate block per step (forced fresh above)
    assert len(_rows(d, "estimated_buses.csv")) == 3 * n_bus

    # no batteries placed -> no batteries.csv at all
    assert not (d / "batteries.csv").exists()


def test_recorder_writes_estimate_only_when_new(tmp_path):
    sim = _lv_sim()
    sim.meters.apply_preset("substation_trafos", sim.net)
    rec = Recorder(tmp_path)
    rec.start({})
    sim._est_wall = 0.0
    rec.record(asdict(sim.run_step(40)))        # fresh estimate at step 40
    sim._est_ms = 1e9                           # throttle: no new estimate
    rec.record(asdict(sim.run_step(41)))        # carries the stale one
    out = rec.stop()
    d = tmp_path / out["id"]
    est = _rows(d, "estimated_buses.csv")
    assert len(est) == len(sim.net.bus)         # ONE block, not two
    assert {r["step"] for r in est} == {"40"}
    assert len(_rows(d, "summary.csv")) == 2    # truth still per step


def test_recorder_strict_payload_carries_no_truth(tmp_path):
    """Fed through a strict-mode store projection, the recording must contain
    the Gemessen + Schätzung layers but no truth file at all."""
    sim = _lv_sim()
    sim.meters.apply_preset("substation_trafos", sim.net)
    store = StateStore(history_size=4, expose_ground_truth=False)
    rec = Recorder(tmp_path)
    rec.start({})
    sim._est_wall = 0.0
    rec.record(store._project(asdict(sim.run_step(40))))
    out = rec.stop()
    d = tmp_path / out["id"]
    for truth_file in ("summary.csv", "buses.csv", "lines.csv", "trafos.csv"):
        assert not (d / truth_file).exists(), f"{truth_file} leaked in strict mode"
    assert len(_rows(d, "measurements_nodes.csv")) == len(sim.meters.node_buses)
    assert (d / "estimated_buses.csv").exists()
    assert (d / "observed_summary.csv").exists()


def test_recorder_pack_list_delete(tmp_path):
    sim = _lv_sim()
    rec = Recorder(tmp_path)
    rec.start({"grid": {"name": "zipnetz"}})
    rec.record(asdict(sim.run_step(10)))
    rid = rec.stop()["id"]

    listed = rec.list()
    assert len(listed) == 1 and listed[0]["id"] == rid and listed[0]["steps"] == 1

    zp = rec.pack(rid)
    with zipfile.ZipFile(zp) as z:
        names = z.namelist()
    assert f"{rid}/metadata.json" in names and f"{rid}/summary.csv" in names

    with pytest.raises(KeyError):
        rec.dir_of("../evil")                   # traversal guard
    rec.delete(rid)
    assert rec.list() == []
    with pytest.raises(KeyError):
        rec.pack(rid)


def test_engine_publishes_into_recorder(tmp_path):
    """End-to-end: the engine loop publishes through the store sink and the
    recorder counts exactly the published steps."""
    grid = {"name": "mini", "buses": [{"name": "b0", "vn_kv": 0.4},
                                      {"name": "b1", "vn_kv": 0.4}]}
    lines = {"lines": [{"name": "L0", "from_bus": 0, "to_bus": 1,
                        "length_km": 0.05, "r_ohm_per_km": 0.2,
                        "x_ohm_per_km": 0.08, "c_nf_per_km": 0.0,
                        "max_i_ka": 0.2}], "transformers": []}
    load = {"resolution_minutes": 1, "steps": 8,
            "loads": [{"name": "LD", "bus": 1, "p_mw": [0.001] * 8}]}
    data = input_data_from_dicts(
        grid, lines, load, {"steps": 8, "generation": []},
        {"steps": 8, "substations": [{"name": "s", "bus": 0, "vm_pu": [1.0] * 8}]})

    async def scenario():
        store = StateStore(history_size=10)
        rec = Recorder(tmp_path)
        store.set_sink(rec.record)
        engine = RealtimeEngine(Simulator(data), store, interval_seconds=0.01)
        rec.start({"grid": {"name": "mini"}})
        engine.start_loop()
        await asyncio.sleep(0.3)
        await engine.stop()
        return rec.stop()

    out = asyncio.run(scenario())
    assert out["steps"] >= 2
    d = tmp_path / out["id"]
    assert len(_rows(d, "summary.csv")) == out["steps"]
    assert len(_rows(d, "buses.csv")) == out["steps"] * 2
