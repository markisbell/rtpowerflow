"""Reference scenario 4 ("Feierabend im Bezirk") — recipe sanity.

The heavy closed-loop physics is covered by tests/test_controller_vertical.py;
this pins the committed artifacts: the scenario file's structure and that the
trimmed picker manifest carries the district it needs.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCEN = ROOT / "data" / "scenarios" / "4-feierabend-im-bezirk-engpass-unsichtbar-f-r-jede-zelle.json"

pytestmark = pytest.mark.skipif(not SCEN.exists(), reason="scenario 4 not committed")


def test_scenario4_recipe_structure():
    doc = json.loads(SCEN.read_text(encoding="utf-8"))
    assert doc["grid_id"] == "mv_rural_3150"
    assert doc["loadgen"]["ev_penetration"] == 0.0   # the wave is MV-level der_ops
    ops = doc["der_ops"]
    assert len(ops) == 42 and all(o["op"] == "add_ev" and o["kw"] == 200.0 for o in ops)
    assert all(1020 <= o["start_min"] <= 1080 and o["dur_min"] == 240 for o in ops)
    ctrls = doc["controllers"]
    assert len(ctrls) == 42 and all(c["scope"] == "cell" for c in ctrls)
    # one Steuerbox per wave station: 42 distinct lumped cells
    cells = {c["cell"] for c in ctrls}
    assert len(cells) == 42 and all(str(c).startswith("lv_") for c in cells)
    m = doc["measurements"]
    assert len(m["node_buses"]) == 154 and len(m["trafo_idxs"]) == 3
    assert doc["engine"] == {"day": 0, "step": 1060, "interval_seconds": 0.2}


def test_trimmed_manifest_carries_the_district():
    man = json.loads((ROOT / "data" / "grid_library.json").read_text(encoding="utf-8"))
    ids = {e["id"] for e in man["grids"]}
    assert "mv_rural_3150" in ids
    # its street-routed LV grids must be present for the splice pairing
    assert {"lv_rural_3150_300266", "lv_rural_3150_300668",
            "lv_rural_3150_300575"} <= ids
