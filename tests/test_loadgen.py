"""Tests for the LPG load library reader and the assignment logic."""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from netzsim.data_loader import input_data_from_dicts
from netzsim.loadgen import (
    AssignPolicy,
    EvPolicy,
    LoadLibrary,
    PvPolicy,
    assign_ev,
    assign_pv,
    assign_to_loads,
)
from netzsim.simulator import Simulator

ROOT = Path(__file__).resolve().parents[1]
REAL_LIB = ROOT / "data" / "lpg_library" / "index.json"


def _make_library(d: Path, steps: int = 24) -> LoadLibrary:
    """A tiny 2-archetype library with flat, distinguishable variants."""
    d.mkdir(parents=True, exist_ok=True)
    archs = [
        ("A1", [[0.3] * steps, [0.5] * steps]),
        ("A2", [[1.0] * steps, [2.0] * steps]),
    ]
    entries = []
    for aid, variants in archs:
        doc = {
            "id": aid, "name": f"{aid}_Test", "label": f"Test {aid}",
            "source": "synthetic", "load_type": "Electricity",
            "resolution_minutes": 1440 // steps, "steps": steps,
            "annual_kwh": 1000.0, "mean_kw": variants[0][0],
            "peak_kw": max(v[0] for v in variants), "n_variants": len(variants),
            "variant_day_of_year": list(range(1, len(variants) + 1)),
            "variants_kw": variants,
        }
        (d / f"{aid}.json").write_text(json.dumps(doc), encoding="utf-8")
        entries.append({k: doc[k] for k in
                        ("id", "name", "label", "annual_kwh", "mean_kw",
                         "peak_kw", "n_variants")} | {"file": f"{aid}.json"})
    (d / "index.json").write_text(json.dumps(
        {"source": "synthetic", "steps": steps, "resolution_minutes": 1440 // steps,
         "archetypes": entries}), encoding="utf-8")
    return LoadLibrary(d)


def test_library_reads_metadata_and_variants(tmp_path):
    lib = _make_library(tmp_path)
    assert lib.available and lib.steps == 24
    assert lib.ids() == ["A1", "A2"]
    assert {a["id"] for a in lib.list()} == {"A1", "A2"}
    a2 = lib.get("A2")
    assert a2.n_variants == 2 and a2.variants_kw[1][0] == 2.0


def test_assign_round_robin_diversity_and_scaling(tmp_path):
    lib = _make_library(tmp_path)
    loads = [{"name": f"L{i}", "bus": i + 1} for i in range(4)]
    doc = assign_to_loads(loads, lib, AssignPolicy(scale=2.0, power_factor=0.95),
                          steps=24)
    assert len(doc["loads"]) == 4
    assert all(len(ld["p_mw"]) == 24 for ld in doc["loads"])

    # interleaved pool -> consecutive loads get different archetypes
    used = [a["archetype"] for a in doc["assignments"]]
    assert used[0] != used[1]
    assert {*used} == {"A1", "A2"}

    # scaling: A1 v0 = 0.3 kW * scale 2 / 1000 = 0.0006 MW
    first = doc["loads"][0]
    assert first["p_mw"][0] == pytest.approx(0.0006)
    tan_phi = math.tan(math.acos(0.95))
    assert first["q_mvar"][0] == pytest.approx(0.0006 * tan_phi, abs=1e-6)


def test_assign_is_deterministic_under_seed(tmp_path):
    lib = _make_library(tmp_path)
    loads = [{"name": f"L{i}", "bus": i + 1} for i in range(6)]
    p = AssignPolicy(mode="random", seed=42)
    a = assign_to_loads(loads, lib, p, steps=24)
    b = assign_to_loads(loads, lib, p, steps=24)
    assert [x["archetype"] for x in a["assignments"]] == \
           [x["archetype"] for x in b["assignments"]]


def test_assign_resamples_to_target_steps(tmp_path):
    lib = _make_library(tmp_path, steps=24)
    loads = [{"name": "L0", "bus": 1}]
    doc = assign_to_loads(loads, lib, AssignPolicy(), steps=48)  # 24 -> 48
    assert doc["steps"] == 48
    assert len(doc["loads"][0]["p_mw"]) == 48


