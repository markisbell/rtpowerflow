"""Load and validate the five input JSON files into typed models."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .models import (
    GenerationFile,
    GridStructure,
    Lines,
    LoadFile,
    SubstationFile,
)

FILES = {
    "grid": "grid_structure.json",
    "lines": "lines.json",
    "load": "load.json",
    "generation": "generation.json",
    "substation": "substation.json",
}


@dataclass
class InputData:
    grid: GridStructure
    lines: Lines
    load: LoadFile
    generation: GenerationFile
    substation: SubstationFile

    @property
    def steps_per_day(self) -> int:
        return self.load.steps


def _read_json(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Required input file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def load_inputs(data_dir: str | Path) -> InputData:
    """Read, parse and cross-validate every input file in ``data_dir``."""
    d = Path(data_dir)
    return input_data_from_dicts(
        grid=_read_json(d / FILES["grid"]),
        lines=_read_json(d / FILES["lines"]),
        load=_read_json(d / FILES["load"]),
        generation=_read_json(d / FILES["generation"]),
        substation=_read_json(d / FILES["substation"]),
    )


def input_data_from_dicts(
    grid: dict,
    lines: dict,
    load: dict,
    generation: dict,
    substation: dict,
) -> InputData:
    """Validate + cross-validate the five input documents already in memory.

    Used both by :func:`load_inputs` (from JSON files) and by the runtime
    grid-swap path (from a freshly converted grid model).
    """
    data = InputData(
        grid=GridStructure.model_validate(grid),
        lines=Lines.model_validate(lines),
        load=LoadFile.model_validate(load),
        generation=GenerationFile.model_validate(generation),
        substation=SubstationFile.model_validate(substation),
    )
    _cross_validate(data)
    return data


def _cross_validate(data: InputData) -> None:
    n_bus = len(data.grid.buses)

    def _check_bus(ref: int, where: str) -> None:
        if not (0 <= ref < n_bus):
            raise ValueError(f"{where}: bus index {ref} out of range [0, {n_bus})")

    for ln in data.lines.lines:
        _check_bus(ln.from_bus, f"line {ln.name}")
        _check_bus(ln.to_bus, f"line {ln.name}")
    for tr in data.lines.transformers:
        _check_bus(tr.hv_bus, f"trafo {tr.name}")
        _check_bus(tr.lv_bus, f"trafo {tr.name}")
    for el in data.load.loads:
        _check_bus(el.bus, f"load {el.name}")
    for el in data.generation.gens:
        _check_bus(el.bus, f"gen {el.name}")
    for el in data.substation.substations:
        _check_bus(el.bus, f"substation {el.name}")

    # All profiles must share the same number of steps.
    steps = {
        "load": data.load.steps,
        "generation": data.generation.steps,
        "substation": data.substation.steps,
    }
    if len(set(steps.values())) != 1:
        raise ValueError(f"Profiles disagree on step count: {steps}")

    if not data.substation.substations:
        raise ValueError("At least one substation (slack/ext_grid) is required.")
