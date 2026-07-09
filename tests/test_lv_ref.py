"""Phase 5 of the vertical integration: gridedit ``lv_ref`` station references.

A drawn MV station may reference a drawn gridformat LV export (same
directory): the importer then SPLICES the real street grid through its own
station transformer (snapped to the MV grid's voltage) instead of the lumped
load — the full self-drawn vertical grid from 110 kV down to the house
connection. A missing reference keeps the station lumped and leaves a note.
"""
from __future__ import annotations

import json

import pytest

from netzsim.data_loader import input_data_from_dicts
from netzsim.grid_catalog import GridCatalog
from netzsim.gridedit_mv_import import convert_gridedit_mv
from netzsim.simulator import Simulator

# minimal drawn MV net: UW -> junction -> two stations (s1 carries the lv_ref)
MV_DOC = {
    "format": "gridedit-mv",
    "name": "Vertikal Testnetz",
    "mv_kv": 20,
    "slack": 0,
    "buses": [
        {"name": "u1", "kind": "uw", "geo": [9.93, 54.66], "sn_mva": 40},
        {"name": "n1", "kind": "junction", "geo": [9.95, 54.67]},
        {"name": "s1", "kind": "station", "geo": [9.97, 54.68],
         "sn_kva": 400, "p_kw": 132, "lv_ref": "dorf"},
        {"name": "s2", "kind": "station", "geo": [9.98, 54.66],
         "sn_kva": 250, "p_kw": 90},
    ],
    "lines": [
        {"from": 0, "to": 1, "length_km": 1.6, "r_ohm_per_km": 0.206,
         "x_ohm_per_km": 0.116, "c_nf_per_km": 250, "max_i_ka": 0.319,
         "type": "NA2XS2Y 1x150 RM/25 12/20 kV", "kind": "cable"},
        {"from": 1, "to": 2, "length_km": 1.9, "r_ohm_per_km": 0.206,
         "x_ohm_per_km": 0.116, "c_nf_per_km": 250, "max_i_ka": 0.319,
         "type": "NA2XS2Y 1x150 RM/25 12/20 kV", "kind": "cable"},
        {"from": 1, "to": 3, "length_km": 1.2, "r_ohm_per_km": 0.206,
         "x_ohm_per_km": 0.116, "c_nf_per_km": 250, "max_i_ka": 0.319,
         "type": "NA2XS2Y 1x150 RM/25 12/20 kV", "kind": "cable"},
    ],
}

# minimal drawn gridformat LV grid: busbar -> two houses
LV_DOC = {
    "format": "gridformat",
    "name": "Dorf",
    "slack_bus": 0,
    "trafo": {"sn_kva": 250, "hv_kv": 20},
    "buses": [
        {"name": "LV_station", "vn_kv": 0.4, "geo": [9.970, 54.680]},
        {"name": "H1", "vn_kv": 0.4, "geo": [9.971, 54.681]},
        {"name": "H2", "vn_kv": 0.4, "geo": [9.972, 54.679]},
    ],
    "lines": [
        {"from": 0, "to": 1, "length_km": 0.08, "r_ohm_per_km": 0.32,
         "x_ohm_per_km": 0.08, "max_i_ka": 0.27,
         "geometry": [[9.970, 54.680], [9.971, 54.681]]},
        {"from": 0, "to": 2, "length_km": 0.09, "r_ohm_per_km": 0.32,
         "x_ohm_per_km": 0.08, "max_i_ka": 0.27,
         "geometry": [[9.970, 54.680], [9.972, 54.679]]},
    ],
    "loads": [{"bus": 1, "peak_mw": 0.02}, {"bus": 2, "peak_mw": 0.015}],
}


def _write(tmp_path, mv=MV_DOC, with_lv=True):
    d = tmp_path / "user_grids"
    d.mkdir(exist_ok=True)
    (d / "ms-netz.json").write_text(json.dumps(mv), encoding="utf-8")
    if with_lv:
        (d / "dorf.json").write_text(json.dumps(LV_DOC), encoding="utf-8")
    return d


