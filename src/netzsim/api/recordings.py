"""Session recording (every published step -> CSV pack, recorder.py) and the
bulk export that replays whole days offline into a pack (exporter.py)."""
from __future__ import annotations

import asyncio
import copy

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .runtime import _recording_meta, runtime

router = APIRouter()


@router.get("/recording")
def recording_status():
    """State of the session recorder (active recording, steps, size)."""
    return runtime.recorder.status()


class RecordingStartRequest(BaseModel):
    name: str | None = None


@router.post("/recording/start")
def recording_start(req: RecordingStartRequest | None = None):
    """Start recording every published step to data/recordings/<id>/."""
    try:
        return runtime.recorder.start(_recording_meta(),
                                      name=req.name if req else None)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))


@router.post("/recording/stop")
def recording_stop():
    """Finish the active recording (flush, close, write metadata.json)."""
    out = runtime.recorder.stop()
    if out is None:
        raise HTTPException(409, "no recording is active")
    return out


@router.get("/recordings")
def recordings():
    """Stored recordings (finished ones carry metadata.json)."""
    return {"recordings": runtime.recorder.list(),
            "active": runtime.recorder.status()}


def _busy_ids() -> set:
    """Packs that are being written right now (live recording or bulk export)."""
    return {runtime.recorder.status().get("id"), runtime.exporter.active_id} - {None}


@router.get("/recordings/{rid}/download")
def recording_download(rid: str):
    """The recording as a ZIP of CSVs + metadata.json."""
    if rid in _busy_ids():
        raise HTTPException(409, "recording is still being written — stop it first")
    try:
        zp = runtime.recorder.pack(rid)
    except KeyError:
        raise HTTPException(404, f"unknown recording '{rid}'")
    return FileResponse(zp, media_type="application/zip", filename=f"{rid}.zip")


@router.delete("/recordings/{rid}")
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


@router.post("/export/days")
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


@router.get("/export")
def export_status():
    """Progress of the bulk export (steps done/total, ETA, errors)."""
    return runtime.exporter.status()


@router.post("/export/cancel")
def export_cancel():
    """Stop the running bulk export; the partial pack is kept and finalized."""
    try:
        return runtime.exporter.cancel()
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
