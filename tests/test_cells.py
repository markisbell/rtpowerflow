"""Phase 0 of the vertical MV/LV integration: ONS cells as a first-class concept.

Importers must describe the vertical structure of what they produce — one cell
per MV/LV secondary substation: spliced street-routed LV grids carry their bus
membership + station transformer(s), stations that stay lumped become
degenerate cells, a standalone LV grid is exactly one cell. The cells travel
through InputData validation into the Simulator and out via ``topology()``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from netzsim.data_loader import input_data_from_dicts
from netzsim.grid_catalog import GridCatalog
from netzsim.osm_lv_import import convert_osm_lv
from netzsim.simulator import Simulator

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "data" / "grid_library_full.json"
DING0_DIR = ROOT / "data" / "ding0_grids"

pytestmark = pytest.mark.skipif(not MANIFEST.exists(), reason="no committed dataset")

DISTRICT = "mv_rural_3150"        # 3 OSM LV subgrids: 300266, 300668, 300575


@pytest.fixture(scope="module")
def catalog() -> GridCatalog:
    return GridCatalog(ding0_dir=DING0_DIR, library_manifest=MANIFEST)


@pytest.fixture(scope="module")
def district(catalog):
    return catalog.get_inputs(DISTRICT, steps=96)


def test_district_cells_structure(district):
    cells = district.cells
    assert cells, "composed district carries no cells"
    assert len({c["id"] for c in cells}) == len(cells), "cell ids not unique"

    spliced = [c for c in cells if not c["lumped"]]
    lumped = [c for c in cells if c["lumped"]]
    assert len(spliced) == 3, "expected the 3 street-routed LV grids as cells"
    assert lumped, "non-routed stations should stay as degenerate lumped cells"

    trafos = district.lines["transformers"]
    n_bus = len(district.grid_structure["buses"])
    seen: set[int] = set()
    for c in spliced:
        assert c["buses"], f"cell {c['id']}: no member buses"
        assert c["lv_busbar"] in c["buses"]
        assert c["mv_bus"] is not None and c["mv_bus"] not in c["buses"]
        assert not seen.intersection(c["buses"]), "cell bus sets overlap"
        seen.update(c["buses"])
        assert all(0 <= b < n_bus for b in c["buses"])
        # the station transformer really connects mv_bus to the cell's busbar
        assert c["station_trafos"], f"cell {c['id']}: no station trafo"
        for ti in c["station_trafos"]:
            assert trafos[ti]["lv_bus"] == c["lv_busbar"]
            assert trafos[ti]["hv_bus"] == c["mv_bus"]
    for c in lumped:
        assert c["buses"] == [] and c["lv_busbar"] is None
        assert c["mv_bus"] is not None and 0 <= c["mv_bus"] < n_bus


def test_district_cells_reach_simulator_and_topology(district):
    data = input_data_from_dicts(district.grid_structure, district.lines,
                                 district.load, district.generation,
                                 district.substation, cells=district.cells)
    sim = Simulator(data)
    assert sim.cells == district.cells
    for c in district.cells:
        if not c["lumped"]:
            assert sim.cell_of_bus[c["lv_busbar"]] == c["id"]
        else:
            assert c["mv_bus"] not in sim.cell_of_bus  # MV bus belongs to no cell
    topo = sim.topology()
    assert topo["cells"] == district.cells


def test_osm_lv_grid_is_exactly_one_cell(catalog):
    entry = next(e for e in catalog._entries.values()
                 if e.voltage == "LV" and e.osm_grid)
    g = convert_osm_lv(entry.osm_grid, steps=96)
    assert len(g.cells) == 1
    c = g.cells[0]
    raw = json.loads(Path(entry.osm_grid).read_text())
    mv_bus = len(raw["buses"])                     # the appended "MS-Netz" bus
    assert c["buses"] == list(range(mv_bus))
    assert c["lv_busbar"] == int(raw["slack_bus"])
    assert c["mv_bus"] == mv_bus
    assert c["station_trafos"] == [0] and not c["lumped"]
    # cells survive validation
    data = input_data_from_dicts(g.grid_structure, g.lines, g.load,
                                 g.generation, g.substation, cells=g.cells)
    assert len(data.cells) == 1


def test_cell_validation_rejects_bad_indices():
    from netzsim.ding0_import import convert_ding0_csv

    g = convert_ding0_csv(DING0_DIR / "ding0_oep_3150", steps=96, scope="mv")
    assert g.cells and all(c["lumped"] for c in g.cells)
    bad = [dict(g.cells[0], buses=[10 ** 6])]
    with pytest.raises(ValueError, match="out of range"):
        input_data_from_dicts(g.grid_structure, g.lines, g.load,
                              g.generation, g.substation, cells=bad)
