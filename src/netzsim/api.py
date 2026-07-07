"""FastAPI application: REST control plane + WebSocket live result stream."""
from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from .config import settings
from .data_loader import input_data_from_dicts, load_inputs
from .engine import RealtimeEngine
from .estimator import EstConfig
from .battery import MODES
from .grid_catalog import GridCatalog, preview
from .measurements import METER_MODES, PRESETS
from .exporter import BulkExporter
from .realpv import load_prices, load_pv_days
from .recorder import Recorder
from .scenarios import ScenarioStore, _slug
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
    recorder: Recorder
    exporter: BulkExporter
    active: dict
    pv_dates: list
    pv_peak_w: float
    est_config: "EstimationConfigModel"


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
    runtime.store = StateStore(history_size=settings.history_size,
                               expose_ground_truth=settings.expose_ground_truth)
    runtime.engine = RealtimeEngine(
        simulator, runtime.store, settings.step_interval_seconds
    )
    runtime.catalog = GridCatalog(ding0_dir=settings.ding0_dir,
                                  library_manifest=settings.grid_library,
                                  user_dir=settings.user_grids_dir)
    runtime.library = LoadLibrary(settings.lpg_library_dir)
    runtime.active = _active_meta(simulator.topology(), grid_id=None, source="data_dir")
    runtime.est_config = EstimationConfigModel()   # DSO-style defaults
    # Real multi-day PV (optional): when the cache is present, PV follows measured
    # days and the UI offers a day slider.
    pv = load_pv_days(settings.real_pv_file, steps=settings.steps_per_day)
    runtime.pv_dates = pv.dates if pv else []
    runtime.pv_peak_w = pv.peak_w if pv else 0.0
    if pv:
        runtime.engine.set_pv_days(pv.shapes)
        log.info("Real PV: %d day(s) loaded from %s", pv.n_days, settings.real_pv_file)
        prices = load_prices(settings.awattar_file, pv.dates)
        if prices is not None:
            runtime.engine.set_prices(prices)
            log.info("aWATTar prices: %d day(s) loaded", len(prices))
    log.info("Grid catalog: %d grid(s); LPG library: %d archetype(s)",
             len(runtime.catalog.list()), len(runtime.library.list()))
    # session recorder: taps the store's published (projected) payload stream
    runtime.recorder = Recorder(settings.recordings_dir)
    runtime.store.set_sink(runtime.recorder.record)
    runtime.exporter = BulkExporter(settings.recordings_dir)
    if settings.record:                       # continuous operation (env opt-in)
        runtime.recorder.start(_recording_meta())
    if settings.autostart:
        runtime.engine.start_loop()
    yield
    await runtime.engine.stop()
    if runtime.exporter.active_id:
        await asyncio.to_thread(runtime.exporter.cancel)
    await asyncio.to_thread(runtime.recorder.stop)


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
@app.get("/manual")
def manual():
    """The German user manual (docs/Benutzerhandbuch.pdf), for the Hilfe menu."""
    from pathlib import Path
    p = Path(__file__).resolve().parents[2] / "docs" / "Benutzerhandbuch.pdf"
    if not p.is_file():
        raise HTTPException(404, "manual not available in this deployment")
    return FileResponse(p, media_type="application/pdf", filename="Benutzerhandbuch.pdf")


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
def node_profiles(bus: int, view: Literal["truth", "measured", "est"] = "est"):
    """Daily curves at one bus. ``view`` picks the layers the caller may see:
    truth (load/generation split + voltage), measured (only the meter's own
    quantities in the metering raster), est (all layers overlaid)."""
    sim = runtime.engine.sim
    if bus < 0 or bus not in sim.net.bus.index:
        raise HTTPException(404, f"unknown bus {bus}")
    return sim.node_profiles(bus, view=view)


@app.get("/line/{line}/profiles")
def line_profiles(line: int, view: Literal["truth", "measured", "est"] = "est"):
    """Daily current + loading curve for one line, with its rated current.
    Lines carry no meters — the measured view is deliberately empty."""
    sim = runtime.engine.sim
    if line < 0 or line not in sim.net.line.index:
        raise HTTPException(404, f"unknown line {line}")
    return sim.line_profiles(line, view=view)