def test_assigned_loads_build_and_solve(tmp_path):
    lib = _make_library(tmp_path, steps=24)
    grid = {"name": "g", "buses": [{"name": "b0", "vn_kv": 0.4},
                                   {"name": "b1", "vn_kv": 0.4}]}
    lines = {"lines": [{"name": "L", "from_bus": 0, "to_bus": 1, "length_km": 0.05,
                        "r_ohm_per_km": 0.2, "x_ohm_per_km": 0.08,
                        "c_nf_per_km": 0.0, "max_i_ka": 0.2}], "transformers": []}
    assigned = assign_to_loads([{"name": "LD", "bus": 1}], lib,
                               AssignPolicy(), steps=24)
    load_doc = {k: assigned[k] for k in ("resolution_minutes", "steps", "loads")}
    substation = {"steps": 24, "substations": [
        {"name": "slack", "bus": 0, "vm_pu": [1.0] * 24}]}
    data = input_data_from_dicts(grid, lines, load_doc,
                                 {"steps": 24, "generation": []}, substation)
    res = Simulator(data).run_step(12)
    assert res.converged


def test_ev_charging_is_additive_and_evening_peaked():
    loads = [{"name": f"L{i}", "bus": i + 1} for i in range(40)]
    doc = assign_ev(loads, EvPolicy(penetration=1.0, charger_kw=11.0, daily_kwh=8.0,
                                    seed=1), steps=24)
    assert len(doc["loads"]) == 40                      # every home at 100%
    p = doc["loads"][0]["p_mw"]
    assert max(p) > 0 and max(p) <= 11.0 / 1000 + 1e-9  # never exceeds the wallbox
    # aggregate charging peaks in the evening (plug-in ~18:00)
    import numpy as np
    agg = np.array([ld["p_mw"] for ld in doc["loads"]]).sum(axis=0)
    assert 16 <= int(agg.argmax()) <= 23

    assert assign_ev(loads, EvPolicy(penetration=0.0), steps=24)["loads"] == []
    half = assign_ev(loads, EvPolicy(penetration=0.5, seed=2), steps=24)
    assert 10 <= len(half["loads"]) <= 30               # ~half, deterministic by seed


def test_ev_charger_power_scales():
    loads = [{"name": "L0", "bus": 1}]
    big = assign_ev(loads, EvPolicy(penetration=1.0, charger_kw=22.0, seed=0), steps=24)
    assert max(big["loads"][0]["p_mw"]) <= 22.0 / 1000 + 1e-9


def test_ev_charger_mix_draws_standard_ratings():
    """Mix mode: every wallbox is one of 3.7/11/22 kW, deterministic by seed,
    and the fixed-rating path stays bit-identical to a non-mix run."""
    loads = [{"name": f"L{i}", "bus": i + 1} for i in range(30)]
    a = assign_ev(loads, EvPolicy(penetration=1.0, charger_mix=True, seed=3), steps=1440)
    ratings = {round(max(ld["p_mw"]) * 1000, 1) for ld in a["loads"]}
    assert ratings <= {3.7, 11.0, 22.0}
    assert len(ratings) > 1                       # actually mixed
    b = assign_ev(loads, EvPolicy(penetration=1.0, charger_mix=True, seed=3), steps=1440)
    assert a["loads"] == b["loads"]               # deterministic under the seed
    fixed = assign_ev(loads, EvPolicy(penetration=1.0, charger_kw=11.0, seed=3), steps=1440)
    assert all(round(max(ld["p_mw"]) * 1000, 1) == 11.0 for ld in fixed["loads"])
    # the mix only changes ratings — the same homes get an EV
    assert [ld["bus"] for ld in a["loads"]] == [ld["bus"] for ld in fixed["loads"]]


def test_pv_assignment_generates_solar():
    loads = [{"name": f"L{i}", "bus": i + 1} for i in range(8)]
    doc = assign_pv(loads, PvPolicy(penetration=1.0, kwp=5.0, seed=1), steps=24)
    assert len(doc["generation"]) == 8        # every bus gets PV at 100%
    p = doc["generation"][0]["p_mw"]
    assert p[0] == 0.0 and p[23] == 0.0       # dark at night
    assert max(p) > 0 and 10 <= p.index(max(p)) <= 13  # peak near solar noon
    assert assign_pv(loads, PvPolicy(penetration=0.0), steps=24)["generation"] == []


def test_pv_mix_varies_size_and_orientation():
    """PV mix: sizes spread around the chosen kWp, midday peaks shift per
    system (east/west roofs), the same buses get PV, deterministic by seed."""
    import numpy as np
    loads = [{"name": f"L{i}", "bus": i + 1} for i in range(20)]
    eq = assign_pv(loads, PvPolicy(penetration=0.6, kwp=10.0, seed=5), steps=1440)
    mx = assign_pv(loads, PvPolicy(penetration=0.6, kwp=10.0, mix=True, seed=5), steps=1440)
    # same selection, only the systems differ
    assert [g["bus"] for g in mx["generation"]] == [g["bus"] for g in eq["generation"]]
    sizes = [max(g["p_mw"]) * 1000 for g in mx["generation"]]
    assert all(4.0 <= s <= 16.0 for s in sizes)          # 0.5–1.5x 10 kWp (+rounding)
    assert max(sizes) - min(sizes) > 2.0                  # actually spread
    peaks = [int(np.argmax(g["p_mw"])) for g in mx["generation"]]
    assert max(peaks) - min(peaks) > 30                   # orientations shift the peak
    assert all(max(g["p_mw"]) * 1000 <= 10.0 for g in eq["generation"])  # equal mode intact
    mx2 = assign_pv(loads, PvPolicy(penetration=0.6, kwp=10.0, mix=True, seed=5), steps=1440)
    assert mx["generation"] == mx2["generation"]          # deterministic


