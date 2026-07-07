"""Bulk export: offline replay of whole days into a recording pack.

The exporter must reproduce the LIVE physics (batteries integrating across
midnight, measurements in the meter raster), honor the estimate switch,
report progress, cancel cleanly with a finalized partial pack, and refuse
concurrent runs.
"""
from __future__ import annotations

import copy
import csv
import json
import time
from pathlib import Path

import pytest

from netzsim.data_loader import input_data_from_dicts
from netzsim.exporter import BulkExporter
from netzsim.osm_lv_import import convert_osm_lv
from netzsim.simulator import Simulator

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


def _wait(exp: BulkExporter, timeout: float = 180.0) -> dict:
    t0 = time.time()
    while exp.status()["active"]:
        assert time.time() - t0 < timeout, "export did not finish in time"
        time.sleep(0.2)
    return exp.status()


def test_bulk_export_two_days_without_estimate(tmp_path):
    sim = _lv_sim()
    sim.meters.apply_preset("substation_trafos", sim.net)
    bus = next(iter(sim._loads_at))
    sim.add_battery(bus, 10.0, 5.0, "self")

    exp = BulkExporter(tmp_path)
    exp.start(copy.deepcopy(sim), {"grid": {"name": "bulk"}}, [0, 1],
              estimate=False, name="zwei tage")
    st = _wait(exp)
    assert st["error"] is None and st["cancelled"] is False
    assert st["steps_done"] == 2 * 96

    d = tmp_path / st["id"]
    meta = json.loads((d / "metadata.json").read_text(encoding="utf-8"))
    assert meta["export"]["days"] == [0, 1]
    assert meta["export"]["estimate"] is False
    assert meta["steps_recorded"] == 192

    summary = _rows(d, "summary.csv")
    assert len(summary) == 192
    assert {r["day"] for r in summary} == {"0", "1"}
    assert len(_rows(d, "buses.csv")) == 192 * len(sim.net.bus)
    assert len(_rows(d, "measurements_nodes.csv")) == 192 * len(sim.meters.node_buses)
    assert not (d / "estimated_buses.csv").exists()   # estimate switched off

    # live physics: the battery SOC integrates ACROSS midnight (day 0 -> 1),
    # instead of resetting like the per-day sweep does
    bats = _rows(d, "batteries.csv")
    assert len(bats) == 192
    soc_end_d0 = float(bats[95]["soc_percent"])
    soc_start_d1 = float(bats[96]["soc_percent"])
    assert abs(soc_start_d1 - soc_end_d0) < 2.0


def test_bulk_export_estimate_in_meter_raster(tmp_path):
    sim = _lv_sim()
    sim.meters.apply_preset("substation_trafos", sim.net)
    exp = BulkExporter(tmp_path)
    exp.start(copy.deepcopy(sim), {}, [0], estimate=True)
    st = _wait(exp)
    assert st["error"] is None
    d = tmp_path / st["id"]
    # 96-step day = 15-min raster -> a fresh estimate at EVERY step
    assert len(_rows(d, "estimated_buses.csv")) == 96 * len(sim.net.bus)


def test_bulk_export_cancel_keeps_partial_pack(tmp_path):
    sim = _lv_sim()
    exp = BulkExporter(tmp_path)
    exp.start(copy.deepcopy(sim), {}, list(range(50)), estimate=False)
    with pytest.raises(RuntimeError):            # no concurrent exports
        exp.start(copy.deepcopy(sim), {}, [0])
    time.sleep(1.0)
    st = exp.cancel()
    assert st["cancelled"] is True and st["active"] is False
    assert 0 < st["steps_done"] < 50 * 96

    d = tmp_path / st["id"]
    meta = json.loads((d / "metadata.json").read_text(encoding="utf-8"))
    assert meta["export"]["cancelled"] is True
    assert meta["steps_recorded"] == st["steps_done"]
    assert len(_rows(d, "summary.csv")) == st["steps_done"]
