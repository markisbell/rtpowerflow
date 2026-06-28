"""Pydantic schemas describing the five input JSON files.

Bus references everywhere are integer indices that match the *order* of the
buses listed in ``grid_structure.json`` (which is exactly how pandapower assigns
bus indices). This keeps the format native to pandapower while staying readable.
"""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field, model_validator


# --------------------------------------------------------------------------- #
# grid_structure.json
# --------------------------------------------------------------------------- #
class BusSpec(BaseModel):
    name: str
    vn_kv: float = Field(..., gt=0, description="Nominal voltage [kV]")
    type: str = "b"  # 'b' busbar, 'n' node
    zone: Optional[str] = None
    in_service: bool = True


class GridStructure(BaseModel):
    name: str = "grid"
    f_hz: float = 50.0
    buses: List[BusSpec]


# --------------------------------------------------------------------------- #
# lines.json
# --------------------------------------------------------------------------- #
class LineSpec(BaseModel):
    name: Optional[str] = None
    from_bus: int
    to_bus: int
    length_km: float = Field(..., gt=0)

    # Either reference a pandapower std_type ...
    std_type: Optional[str] = None
    # ... or give explicit per-km parameters.
    r_ohm_per_km: Optional[float] = None
    x_ohm_per_km: Optional[float] = None
    c_nf_per_km: Optional[float] = None
    max_i_ka: Optional[float] = None
    parallel: int = 1
    in_service: bool = True

    @model_validator(mode="after")
    def _check_params(self) -> "LineSpec":
        if self.std_type is None:
            missing = [
                k
                for k in ("r_ohm_per_km", "x_ohm_per_km", "c_nf_per_km", "max_i_ka")
                if getattr(self, k) is None
            ]
            if missing:
                raise ValueError(
                    f"line {self.name or (self.from_bus, self.to_bus)}: "
                    f"give 'std_type' or all of {missing}"
                )
        return self


class TransformerSpec(BaseModel):
    name: Optional[str] = None
    hv_bus: int
    lv_bus: int

    # Either reference a pandapower std_type ...
    std_type: Optional[str] = None
    # ... or give explicit physical parameters (native to
    # ``create_transformer_from_parameters``).
    sn_mva: Optional[float] = None
    vn_hv_kv: Optional[float] = None
    vn_lv_kv: Optional[float] = None
    vk_percent: Optional[float] = None
    vkr_percent: Optional[float] = None
    pfe_kw: Optional[float] = None
    i0_percent: Optional[float] = None
    shift_degree: float = 0.0
    parallel: int = 1
    in_service: bool = True

    @model_validator(mode="after")
    def _check_params(self) -> "TransformerSpec":
        if self.std_type is None:
            missing = [
                k
                for k in (
                    "sn_mva", "vn_hv_kv", "vn_lv_kv",
                    "vk_percent", "vkr_percent", "pfe_kw", "i0_percent",
                )
                if getattr(self, k) is None
            ]
            if missing:
                raise ValueError(
                    f"transformer {self.name or (self.hv_bus, self.lv_bus)}: "
                    f"give 'std_type' or all of {missing}"
                )
        return self


class Lines(BaseModel):
    lines: List[LineSpec] = Field(default_factory=list)
    transformers: List[TransformerSpec] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Time-series profiles (load.json / generation.json / substation.json)
# --------------------------------------------------------------------------- #
class _Profile(BaseModel):
    """Common base: a named element bound to a bus with per-step arrays."""

    name: Optional[str] = None
    bus: int


class LoadProfile(_Profile):
    p_mw: List[float]
    q_mvar: Optional[List[float]] = None


class GenProfile(_Profile):
    p_mw: List[float]
    q_mvar: Optional[List[float]] = None


class SubstationProfile(_Profile):
    """Connection to an upper grid layer, modelled as an ext_grid (slack).

    The voltage set-point per step is the native lever; optionally the active
    power exchange limits can be provided for documentation/limits checks.
    """

    vm_pu: List[float]
    va_degree: Optional[List[float]] = None


class _ProfileFile(BaseModel):
    resolution_minutes: int = 15
    steps: int = 96

    @model_validator(mode="after")
    def _check_steps(self) -> "_ProfileFile":
        for attr in ("loads", "gens", "substations"):
            items = getattr(self, attr, None)
            if not items:
                continue
            for it in items:
                for field in ("p_mw", "q_mvar", "vm_pu", "va_degree"):
                    arr = getattr(it, field, None)
                    if arr is not None and len(arr) != self.steps:
                        raise ValueError(
                            f"{attr} '{it.name}': '{field}' has {len(arr)} "
                            f"values, expected {self.steps}"
                        )
        return self


class LoadFile(_ProfileFile):
    loads: List[LoadProfile]


class GenerationFile(_ProfileFile):
    gens: List[GenProfile] = Field(default_factory=list, alias="generation")

    model_config = {"populate_by_name": True}


class SubstationFile(_ProfileFile):
    substations: List[SubstationProfile]
