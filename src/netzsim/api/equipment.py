"""Runtime equipment placed while the simulation runs: local battery storage,
overload controllers (netzdienliche Steuerung), rONTs, and the per-node
configurable DERs (rooftop PV, EV charge windows)."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..battery import MODES
from .runtime import runtime

router = APIRouter()


# --------------------------------------------------------------------------- #
# Local battery storage (manually placed at runtime)
# --------------------------------------------------------------------------- #
def _battery_dict(b) -> dict:
    return {"index": b.storage_idx, "bus": b.bus, "name": b.name, "mode": b.mode,
            "capacity_kwh": round(b.capacity_mwh * 1000, 3), "power_kw": round(b.power_mw * 1000, 3),
            "soc_percent": round(b.soc_frac() * 100, 2)}


class BatteryRequest(BaseModel):
    bus: int
    capacity_kwh: float = Field(10.0, gt=0, le=1000)
    power_kw: float = Field(5.0, gt=0, le=500)
    mode: str = "self"
    soc0: float = Field(0.5, ge=0, le=1)


@router.get("/batteries")
def batteries():
    """Current batteries + available modes + whether price data is loaded."""
    sim = runtime.engine.sim
    return {"modes": list(MODES), "has_prices": sim.prices is not None,
            "batteries": [_battery_dict(b) for b in sim.batteries]}


@router.post("/battery")
async def add_battery(req: BatteryRequest):
    """Deploy a battery at a bus (strategy self | peak | price)."""
    sim = runtime.engine.sim
    if req.bus not in sim.net.bus.index:
        raise HTTPException(404, f"unknown bus {req.bus}")
    if req.mode not in MODES:
        raise HTTPException(422, f"mode must be one of {MODES}")
    b = sim.add_battery(req.bus, req.capacity_kwh, req.power_kw, req.mode, req.soc0)
    return _battery_dict(b)


@router.post("/battery/{idx}/mode")
async def battery_mode(idx: int, name: str = Query(...)):
    """Switch a deployed battery's operating strategy (self | peak | price)."""
    if name not in MODES:
        raise HTTPException(422, f"name must be one of {MODES}")
    if not runtime.engine.sim.set_battery_mode(idx, name):
        raise HTTPException(404, f"no battery with index {idx}")
    return batteries()


@router.post("/battery/{idx}/size")
async def battery_size(idx: int,
                       capacity_kwh: float = Query(..., gt=0, le=10_000),
                       power_kw: float = Query(..., gt=0, le=5_000)):
    """Resize a deployed battery — standard home units at a node, freely
    chosen energy/power for a large battery at the substation busbar."""
    if not runtime.engine.sim.set_battery_size(idx, capacity_kwh, power_kw):
        raise HTTPException(404, f"no battery with index {idx}")
    return batteries()


@router.delete("/battery/{idx}")
async def remove_battery(idx: int):
    """Remove a deployed battery."""
    if not runtime.engine.sim.remove_battery(idx):
        raise HTTPException(404, f"no battery with index {idx}")
    return {"removed": idx}


@router.get("/battery/{idx}/profiles")
def battery_profiles(idx: int):
    """Daily SOC + charge/discharge curve (+ price) for one battery, current day."""
    prof = runtime.engine.sim.battery_profiles(idx)
    if prof is None:
        raise HTTPException(404, f"no battery with index {idx}")
    return prof


# --------------------------------------------------------------------------- #
# Overload controllers (netzdienliche Steuerung, placed like batteries/meters)
# --------------------------------------------------------------------------- #
class ControllerRequest(BaseModel):
    scope: Literal["station", "bus", "cell", "mv"] = "station"
    bus: int | None = None                            # required for scope "bus"
    cell: str | None = None                           # required for scope "cell"
    limit_pct: float = Field(100.0, ge=20, le=150)    # curtail above this loading


@router.get("/controllers")
def controllers():
    """Placed overload controllers with their live curtailment factors."""
    return {"controllers": [c.as_dict() for c in runtime.engine.sim.controllers]}


@router.post("/controller")
async def add_controller(req: ControllerRequest):
    """Place an overload controller (station = whole grid, bus = one node,
    cell = one spliced ONS cell, mv = the coordinating MV level)."""
    try:
        c = runtime.engine.sim.add_controller(req.scope, req.bus, req.limit_pct,
                                              cell=req.cell)
    except KeyError as exc:
        raise HTTPException(404, f"unknown domain {exc}")
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return c.as_dict()


@router.post("/controller/{cid}/config")
async def controller_config(cid: int, limit_pct: float = Query(..., ge=20, le=150)):
    """Change a controller's loading limit."""
    if not runtime.engine.sim.set_controller(cid, limit_pct):
        raise HTTPException(404, f"no controller {cid}")
    return controllers()


@router.delete("/controller/{cid}")
async def remove_controller(cid: int):
    """Remove an overload controller."""
    if not runtime.engine.sim.remove_controller(cid):
        raise HTTPException(404, f"no controller {cid}")
    return {"removed": cid}


