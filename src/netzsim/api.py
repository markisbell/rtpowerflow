"""FastAPI application: REST control plane + WebSocket live result stream."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .config import settings
from .data_loader import input_data_from_dicts, load_inputs
from .engine import RealtimeEngine
from .grid_catalog import GridCatalog, preview
from .loadgen import (
    AssignPolicy,
    EvPolicy,
    LoadLibrary,
    PvPolicy,
    assign_ev,
    assign_pv,
    assign_to_loads,
)
from .simulator import Simulator
from .state import StateStore

log = logging.getLogger("netzsim.api")


class App:
    """Container for the long-lived runtime objects."""

    store: StateStore
    engine: RealtimeEngine
    catalog: GridCatalog
    library: LoadLibrary
    active: dict


runtime = App()


def _active_meta(topo: dict, *, grid_id, source, category=None, notes=None) -> dict:
    return {
        "grid_id": grid_id,
        "name": topo["name"],
        "category": category,
        "source": source,
        "n_bus": len(topo["buses"]),
        "n_line": len(topo["lines"]),
        "n_trafo": topo["n_trafo"],
        "n_load": topo["n_load"],
        "notes": notes or [],
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Loading inputs from %s", settings.data_dir)
    data = load_inputs(settings.data_dir)
    simulator = Simulator(data, warm_start=settings.warm_start)
    runtime.store = StateStore(history_size=settings.history_size)
    runtime.engine = RealtimeEngine(
        simulator, runtime.store, settings.step_interval_seconds
    )
    runtime.catalog = GridCatalog(ding0_dir=settings.ding0_dir,
                                  library_manifest=settings.grid_library)
    runtime.library = LoadLibrary(settings.lpg_library_dir)
    runtime.active = _active_meta(simulator.topology(), grid_id=None, source="data_dir")
    log.info("Grid catalog: %d grid(s); LPG library: %d archetype(s)",
             len(runtime.catalog.list()), len(runtime.library.list()))
    if settings.autostart:
        runtime.engine.start_loop()
    yield
    await runtime.engine.stop()


app = FastAPI(title="netzsim", version="0.2.0", lifespan=lifespan)

_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins or ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- #
# REST
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/status")
def status():
    return runtime.engine.status


@app.get("/network")
def network():
    return runtime.engine.sim.topology()


@app.get("/node/{bus}/profiles")
def node_profiles(bus: int):
    """Daily load/generation + voltage curves at one bus (residential / EV / PV)."""
    sim = runtime.engine.sim
    if bus < 0 or bus not in sim.net.bus.index:
        raise HTTPException(404, f"unknown bus {bus}")
    return sim.node_profiles(bus)


@app.get("/line/{line}/profiles")
def line_profiles(line: int):
    """Daily current + loading curve for one line, with its rated current."""
    sim = runtime.engine.sim
    if line < 0 or line not in sim.net.line.index:
        raise HTTPException(404, f"unknown line {line}")
    return sim.line_profiles(line)


@app.get("/trafo/{trafo}/profiles")
def trafo_profiles(trafo: int):
    """Daily power exchange + loading curve for one transformer, with its rating."""
    sim = runtime.engine.sim
    if trafo < 0 or trafo not in sim.net.trafo.index:
        raise HTTPException(404, f"unknown trafo {trafo}")
    return sim.trafo_profiles(trafo)


@app.get("/state")
def state():
    latest = runtime.store.latest
    if latest is None:
        raise HTTPException(404, "No result computed yet.")
    return latest


@app.get("/history")
def history(limit: int = Query(default=96, ge=1, le=10_000)):
    return runtime.store.history(limit=limit)


# These run on the event loop (async def) — the engine schedules its loop task
# via asyncio.create_task and toggles an asyncio.Event, neither of which is safe
# from FastAPI's sync-endpoint threadpool ("no running event loop").
@app.post("/control/start")
async def control_start():
    runtime.engine.start_loop()
    return runtime.engine.status


@app.post("/control/pause")
async def control_pause():
    runtime.engine.pause()
    return runtime.engine.status


@app.post("/control/resume")
async def control_resume():
    runtime.engine.resume()
    return runtime.engine.status


@app.post("/control/seek")
async def control_seek(step: int = Query(..., ge=0)):
    runtime.engine.seek(step)
    return runtime.engine.status


@app.post("/control/interval")
async def control_interval(seconds: float = Query(..., ge=0.1, le=1.0)):
    """Set the accelerated-tick interval (real seconds per simulated step)."""
    runtime.engine.set_interval(seconds)
    return runtime.engine.status


# --------------------------------------------------------------------------- #
# Grid catalog + runtime grid swap
# --------------------------------------------------------------------------- #
@app.get("/grids")
def grids():
    return {
        "available": runtime.catalog.available,
        "grids": runtime.catalog.list(),
    }


@app.get("/grids/{grid_id}")
def grid_preview(grid_id: str):
    if not runtime.catalog.has(grid_id):
        raise HTTPException(404, f"unknown grid '{grid_id}'")
    g = runtime.catalog.get_inputs(grid_id, steps=settings.steps_per_day)
    return preview(g)


# --------------------------------------------------------------------------- #
# Load generation (cached LPG archetype library)
# --------------------------------------------------------------------------- #
class LoadgenPolicy(BaseModel):
    archetypes: list[str] | None = None
    mode: str = "round_robin"          # "round_robin" | "random"
    seed: int = 0
    scale: float = 1.0
    power_factor: float = 0.95
    jitter_minutes: int = 0
    ev_penetration: float = 0.0        # fraction of homes with an EV (synthetic)
    ev_charger_kw: float = 11.0        # wallbox power
    ev_daily_kwh: float = 8.0          # mean energy charged per day
    pv_penetration: float = 0.0        # fraction of load buses with rooftop PV
    pv_kwp: float = 5.0                # peak kW per PV system


def _assign_policy(p: LoadgenPolicy) -> AssignPolicy:
    return AssignPolicy(
        archetypes=p.archetypes, mode=p.mode, seed=p.seed, scale=p.scale,
        power_factor=p.power_factor, jitter_minutes=p.jitter_minutes,
    )


@app.get("/loadgen/archetypes")
def loadgen_archetypes():
    return {
        "available": runtime.library.available,
        "ev_available": True,  # synthetic model, no library needed
        "steps": runtime.library.steps,
        "archetypes": runtime.library.list(),
    }


def _assigned_load_doc(g, policy: LoadgenPolicy) -> dict:
    """Base household loads + any synthetic EV charging loads (additive)."""
    if not runtime.library.available:
        raise HTTPException(409, "no LPG library built; run scripts/build_lpg_library.py")
    try:
        doc = assign_to_loads(
            g.load["loads"], runtime.library, _assign_policy(policy),
            steps=settings.steps_per_day,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    n_ev = 0
    if policy.ev_penetration > 0:
        ev = assign_ev(
            g.load["loads"],
            EvPolicy(penetration=policy.ev_penetration, charger_kw=policy.ev_charger_kw,
                     daily_kwh=policy.ev_daily_kwh, seed=policy.seed),
            steps=settings.steps_per_day,
        )
        doc["loads"] = doc["loads"] + ev["loads"]
        n_ev = len(ev["loads"])
    doc["n_ev"] = n_ev
    return doc


def _pv_gen_doc(g, policy: LoadgenPolicy) -> dict | None:
    if policy.pv_penetration <= 0:
        return None
    return assign_pv(
        g.load["loads"], PvPolicy(penetration=policy.pv_penetration,
                                  kwp=policy.pv_kwp, seed=policy.seed),
        steps=settings.steps_per_day,
    )


class AssignRequest(BaseModel):
    grid_id: str
    policy: LoadgenPolicy = LoadgenPolicy()


@app.post("/loadgen/assign")
def loadgen_assign(req: AssignRequest):
    """Preview LPG load + EV + PV assignment (deterministic given the policy)."""
    if not runtime.catalog.has(req.grid_id):
        raise HTTPException(404, f"unknown grid '{req.grid_id}'")
    g = runtime.catalog.get_inputs(req.grid_id, steps=settings.steps_per_day)
    assigned = _assigned_load_doc(g, req.policy)
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
        "archetypes_used": sorted({a["archetype"] for a in assigned["assignments"]}),
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


@app.post("/config/apply")
async def config_apply(req: ApplyGridRequest):
    if not runtime.catalog.has(req.grid_id):
        raise HTTPException(404, f"unknown grid '{req.grid_id}'")
    try:
        g = runtime.catalog.get_inputs(req.grid_id, steps=settings.steps_per_day)
        gen_doc = g.generation
        n_ev = n_pv = 0
        if req.loadgen is not None:
            assigned = _assigned_load_doc(g, req.loadgen)
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
            g.grid_structure, g.lines, load_doc, gen_doc, g.substation
        )
    except HTTPException:
        raise
    except Exception as exc:  # conversion / validation failure
        raise HTTPException(400, f"failed to load grid '{req.grid_id}': {exc}")

    await runtime.engine.reconfigure(data)
    topo = runtime.engine.sim.topology()
    runtime.active = _active_meta(
        topo, grid_id=req.grid_id, source="catalog",
        category=runtime.catalog._entries[req.grid_id].category, notes=g.notes,
    )
    runtime.active.update(load_source=load_source, n_ev=n_ev, n_pv=n_pv)
    return {"status": runtime.engine.status, "active": runtime.active, "network": topo}


@app.get("/config/active")
def config_active():
    return runtime.active


# --------------------------------------------------------------------------- #
# WebSocket: pushes every solved step as JSON
# --------------------------------------------------------------------------- #
@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    await runtime.store.subscribe(websocket)
    # send latest immediately so a fresh client isn't blank until the next tick
    if runtime.store.latest:
        await websocket.send_json(runtime.store.latest)
    try:
        while True:
            await websocket.receive_text()  # keepalive / ignore client msgs
    except WebSocketDisconnect:
        pass
    finally:
        await runtime.store.unsubscribe(websocket)


# --------------------------------------------------------------------------- #
# Tiny built-in monitor page (no build step required)
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def index():
    return """<!doctype html><html><head><meta charset="utf-8">
