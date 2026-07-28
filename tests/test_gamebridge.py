"""Puppet mode (gamebridge): the external clock must be a drop-in replacement
for the internal accelerated tick.

The core guarantee (simgames ROADMAP Phase 0, Spike C): N externally clocked
steps produce the IDENTICAL result sequence as N internally clocked steps on
the same inputs — the game can own time without changing the physics.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from netzsim.config import settings

# The engine must not tick on its own during these tests.
settings.autostart = False

from netzsim.api import app  # noqa: E402  (needs the settings override first)

ROOT = Path(__file__).resolve().parents[1]

N_STEPS = 100

# Non-deterministic per-run fields (wall-clock) excluded from the comparison;
# everything physical must match exactly.
_COMPARE_KEYS = ("step", "day", "time_of_day", "converged", "buses", "lines",
                 "trafos", "ext_grids", "summary")


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory):
    sys.path.insert(0, str(ROOT / "scripts"))
    import generate_sample_data as g  # type: ignore

    out = tmp_path_factory.mktemp("data")
    g.DATA = out
    g.main()
    return out


def _normalize(results: list[dict]) -> list[dict]:
    return [{k: r[k] for k in _COMPARE_KEYS} for r in results]


def _run_internal_clock(data_dir: Path, n: int) -> list[dict]:
    """Drive the REAL internal loop (start_loop + accelerated tick)."""
    from netzsim.data_loader import load_inputs
    from netzsim.engine import RealtimeEngine
    from netzsim.simulator import Simulator
    from netzsim.state import StateStore

    async def go() -> list[dict]:
        store = StateStore(history_size=n + 10)
        engine = RealtimeEngine(
            Simulator(load_inputs(data_dir)), store, interval_seconds=0.01
        )
        engine.start_loop()
        while len(store.history()) < n:
            await asyncio.sleep(0.01)
        await engine.stop()
        return store.history(limit=None)[:n]

    return asyncio.run(go())


def _run_external_clock(data_dir: Path, n: int) -> list[dict]:
    """Drive the SAME engine via external_step only (puppet mode)."""
    from netzsim.data_loader import load_inputs
    from netzsim.engine import RealtimeEngine
    from netzsim.simulator import Simulator
    from netzsim.state import StateStore

    async def go() -> list[dict]:
        store = StateStore(history_size=n + 10)
        engine = RealtimeEngine(
            Simulator(load_inputs(data_dir)), store, interval_seconds=0.01
        )
        for _ in range(n):
            await engine.external_step()
        return store.history(limit=None)[:n]

    return asyncio.run(go())


def test_external_clock_equivalence(data_dir):
    internal = _normalize(_run_internal_clock(data_dir, N_STEPS))
    external = _normalize(_run_external_clock(data_dir, N_STEPS))
    assert len(internal) == len(external) == N_STEPS
    for i, (a, b) in enumerate(zip(internal, external)):
        assert a == b, f"step {i}: internal and external results differ"


def test_external_step_refused_while_internal_clock_runs(data_dir):
    from netzsim.data_loader import load_inputs
    from netzsim.engine import RealtimeEngine
    from netzsim.simulator import Simulator
    from netzsim.state import StateStore

    async def go() -> None:
        engine = RealtimeEngine(
            Simulator(load_inputs(data_dir)), StateStore(), interval_seconds=0.01
        )
        engine.start_loop()
        with pytest.raises(RuntimeError):
            await engine.external_step()
        engine.pause()  # paused internal clock => external stepping is allowed
        await engine.external_step()
        await engine.stop()

    asyncio.run(go())


def test_gb_endpoints(monkeypatch):
    """API smoke: version handshake + /gb/step advances exactly one step and
    returns the same projection as /state."""
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        version = client.get("/gb/version").json()
        assert version["backend"] == "netzsim"
        assert "pandapower" in version["solver"]

        step0 = client.get("/status").json()["step"]
        result = client.post("/gb/step")
        assert result.status_code == 200
        body = result.json()
        assert body["converged"] is True
        assert body["step"] == step0
        assert client.get("/status").json()["step"] == step0 + 1
        assert client.get("/state").json() == body
