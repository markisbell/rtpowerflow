"""Core surface: monitor page, health, topology, live results, per-element
day curves, and the WebSocket step stream."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse

from .. import __version__
from ..config import settings
from .runtime import runtime

router = APIRouter()

# Strict observability (§12): with NETZSIM_EXPOSE_GROUND_TRUTH=false the wire
# must not carry the solved truth. StateStore already strips /state, /ws and
# /history; the day-graph endpoints are gated HERE — a requested truth view
# downgrades to the measured layer (mirroring the UI's own fallback), and the
# est view keeps its estimate/measured layers but loses the truth arrays.
_TRUTH_KEYS = {"node": ("series", "voltage"),
               "line": ("current", "loading"),
               "trafo": ("power", "loading")}


def _gate_view(view: str) -> str:
    return "measured" if view == "truth" and not settings.expose_ground_truth else view


def _gate_profiles(out: dict, kind: str) -> dict:
    if not settings.expose_ground_truth and out.get("view") == "est":
        for k in _TRUTH_KEYS[kind]:
            out[k] = []
    return out


@router.get("/manual")
def manual():
    """The German user manual (docs/Benutzerhandbuch.pdf), for the Hilfe menu."""
    p = Path(__file__).resolve().parents[3] / "docs" / "Benutzerhandbuch.pdf"
    if not p.is_file():
        raise HTTPException(404, "manual not available in this deployment")
    return FileResponse(p, media_type="application/pdf", filename="Benutzerhandbuch.pdf")


@router.get("/health")
def health():
    """Liveness + identity probe. `app` identifies netzsim when several
    services share a port range (e.g. netzsim next to rtheatflow) — the dev
    launcher only treats a listener as "already running" if it answers
    with app=netzsim, otherwise it moves to the next free port."""
    return {"status": "ok", "app": "netzsim", "version": __version__}


@router.get("/status")
def status():
    """Engine state: running, step, day, steps_per_day, tick interval, n_days."""
    return runtime.engine.status


@router.get("/network")
def network():
    """Static topology: buses (with layout coordinates), lines, transformers,
    ext_grids, ONS cells, element counts — the id directory for all other calls."""
    return runtime.engine.sim.topology()


@router.get("/node/{bus}/profiles")
def node_profiles(bus: int, view: Literal["truth", "measured", "est"] = "est"):
    """Daily curves at one bus. ``view`` picks the layers the caller may see:
    truth (load/generation split + voltage), measured (only the meter's own
    quantities in the metering raster), est (all layers overlaid)."""
    sim = runtime.engine.sim
    if bus < 0 or bus not in sim.net.bus.index:
        raise HTTPException(404, f"unknown bus {bus}")
    return _gate_profiles(sim.node_profiles(bus, view=_gate_view(view)), "node")


@router.get("/line/{line}/profiles")
def line_profiles(line: int, view: Literal["truth", "measured", "est"] = "est"):
    """Daily current + loading curve for one line, with its rated current.
    Lines carry no meters — the measured view is deliberately empty."""
    sim = runtime.engine.sim
    if line < 0 or line not in sim.net.line.index:
        raise HTTPException(404, f"unknown line {line}")
    return _gate_profiles(sim.line_profiles(line, view=_gate_view(view)), "line")


@router.get("/trafo/{trafo}/profiles")
def trafo_profiles(trafo: int, view: Literal["truth", "measured", "est"] = "est"):
    """Daily power exchange + loading curve for one transformer, with its rating.
    The measured layer appears only for a metered transformer."""
    sim = runtime.engine.sim
    if trafo < 0 or trafo not in sim.net.trafo.index:
        raise HTTPException(404, f"unknown trafo {trafo}")
    return _gate_profiles(sim.trafo_profiles(trafo, view=_gate_view(view)), "trafo")


@router.get("/state")
def state():
    """The latest solved StepResult (404 until the first solve)."""
    latest = runtime.store.latest
    if latest is None:
        raise HTTPException(404, "No result computed yet.")
    return latest


@router.get("/history")
def history(limit: int = Query(default=96, ge=1, le=10_000)):
    """Recent StepResults from the in-memory ring buffer (newest last)."""
    return runtime.store.history(limit=limit)


# --------------------------------------------------------------------------- #
# WebSocket: pushes every solved step as JSON
# --------------------------------------------------------------------------- #
@router.websocket("/ws")
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
@router.get("/", response_class=HTMLResponse)
def index():
    """Tiny built-in HTML live monitor (WebSocket-fed; no build step needed)."""
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