@app.get("/trafo/{trafo}/profiles")
def trafo_profiles(trafo: int, view: Literal["truth", "measured", "est"] = "est"):
    """Daily power exchange + loading curve for one transformer, with its rating.
    The measured layer appears only for a metered transformer."""
    sim = runtime.engine.sim
    if trafo < 0 or trafo not in sim.net.trafo.index:
        raise HTTPException(404, f"unknown trafo {trafo}")
    return sim.trafo_profiles(trafo, view=view)


@app.get("/state")
def state():
    latest = runtime.store.latest
    if latest is None:
        raise HTTPException(404, "No result computed yet.")
    return latest


@app.get("/history")
def history(limit: int = Query(default=96, ge=1, le=10_000)):
    return runtime.store.history(limit=limit)


# --------------------------------------------------------------------------- #
# Session recording: every published step -> CSV pack on disk (recorder.py)
# --------------------------------------------------------------------------- #
def _recording_meta() -> dict:
    """The reproducibility recipe stored in a recording's metadata.json: what
    was simulated (grid + loadgen), what was measurable (placement, TAF mode),
    which estimation policy ran, and how fast the clock ticked."""
    sim = runtime.engine.sim
    return {
        "netzsim_version": app.version,
        "grid": runtime.active,
        "measurements": sim.meters.placement(sim.net),
        "estimation": dataclasses.asdict(sim.est_config),
        "engine": runtime.engine.status,
        "expose_ground_truth": settings.expose_ground_truth,
    }


@app.get("/recording")
def recording_status():
    """State of the session recorder (active recording, steps, size)."""
    return runtime.recorder.status()


class RecordingStartRequest(BaseModel):
    name: str | None = None


@app.post("/recording/start")
def recording_start(req: RecordingStartRequest | None = None):
    """Start recording every published step to data/recordings/<id>/."""
    try:
        return runtime.recorder.start(_recording_meta(),
                                      name=req.name if req else None)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))


@app.post("/recording/stop")
def recording_stop():
    """Finish the active recording (flush, close, write metadata.json)."""
    out = runtime.recorder.stop()
    if out is None:
        raise HTTPException(409, "no recording is active")
    return out


@app.get("/recordings")
def recordings():
    """Stored recordings (finished ones carry metadata.json)."""
    return {"recordings": runtime.recorder.list(),
            "active": runtime.recorder.status()}


def _busy_ids() -> set:
    """Packs that are being written right now (live recording or bulk export)."""
    return {runtime.recorder.status().get("id"), runtime.exporter.active_id} - {None}


@app.get("/recordings/{rid}/download")
def recording_download(rid: str):
    """The recording as a ZIP of CSVs + metadata.json."""
    if rid in _busy_ids():
        raise HTTPException(409, "recording is still being written — stop it first")
    try:
        zp = runtime.recorder.pack(rid)
    except KeyError:
        raise HTTPException(404, f"unknown recording '{rid}'")
    return FileResponse(zp, media_type="application/zip", filename=f"{rid}.zip")


@app.delete("/recordings/{rid}")
def recording_delete(rid: str):
    """Remove a stored recording (and its cached ZIP)."""
    if rid in _busy_ids():
        raise HTTPException(409, "recording is still being written — stop it first")
    try:
        runtime.recorder.delete(rid)
    except KeyError:
        raise HTTPException(404, f"unknown recording '{rid}'")
    return {"deleted": rid}


# --------------------------------------------------------------------------- #
# Bulk export: replay whole days offline into a recording pack (exporter.py)
# --------------------------------------------------------------------------- #
class ExportDaysRequest(BaseModel):
    """``days`` is either a count (3 → days 0..2) or an explicit list of day
    indices (real-PV days, e.g. [0, 5, 11]); day indices wrap modulo the
    available PV days, exactly like the live day counter."""
    days: int | list[int] = Field(..., description="count or explicit day indices")
    name: str | None = None
    estimate: bool = True


@app.post("/export/days")
async def export_days(req: ExportDaysRequest):
    """Simulate whole days of the CURRENT setup as fast as possible and store
    them as a recording pack (appears under /recordings when finished)."""
    if runtime.exporter.active_id:
        raise HTTPException(409, "a bulk export is already running")
    days = list(range(req.days)) if isinstance(req.days, int) else [int(d) for d in req.days]
    if not days or len(days) > 366 or any(d < 0 for d in days):
        raise HTTPException(400, "days must be 1..366 or a list of day indices >= 0")

    # take a CLEAN copy: briefly park the engine so no solve is mid-flight,
    # deep-copy off the event loop, then let the live clock tick on
    eng = runtime.engine
    was_running = eng.status["running"]
    if was_running:
        eng.pause()
        await asyncio.sleep(min(eng.interval, 1.0) + 0.1)   # drain in-flight step
    try:
        sim_copy = await asyncio.to_thread(copy.deepcopy, eng.sim)
    finally:
        if was_running:
            eng.resume()
    try:
        return runtime.exporter.start(sim_copy, _recording_meta(), days,
                                      estimate=req.estimate, name=req.name)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))


