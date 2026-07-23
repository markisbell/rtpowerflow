"""Reference feeders (IEEE / CIGRE / Kerber) -> GridInputs.

Pins two contracts:
1. Every catalog reference feeder converts, validates, builds and SOLVES —
   and its converted bus count matches the pinned catalog metadata (a
   pandapower upgrade that reshapes a reference net trips here first).
2. EXACTNESS at the evening peak: the conversion adds only the time
   dimension. At the 19:00 step every load sits exactly at its published
   nominal value, so the netzsim solve must reproduce the pandapower solve
   of the source net itself (balanced-folded where the source is
   asymmetric) to solver precision.
"""
from __future__ import annotations

import pytest

from netzsim.data_loader import input_data_from_dicts
from netzsim.grid_catalog import GridCatalog
from netzsim.reference_import import (
    PEAK_HOUR, REFERENCE_GRIDS, convert_pandapower_net, convert_reference,
)
from netzsim.simulator import Simulator

STEPS = 96
PEAK_STEP = int(PEAK_HOUR / 24 * STEPS)          # 19:00 -> step 76


def _sim(g):
    data = input_data_from_dicts(g.grid_structure, g.lines, g.load,
                                 g.generation, g.substation)
    return Simulator(data)


# --------------------------------------------------------------------------- #
# 1. all reference feeders convert + solve; catalog metadata stays true
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("key", list(REFERENCE_GRIDS))
def test_reference_feeder_converts_and_solves(key):
    g = convert_reference(key, steps=STEPS)
    meta = REFERENCE_GRIDS[key]
    assert len(g.grid_structure["buses"]) == meta["nodes"]
    res = _sim(g).run_step(PEAK_STEP)
    assert res.converged
    s = res.summary
    assert 0.85 < s["vm_pu_min"] <= s["vm_pu_max"] < 1.11
    assert s["total_load_mw"] > 0


def test_catalog_offers_reference_feeders():
    cat = GridCatalog()                            # no dataset needed
    items = {i["id"]: i for i in cat.list()}
    for key, meta in REFERENCE_GRIDS.items():
        it = items[key]
        assert it["source"] == "reference"
        assert it["voltage"] == meta["voltage"]
        assert it["character"] == meta["origin"]   # UI origin tag
        assert it["nodes"] == meta["nodes"]
        assert it["geo"] is False                  # schematic views, no map
    inputs = cat.get_inputs("kerber_landnetz", steps=STEPS)
    assert len(inputs.load["loads"]) == 14


def test_preview_carries_schematic_layout_without_geo():
    # reference feeders have no geo -> the /grids/{id} preview must attach
    # the synthetic layouts so the UI can draw a single-line diagram
    from netzsim.grid_catalog import preview

    cat = GridCatalog()
    p = preview(cat.get_inputs("ieee_33bw", steps=STEPS))
    assert all(b.get("geo") is None for b in p["buses"])
    for b in p["buses"]:
        assert 0.0 <= b["tx"] <= 1.0 and 0.0 <= b["ty"] <= 1.0
        assert 0.0 <= b["x"] <= 1.0 and 0.0 <= b["y"] <= 1.0
    # the 5 open tie lines are marked so the diagram can dash them
    assert sum(not ln["in_service"] for ln in p["lines"]) == 5


def test_household_flags_follow_voltage_level():
    # LV connections are households (LPG/EV/PV seeding + SLP basis work),
    # MV loads are aggregates
    lv = convert_reference("kerber_dorfnetz", steps=STEPS)
    assert all(ld["household"] for ld in lv.load["loads"])
    mv = convert_reference("ieee_33bw", steps=STEPS)
    assert not any(ld["household"] for ld in mv.load["loads"])


# --------------------------------------------------------------------------- #
# 2. exactness: at the peak step netzsim == pandapower on the source net
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("key", ["ieee_33bw", "cigre_mv", "cigre_lv",
                                 "kerber_dorfnetz", "ieee_european_lv"])
def test_peak_step_reproduces_the_published_case(key):
    import pandapower as pp
    import pandas as pd

    from netzsim.reference_import import _factories

    net = _factories()[key]()
    # balanced fold of asymmetric loads (runpp ignores net.asymmetric_load;
    # netzsim models their three-phase sum as a balanced load)
    for _, ld in net.asymmetric_load.iterrows():
        if not bool(ld["in_service"]):
            continue
        scale = float(ld.get("scaling", 1.0) or 1.0)
        pp.create_load(
            net, bus=int(ld["bus"]),
            p_mw=(ld["p_a_mw"] + ld["p_b_mw"] + ld["p_c_mw"]) * scale,
            q_mvar=(ld["q_a_mvar"] + ld["q_b_mvar"] + ld["q_c_mvar"]) * scale)

    bus_map: dict[int, int] = {}
    g = convert_pandapower_net(net, name=key, steps=STEPS, bus_map=bus_map)
    res = _sim(g).run_step(PEAK_STEP)
    assert res.converged
    vm_netzsim = {b["index"]: b["vm_pu"] for b in res.buses}

    pp.runpp(net)
    diffs = [
        abs(float(net.res_bus.vm_pu.loc[old]) - vm_netzsim[new])
        for old, new in bus_map.items()
        if not pd.isna(net.res_bus.vm_pu.loc[old])
    ]
    assert diffs and max(diffs) < 2e-6             # 1-W profile rounding floor