<title>netzsim</title>
<style>
 body{font-family:system-ui,sans-serif;margin:1.5rem;background:#0f1115;color:#e6e6e6}
 h1{font-size:1.2rem} table{border-collapse:collapse;margin-top:.5rem}
 td,th{border:1px solid #333;padding:.2rem .5rem;font-variant-numeric:tabular-nums}
 .k{color:#7fd1ff} .bad{color:#ff6b6b}
</style></head><body>
<h1>netzsim &mdash; realtime power flow</h1>
<div id="hdr">connecting&hellip;</div>
<div id="sum"></div>
<table id="lines"><thead><tr><th>line</th><th>loading %</th><th>I [kA]</th></tr></thead><tbody></tbody></table>
<script>
const ws=new WebSocket(`ws://${location.host}/ws`);
ws.onmessage=e=>{const d=JSON.parse(e.data);
 document.getElementById('hdr').innerHTML=
  `<span class="k">day</span> ${d.day} &nbsp; <span class="k">t</span> ${d.time_of_day} `+
  `(step ${d.step}) &nbsp; ${d.converged?'converged':'<span class=bad>NOT converged</span>'} `+
  `&nbsp; ${d.solve_ms} ms`;
 const s=d.summary||{};
 document.getElementById('sum').innerHTML= d.converged?
  `Vmin ${s.vm_pu_min} / Vmax ${s.vm_pu_max} pu &nbsp;|&nbsp; max line ${s.max_line_loading_percent}% `+
  `&nbsp;|&nbsp; load ${s.total_load_mw} MW, gen ${s.total_gen_mw} MW, slack ${s.total_ext_grid_mw} MW, loss ${s.total_losses_mw} MW`:'';
 const tb=document.querySelector('#lines tbody');tb.innerHTML='';
 (d.lines||[]).forEach(l=>{const tr=document.createElement('tr');
  const cls=l.loading_percent>100?' class=bad':'';
  tr.innerHTML=`<td>${l.name??l.index}</td><td${cls}>${l.loading_percent}</td><td>${l.i_ka}</td>`;tb.appendChild(tr);});
};
ws.onclose=()=>document.getElementById('hdr').textContent='disconnected';
</script></body></html>"""