def test_pv_offsets_load_in_a_solved_net(tmp_path):
    lib = _make_library(tmp_path, steps=24)
    grid = {"name": "g", "buses": [{"name": "b0", "vn_kv": 0.4},
                                   {"name": "b1", "vn_kv": 0.4}]}
    lines = {"lines": [{"name": "L", "from_bus": 0, "to_bus": 1, "length_km": 0.05,
                        "r_ohm_per_km": 0.2, "x_ohm_per_km": 0.08,
                        "c_nf_per_km": 0.0, "max_i_ka": 0.2}], "transformers": []}
    loads = [{"name": "LD", "bus": 1}]
    load_doc = {k: assign_to_loads(loads, lib, AssignPolicy(), steps=24)[k]
                for k in ("resolution_minutes", "steps", "loads")}
    gen = assign_pv(loads, PvPolicy(penetration=1.0, kwp=20.0, seed=0), steps=24)
    substation = {"steps": 24, "substations": [
        {"name": "slack", "bus": 0, "vm_pu": [1.0] * 24}]}
    data = input_data_from_dicts(grid, lines, load_doc, gen, substation)
    sim = Simulator(data)
    res = sim.run_step(12)  # solar noon
    assert res.converged and res.summary["total_gen_mw"] > 0  # PV is generating


def test_mfh_sums_multiple_household_profiles(tmp_path):
    """A multi-family building is the sum of n household profiles."""
    lib = _make_library(tmp_path)          # pool order: (A1,0),(A2,0),(A1,1),(A2,1)
    doc = assign_to_loads([{"name": "MFH", "bus": 1}], lib,
                          AssignPolicy(households_range=(2, 2)), steps=24)
    ld = doc["loads"][0]
    assert ld["households"] == 2
    # round_robin, no jitter: A1 v0 (0.3 kW) + A2 v0 (1.0 kW) summed
    assert ld["p_mw"][0] == pytest.approx((0.3 + 1.0) / 1000.0)
    assert doc["assignments"][0]["households"] == 2


def test_mfh_range_is_deterministic_and_bounded(tmp_path):
    lib = _make_library(tmp_path)
    loads = [{"name": f"B{i}", "bus": i + 1} for i in range(10)]
    p = AssignPolicy(households_range=(3, 6), seed=7)
    a = assign_to_loads(loads, lib, p, steps=24)
    b = assign_to_loads(loads, lib, p, steps=24)
    ns = [x["households"] for x in a["assignments"]]
    assert all(3 <= n <= 6 for n in ns) and len(set(ns)) > 1
    assert ns == [x["households"] for x in b["assignments"]]


def test_single_family_default_unchanged(tmp_path):
    """No households_range -> exactly the old single-profile behavior."""
    lib = _make_library(tmp_path)
    loads = [{"name": f"L{i}", "bus": i + 1} for i in range(4)]
    doc = assign_to_loads(loads, lib, AssignPolicy(), steps=24)
    assert all(ld["households"] == 1 for ld in doc["loads"])
    assert doc["loads"][0]["p_mw"][0] == pytest.approx(0.0003)   # A1 v0 alone


def test_empty_library_raises(tmp_path):
    lib = LoadLibrary(tmp_path)  # no index.json
    assert lib.available is False
    with pytest.raises(ValueError):
        assign_to_loads([{"name": "L", "bus": 0}], lib, AssignPolicy(), steps=24)


@pytest.mark.skipif(not REAL_LIB.exists(), reason="LPG library not built yet")
def test_real_library_loads_and_assigns():
    lib = LoadLibrary(REAL_LIB.parent)
    assert lib.available and len(lib.list()) >= 1
    aid = lib.ids()[0]
    arch = lib.get(aid)
    assert len(arch.variants_kw[0]) == lib.steps == 1440
    doc = assign_to_loads([{"name": "L", "bus": 0}], lib, AssignPolicy(), steps=1440)
    assert len(doc["loads"][0]["p_mw"]) == 1440
