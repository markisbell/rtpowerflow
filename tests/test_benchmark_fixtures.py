"""Benchmark fixtures (benchmarks/fixtures/): the frozen validation inputs.

Runs in the NORMAL dev suite (no OpenDSS/Octave needed): every fixture must
load through the real pipeline, build, solve, and still match the metadata
its MANIFEST recorded at freeze time — if a pipeline change alters the
physics of the frozen inputs, this trips BEFORE the public numbers drift.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from netzsim.data_loader import load_inputs
from netzsim.simulator import Simulator

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "benchmarks" / "fixtures"
MANIFEST = FIXTURES / "MANIFEST.json"

pytestmark = pytest.mark.skipif(not MANIFEST.exists(),
                                reason="benchmark fixtures not present")


def _entries():
    return json.loads(MANIFEST.read_text(encoding="utf-8"))["fixtures"]


@pytest.mark.parametrize("entry", _entries() if MANIFEST.exists() else [],
                         ids=lambda e: e["id"])
def test_fixture_loads_solves_and_matches_manifest(entry):
    sim = Simulator(load_inputs(FIXTURES / entry["id"]))
    assert len(sim.net.bus) == entry["n_bus"]
    assert len(sim.prof.load_idx) == entry["n_load"]
    assert len(sim.prof.sgen_idx) == entry["n_sgen"]
    assert sim.steps_per_day == entry["steps"]

    res = sim.run_step(720)                      # the manifest's noon anchor
    assert res.converged
    vm_min = min(b["vm_pu"] for b in res.buses)
    assert vm_min == pytest.approx(entry["noon_vm_pu_min"], abs=1e-6), \
        "the frozen fixture's noon physics changed — the committed " \
        "benchmark numbers no longer describe this code"
