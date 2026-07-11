"""Scenarios: save the configured live setup as a recipe file; load it back
(grid + loadgen + runtime DER ops + batteries/controllers/rONTs + meters +
the engine clock)."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import settings
from ..data_loader import input_data_from_dicts, load_inputs
from ..scenarios import ScenarioStore
from .grids import LoadgenPolicy, _assigned_load_doc, _grid_character, _pv_gen_doc
from .runtime import _active_meta, _recording_meta, runtime

log = logging.getLogger("netzsim.api")

router = APIRouter()


class ScenarioSaveRequest(BaseModel):
    name: str
    description: str = ""


def _scenario_store() -> ScenarioStore:
    return ScenarioStore(settings.scenarios_dir)


@router.get("/scenarios")
def scenarios_list():
    """Saved scenarios (name, description, grid, created)."""
    return {"scenarios": _scenario_store().list()}


@router.post("/scenarios")
async def scenarios_save(req: ScenarioSaveRequest):
    """Save the CURRENT live setup as a scenario recipe: grid + loadgen policy
    + runtime DER ops + batteries + meters + the engine clock."""
    if not req.name.strip():
        raise HTTPException(422, "scenario name must not be empty")
    sim = runtime.engine.sim
    st = runtime.engine.status
    doc = {
        "name": req.name.strip(),
        "description": req.description.strip(),
        "grid_id": runtime.active.get("grid_id"),
        "loadgen": runtime.active.get("loadgen"),
        "der_ops": list(sim.der_log),
        "batteries": [{"bus": b.bus, "capacity_kwh": round(b.capacity_mwh * 1000, 3),
                       "power_kw": round(b.power_mw * 1000, 3), "mode": b.mode}
                      for b in sim.batteries],
        "controllers": [{"scope": c.scope, "bus": c.bus, "cell": c.cell,
                         "limit_pct": c.limit_pct}
                        for c in sim.controllers],
        "ronts": [{"trafo": r.trafo, "v_target": r.v_target,
                   "deadband": r.deadband}
                  for r in sim.ronts],
        # external nodes: the PLACEMENT is part of the recipe, the live
        # mailbox values are not (a loaded scenario starts silent/stale)
        "ext_nodes": [{"bus": x.bus, "name": x.name, "hold_s": x.hold_s,
                       "on_timeout": x.on_timeout, "p_max_kw": x.p_max_kw}
                      for x in sim.ext_nodes],
        "measurements": {"node_buses": sorted(sim.meters.node_buses),
                         "trafo_idxs": sorted(sim.meters.trafo_idxs),
                         "mode": sim.meters.mode,
                         # per-device TAF overrides (only where they differ)
                         "node_modes": {str(b): m for b, m in sorted(sim.meters.node_modes.items())},
                         "trafo_modes": {str(tr): m for tr, m in sorted(sim.meters.trafo_modes.items())}},
        "engine": {"day": st["day"], "step": st["step"],
                   "interval_seconds": st["interval_seconds"]},
    }
    sid = _scenario_store().write(doc)
    return {"id": sid, **{k: doc[k] for k in ("name", "description", "grid_id")}}


@router.delete("/scenarios/{sid}")
async def scenarios_delete(sid: str):
    if not _scenario_store().delete(sid):
        raise HTTPException(404, f"unknown scenario '{sid}'")
    return {"deleted": sid}


@router.post("/scenarios/{sid}/load")
async def scenarios_load(sid: str):
    """Replay a scenario recipe: apply grid + loads, then the runtime layers
    (DER ops, batteries, meters), seek to the stored clock and run."""
    doc = _scenario_store().read(sid)
    if doc is None:
        raise HTTPException(404, f"unknown scenario '{sid}'")

    # 1) grid + load configuration (the deterministic base)
    gid = doc.get("grid_id")
    n_ev = n_pv = 0
    load_source = "placeholder"
    notes: list = []
    try:
        if gid:
            if not runtime.catalog.has(gid):
                raise HTTPException(409, f"scenario references unknown grid '{gid}'")
            g = runtime.catalog.get_inputs(gid, steps=settings.steps_per_day)
            notes = g.notes
            gen_doc = g.generation
            policy = LoadgenPolicy(**doc["loadgen"]) if doc.get("loadgen") else None
            if policy is not None:
                assigned = _assigned_load_doc(g, policy, _grid_character(gid))
                load_doc = {k: assigned[k] for k in ("resolution_minutes", "steps", "loads")}
                load_source = "lpg"
                n_ev = assigned["n_ev"]
                pv = _pv_gen_doc(g, policy)
                if pv is not None:
                    gen_doc = pv
                    n_pv = len(pv["generation"])
            else:
                load_doc = g.load
            data = input_data_from_dicts(g.grid_structure, g.lines, load_doc,
                                         gen_doc, g.substation, cells=g.cells)
        else:
            data = load_inputs(settings.data_dir)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"failed to load scenario '{sid}': {exc}")

    # a recording documents ONE configuration — finish it before the swap
    await asyncio.to_thread(runtime.recorder.stop)
    await runtime.engine.reconfigure(data, autostart=False)
    sim = runtime.engine.sim

    # 2) runtime layers, tolerant per entry (a hand-edited file may not match)
    for op in doc.get("der_ops", []):
        try:
            sim.apply_der_op(op)
        except Exception:  # noqa: BLE001
            log.warning("scenario '%s': skipped DER op %s", sid, op)
    for b in doc.get("batteries", []):
        try:
            sim.add_battery(int(b["bus"]), float(b.get("capacity_kwh", 10.0)),
                            float(b.get("power_kw", 5.0)), b.get("mode", "self"))
        except Exception:  # noqa: BLE001
            log.warning("scenario '%s': skipped battery %s", sid, b)
    for c in doc.get("controllers", []):
        try:
            sim.add_controller(c.get("scope", "station"), c.get("bus"),
                               float(c.get("limit_pct", 100.0)),
                               cell=c.get("cell"))
        except Exception:  # noqa: BLE001
            log.warning("scenario '%s': skipped controller %s", sid, c)
    for r in doc.get("ronts", []):
        try:
            sim.add_ront(int(r["trafo"]), float(r.get("v_target", 1.0)),
                         float(r.get("deadband", 0.015)))
        except Exception:  # noqa: BLE001
            log.warning("scenario '%s': skipped rONT %s", sid, r)
    for x in doc.get("ext_nodes", []):
        try:
            sim.add_ext_node(int(x["bus"]), x.get("name"),
                             float(x.get("hold_s", 30.0)),
                             x.get("on_timeout", "hold"),
                             float(x.get("p_max_kw", 50.0)))
        except Exception:  # noqa: BLE001
            log.warning("scenario '%s': skipped external node %s", sid, x)
    m = doc.get("measurements") or {}
    sim.meters.clear()
    for bus in m.get("node_buses", []):
        sim.meters.add_node(int(bus))
    for tr in m.get("trafo_idxs", []):
        sim.meters.add_trafo(int(tr))
    sim.meters.prune(sim.net)
    if m.get("mode") in ("full", "standard"):
        sim.meters.set_mode(m["mode"])
    for bus, md in (m.get("node_modes") or {}).items():   # per-device overrides
        try:
            sim.meters.set_node_mode(int(bus), md)
        except (KeyError, ValueError):
            log.warning("scenario '%s': skipped node meter mode %s=%s", sid, bus, md)
    for tr, md in (m.get("trafo_modes") or {}).items():
        try:
            sim.meters.set_trafo_mode(int(tr), md)
        except (KeyError, ValueError):
            log.warning("scenario '%s': skipped trafo meter mode %s=%s", sid, tr, md)

    # 3) the engine clock, then run
    eng = doc.get("engine") or {}
    if eng.get("interval_seconds"):
        runtime.engine.set_interval(min(max(float(eng["interval_seconds"]), 0.1), 1.0))
    runtime.engine.seek_day(int(eng.get("day", 0)))
    runtime.engine.seek(int(eng.get("step", 0)))
    runtime.engine.start_loop()

    topo = sim.topology()
    runtime.active = _active_meta(
        topo, grid_id=gid, source="scenario",
        category=runtime.catalog._entries[gid].category if gid and runtime.catalog.has(gid) else None,
        notes=notes)
    runtime.active.update(load_source=load_source, n_ev=n_ev, n_pv=n_pv,
                          loadgen=doc.get("loadgen"), scenario=doc.get("name"))
    if settings.record:                 # continuous operation: one file per setup
        runtime.recorder.start(_recording_meta())
    return {"status": runtime.engine.status, "active": runtime.active, "network": topo}