def test_lv_ref_splices_the_drawn_grid(tmp_path):
    d = _write(tmp_path)
    g = convert_gridedit_mv(d / "ms-netz.json", steps=96)
    buses = g.grid_structure["buses"]
    # 4 MV buses + HV bus + 3 spliced LV buses (the synthetic MS-Netz is dropped)
    assert len(buses) == 4 + 1 + 3
    assert [b["vn_kv"] for b in buses[5:]] == [0.4, 0.4, 0.4]
    assert buses[5]["name"] == "dorf:LV_station"
    # station s1 has NO lumped load anymore; s2 keeps its lump
    names = [ld["name"] for ld in g.load["loads"]]
    assert "Station_1" not in names and "Station_2" in names or \
        sum(n.startswith("Station") for n in names) == 1
    # the spliced building loads are LPG households, prefixed and re-based
    hh = [ld for ld in g.load["loads"] if ld.get("household", True)]
    assert len(hh) == 2 and all(ld["name"].startswith("dorf:") for ld in hh)
    assert {ld["bus"] for ld in hh} == {6, 7}
    # the station transformer connects s1 (bus 2) to the spliced busbar (bus 5)
    st = next(t for t in g.lines["transformers"] if t["name"].startswith("dorf:"))
    assert st["hv_bus"] == 2 and st["lv_bus"] == 5
    assert st["std_type"].endswith("20/0.4 kV")
    assert any("spliced drawn LV grid 'dorf'" in n for n in g.notes)


def test_lv_ref_cells(tmp_path):
    d = _write(tmp_path)
    g = convert_gridedit_mv(d / "ms-netz.json", steps=96)
    by_id = {c["id"]: c for c in g.cells}
    dorf = by_id["dorf"]
    assert not dorf["lumped"]
    assert dorf["buses"] == [5, 6, 7] and dorf["lv_busbar"] == 5
    assert dorf["mv_bus"] == 2 and len(dorf["station_trafos"]) == 1
    # the unreferenced station stays a degenerate lumped cell
    s2 = next(c for c in g.cells if c["lumped"])
    assert s2["mv_bus"] == 3 and s2["buses"] == []


def test_lv_ref_missing_stays_lumped(tmp_path):
    mv = json.loads(json.dumps(MV_DOC))
    mv["buses"][2]["lv_ref"] = "gibtsnicht"
    d = _write(tmp_path, mv, with_lv=False)
    g = convert_gridedit_mv(d / "ms-netz.json", steps=96)
    assert len(g.grid_structure["buses"]) == 5          # nothing spliced
    assert sum(ld["name"].startswith("Station") for ld in g.load["loads"]) == 2
    assert any("lv_ref 'gibtsnicht' not found" in n for n in g.notes)
    assert all(c["lumped"] for c in g.cells)


def test_lv_ref_trafo_snaps_to_mv_voltage(tmp_path):
    """The LV file was drawn for 20 kV — referenced from a 10-kV net, its
    station transformer must re-snap to a 10/0.4 standard unit."""
    mv = json.loads(json.dumps(MV_DOC))
    mv["mv_kv"] = 10
    d = _write(tmp_path, mv)
    g = convert_gridedit_mv(d / "ms-netz.json", steps=96)
    st = next(t for t in g.lines["transformers"] if t["name"].startswith("dorf:"))
    assert st["std_type"].endswith("10/0.4 kV")


def test_lv_ref_solves_and_reaches_the_vertical_runtime(tmp_path):
    """The composed self-drawn vertical grid must solve, carry its cells into
    the Simulator and accept the vertical actors (Steuerbox at the drawn cell)."""
    d = _write(tmp_path)
    g = convert_gridedit_mv(d / "ms-netz.json", steps=96)
    data = input_data_from_dicts(g.grid_structure, g.lines, g.load,
                                 g.generation, g.substation, cells=g.cells)
    sim = Simulator(data)
    assert sim.cell_of_bus[5] == "dorf"
    c = sim.add_controller("cell", cell="dorf")
    assert c.cell == "dorf"
    r = sim.add_ront(next(t for t in sim.net.trafo.index
                          if str(sim.net.trafo.at[t, "name"]).startswith("dorf")))
    assert r.busbar == 5
    for step in (12, 48, 84):
        assert sim.run_step(step).converged


def test_lv_ref_through_the_catalog(tmp_path):
    """The catalog resolves the reference relative to the MV file — the way
    gridedit exports land in user_grids/."""
    d = _write(tmp_path)
    cat = GridCatalog(user_dir=d)
    g = cat.get_inputs("user_ms-netz", steps=96)
    assert any(b["name"] == "dorf:LV_station" for b in g.grid_structure["buses"])
    assert any(not c["lumped"] for c in g.cells)
