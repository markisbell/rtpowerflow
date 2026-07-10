"""Observability: measurement-device placement (smart meters + transformer
meters, per-device TAF fidelity) and the state-estimation policy."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..config import settings
from ..estimator import EstConfig
from ..measurements import METER_MODES, PRESETS
from .runtime import runtime

router = APIRouter()


def _measurements_response() -> dict:
    sim = runtime.engine.sim
    p = sim.measurement_placement()
    return {**p, "presets": list(PRESETS),
            "expose_ground_truth": settings.expose_ground_truth}


class NodeMeterRequest(BaseModel):
    bus: int
    mode: Literal["full", "standard"] | None = None   # per-device TAF mode


class TrafoMeterRequest(BaseModel):
    trafo: int
    mode: Literal["full", "standard"] | None = None   # per-device TAF mode


# --------------------------------------------------------------------------- #
# State-estimation policy (UI tab "Schätzung"): what the WLS estimator may use.
# Defaults mirror real DSO practice — no PV / EV pseudo-measurements.
# --------------------------------------------------------------------------- #
class EstimationConfigModel(BaseModel):
    pv_pseudo: bool = False
    ev_pseudo: bool = False
    load_basis: Literal["profile", "slp"] = "profile"
    slp_annual_kwh: float = Field(4000.0, ge=500, le=20000)
    pseudo_std_pct: float = Field(50.0, ge=5, le=300)
    zero_injection: bool = True
    # vertical estimation: two-stage cell/MV WLS on districts ("auto" uses it
    # whenever the grid has spliced ONS cells and a real MV level)
    hierarchy: Literal["auto", "monolithic", "hierarchical"] = "auto"


@router.get("/estimation/config")
def estimation_config():
    """The active estimation policy (an operator setting: survives grid swaps)."""
    return runtime.est_config.model_dump()


@router.post("/estimation/config")
async def set_estimation_config(cfg: EstimationConfigModel):
    """Swap the estimation policy; the estimator rebuilds its profile knowledge
    on the next solved step, so the effect is visible within seconds."""
    runtime.est_config = cfg
    runtime.engine.set_est_config(EstConfig(**cfg.model_dump()))
    return cfg.model_dump()


@router.get("/measurements")
def measurements():
    """Current meter placement + coverage + available presets. `expose_ground_truth`
    tells the UI whether the true power flow is available (reveal toggle)."""
    return _measurements_response()


@router.post("/measurements/node")
async def place_node_meter(req: NodeMeterRequest):
    """Install a smart meter at a bus (upsert: an optional ``mode`` sets or
    changes the device's TAF fidelity — full = TAF 9/10/14 1-min telemetry,
    standard = TAF 7 15-min Lastgang)."""
    try:
        runtime.engine.sim.place_node_meter(req.bus)
        if req.mode:
            runtime.engine.sim.set_node_meter_mode(req.bus, req.mode)
    except KeyError:
        raise HTTPException(404, f"unknown bus {req.bus}")
    return _measurements_response()


@router.delete("/measurements/node/{bus}")
async def remove_node_meter(bus: int):
    runtime.engine.sim.remove_node_meter(bus)
    return _measurements_response()


@router.post("/measurements/trafo")
async def place_trafo_meter(req: TrafoMeterRequest):
    """Install a measurement at a transformer (upsert; optional per-device
    TAF ``mode``, see /measurements/node)."""
    try:
        runtime.engine.sim.place_trafo_meter(req.trafo)
        if req.mode:
            runtime.engine.sim.set_trafo_meter_mode(req.trafo, req.mode)
    except KeyError:
        raise HTTPException(404, f"unknown transformer {req.trafo}")
    return _measurements_response()


@router.delete("/measurements/trafo/{trafo}")
async def remove_trafo_meter(trafo: int):
    runtime.engine.sim.remove_trafo_meter(trafo)
    return _measurements_response()


@router.post("/measurements/mode")
async def measurements_mode(name: str = Query(...)):
    """Meter fidelity: 'full' (V/P/Q/I every step) or 'standard' (German
    Lastgang metering: 15-minute mean active power only)."""
    if name not in METER_MODES:
        raise HTTPException(422, f"name must be one of {METER_MODES}")
    runtime.engine.sim.set_meter_mode(name)
    return _measurements_response()


@router.post("/measurements/preset")
async def measurements_preset(name: str = Query(...), cell: str | None = Query(None)):
    """Bulk placement: all_nodes | all_trafos | substation_trafos |
    digital_stations | cell_full | clear. ``digital_stations`` = one station
    measurement per ONS cell; ``cell_full`` (requires ``cell=<id>``) = full
    SMGW rollout of that one cell."""
    if name not in PRESETS:
        raise HTTPException(422, f"name must be one of {PRESETS}")
    if name == "cell_full" and not cell:
        raise HTTPException(422, "preset 'cell_full' requires the 'cell' parameter")
    try:
        runtime.engine.sim.apply_meter_preset(name, cell=cell)
    except KeyError:
        raise HTTPException(404, f"unknown cell '{cell}'")
    return _measurements_response()
