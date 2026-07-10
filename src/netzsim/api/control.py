"""Engine control (start/pause/seek/interval) + the real-PV day calendar."""
from __future__ import annotations

from fastapi import APIRouter, Query

from .runtime import runtime

router = APIRouter()


# These run on the event loop (async def) — the engine schedules its loop task
# via asyncio.create_task and toggles an asyncio.Event, neither of which is safe
# from FastAPI's sync-endpoint threadpool ("no running event loop").
@router.post("/control/start")
async def control_start():
    runtime.engine.start_loop()
    return runtime.engine.status


@router.post("/control/pause")
async def control_pause():
    runtime.engine.pause()
    return runtime.engine.status


@router.post("/control/resume")
async def control_resume():
    runtime.engine.resume()
    return runtime.engine.status


@router.post("/control/seek")
async def control_seek(step: int = Query(..., ge=0)):
    runtime.engine.seek(step)
    return runtime.engine.status


@router.post("/control/interval")
async def control_interval(seconds: float = Query(..., ge=0.1, le=1.0)):
    """Set the accelerated-tick interval (real seconds per simulated step)."""
    runtime.engine.set_interval(seconds)
    return runtime.engine.status


@router.post("/control/seekday")
async def control_seekday(day: int = Query(..., ge=0)):
    """Jump to a specific real-PV day (wraps within the available days)."""
    runtime.engine.seek_day(day)
    return runtime.engine.status


@router.get("/pv/days")
def pv_days():
    """Real-PV day calendar for the day slider (empty when no cache is loaded)."""
    return {"available": bool(runtime.pv_dates), "peak_w": runtime.pv_peak_w,
            "dates": runtime.pv_dates}