# --------------------------------------------------------------------------- #
# rONT (regelbarer Ortsnetztrafo): on-load tap changer per station transformer
# --------------------------------------------------------------------------- #
class RontRequest(BaseModel):
    trafo: int
    v_target: float = Field(1.0, ge=0.9, le=1.1)      # busbar setpoint, pu
    deadband: float = Field(0.015, ge=0.005, le=0.05)  # half band, pu


@router.get("/ronts")
def ronts():
    """Activated rONTs with their live tap positions."""
    return {"ronts": [r.as_dict() for r in runtime.engine.sim.ronts]}


@router.post("/ront")
async def add_ront(req: RontRequest):
    """Activate an rONT on a station transformer (one per trafo)."""
    try:
        r = runtime.engine.sim.add_ront(req.trafo, req.v_target, req.deadband)
    except KeyError:
        raise HTTPException(404, f"unknown trafo {req.trafo}")
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return r.as_dict()


@router.post("/ront/{rid}/config")
async def ront_config(rid: int,
                      v_target: float | None = Query(None, ge=0.9, le=1.1),
                      deadband: float | None = Query(None, ge=0.005, le=0.05)):
    """Change an rONT's voltage setpoint / deadband."""
    if not runtime.engine.sim.set_ront(rid, v_target, deadband):
        raise HTTPException(404, f"no rONT {rid}")
    return ronts()


@router.delete("/ront/{rid}")
async def remove_ront(rid: int):
    """Deactivate an rONT (the transformer's original tap data is restored)."""
    if not runtime.engine.sim.remove_ront(rid):
        raise HTTPException(404, f"no rONT {rid}")
    return {"removed": rid}


# --------------------------------------------------------------------------- #
# Runtime-configurable DERs: PV size + EV charge window per node
# --------------------------------------------------------------------------- #
class PvRequest(BaseModel):
    bus: int
    kwp: float = Field(5.0, gt=0, le=100)


class EvRequest(BaseModel):
    bus: int
    kw: float = Field(11.0, gt=0, le=50)
    start_min: int = Field(18 * 60, ge=0, lt=1440)
    dur_min: int = Field(120, ge=60, le=240)


@router.get("/node/{bus}/der")
def node_der(bus: int):
    """The bus's configurable DERs (PV kWp, EV charge window), derived live
    from the profile rows — LoadStudio-assigned systems are editable too."""
    try:
        return runtime.engine.sim.node_der(bus)
    except KeyError:
        raise HTTPException(404, f"unknown bus {bus}")


@router.post("/pv")
async def add_pv(req: PvRequest):
    """Add a rooftop-PV system at a bus at runtime."""
    try:
        return runtime.engine.sim.add_pv(req.bus, req.kwp)
    except KeyError:
        raise HTTPException(404, f"unknown bus {req.bus}")


@router.post("/pv/{sgen}")
async def set_pv(sgen: int, kwp: float = Query(..., gt=0)):
    """Rescale a PV system's peak power (kWp)."""
    sim = runtime.engine.sim
    if not sim.set_pv_kwp(sgen, kwp):
        raise HTTPException(404, f"unknown PV system {sgen}")
    return sim.node_der(int(sim.net.sgen.at[sgen, "bus"]))


@router.delete("/pv/{sgen}")
async def remove_pv(sgen: int):
    """Remove a PV system."""
    sim = runtime.engine.sim
    bus = int(sim.net.sgen.at[sgen, "bus"]) if sgen in sim.net.sgen.index else None
    if bus is None or not sim.remove_pv(sgen):
        raise HTTPException(404, f"unknown PV system {sgen}")
    return sim.node_der(bus)


@router.post("/ev")
async def add_ev(req: EvRequest):
    """Add an EV home-charging load at a bus at runtime (1-4 h window)."""
    try:
        return runtime.engine.sim.add_ev(req.bus, req.kw, req.start_min, req.dur_min)
    except KeyError:
        raise HTTPException(404, f"unknown bus {req.bus}")


@router.post("/ev/{load}")
async def set_ev(load: int, start_min: int = Query(..., ge=0, lt=1440),
                 dur_min: int = Query(..., ge=60, le=240)):
    """Move an EV's charge window (start instant + 1-4 h duration)."""
    sim = runtime.engine.sim
    if not sim.set_ev(load, start_min, dur_min):
        raise HTTPException(404, f"unknown EV load {load}")
    return sim.node_der(int(sim.net.load.at[load, "bus"]))


@router.delete("/ev/{load}")
async def remove_ev(load: int):
    """Remove an EV charging load."""
    sim = runtime.engine.sim
    bus = int(sim.net.load.at[load, "bus"]) if load in sim.net.load.index else None
    if bus is None or not sim.remove_ev(load):
        raise HTTPException(404, f"unknown EV load {load}")
    return sim.node_der(bus)
