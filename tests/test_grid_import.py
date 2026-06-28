"""Tests for the European-Archetype xlsx -> netzsim input converter."""
from __future__ import annotations

import io
from pathlib import Path

import pandas as pd
import pytest

import numpy as np

from netzsim.data_loader import InputData
from netzsim.grid_import import convert_xlsx_bytes
from netzsim.grid_import.xlsx import convert_from_zip
from netzsim.models import (
    GenerationFile,
    GridStructure,
    Lines,
    LoadFile,
    SubstationFile,
)
from netzsim.simulator import Simulator

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "European Archetpye Distribution Grid Models.zip"


def _workbook_bytes(*, extra_line_to_missing: bool = False) -> bytes:
    """A minimal valid workbook: slack(MV) --trafo--> LV1 --line--> LV2(load)."""
    nodes = pd.DataFrame({
        "Name of the Node": ["N0", "N1", "N2"],
        "Type": ["Slack", "PQ", "PQ"],
        "Nominal Voltage [kV]": [20.0, 0.4, 0.4],
    })
    lines = pd.DataFrame({
        "Name of the line": ["L1"] + (["L2"] if extra_line_to_missing else []),
        "From node": ["N1"] + (["N2"] if extra_line_to_missing else []),
        "To node": ["N2"] + (["N9"] if extra_line_to_missing else []),  # N9 undeclared
        "Type": ["TYPE_A"] + (["TYPE_A"] if extra_line_to_missing else []),
        "Length [km]": [0.05] + ([0.02] if extra_line_to_missing else []),
    })
    line_types = pd.DataFrame({
        "Name of line type": ["TYPE_A"],
        "R [Ohm/km]": [0.2], "X [Ohm/km]": [0.08],
        "B [Mikro S/km]": [0.0], "Nominal Current I [A]": [200.0],
    })
    trafos = pd.DataFrame({
        "Name of the transformer": ["T1"],
        "From node": ["N0"], "To node": ["N1"],
        "Smax [MVA]": [0.4], "V_P [kV]": [20.0], "V_S [kV]": [0.4],
        "U_sc [%]": ["tbd"], "P_cl [kW]": ["tbd"],
        "P_il [kW]": ["tbd"], "I_nl [%]": ["tbd"],
        "Number of parallel transformers": [0],
    })
    loads = pd.DataFrame({
        "Name of the load": ["LD1"], "Node": ["N2"],
        "Pmax [MW]": [1.0], "Sn [MVA]": [1.0],
    })

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        nodes.to_excel(xw, sheet_name="Nodes", index=False)
        lines.to_excel(xw, sheet_name="Lines", index=False)
        line_types.to_excel(xw, sheet_name="Line Types", index=False)
        trafos.to_excel(xw, sheet_name="Transformers", index=False)
        loads.to_excel(xw, sheet_name="Loads", index=False)
    return buf.getvalue()


def _island_workbook_bytes() -> bytes:
    """slack(N0)--trafo-->N1--line-->N2, plus an ISOLATED pair N3--line--N4."""
    nodes = pd.DataFrame({
        "Name of the Node": ["N0", "N1", "N2", "N3", "N4"],
        "Type": ["Slack", "PQ", "PQ", "PQ", "PQ"],
        "Nominal Voltage [kV]": [20.0, 0.4, 0.4, 0.4, 0.4],
    })
    lines = pd.DataFrame({
        "Name of the line": ["L1", "L_ISLAND"],
        "From node": ["N1", "N3"],
        "To node": ["N2", "N4"],          # N3/N4 connect only to each other
        "Type": ["TYPE_A", "TYPE_A"],
        "Length [km]": [0.05, 0.04],
    })
    line_types = pd.DataFrame({
        "Name of line type": ["TYPE_A"], "R [Ohm/km]": [0.2], "X [Ohm/km]": [0.08],
        "B [Mikro S/km]": [0.0], "Nominal Current I [A]": [200.0],
    })
    trafos = pd.DataFrame({
        "Name of the transformer": ["T1"], "From node": ["N0"], "To node": ["N1"],
        "Smax [MVA]": [0.4], "V_P [kV]": [20.0], "V_S [kV]": [0.4],
        "U_sc [%]": ["tbd"], "P_cl [kW]": ["tbd"], "P_il [kW]": ["tbd"],
        "I_nl [%]": ["tbd"], "Number of parallel transformers": [0],
    })
    loads = pd.DataFrame({
        "Name of the load": ["LD2", "LD4"], "Node": ["N2", "N4"],
        "Pmax [MW]": [1.0, 1.0], "Sn [MVA]": [1.0, 1.0],
    })
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        nodes.to_excel(xw, sheet_name="Nodes", index=False)
        lines.to_excel(xw, sheet_name="Lines", index=False)
        line_types.to_excel(xw, sheet_name="Line Types", index=False)
        trafos.to_excel(xw, sheet_name="Transformers", index=False)
        loads.to_excel(xw, sheet_name="Loads", index=False)
    return buf.getvalue()


