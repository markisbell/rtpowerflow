"""Grid catalog (committed dataset + user grids), LPG load generation, and the
runtime grid swap (/config/apply)."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..config import settings
from ..data_loader import input_data_from_dicts
from ..grid_catalog import preview
from ..loadgen import (
    AssignPolicy,
    EvPolicy,
    PvPolicy,
    assign_ev,
    assign_pv,
    assign_to_loads,
)
from ..scenarios import _slug
from .runtime import _active_meta, _recording_meta, runtime

log = logging.getLogger("netzsim.api")

router = APIRouter()


# --------------------------------------------------------------------------- #
# Grid catalog + import
# --------------------------------------------------------------------------- #
@router.get("/grids")
def grids():
    """List the importable grids of the committed dataset + user imports."""
    return {
        "available": runtime.catalog.available,
        "grids": runtime.catalog.list(),
    }


@router.get("/grids/{grid_id}")
def grid_preview(grid_id: str):
    """Net-free topology preview of a catalog grid (+ converter notes)."""
    if not runtime.catalog.has(grid_id):
        raise HTTPException(404, f"unknown grid '{grid_id}'")
    g = runtime.catalog.get_inputs(grid_id, steps=settings.steps_per_day)
    return preview(g)


class GridImportRequest(BaseModel):
    doc: dict                      # the raw grid JSON (gridformat or gridedit-mv)
    name: str | None = None        # display name; defaults to the doc's name


@router.post("/grids/import")
def grids_import(req: GridImportRequest):
    """Import a grid file (gridgen/gridedit JSON) into the user catalog.

    The document is written to ``user_grids/`` and validated by actually
    converting it; a file that does not convert is removed again (400)."""
    base = _slug(req.name or str(req.doc.get("name") or "import"))
    directory = Path(settings.user_grids_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path, n = directory / f"{base}.json", 1
    while path.exists():
        n += 1
        path = directory / f"{base}-{n}.json"
    path.write_text(json.dumps(req.doc, indent=2, ensure_ascii=False),
                    encoding="utf-8")
    gid = f"user_{path.stem}"
    try:
        if not runtime.catalog.has(gid):        # triggers the user-dir rescan
            raise ValueError("file not recognized by the catalog")
        g = runtime.catalog.get_inputs(gid, steps=settings.steps_per_day)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — conversion failed: reject upload
        path.unlink(missing_ok=True)
        runtime.catalog._scan_user()
        raise HTTPException(400, f"not an importable grid file: {exc}")
    p = preview(g)
    return {"id": gid, "name": req.doc.get("name") or path.stem,
            "n_bus": p["n_bus"], "n_load": p["n_load"], "notes": g.notes}


def _trafo_sn_mva(g) -> float | None:
    """Summed transformer rating of a grid (std-type lookup, best effort)."""
    try:
        from pandapower.std_types import basic_std_types
        types = basic_std_types()["trafo"]
    except Exception:  # noqa: BLE001
        return None
    total = 0.0
    for t in g.lines.get("transformers", []):
        st = t.get("std_type")
        if st in types:
            total += float(types[st].get("sn_mva", 0.0))
    return round(total, 4) if total > 0 else None


# --------------------------------------------------------------------------- #
# Load generation (cached LPG archetype library)
# --------------------------------------------------------------------------- #
class LoadgenPolicy(BaseModel):
    archetypes: list[str] | None = None
    mode: str = "round_robin"          # "round_robin" | "random"
    seed: int = Field(0, ge=0)
    scale: float = Field(1.0, gt=0, le=10)
    power_factor: float = Field(0.95, ge=0.5, le=1.0)
    jitter_minutes: int = Field(0, ge=0, le=120)
    ev_penetration: float = Field(0.0, ge=0, le=1)   # fraction of homes with an EV
    ev_charger_kw: float = Field(11.0, gt=0, le=50)  # wallbox power
    ev_charger_mix: bool = False                     # random 3.7/11/22 kW per EV
    ev_daily_kwh: float = Field(8.0, gt=0, le=100)   # mean energy charged per day
    pv_penetration: float = Field(0.0, ge=0, le=1)   # fraction of load buses with PV
    pv_kwp: float = Field(5.0, gt=0, le=100)         # peak kW per PV system
    pv_mix: bool = False                             # random size + orientation per system
    # multi-family buildings: sum mfh_min..mfh_max household profiles per load.
    # "auto" applies it to suburban/urban grids only; default "off" keeps
    # existing recipes (saved scenarios) bit-identical.
    mfh: Literal["auto", "off", "on"] = "off"
    mfh_min: int = Field(3, ge=1, le=12)
    mfh_max: int = Field(6, ge=1, le=20)


def _grid_character(grid_id: str) -> str | None:
    e = runtime.catalog._entries.get(grid_id)
    return e.character if e else None


def _households_range(p: LoadgenPolicy, character: str | None) -> tuple[int, int] | None:
    if p.mfh == "off" or p.mfh_max < p.mfh_min:
        return None
    if p.mfh == "auto" and character not in ("suburban", "urban"):
        return None
    return (p.mfh_min, p.mfh_max)


def _assign_policy(p: LoadgenPolicy, character: str | None = None) -> AssignPolicy:
    return AssignPolicy(
        archetypes=p.archetypes, mode=p.mode, seed=p.seed, scale=p.scale,
        power_factor=p.power_factor, jitter_minutes=p.jitter_minutes,
        households_range=_households_range(p, character),
    )


@router.get("/loadgen/archetypes")
def loadgen_archetypes():
    """List the cached LPG household archetypes (steps, metadata)."""
    return {
        "available": runtime.library.available,
        "ev_available": True,  # synthetic model, no library needed
        "steps": runtime.library.steps,
        "archetypes": runtime.library.list(),
    }


def _household_loads(g) -> list[dict]:
    """The loads that represent real households — LPG/EV/PV attach only to these.
    Interconnected districts flag building loads ``household: true`` and lumped
    station / MV loads ``false``; grids without flags are all-household (LV)."""
    return [ld for ld in g.load["loads"] if ld.get("household", True)]


def _assigned_load_doc(g, policy: LoadgenPolicy, character: str | None = None) -> dict:
    """Base household loads + any synthetic EV charging loads (additive). Loads
    that are not households (lumped LV stations in a district) pass through with
    their aggregate profiles untouched."""
    if not runtime.library.available:
        raise HTTPException(409, "no LPG library built; run scripts/build_lpg_library.py")
    households = _household_loads(g)
    try:
        doc = assign_to_loads(
            households, runtime.library, _assign_policy(policy, character),
            steps=settings.steps_per_day,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    n_ev = 0
    ev_buses: set[int] = set()
    if policy.ev_penetration > 0:
        ev = assign_ev(
            households,
            EvPolicy(penetration=policy.ev_penetration, charger_kw=policy.ev_charger_kw,
                     charger_mix=policy.ev_charger_mix,
                     daily_kwh=policy.ev_daily_kwh, seed=policy.seed),
            steps=settings.steps_per_day,
        )
        doc["loads"] = doc["loads"] + ev["loads"]
        n_ev = len(ev["loads"])
        ev_buses = {int(ld["bus"]) for ld in ev["loads"]}
    # flag each household assignment that got an EV (the UI contract carries it)
    doc["assignments"] = [dict(a, ev=int(a["bus"]) in ev_buses)
                          for a in doc["assignments"]]
    fixed = [ld for ld in g.load["loads"] if not ld.get("household", True)]
    doc["loads"] = doc["loads"] + [
        {k: ld[k] for k in ("name", "bus", "p_mw", "q_mvar", "household") if k in ld}
        for ld in fixed]
    doc["n_ev"] = n_ev
    return doc


def _pv_gen_doc(g, policy: LoadgenPolicy) -> dict | None:
    if policy.pv_penetration <= 0:
        return None
    return assign_pv(
        _household_loads(g), PvPolicy(penetration=policy.pv_penetration,
                                      kwp=policy.pv_kwp, mix=policy.pv_mix,
                                      seed=policy.seed),
        steps=settings.steps_per_day,
    )


class AssignRequest(BaseModel):
    grid_id: str
    policy: LoadgenPolicy = LoadgenPolicy()


@router.post("/loadgen/assign")
def loadgen_assign(req: AssignRequest):
    """Preview LPG load + EV + PV assignment (deterministic given the policy)."""
    if not runtime.catalog.has(req.grid_id):
        raise HTTPException(404, f"unknown grid '{req.grid_id}'")
    g = runtime.catalog.get_inputs(req.grid_id, steps=settings.steps_per_day)
    assigned = _assigned_load_doc(g, req.policy, _grid_character(req.grid_id))
    pv = _pv_gen_doc(g, req.policy)

    import numpy as np
    steps = assigned["steps"]
    load = np.array([ld["p_mw"] for ld in assigned["loads"]]).sum(axis=0)
    pv_total = (np.array([gn["p_mw"] for gn in pv["generation"]]).sum(axis=0)
                if pv and pv["generation"] else np.zeros(steps))
    net = load - pv_total
    return {
        "grid_id": req.grid_id,
        "steps": steps,
        "n_load": len(assigned["loads"]),
        "n_ev": assigned["n_ev"],
        "n_pv": len(pv["generation"]) if pv else 0,
        "n_households": int(sum(a.get("households") or 1
                                for a in assigned["assignments"])),
        "n_mfh": int(sum(1 for a in assigned["assignments"]
                         if (a.get("households") or 1) > 1)),
        "archetypes_used": sorted({a["archetype"] for a in assigned["assignments"]}),
        "trafo_sn_mva": _trafo_sn_mva(g),   # None if no std-type rating known
        "total_load_p_mw": [round(float(x), 6) for x in load],
        "total_pv_p_mw": [round(float(x), 6) for x in pv_total],
        "net_p_mw": [round(float(x), 6) for x in net],
        "peak_load_mw": round(float(load.max()), 6),
        "peak_net_mw": round(float(net.max()), 6),
        "min_net_mw": round(float(net.min()), 6),  # negative => reverse flow
        "mean_load_mw": round(float(load.mean()), 6),
        "assignments": assigned["assignments"],
    }


# --------------------------------------------------------------------------- #
# Runtime grid swap (optionally with LPG loads + EV + PV)
# --------------------------------------------------------------------------- #
class ApplyGridRequest(BaseModel):
    grid_id: str
    loadgen: LoadgenPolicy | None = None


@router.post("/config/apply")
async def config_apply(req: ApplyGridRequest):
    """Convert a catalog grid (optionally with LPG loads + EV + PV) and swap
    the running engine onto it."""
    if not runtime.catalog.has(req.grid_id):
        raise HTTPException(404, f"unknown grid '{req.grid_id}'")
    try:
        g = runtime.catalog.get_inputs(req.grid_id, steps=settings.steps_per_day)
        gen_doc = g.generation
        n_ev = n_pv = 0
        if req.loadgen is not None:
            assigned = _assigned_load_doc(g, req.loadgen, _grid_character(req.grid_id))
            load_doc = {k: assigned[k] for k in ("resolution_minutes", "steps", "loads")}
            load_source = "lpg"
            n_ev = assigned["n_ev"]
            pv = _pv_gen_doc(g, req.loadgen)
            if pv is not None:
                gen_doc = pv
                n_pv = len(pv["generation"])
        else:
            load_doc = g.load
            load_source = "placeholder"
        data = input_data_from_dicts(
            g.grid_structure, g.lines, load_doc, gen_doc, g.substation,
            cells=g.cells,
        )
    except HTTPException:
        raise
    except Exception as exc:  # conversion / validation failure
        raise HTTPException(400, f"failed to load grid '{req.grid_id}': {exc}")

    # a recording documents ONE configuration — finish it before the swap
    await asyncio.to_thread(runtime.recorder.stop)
    await runtime.engine.reconfigure(data)
    topo = runtime.engine.sim.topology()
    runtime.active = _active_meta(
        topo, grid_id=req.grid_id, source="catalog",
        category=runtime.catalog._entries[req.grid_id].category, notes=g.notes,
    )
    runtime.active.update(load_source=load_source, n_ev=n_ev, n_pv=n_pv,
                          loadgen=req.loadgen.model_dump() if req.loadgen else None)
    if settings.record:                 # continuous operation: one file per setup
        runtime.recorder.start(_recording_meta())
    return {"status": runtime.engine.status, "active": runtime.active, "network": topo}


@router.get("/config/active")
def config_active():
    """Metadata of the currently loaded grid (id, counts, load source, notes)."""
    return runtime.active
