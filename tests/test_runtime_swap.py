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
ARCHIVE = ROOT / "European Archetpye Distribution Grid Models.zip"


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


@pytest.mark.skipif(not ARCHIVE.exists(), reason="archive zip not present")
def test_grid_catalog_lists_previews_and_converts():
    cat = GridCatalog(str(ARCHIVE), "Low Voltage Network Models/03_LV")
    assert cat.available
    listing = cat.list()
    assert len(listing) >= 20
    entry = listing[0]
    assert {"id", "name", "category", "thumbnail"} <= entry.keys()

    g = cat.get_inputs(entry["id"], steps=96)
    p = preview(g)
    assert p["n_bus"] >= 1
    assert len(p["buses"]) == p["n_bus"]
    assert "trafos" in p and "notes" in p

    # once cached, the listing carries counts
    assert any("n_bus" in it for it in cat.list())

    thumb = cat.thumbnail_bytes(entry["id"])
    assert thumb is None or thumb[:4] == b"\x89PNG"


def test_absent_archive_is_empty_not_an_error():
    cat = GridCatalog(str(ROOT / "does-not-exist.zip"))
    assert cat.available is False
    assert cat.list() == []
    assert cat.has("anything") is False
