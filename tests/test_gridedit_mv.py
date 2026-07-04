"""gridedit MV-layer exports (format "gridedit-mv"): catalog detection, the
110-kV-feed conversion, per-type profiles, and loadgen exclusion."""
from __future__ import annotations

import json

from netzsim.data_loader import input_data_from_dicts
from netzsim.grid_catalog import GridCatalog
from netzsim.gridedit_mv_import import convert_gridedit_mv
from netzsim.simulator import Simulator

# A small MS drawing around Kappeln: UW -> junction -> {station, HPC park,
# wind farm}. Values match what the editor's toMvDocs emits.
MV_DOC = {
    "format": "gridedit-mv",
    "name": "Testnetz Kappeln MS",
    "mv_kv": 20,
    "slack": 0,
    "buses": [
        {"name": "u1", "kind": "uw", "geo": [9.93, 54.66], "sn_mva": 40},
        {"name": "n1", "kind": "junction", "geo": [9.95, 54.67]},
        {"name": "s1", "kind": "station", "geo": [9.97, 54.68],
         "sn_kva": 400, "p_kw": 132},
        {"name": "v1", "kind": "consumer", "geo": [9.96, 54.65],
         "subtype": "charge", "p_kw": 300},
        {"name": "g1", "kind": "gen", "geo": [9.99, 54.69],
         "subtype": "wind", "gen_kw": 3000},
    ],
    "lines": [
        {"from": 0, "to": 1, "length_km": 1.6, "r_ohm_per_km": 0.206,
         "x_ohm_per_km": 0.116, "c_nf_per_km": 250, "max_i_ka": 0.319,
         "type": "NA2XS2Y 1x150 RM/25 12/20 kV", "kind": "cable",
         "geometry": [[9.93, 54.66], [9.95, 54.67]]},
        {"from": 1, "to": 2, "length_km": 1.9, "r_ohm_per_km": 0.206,
         "x_ohm_per_km": 0.116, "c_nf_per_km": 250, "max_i_ka": 0.319,
         "type": "NA2XS2Y 1x150 RM/25 12/20 kV", "kind": "cable",
         "geometry": [[9.95, 54.67], [9.97, 54.68]]},
        {"from": 1, "to": 3, "length_km": 2.2, "r_ohm_per_km": 0.4132,
         "x_ohm_per_km": 0.339, "c_nf_per_km": 9.7, "max_i_ka": 0.29,
         "type": "70-AL1/11-ST1A 20.0", "kind": "overhead",
         "geometry": [[9.95, 54.67], [9.96, 54.65]]},
        {"from": 1, "to": 4, "length_km": 3.1, "r_ohm_per_km": 0.4132,
         "x_ohm_per_km": 0.339, "c_nf_per_km": 9.7, "max_i_ka": 0.29,
         "type": "70-AL1/11-ST1A 20.0", "kind": "overhead",
         "geometry": [[9.95, 54.67], [9.99, 54.69]]},
    ],
}


def _write(tmp_path, doc=MV_DOC, fname="ms-netz"):
    d = tmp_path / "user_grids"
    d.mkdir(exist_ok=True)
    (d / f"{fname}.json").write_text(json.dumps(doc), encoding="utf-8")
    return d


def test_catalog_detects_mv_and_converts(tmp_path):
    d = _write(tmp_path)
    cat = GridCatalog(user_dir=d)
    entry = next(i for i in cat.list() if i["id"] == "user_ms-netz")
    assert entry["voltage"] == "MV" and entry["character"] == "user"
    assert entry["geo"] is True and entry["nodes"] == 5

    g = cat.get_inputs("user_ms-netz", steps=96)
    data = input_data_from_dicts(g.grid_structure, g.lines, g.load,
                                 g.generation, g.substation)
    for step in (12, 48, 84):                       # night, midday, evening
        assert Simulator(data).run_step(step).converged


def test_hv_feed_and_bus_indices(tmp_path):
    d = _write(tmp_path)
    g = convert_gridedit_mv(d / "ms-netz.json", steps=96)
    # file bus indices unchanged; the 110-kV bus is appended last
    assert [b["vn_kv"] for b in g.grid_structure["buses"][:5]] == [20.0] * 5
    hv = g.grid_structure["buses"][-1]
    assert hv["vn_kv"] == 110.0 and hv["zone"] == "HV"
    trafo = g.lines["transformers"][0]
    assert trafo["std_type"] == "40 MVA 110/20 kV"
    assert trafo["hv_bus"] == 5 and trafo["lv_bus"] == 0
    # the slack feeds the HV bus, not the MV net directly
    assert g.substation["substations"][0]["bus"] == 5
    # line geometry survives for the live map
    assert g.lines["lines"][0]["geometry"] == [[9.93, 54.66], [9.95, 54.67]]