@app.get("/export")
def export_status():
    """Progress of the bulk export (steps done/total, ETA, errors)."""
    return runtime.exporter.status()


@app.post("/export/cancel")
def export_cancel():
    """Stop the running bulk export; the partial pack is kept and finalized."""
    try:
        return runtime.exporter.cancel()
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))


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


@app.post("/control/seekday")
async def control_seekday(day: int = Query(..., ge=0)):
    """Jump to a specific real-PV day (wraps within the available days)."""
    runtime.engine.seek_day(day)
    return runtime.engine.status


@app.get("/pv/days")
def pv_days():
    """Real-PV day calendar for the day slider (empty when no cache is loaded)."""
    return {"available": bool(runtime.pv_dates), "peak_w": runtime.pv_peak_w,
            "dates": runtime.pv_dates}


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


@app.get("/batteries")
def batteries():
    """Current batteries + available modes + whether price data is loaded."""
    sim = runtime.engine.sim
    return {"modes": list(MODES), "has_prices": sim.prices is not None,
            "batteries": [_battery_dict(b) for b in sim.batteries]}


@app.post("/battery")
async def add_battery(req: BatteryRequest):
    sim = runtime.engine.sim
    if req.bus not in sim.net.bus.index:
        raise HTTPException(404, f"unknown bus {req.bus}")
    if req.mode not in MODES:
        raise HTTPException(422, f"mode must be one of {MODES}")
    b = sim.add_battery(req.bus, req.capacity_kwh, req.power_kw, req.mode, req.soc0)
    return _battery_dict(b)


@app.post("/battery/{idx}/mode")
async def battery_mode(idx: int, name: str = Query(...)):
    """Switch a deployed battery's operating strategy (self | peak | price)."""
    if name not in MODES:
        raise HTTPException(422, f"name must be one of {MODES}")
    if not runtime.engine.sim.set_battery_mode(idx, name):
        raise HTTPException(404, f"no battery with index {idx}")
    return batteries()


@app.post("/battery/{idx}/size")
async def battery_size(idx: int,
                       capacity_kwh: float = Query(..., gt=0, le=10_000),
                       power_kw: float = Query(..., gt=0, le=5_000)):
    """Resize a deployed battery — standard home units at a node, freely
    chosen energy/power for a large battery at the substation busbar."""
    if not runtime.engine.sim.set_battery_size(idx, capacity_kwh, power_kw):
        raise HTTPException(404, f"no battery with index {idx}")
    return batteries()


@app.delete("/battery/{idx}")
async def remove_battery(idx: int):
    if not runtime.engine.sim.remove_battery(idx):
        raise HTTPException(404, f"no battery with index {idx}")
    return {"removed": idx}


# --------------------------------------------------------------------------- #
# Overload controllers (netzdienliche Steuerung, placed like batteries/meters)
# --------------------------------------------------------------------------- #
class ControllerRequest(BaseModel):
    scope: Literal["station", "bus"] = "station"
    bus: int | None = None                            # required for scope "bus"
    limit_pct: float = Field(100.0, ge=20, le=150)    # curtail above this loading


@app.get("/controllers")
def controllers():
    """Placed overload controllers with their live curtailment factors."""
    return {"controllers": [c.as_dict() for c in runtime.engine.sim.controllers]}


@app.post("/controller")
async def add_controller(req: ControllerRequest):
    """Place an overload controller (station = whole grid, bus = one node)."""
    try:
        c = runtime.engine.sim.add_controller(req.scope, req.bus, req.limit_pct)
    except KeyError:
        raise HTTPException(404, f"unknown bus {req.bus}")
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    return c.as_dict()


@app.post("/controller/{cid}/config")
async def controller_config(cid: int, limit_pct: float = Query(..., ge=20, le=150)):
    """Change a controller's loading limit."""
    if not runtime.engine.sim.set_controller(cid, limit_pct):
        raise HTTPException(404, f"no controller {cid}")
    return controllers()


