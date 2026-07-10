"""Long-lived runtime objects shared by every API router, plus the two small
metadata helpers several router areas need (active-grid meta, the recording
reproducibility recipe)."""
from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

from ..config import settings

if TYPE_CHECKING:  # annotations only — assigned in the app's lifespan
    from ..engine import RealtimeEngine
    from ..exporter import BulkExporter
    from ..grid_catalog import GridCatalog
    from ..loadgen import LoadLibrary
    from ..recorder import Recorder
    from ..state import StateStore
    from .measurements import EstimationConfigModel

API_VERSION = "0.2.0"


class App:
    """Container for the long-lived runtime objects."""

    store: "StateStore"
    engine: "RealtimeEngine"
    catalog: "GridCatalog"
    library: "LoadLibrary"
    recorder: "Recorder"
    exporter: "BulkExporter"
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


def _recording_meta() -> dict:
    """The reproducibility recipe stored in a recording's metadata.json: what
    was simulated (grid + loadgen), what was measurable (placement, TAF mode),
    which estimation policy ran, and how fast the clock ticked."""
    sim = runtime.engine.sim
    return {
        "netzsim_version": API_VERSION,
        "grid": runtime.active,
        "measurements": sim.meters.placement(sim.net),
        "estimation": dataclasses.asdict(sim.est_config),
        "engine": runtime.engine.status,
        "expose_ground_truth": settings.expose_ground_truth,
    }