def test_profiles_per_type_and_no_households(tmp_path):
    doc = dict(MV_DOC)
    doc["buses"] = list(MV_DOC["buses"]) + [
        {"name": "g2", "kind": "gen", "geo": [9.94, 54.70],
         "subtype": "pv", "gen_kw": 2000},
        {"name": "g3", "kind": "gen", "geo": [9.92, 54.70],
         "subtype": "biogas", "gen_kw": 500},
    ]
    doc["lines"] = list(MV_DOC["lines"]) + [
        {"from": 1, "to": 5, "length_km": 1.0, "r_ohm_per_km": 0.206,
         "x_ohm_per_km": 0.116, "c_nf_per_km": 250, "max_i_ka": 0.319,
         "type": "NA2XS2Y 1x150 RM/25 12/20 kV", "kind": "cable",
         "geometry": [[9.95, 54.67], [9.94, 54.70]]},
        {"from": 1, "to": 6, "length_km": 1.0, "r_ohm_per_km": 0.206,
         "x_ohm_per_km": 0.116, "c_nf_per_km": 250, "max_i_ka": 0.319,
         "type": "NA2XS2Y 1x150 RM/25 12/20 kV", "kind": "cable",
         "geometry": [[9.95, 54.67], [9.92, 54.70]]},
    ]
    d = _write(tmp_path, doc)
    g = convert_gridedit_mv(d / "ms-netz.json", steps=96)

    # every load is non-household -> LPG/EV/PV assignment skips them all
    assert g.load["loads"] and all(ld["household"] is False
                                   for ld in g.load["loads"])
    gens = {gs["name"].split("_")[0]: gs["p_mw"] for gs in
            g.generation["generation"]}
    assert gens["PV"][0] == 0.0 and max(gens["PV"]) > 1.5      # bell, dark at night
    assert min(gens["Wind"]) > 0.1 and max(gens["Wind"]) < 3.0  # gusty, never rated
    assert len(set(gens["Biogas"])) == 1                        # steady base load
    # station peaks in the evening, HPC late afternoon, both > 0 at night
    loads = {ld["name"].split("_")[0]: ld["p_mw"] for ld in g.load["loads"]}
    assert max(loads["Station"]) <= 0.132 + 1e-6
    assert loads["HPC"][0] >= 0.015 - 1e-6                      # floor, not zero


def test_echeck_stamp_surfaces_in_notes(tmp_path):
    doc = dict(MV_DOC, echeck={"ok": False, "failures": ["connected"]})
    d = _write(tmp_path, doc, fname="mangelhaft")
    g = convert_gridedit_mv(d / "mangelhaft.json", steps=96)
    assert any("E-Check FAIL: connected" in n for n in g.notes)


def test_real_pv_days_only_touch_pv_sgens(tmp_path):
    """The real measured PV day shapes (day slider) must scale PV systems only —
    wind/biogas keep their built-in profiles; a grid without any PV sgen does
    not attach the real days at all."""
    import numpy as np

    doc = dict(MV_DOC)
    doc["buses"] = list(MV_DOC["buses"]) + [
        {"name": "g2", "kind": "gen", "geo": [9.94, 54.70],
         "subtype": "pv", "gen_kw": 2000},
    ]
    doc["lines"] = list(MV_DOC["lines"]) + [
        {"from": 1, "to": 5, "length_km": 1.0, "r_ohm_per_km": 0.206,
         "x_ohm_per_km": 0.116, "c_nf_per_km": 250, "max_i_ka": 0.319,
         "type": "NA2XS2Y 1x150 RM/25 12/20 kV", "kind": "cable",
         "geometry": [[9.95, 54.67], [9.94, 54.70]]},
    ]
    d = _write(tmp_path, doc)
    g = convert_gridedit_mv(d / "ms-netz.json", steps=96)
    data = input_data_from_dicts(g.grid_structure, g.lines, g.load,
                                 g.generation, g.substation)
    sim = Simulator(data)
    assert sim.sgen_kind == ["wind", "pv"]
    sim.set_pv_days(np.full((1, 96), 0.5))
    assert sim.pv_days is not None
    col = sim._sgen_p_col(0, 10)                       # 02:30 — dark
    assert col[0] == sim.prof.sgen_p[0, 10] > 0.1      # wind: built-in profile
    assert col[1] == sim.sgen_peak[1] * 0.5            # pv: real day shape

    # wind-only grid: the real days must not attach (day slider stays off)
    g2 = convert_gridedit_mv(d / "ms-netz.json", steps=96)
    g2.generation["generation"] = g2.generation["generation"][:1]
    data2 = input_data_from_dicts(g2.grid_structure, g2.lines, g2.load,
                                  g2.generation, g2.substation)
    sim2 = Simulator(data2)
    sim2.set_pv_days(np.full((1, 96), 0.5))
    assert sim2.pv_days is None and sim2.n_days == 1
