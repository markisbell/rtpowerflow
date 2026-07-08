"""Phase 1.2 of the vertical MV/LV integration: hierarchical two-stage WLS.

Every spliced ONS cell estimates locally (slack at its feeding MV bus, set
from the previous MV estimate); the cells' boundary flows — measured, else
cell-estimated, else profile pseudo — feed the reduced MV-level WLS. The
composed result mirrors the monolithic estimate's shape (plus ``mode`` and
per-cell metadata), so all downstream consumers work unchanged.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from netzsim.data_loader import input_data_from_dicts
from netzsim.estimator import EstConfig, Estimator, HierarchicalEstimator, wants_hierarchy
from netzsim.grid_catalog import GridCatalog
from netzsim.simulator import Simulator

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "data" / "grid_library_full.json"
DING0_DIR = ROOT / "data" / "ding0_grids"

pytestmark = pytest.mark.skipif(not MANIFEST.exists(), reason="no committed dataset")

DISTRICT = "mv_rural_3150"


@pytest.fixture(scope="module")
def catalog() -> GridCatalog:
    return GridCatalog(ding0_dir=DING0_DIR, library_manifest=MANIFEST)


@pytest.fixture(scope="module")
def district_sim(catalog) -> Simulator:
    g = catalog.get_inputs(DISTRICT, steps=96)
    data = input_data_from_dicts(g.grid_structure, g.lines, g.load,
                                 g.generation, g.substation, cells=g.cells)
    return Simulator(data)


def _fresh_estimate(sim, step: int):
    """Solve ``step`` with a forced-fresh estimation run."""
    sim._est_wall = 0.0
    res = sim.run_step(step)
    assert res.converged
    assert res.estimated is not None, "no estimate produced"
    return res.estimated


def test_hierarchy_resolution(catalog, district_sim):
    cfg = EstConfig()
    assert wants_hierarchy(cfg, district_sim.cells, len(district_sim.net.bus))
    assert isinstance(district_sim._make_estimator(district_sim.net),
                      HierarchicalEstimator)
    # forcing monolithic wins over cells
    assert not wants_hierarchy(EstConfig(hierarchy="monolithic"),
                               district_sim.cells, len(district_sim.net.bus))
    # a standalone LV grid (one cell, no MV level) always stays monolithic
    g = catalog.get_inputs("lv_rural_3150_300266", steps=96)
    data = input_data_from_dicts(g.grid_structure, g.lines, g.load,
                                 g.generation, g.substation, cells=g.cells)
    lv_sim = Simulator(data)
    assert not wants_hierarchy(cfg, lv_sim.cells, len(lv_sim.net.bus))
    assert isinstance(lv_sim._make_estimator(lv_sim.net), Estimator)


def test_hierarchical_estimate_composes_and_converges(district_sim):
    sim = district_sim
    sim.meters.clear()
    sim.apply_meter_preset("all_nodes")
    sim.apply_meter_preset("all_trafos")
    est1 = _fresh_estimate(sim, 72)          # 18:00
    assert est1["mode"] == "hierarchical"
    # the composed arrays cover the whole net — every consumer sees the
    # familiar shape
    assert len(est1["buses"]) == len(sim.net.bus)
    assert len(est1["lines"]) == len(sim.net.line)
    assert len(est1["trafos"]) == len(sim.net.trafo)
    spliced = [c for c in sim.cells if not c["lumped"]]
    assert len(est1["cells"]) == len(spliced)
    assert all(m["boundary_src"] == "meter" for m in est1["cells"])

    # second run: the cell slacks now use the MV estimate of run 1 — under
    # full metering the composed estimate must track the truth closely
    est2 = _fresh_estimate(sim, 73)
    err = est2["error"]["max_dv_pu"]
    assert err is not None and err < 0.01, f"max|dV| = {err}"
    for m in est2["cells"]:
        assert m["error"]["max_dv_pu"] is not None
        assert m["error"]["max_dv_pu"] < 0.01
    sim.meters.clear()


def test_unmetered_cell_boundary_is_pseudo_and_honest(district_sim):
    """Vertical honesty: a cell nobody meters contributes only its profile
    pseudo to the MV stage — and its internal state stays a profile guess."""
    sim = district_sim
    sim.meters.clear()
    spliced = [c for c in sim.cells if not c["lumped"]]
    metered, dark = spliced[0], spliced[1]
    sim.apply_meter_preset("cell_full", cell=metered["id"])
    est = _fresh_estimate(sim, 72)
    srcs = {m["id"]: m["boundary_src"] for m in est["cells"]}
    assert srcs[metered["id"]] == "meter"        # cell_full includes the trafo
    assert srcs[dark["id"]] == "pseudo"
    errs = {m["id"]: (m.get("error") or {}).get("max_dv_pu") for m in est["cells"]}
    assert errs[metered["id"]] is not None and errs[dark["id"]] is not None
    assert errs[metered["id"]] <= errs[dark["id"]] + 1e-9, (
        f"metered cell ({errs[metered['id']]}) should not estimate worse "
        f"than the dark cell ({errs[dark['id']]})")
    sim.meters.clear()


def test_monolithic_forced_on_district(district_sim):
    sim = district_sim
    sim.set_est_config(EstConfig(hierarchy="monolithic"))
    sim.meters.clear()
    sim.apply_meter_preset("digital_stations")
    est = _fresh_estimate(sim, 72)
    assert est["mode"] == "monolithic"
    assert "cells" not in est
    sim.meters.clear()
    sim.set_est_config(EstConfig())