@app.delete("/controller/{cid}")
async def remove_controller(cid: int):
    if not runtime.engine.sim.remove_controller(cid):
        raise HTTPException(404, f"no controller {cid}")
    return {"removed": cid}


@app.get("/battery/{idx}/profiles")
def battery_profiles(idx: int):
    """Daily SOC + charge/discharge curve (+ price) for one battery, current day."""
    prof = runtime.engine.sim.battery_profiles(idx)
    if prof is None:
        raise HTTPException(404, f"no battery with index {idx}")
    return prof


# --------------------------------------------------------------------------- #
# Scenarios: save the configured live setup as a recipe file; load it back
# --------------------------------------------------------------------------- #
class ScenarioSaveRequest(BaseModel):
    name: str
    description: str = ""


def _scenario_store() -> ScenarioStore:
    return ScenarioStore(settings.scenarios_dir)


@app.get("/scenarios")
def scenarios_list():
    """Saved scenarios (name, description, grid, created)."""
    return {"scenarios": _scenario_store().list()}


@app.post("/scenarios")
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
        "controllers": [{"scope": c.scope, "bus": c.bus, "limit_pct": c.limit_pct}
                        for c in sim.controllers],
        "measurements": {"node_buses": sorted(sim.meters.node_buses),
                         "trafo_idxs": sorted(sim.meters.trafo_idxs),
                         "mode": sim.meters.mode},
        "engine": {"day": st["day"], "step": st["step"],
                   "interval_seconds": st["interval_seconds"]},
    }
    sid = _scenario_store().write(doc)
    return {"id": sid, **{k: doc[k] for k in ("name", "description", "grid_id")}}


@app.delete("/scenarios/{sid}")
async def scenarios_delete(sid: str):
    if not _scenario_store().delete(sid):
        raise HTTPException(404, f"unknown scenario '{sid}'")
    return {"deleted": sid}


@app.post("/scenarios/{sid}/load")
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
                                         gen_doc, g.substation)
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
                               float(c.get("limit_pct", 100.0)))
        except Exception:  # noqa: BLE001
            log.warning("scenario '%s': skipped controller %s", sid, c)
    m = doc.get("measurements") or {}
    sim.meters.clear()
    for bus in m.get("node_buses", []):
        sim.meters.add_node(int(bus))
    for tr in m.get("trafo_idxs", []):
        sim.meters.add_trafo(int(tr))
    sim.meters.prune(sim.net)
    if m.get("mode") in ("full", "standard"):
        sim.meters.set_mode(m["mode"])

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


@app.get("/node/{bus}/der")
def node_der(bus: int):
    """The bus's configurable DERs (PV kWp, EV charge window), derived live
    from the profile rows — LoadStudio-assigned systems are editable too."""
    try:
        return runtime.engine.sim.node_der(bus)
    except KeyError:
        raise HTTPException(404, f"unknown bus {bus}")


@app.post("/pv")
async def add_pv(req: PvRequest):
    """Add a rooftop-PV system at a bus at runtime."""
    try:
        return runtime.engine.sim.add_pv(req.bus, req.kwp)
    except KeyError:
        raise HTTPException(404, f"unknown bus {req.bus}")


@app.post("/pv/{sgen}")
async def set_pv(sgen: int, kwp: float = Query(..., gt=0)):
    """Rescale a PV system's peak power (kWp)."""
    sim = runtime.engine.sim
    if not sim.set_pv_kwp(sgen, kwp):
        raise HTTPException(404, f"unknown PV system {sgen}")
    return sim.node_der(int(sim.net.sgen.at[sgen, "bus"]))


@app.delete("/pv/{sgen}")
async def remove_pv(sgen: int):
    """Remove a PV system."""
    sim = runtime.engine.sim
    bus = int(sim.net.sgen.at[sgen, "bus"]) if sgen in sim.net.sgen.index else None
    if bus is None or not sim.remove_pv(sgen):
        raise HTTPException(404, f"unknown PV system {sgen}")
    return sim.node_der(bus)


@app.delete("/ev/{load}")
async def remove_ev(load: int):
    """Remove an EV charging load."""
    sim = runtime.engine.sim
    bus = int(sim.net.load.at[load, "bus"]) if load in sim.net.load.index else None
    if bus is None or not sim.remove_ev(load):
        raise HTTPException(404, f"unknown EV load {load}")
    return sim.node_der(bus)


