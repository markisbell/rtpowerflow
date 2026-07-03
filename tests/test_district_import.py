"""Interconnected districts: the MV ring + spliced street-routed LV subgrids.

An MV manifest entry subsumes the OSM LV grids of its district: the composed
network connects each spliced LV grid through its real ding0 station transformer,
keeps the remaining LV grids lumped, and must solve as one 20 kV + 0.4 kV net.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from netzsim.data_loader import input_data_from_dicts
from netzsim.grid_catalog import GridCatalog
from netzsim.simulator import Simulator

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "data" / "grid_library.json"
DING0_DIR = ROOT / "data" / "ding0_grids"

pytestmark = pytest.mark.skipif(not MANIFEST.exists(), reason="no committed dataset")

DISTRICT = "mv_rural_3150"        # 3 OSM LV subgrids: 300266, 300668, 300575


@pytest.fixture(scope="module")
def catalog() -> GridCatalog:
    return GridCatalog(ding0_dir=DING0_DIR, library_manifest=MANIFEST)


@pytest.fixture(scope="module")
def district(catalog):
    return catalog.get_inputs(DISTRICT, steps=96)


def test_mv_entries_subsume_their_osm_lv_grids(catalog):
    import json
    raw = {g["id"]: g for g in json.loads(MANIFEST.read_text())["grids"]}
    mv = [e for e in catalog._entries.values() if e.voltage == "MV"]
    assert mv, "manifest has no MV districts"
    for e in mv:
        assert e.lv_subgrids, f"{e.id}: no LV subgrids paired"
        # the listed node count covers MV + spliced LV nodes
        assert (e.nodes or 0) > raw[e.id]["nodes"]


def test_composed_district_structure(district):
    buses = district.grid_structure["buses"]
    names = {b["name"] for b in buses}
    # spliced LV grids are present, name-prefixed, with the busbar bus
    for lvid in ("300266", "300668", "300575"):
        assert f"lv{lvid}:LV_station" in names, f"LV grid {lvid} not spliced"
    # every bus is geo-referenced (MV ring and LV street grids alike)
    assert all(b.get("geo") for b in buses)
    # both voltage levels exist
    vns = {b["vn_kv"] for b in buses}
    assert 0.4 in vns and max(vns) > 1.0

    # each spliced grid hangs off a real station transformer; indices are valid
    trafos = district.lines["transformers"]
    assert len(trafos) >= 3
    busbar_ids = {t["lv_bus"] for t in trafos}
    for lvid in ("300266", "300668", "300575"):
        i = next(i for i, b in enumerate(buses) if b["name"] == f"lv{lvid}:LV_station")
        assert i in busbar_ids, f"busbar of {lvid} not connected by a trafo"
    for t in trafos:
        assert t["sn_mva"] > 0 and t["hv_bus"] < len(buses) and t["lv_bus"] < len(buses)
    for ln in district.lines["lines"]:
        assert ln["from_bus"] < len(buses) and ln["to_bus"] < len(buses)


def test_spliced_loads_are_households_lumped_stay_fixed(district):
    loads = district.load["loads"]
    households = [ld for ld in loads if ld.get("household", True)]
    fixed = [ld for ld in loads if not ld.get("household", True)]
    assert households and fixed
    # spliced building loads are households; no lumped load remains for them
    assert all(ld["name"].startswith("lv3") for ld in households)
    fixed_names = {ld["name"] for ld in fixed}
    for lvid in ("300266", "300668", "300575"):
        assert f"lv_{lvid}" not in fixed_names, f"{lvid} both spliced and lumped"
    # the district still has lumped loads for the non-routed LV grids
    assert any(n.startswith("lv_") for n in fixed_names)


def test_district_solves_within_limits(district):
    data = input_data_from_dicts(district.grid_structure, district.lines,
                                 district.load, district.generation,
                                 district.substation)
    res = Simulator(data).run_step(76)            # ~19:00, the evening peak
    assert res.converged, "district power flow did not converge"
    s = res.summary
    assert s["vm_pu_min"] >= 0.90, f"vmin={s['vm_pu_min']:.3f}"
    assert s["vm_pu_max"] <= 1.10, f"vmax={s['vm_pu_max']:.3f}"
    assert s["max_trafo_loading_percent"] is not None
    assert s["n_bus"] == len(district.grid_structure["buses"])


def test_district_2279_lpg_step2_converges(catalog):
    """Regression: district 2279 + LPG loads stalled plain Newton-Raphson on a
    healthy operating point (zero-impedance ding0 busbar links + dc-based init
    oscillate at certain 1-min load patterns; hundreds of steps/day failed).
    run_step's retry ladder (warm → flat → Iwamoto) must converge it."""
    lpg_dir = ROOT / "data" / "lpg_library"
    if not (lpg_dir / "index.json").exists():
        pytest.skip("no LPG library in the dataset")
    from netzsim.loadgen import AssignPolicy, LoadLibrary, assign_to_loads

    g = catalog.get_inputs("mv_suburban_2279", steps=1440)
    lib = LoadLibrary(lpg_dir)
    hh = [ld for ld in g.load["loads"] if ld.get("household", True)]
    fx = [ld for ld in g.load["loads"] if not ld.get("household", True)]
    doc = assign_to_loads(hh, lib, AssignPolicy(seed=0), steps=1440)
    loads = doc["loads"] + [{k: ld[k] for k in ("name", "bus", "p_mw", "q_mvar") if k in ld} for ld in fx]
    data = input_data_from_dicts(
        g.grid_structure, g.lines,
        {"resolution_minutes": 1, "steps": 1440, "loads": loads},
        g.generation, g.substation)

    sim = Simulator(data)
    for step in (2, 3, 4, 11, 14):                # previously non-converging
        res = sim.run_step(step)
        assert res.converged, f"step {step} did not converge"
        assert res.summary["vm_pu_min"] >= 0.90
