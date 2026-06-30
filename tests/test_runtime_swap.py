"""Tests for the runtime grid catalog (/grids) and engine.reconfigure (§3c)."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from netzsim.data_loader import input_data_from_dicts
from netzsim.engine import RealtimeEngine
from netzsim.grid_catalog import GridCatalog, preview
from netzsim.simulator import Simulator
from netzsim.state import StateStore

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "data" / "grid_library.json"
DING0_DIR = ROOT / "data" / "ding0_grids"


def _grid(n_lv: int, steps: int = 8):
    """A minimal solvable radial grid: slack(bus0) - n_lv LV buses in a line."""
    nb = 1 + n_lv
    grid = {"name": f"grid{n_lv}", "buses": [
        {"name": f"b{i}", "vn_kv": 0.4} for i in range(nb)]}
    lines = {"lines": [
        {"name": f"L{i}", "from_bus": i, "to_bus": i + 1, "length_km": 0.05,
         "r_ohm_per_km": 0.2, "x_ohm_per_km": 0.08, "c_nf_per_km": 0.0,
         "max_i_ka": 0.2} for i in range(nb - 1)], "transformers": []}
    load = {"resolution_minutes": 1, "steps": steps,
            "loads": [{"name": "LD", "bus": nb - 1, "p_mw": [0.001] * steps}]}
    generation = {"steps": steps, "generation": []}
    substation = {"steps": steps, "substations": [
        {"name": "slack", "bus": 0, "vm_pu": [1.0] * steps}]}
    return input_data_from_dicts(grid, lines, load, generation, substation)


def test_engine_reconfigure_swaps_grid():
    async def scenario():
        store = StateStore(history_size=10)
        engine = RealtimeEngine(Simulator(_grid(1, 8)), store, interval_seconds=0.01)
        assert len(engine.sim.net.bus) == 2
        engine.step, engine.day = 5, 3  # pretend we ran a while

        await engine.reconfigure(_grid(3, 8), autostart=False)

        assert len(engine.sim.net.bus) == 4        # new topology in place
        assert engine.step == 0 and engine.day == 0  # clock reset
        assert engine.steps_per_day == 8
        assert store.latest is None                # history cleared
        assert not engine.status["running"]        # stayed paused (autostart=False)
        assert engine.sim.run_step(0).converged    # the new net actually solves

    asyncio.run(scenario())


def test_reconfigure_resumes_when_running():
    async def scenario():
        store = StateStore(history_size=10)
        engine = RealtimeEngine(Simulator(_grid(1, 8)), store, interval_seconds=0.01)
        engine.start_loop()
        await asyncio.sleep(0.05)                  # let it tick at least once
        await engine.reconfigure(_grid(2, 8))      # autostart inferred from running
        assert engine.status["running"]
        await engine.stop()

    asyncio.run(scenario())


@pytest.mark.skipif(not MANIFEST.exists(), reason="grid library not built")
def test_grid_library_catalog_lists_and_converts_scopes():
    cat = GridCatalog(ding0_dir=str(DING0_DIR), library_manifest=str(MANIFEST))
    assert cat.available
    listing = cat.list()
    assert len(listing) >= 1
    entry = listing[0]
    assert {"id", "name", "category", "voltage", "character", "nodes", "geo"} <= entry.keys()
    assert entry["geo"] is True  # ding0 grids carry real lat/lon

    mv = next((it for it in listing if it["voltage"] == "MV"), None)
    assert mv is not None, "library should contain at least one MV grid"
    g = cat.get_inputs(mv["id"], steps=96)
    p = preview(g)
    assert p["n_bus"] >= 1 and len(p["buses"]) == p["n_bus"]
    assert all(b["vn_kv"] > 1.0 for b in p["buses"])  # MV scope keeps only MV buses

    lv = next((it for it in listing if it["voltage"] == "LV"), None)
    if lv is not None:
        pl = preview(cat.get_inputs(lv["id"], steps=96))
        assert all(b["vn_kv"] <= 1.0 for b in pl["buses"])  # LV scope is a single 0.4 kV grid

    # once cached, the listing carries counts
    assert any("n_bus" in it for it in cat.list())


def test_absent_source_is_empty_not_an_error():
    cat = GridCatalog(ding0_dir=str(ROOT / "does-not-exist"))
    assert cat.available is False
    assert cat.list() == []
    assert cat.has("anything") is False
