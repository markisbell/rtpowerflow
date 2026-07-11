"""External nodes: the live P/Q feed interface for individual buses
(``docs/EXTERNAL_NODES.md``). Clients push setpoints; the engine applies the
latest value per tick (sample-and-hold). No authentication by design (local
teaching tool) — protection is validation only, bounded per node by
``p_max_kw``."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .runtime import runtime

router = APIRouter()


class ExtCreateRequest(BaseModel):
    bus: int
    name: str | None = None
    hold_s: float = Field(30.0, ge=1, le=3600)     # staleness threshold [s]
    on_timeout: Literal["hold", "zero"] = "hold"
    p_max_kw: float = Field(50.0, gt=0, le=1000)   # value bound + pseudo width


class ExtValueRequest(BaseModel):
    p_kw: float                                     # signed: + load, − feed-in
    q_kvar: float = 0.0


class ExtBatchValue(ExtValueRequest):
    id: int


@router.get("/ext")
def ext_nodes():
    """Placed external nodes with their live status (applied value, telegram
    age, stale flag)."""
    return {"ext_nodes": [x.as_dict() for x in runtime.engine.sim.ext_nodes]}


@router.post("/ext")
async def add_ext_node(req: ExtCreateRequest):
    """Attach an external node at a bus: from now on the bus's injection is
    fully controlled by pushed values (one node per bus)."""
    try:
        x = runtime.engine.sim.add_ext_node(req.bus, req.name, req.hold_s,
                                            req.on_timeout, req.p_max_kw)
    except KeyError:
        raise HTTPException(404, f"unknown bus {req.bus}")
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return x.as_dict()


@router.get("/ext/{eid}/history")
def ext_history(eid: int):
    """The node's received-value day ring: the APPLIED kW per step of the
    current day (null = the engine has not passed that step since the node
    was placed). This is the node's 'day graph' — an external node has no
    forecast, only what actually arrived."""
    x = next((n for n in runtime.engine.sim.ext_nodes if n.eid == eid), None)
    if x is None:
        raise HTTPException(404, f"no external node {eid}")
    return {"id": x.eid, "bus": x.bus, "name": x.name,
            "steps_per_day": len(x.history), "p_kw": x.history}


@router.put("/ext/{eid}/value")
async def set_ext_value(eid: int, req: ExtValueRequest):
    """THE hot path: push a setpoint into the node's mailbox (latest wins;
    the engine samples it non-blocking on its next tick)."""
    try:
        x = runtime.engine.sim.set_ext_value(eid, req.p_kw, req.q_kvar)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    if x is None:
        raise HTTPException(404, f"no external node {eid}")
    return x.as_dict()


@router.post("/ext/values")
async def set_ext_values(values: list[ExtBatchValue]):
    """Batch variant for multi-node feeders — tolerant per entry (unknown ids
    and out-of-bound values are reported, valid entries still apply)."""
    sim = runtime.engine.sim
    updated, errors = [], []
    for v in values:
        try:
            x = sim.set_ext_value(v.id, v.p_kw, v.q_kvar)
        except ValueError as exc:
            errors.append({"id": v.id, "error": str(exc)})
            continue
        if x is None:
            errors.append({"id": v.id, "error": "unknown external node"})
        else:
            updated.append(x.as_dict())
    return {"updated": updated, "errors": errors}


@router.delete("/ext/{eid}")
async def remove_ext_node(eid: int):
    """Detach an external node (the bus falls back to its grid profile — for
    an external node's zeroed row that means 0 kW)."""
    if not runtime.engine.sim.remove_ext_node(eid):
        raise HTTPException(404, f"no external node {eid}")
    return {"removed": eid}