def test_isolated_island_is_reconnected_and_solves():
    grid = convert_xlsx_bytes(_island_workbook_bytes(), steps=96)
    assert any("reconnected" in n for n in grid.notes)
    tie = [ln for ln in grid.lines["lines"] if str(ln["name"]).startswith("TIE_")]
    assert len(tie) == 1  # one synthetic tie to the busbar

    sim = Simulator(_as_input_data(grid))
    sim.run_step(40)
    assert not np.isnan(sim.net.res_bus.vm_pu).any()  # every bus is now supplied


def test_islands_left_disconnected_when_disabled():
    grid = convert_xlsx_bytes(_island_workbook_bytes(), steps=96, reconnect_islands=False)
    assert not any("reconnected" in n for n in grid.notes)
    sim = Simulator(_as_input_data(grid))
    sim.run_step(40)
    assert np.isnan(sim.net.res_bus.vm_pu).any()  # island stays unsupplied (NaN)


def _as_input_data(grid, steps: int = 96) -> InputData:
    data = InputData(
        grid=GridStructure.model_validate(grid.grid_structure),
        lines=Lines.model_validate(grid.lines),
        load=LoadFile.model_validate(grid.load),
        generation=GenerationFile.model_validate(grid.generation),
        substation=SubstationFile.model_validate(grid.substation),
    )
    return data


def test_convert_minimal_builds_and_solves():
    grid = convert_xlsx_bytes(_workbook_bytes(), steps=96)
    assert len(grid.grid_structure["buses"]) == 3
    assert len(grid.lines["lines"]) == 1
    assert len(grid.lines["transformers"]) == 1
    assert len(grid.load["loads"]) == 1
    assert grid.notes == []  # nothing auto-created

    # tbd transformer electrical fields fall back to defaults
    tr = grid.lines["transformers"][0]
    assert tr["vk_percent"] == 4.0 and tr["vkr_percent"] == 1.0
    assert tr["hv_bus"] == 0 and tr["lv_bus"] == 1  # HV/LV oriented by voltage

    # converts cleanly through pydantic, builds, and solves
    sim = Simulator(_as_input_data(grid))
    res = sim.run_step(40)
    assert res.converged
    assert len(sim.net.trafo) == 1

    # transformer results surface in StepResult + summary
    assert len(res.trafos) == 1
    tr_res = res.trafos[0]
    assert tr_res["hv_bus"] == 0 and tr_res["lv_bus"] == 1
    assert tr_res["loading_percent"] >= 0.0
    assert res.summary["n_trafo"] == 1
    assert res.summary["max_trafo_loading_percent"] == tr_res["loading_percent"]


def test_missing_endpoint_is_auto_created():
    grid = convert_xlsx_bytes(_workbook_bytes(extra_line_to_missing=True), steps=96)
    assert len(grid.notes) == 1 and "N9" in grid.notes[0]
    names = [b["name"] for b in grid.grid_structure["buses"]]
    assert "N9" in names                      # appended after the declared nodes
    assert names.index("N9") == 3
    Simulator(_as_input_data(grid)).run_step(0)  # still solvable


def test_profile_lengths_match_steps():
    grid = convert_xlsx_bytes(_workbook_bytes(), steps=1440)
    assert grid.load["steps"] == 1440
    assert len(grid.load["loads"][0]["p_mw"]) == 1440
    assert len(grid.substation["substations"][0]["vm_pu"]) == 1440


@pytest.mark.skipif(not ARCHIVE.exists(), reason="archive zip not present")
def test_real_archetype_grid_end_to_end():
    grid = convert_from_zip(str(ARCHIVE), "network_10_1_1137", steps=1440)
    sim = Simulator(_as_input_data(grid))
    assert len(sim.net.bus) >= 148
    assert len(sim.net.trafo) == 1
    res = sim.run_step(1140)  # evening peak
    assert res.converged
    assert 0.0 < sim.net.res_trafo.loading_percent.iloc[0] < 200.0
