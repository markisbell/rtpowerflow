"""Load and validate the five input JSON files into typed models."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
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
    # vertical MV/LV structure: one dict per ONS cell (see GridInputs.cells).
    # Runtime metadata from the importers — not part of the five-file contract,
    # so file-based grids (load_inputs) simply have none.
    cells: list[dict] = field(default_factory=list)

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
    cells: list[dict] | None = None,
) -> InputData:
    """Validate + cross-validate the five input documents already in memory.

    Used both by :func:`load_inputs` (from JSON files) and by the runtime
    grid-swap path (from a freshly converted grid model, which may also carry
    the vertical ONS-``cells`` structure).
    """
    data = InputData(
        grid=GridStructure.model_validate(grid),
        lines=Lines.model_validate(lines),
        load=LoadFile.model_validate(load),
        generation=GenerationFile.model_validate(generation),
        substation=SubstationFile.model_validate(substation),
        cells=list(cells or []),
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

    n_trafo = len(data.lines.transformers)
    seen_ids: set[str] = set()
    for c in data.cells:
        cid = str(c.get("id") or "")
        if not cid or cid in seen_ids:
            raise ValueError(f"cell '{cid}': missing or duplicate id")
        seen_ids.add(cid)
        for b in c.get("buses", []):
            _check_bus(int(b), f"cell {cid}")
        for key in ("lv_busbar", "mv_bus"):
            if c.get(key) is not None:
                _check_bus(int(c[key]), f"cell {cid} {key}")
        for ti in c.get("station_trafos", []):
            if not (0 <= int(ti) < n_trafo):
                raise ValueError(f"cell {cid}: trafo index {ti} out of range [0, {n_trafo})")

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