@app.post("/ev")
async def add_ev(req: EvRequest):
    """Add an EV home-charging load at a bus at runtime (1-4 h window)."""
    try:
        return runtime.engine.sim.add_ev(req.bus, req.kw, req.start_min, req.dur_min)
    except KeyError:
        raise HTTPException(404, f"unknown bus {req.bus}")


@app.post("/ev/{load}")
async def set_ev(load: int, start_min: int = Query(..., ge=0, lt=1440),
                 dur_min: int = Query(..., ge=60, le=240)):
    """Move an EV's charge window (start instant + 1-4 h duration)."""
    sim = runtime.engine.sim
    if not sim.set_ev(load, start_min, dur_min):
        raise HTTPException(404, f"unknown EV load {load}")
    return sim.node_der(int(sim.net.load.at[load, "bus"]))


# --------------------------------------------------------------------------- #
# Observability: measurement device placement (smart meters + transformer meters)
# --------------------------------------------------------------------------- #
def _measurements_response() -> dict:
    sim = runtime.engine.sim
    p = sim.measurement_placement()
    return {**p, "presets": list(PRESETS),
            "expose_ground_truth": settings.expose_ground_truth}


class NodeMeterRequest(BaseModel):
    bus: int


class TrafoMeterRequest(BaseModel):
    trafo: int


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


@app.get("/estimation/config")
def estimation_config():
    """The active estimation policy (an operator setting: survives grid swaps)."""
    return runtime.est_config.model_dump()


@app.post("/estimation/config")
async def set_estimation_config(cfg: EstimationConfigModel):
    """Swap the estimation policy; the estimator rebuilds its profile knowledge
    on the next solved step, so the effect is visible within seconds."""
    runtime.est_config = cfg
    runtime.engine.set_est_config(EstConfig(**cfg.model_dump()))
    return cfg.model_dump()


@app.get("/measurements")
def measurements():
    """Current meter placement + coverage + available presets. `expose_ground_truth`
    tells the UI whether the true power flow is available (reveal toggle)."""
    return _measurements_response()


@app.post("/measurements/node")
async def place_node_meter(req: NodeMeterRequest):
    """Install a smart meter at a bus (reveals its voltage, P, Q, current)."""
    try:
        runtime.engine.sim.place_node_meter(req.bus)
    except KeyError:
        raise HTTPException(404, f"unknown bus {req.bus}")
    return _measurements_response()


@app.delete("/measurements/node/{bus}")
async def remove_node_meter(bus: int):
    runtime.engine.sim.remove_node_meter(bus)
    return _measurements_response()


@app.post("/measurements/trafo")
async def place_trafo_meter(req: TrafoMeterRequest):
    """Install a measurement at a transformer (reveals its loading, HV P/Q/I)."""
    try:
        runtime.engine.sim.place_trafo_meter(req.trafo)
    except KeyError:
        raise HTTPException(404, f"unknown transformer {req.trafo}")
    return _measurements_response()


@app.delete("/measurements/trafo/{trafo}")
async def remove_trafo_meter(trafo: int):
    runtime.engine.sim.remove_trafo_meter(trafo)
    return _measurements_response()


@app.post("/measurements/mode")
async def measurements_mode(name: str = Query(...)):
    """Meter fidelity: 'full' (V/P/Q/I every step) or 'standard' (German
    Lastgang metering: 15-minute mean active power only)."""
    if name not in METER_MODES:
        raise HTTPException(422, f"name must be one of {METER_MODES}")
    runtime.engine.sim.set_meter_mode(name)
    return _measurements_response()


@app.post("/measurements/preset")
async def measurements_preset(name: str = Query(...)):
    """Bulk placement: all_nodes | all_trafos | substation_trafos | clear."""
    if name not in PRESETS:
        raise HTTPException(422, f"name must be one of {PRESETS}")
    runtime.engine.sim.apply_meter_preset(name)
    return _measurements_response()


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


class GridImportRequest(BaseModel):
    doc: dict                      # the raw grid JSON (gridformat or gridedit-mv)
    name: str | None = None        # display name; defaults to the doc's name


@app.post("/grids/import")
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


@app.get("/loadgen/archetypes")
def loadgen_archetypes():
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


@app.post("/loadgen/assign")
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


@app.post("/config/apply")
async def config_apply(req: ApplyGridRequest):
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
            g.grid_structure, g.lines, load_doc, gen_doc, g.substation
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
