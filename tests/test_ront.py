"""rONT (Phase 3 of the vertical integration): on-load tap changer.

The regulator must hold its LV busbar inside the voltage band using ONLY the
operator's view (busbar meter, else the state estimate), step one tap per
action in the closed loop, be blind without any data, and restore the
transformer's original tap data on removal.
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


def _sim() -> Simulator:
    cat = GridCatalog(ding0_dir=DING0_DIR, library_manifest=MANIFEST)
    g = cat.get_inputs("lv_suburban_1864_265991", steps=96)
    data = input_data_from_dicts(g.grid_structure, g.lines, g.load,
                                 g.generation, g.substation, cells=g.cells)
    return Simulator(data)


def _busbar_v(sim, busbar: int) -> float:
    return float(sim.net.res_bus.at[busbar, "vm_pu"])


def test_ront_regulates_busbar_with_meter():
    sim = _sim()
    r = sim.add_ront(0, v_target=1.03)
    sim.meters.add_node(r.busbar)           # the busbar SMGW delivers V
    for _ in range(5):                       # closed loop: tap acts next step
        sim._est_wall = float("inf")
        res = sim.run_step(76)
        assert res.converged
    assert r.seen_src == "meter"
    v = _busbar_v(sim, r.busbar)
    assert 1.03 - r.deadband <= v <= 1.03 + r.deadband, f"vm={v}"
    assert r.tap_pos < 0, "raising the LV voltage needs a NEGATIVE tap"
    assert res.ronts and res.ronts[0]["tap_pos"] == r.tap_pos

    # move the setpoint down: the regulator steps back up through the band
    sim.set_ront(r.rid, v_target=0.97)
    for _ in range(6):
        sim._est_wall = float("inf")
        sim.run_step(76)
    v = _busbar_v(sim, r.busbar)
    assert 0.97 - r.deadband <= v <= 0.97 + r.deadband, f"vm={v}"
    assert r.tap_pos > 0


def test_ront_estimate_fed_and_blind():
    sim = _sim()
    r = sim.add_ront(0, v_target=1.03)
    sim.meters.add_trafo(0)                 # no busbar meter: estimate only
    for _ in range(5):
        sim._est_wall = 0.0                 # fresh telegram per step
        sim.run_step(76)
    assert r.seen_src == "estimate"
    assert r.tap_pos < 0, "estimate-fed rONT must regulate too"

    blind = _sim()
    rb = blind.add_ront(0, v_target=1.03)
    for _ in range(3):                       # no meters at all -> no estimate
        blind.run_step(76)
    assert rb.seen_v is None and rb.seen_src is None
    assert rb.tap_pos == 0, "a blind rONT must hold its position"


def test_ront_add_remove_restores_tap_data():
    sim = _sim()
    before = {c: sim.net.trafo.at[0, c]
              for c in ("tap_min", "tap_max", "tap_step_percent", "tap_pos")}
    r = sim.add_ront(0)
    assert float(sim.net.trafo.at[0, "tap_min"]) == -4.0
    assert float(sim.net.trafo.at[0, "tap_step_percent"]) == 1.5
    with pytest.raises(ValueError):
        sim.add_ront(0)                      # one rONT per transformer
    assert sim.remove_ront(r.rid)
    after = {c: sim.net.trafo.at[0, c] for c in before}
    assert after == before, "removal must restore the shipped tap data"
    with pytest.raises(KeyError):
        sim.add_ront(999)
