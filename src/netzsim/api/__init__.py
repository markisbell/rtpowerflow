"""FastAPI application: REST control plane + WebSocket live result stream.

The surface is split into routers by area (one module per area, see below);
this package assembles them into the ``app`` and owns the lifespan that
builds the long-lived runtime objects. ``from netzsim.api import app`` and
the request models keep working exactly as when this was one module."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ..config import settings
from ..data_loader import load_inputs
from ..engine import RealtimeEngine
from ..exporter import BulkExporter
from ..grid_catalog import GridCatalog
from ..loadgen import LoadLibrary
from ..realpv import load_prices, load_pv_days
from ..recorder import Recorder
from ..simulator import Simulator
from ..state import StateStore
from . import control, core, equipment, grids, measurements, recordings, scenarios
from .measurements import EstimationConfigModel
from .runtime import API_VERSION, App, _active_meta, _recording_meta, runtime

log = logging.getLogger("netzsim.api")


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


app = FastAPI(title="netzsim", version=API_VERSION, lifespan=lifespan)

_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins or ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(core.router)
app.include_router(control.router)
app.include_router(recordings.router)
app.include_router(equipment.router)
app.include_router(measurements.router)
app.include_router(grids.router)
app.include_router(scenarios.router)

# Back-compat facade: everything external code imported from the old
# single-module ``netzsim.api`` stays importable from the package root.
from .equipment import (  # noqa: E402
    BatteryRequest,
    ControllerRequest,
    EvRequest,
    PvRequest,
    RontRequest,
)
from .grids import (  # noqa: E402
    ApplyGridRequest,
    AssignRequest,
    GridImportRequest,
    LoadgenPolicy,
    _households_range,
)
from .measurements import NodeMeterRequest, TrafoMeterRequest  # noqa: E402
from .recordings import ExportDaysRequest, RecordingStartRequest  # noqa: E402
from .scenarios import ScenarioSaveRequest  # noqa: E402

__all__ = [
    "API_VERSION",
    "App",
    "ApplyGridRequest",
    "AssignRequest",
    "BatteryRequest",
    "ControllerRequest",
    "EstimationConfigModel",
    "EvRequest",
    "ExportDaysRequest",
    "GridImportRequest",
    "LoadgenPolicy",
    "NodeMeterRequest",
    "PvRequest",
    "RecordingStartRequest",
    "RontRequest",
    "ScenarioSaveRequest",
    "TrafoMeterRequest",
    "app",
    "runtime",
]
