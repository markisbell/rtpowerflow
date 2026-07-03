"""User-drawn grids (gridformat JSON exported by the sibling gridedit editor):
catalog scan of the user_grids folder + the importer honoring the chosen
substation transformer rating carried in the file's `trafo` field."""
from __future__ import annotations

import json

from netzsim.data_loader import input_data_from_dicts
from netzsim.grid_catalog import GridCatalog
from netzsim.osm_lv_import import convert_osm_lv
from netzsim.simulator import Simulator

# A minimal editor export: station -> cabinet main line, two houses on the
# cabinet. Coordinates are around Kappeln (the gridedit demo village).
DOC = {
    "name": "Testnetz Kappeln",
    "station": [9.93, 54.66],
    "slack_bus": 0,
    "trafo": {"sn_kva": 400, "hv_kv": 20},
    "buses": [
        {"name": "LV_station", "vn_kv": 0.4, "geo": [9.93, 54.66], "role": "slack"},
        {"name": "KVS_1", "vn_kv": 0.4, "geo": [9.932, 54.661], "role": "cabinet"},
        {"name": "H_1", "vn_kv": 0.4, "geo": [9.9322, 54.6612], "role": "load"},
        {"name": "H_2", "vn_kv": 0.4, "geo": [9.9318, 54.6612], "role": "load"},
    ],
    "lines": [
        {"from": 0, "to": 1, "length_km": 0.21, "r_ohm_per_km": 0.206,
         "x_ohm_per_km": 0.078, "c_nf_per_km": 0.0, "max_i_ka": 0.293,
         "parallel": 1, "type": "NAYY 4x150",
         "geometry": [[9.93, 54.66], [9.932, 54.661]]},
        {"from": 1, "to": 2, "length_km": 0.03, "r_ohm_per_km": 1.2,
         "x_ohm_per_km": 0.082, "c_nf_per_km": 0.0, "max_i_ka": 0.107,
         "parallel": 1, "type": "NAYY 4x25",
         "geometry": [[9.932, 54.661], [9.9322, 54.6612]]},
        {"from": 1, "to": 3, "length_km": 0.04, "r_ohm_per_km": 1.2,
         "x_ohm_per_km": 0.082, "c_nf_per_km": 0.0, "max_i_ka": 0.107,
         "parallel": 1, "type": "NAYY 4x25",
         "geometry": [[9.932, 54.661], [9.9318, 54.6612]]},
    ],
    "loads": [{"bus": 2, "peak_mw": 0.0045}, {"bus": 3, "peak_mw": 0.0012}],
}


def _write(tmp_path, name="mein-netz"):
    d = tmp_path / "user_grids"
    d.mkdir(exist_ok=True)
    (d / f"{name}.json").write_text(json.dumps(DOC), encoding="utf-8")
    return d


def test_catalog_lists_and_converts_user_grid(tmp_path):
    d = _write(tmp_path)
    cat = GridCatalog(user_dir=d)
    assert cat.available
    items = cat.list()
    entry = next(i for i in items if i["id"] == "user_mein-netz")
    assert entry["voltage"] == "LV" and entry["character"] == "user"
    assert entry["geo"] is True and entry["nodes"] == 4

    g = cat.get_inputs("user_mein-netz", steps=96)
    data = input_data_from_dicts(g.grid_structure, g.lines, g.load,
                                 g.generation, g.substation)
    res = Simulator(data).run_step(48)
    assert res.converged


def test_new_export_appears_without_restart(tmp_path):
    d = _write(tmp_path)
    cat = GridCatalog(user_dir=d)
    assert not cat.has("user_zweites-netz")
    (d / "zweites-netz.json").write_text(json.dumps(DOC), encoding="utf-8")
    assert cat.has("user_zweites-netz")           # lazy rescan on lookup
    (d / "zweites-netz.json").unlink()
    assert not any(i["id"] == "user_zweites-netz" for i in cat.list())


def test_trafo_rating_from_file_wins(tmp_path):
    d = _write(tmp_path)
    g = convert_osm_lv(d / "mein-netz.json", steps=96)
    trafo = g.lines["transformers"][0]
    # 400 kVA chosen in the editor -> the 0.4 MVA standard unit, NOT the
    # auto-sizing (which would pick 0.25 MVA for ~6 kW of peak load)
    assert trafo["std_type"] == "0.4 MVA 20/0.4 kV"
    assert trafo["parallel"] == 1
    assert any("rated 400 kVA" in n for n in g.notes)


def test_trafo_hv_10kv_and_parallel_for_1mva(tmp_path):
    doc = dict(DOC, trafo={"sn_kva": 1000, "hv_kv": 10})
    d = tmp_path / "user_grids"
    d.mkdir()
    (d / "gross.json").write_text(json.dumps(doc), encoding="utf-8")
    g = convert_osm_lv(d / "gross.json", steps=96)
    trafo = g.lines["transformers"][0]
    assert trafo["std_type"] == "0.63 MVA 10/0.4 kV"
    assert trafo["parallel"] == 2                  # 2x630 covers the rated 1000
    mv_bus = g.grid_structure["buses"][trafo["hv_bus"]]
    assert mv_bus["vn_kv"] == 10.0
    data = input_data_from_dicts(g.grid_structure, g.lines, g.load,
                                 g.generation, g.substation)
    assert Simulator(data).run_step(48).converged
