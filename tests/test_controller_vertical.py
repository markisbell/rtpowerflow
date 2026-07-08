"""Phase 2 of the vertical MV/LV integration: the grid-traffic-light cascade.

Scope ``cell`` limits a controller's domain to ONE spliced ONS cell; scope
``mv`` is the coordinator — it watches only the MV level (through meters and
the MV-stage estimate, never the truth) and broadcasts its factors as SIGNALS
to every placed cell controller, which applies min(local law, signal).
Executing a signal needs a device, not a meter: a locally blind cell
controller still dims on command. Cells without a controller stay
uncoordinated.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from netzsim.data_loader import input_data_from_dicts
from netzsim.grid_catalog import GridCatalog
from netzsim.simulator import Simulator

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "data" / "grid_library_full.json"
DING0_DIR = ROOT / "data" / "ding0_grids"

pytestmark = pytest.mark.skipif(not MANIFEST.exists(), reason="no committed dataset")

DISTRICT = "mv_rural_3150"
EV_STEP = 76                     # 19:00 on the 96-step raster — EVs charging


@pytest.fixture()
def sim() -> Simulator:
    cat = GridCatalog(ding0_dir=DING0_DIR, library_manifest=MANIFEST)
    g = cat.get_inputs(DISTRICT, steps=96)
    data = input_data_from_dicts(g.grid_structure, g.lines, g.load,
                                 g.generation, g.substation, cells=g.cells)
    s = Simulator(data)
    # give every spliced cell a controllable lever: one 22-kW wallbox charging
    # 18:00–22:00 at two member buses each
    for c in s.cells:
        if not c["lumped"]:
            for b in c["buses"][2:4]:
                s.add_ev(int(b), kw=22.0, start_min=18 * 60, dur_min=240)
    return s


def _run_with_est(sim, step: int):
    sim._est_wall = 0.0
    res = sim.run_step(step)
    assert res.converged
    return res


def test_scope_validation(sim):
    with pytest.raises(KeyError):
        sim.add_controller("cell", cell="no-such-cell")
    spliced = [c for c in sim.cells if not c["lumped"]]
    c = sim.add_controller("cell", cell=spliced[0]["id"])
    assert c.cell == spliced[0]["id"]
    assert sim.add_controller("mv").scope == "mv"


def test_mv_scope_rejected_without_mv_level():
    cat = GridCatalog(ding0_dir=DING0_DIR, library_manifest=MANIFEST)
    g = cat.get_inputs("lv_rural_3150_300266", steps=96)
    data = input_data_from_dicts(g.grid_structure, g.lines, g.load,
                                 g.generation, g.substation, cells=g.cells)
    lv = Simulator(data)
    with pytest.raises(ValueError):
        lv.add_controller("mv")


def test_cell_scope_rows_and_domain(sim):
    """A cell controller throttles ONLY its own cell's DERs and reads only its
    own station trafo — proven with the estimation BLOCKED, so meter readings
    are the sole possible source."""
    spliced = [c for c in sim.cells if not c["lumped"]]
    a, b = spliced[0], spliced[1]
    ca = sim.add_controller("cell", cell=a["id"], limit_pct=100.0)
    ev_rows, _ = sim._controller_rows(ca)
    buses_a = set(a["buses"])
    assert ev_rows, "no EV rows in the cell"
    assert all(int(sim.net.load.at[sim.prof.load_idx[i], "bus"]) in buses_a
               for i in ev_rows)
    sim._est_wall = float("inf")             # no estimate: meters only
    # meter the OTHER cell's station only: controller a stays blind
    sim.meters.clear()
    for t in b["station_trafos"]:
        sim.meters.add_trafo(int(t))
    res = sim.run_step(EV_STEP)
    assert res.converged and res.estimated is None
    assert ca.seen_pct is None, "cell controller saw a foreign station meter"
    # its own station meter -> direct device reading
    for t in a["station_trafos"]:
        sim.meters.add_trafo(int(t))
    sim.run_step(EV_STEP + 1)
    assert ca.seen_src == "meter" and ca.seen_pct is not None


def test_traffic_light_cascade(sim):
    """The coordinator sees the MV level (estimate), dims via signals; cell
    controllers execute even when locally blind; cells without a controller
    stay uncoordinated."""
    spliced = [c for c in sim.cells if not c["lumped"]]
    with_ctrl, blind_ctrl, no_ctrl = spliced[0], spliced[1], spliced[2]
    sim.meters.clear()
    sim.apply_meter_preset("digital_stations")   # MV estimate gets boundaries
    # the coordinated cells: one fully observable, one without any local meter
    for t in blind_ctrl["station_trafos"]:
        sim.meters.remove_trafo(int(t))
    c1 = sim.add_controller("cell", cell=with_ctrl["id"])
    c2 = sim.add_controller("cell", cell=blind_ctrl["id"])
    coord = sim.add_controller("mv", limit_pct=20.0)   # current loading counts
    # a few closed-loop steps in the evening import window
    for k in range(4):
        _run_with_est(sim, EV_STEP + k)
    assert coord.seen_pct is not None and coord.seen_pct > 20.0
    assert coord.seen_src in ("meter", "estimate")
    assert coord.ev_factor < 1.0, "coordinator did not ratchet the EV signal"
    assert coord.pv_factor == 1.0, "evening import must pull the EV lever"
    # both cell controllers received and execute the signal ...
    assert c1.signal_ev < 1.0 and c2.signal_ev < 1.0
    # cell 2 has no meter: it sees at most the (pseudo-based) estimate —
    # yet the received signal is executed regardless of local observability
    assert c2.seen_src != "meter", "cell 2 has no meter to read"
    assert c2.effective_ev < 1.0, "an unmetered cell must still execute the signal"
    # ... and the throttling really reaches the EV loads of coordinated cells
    ev1, _ = sim._controller_rows(c1)
    li = sim.prof.load_idx[ev1[0]]
    applied = float(sim.net.load.at[li, "p_mw"])
    profile = float(sim.prof.load_p[ev1[0], (EV_STEP + 3) % 96])
    assert applied < profile * 0.999, "EV load of a coordinated cell not dimmed"
    # the uncoordinated cell keeps its full charging power
    dummy = sim.add_controller("cell", cell=no_ctrl["id"])   # only to get rows
    ev3, _ = sim._controller_rows(dummy)
    sim.remove_controller(dummy.cid)
    li3 = sim.prof.load_idx[ev3[0]]
    applied3 = float(sim.net.load.at[li3, "p_mw"])
    profile3 = float(sim.prof.load_p[ev3[0], (EV_STEP + 3) % 96])
    assert applied3 == pytest.approx(profile3, rel=1e-9), \
        "cell without a controller must stay uncoordinated"
    # the wire format carries the cascade
    d = coord.as_dict()
    assert d["signals"] and all(v["ev"] < 1.0 for v in d["signals"].values())
