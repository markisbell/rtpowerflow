"""Gamebridge: the co-simulation control surface for an external game clock.

Contract sketch (simgames ROADMAP §5, contract v1 is authored in that repo):
the game owns time; netzsim never self-advances in puppet mode
(``NETZSIM_EXTERNAL_CLOCK=true``). Divergence is data, not an HTTP error —
``/gb/step`` returns the published StepResult whose ``converged`` field the
game maps to gameplay events.

Only the stepping surface lives here for now; topology CRUD (``/gb/net/*``)
follows with contract v1 (simgames Phase 2).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

import pandapower

from ..config import settings
from .runtime import API_VERSION, runtime

router = APIRouter(prefix="/gb", tags=["gamebridge"])

CONTRACT_VERSION = "0.1"  # pre-v1: stepping only


@router.get("/version")
def version():
    """Contract handshake: the game refuses to run on a contract mismatch."""
    return {
        "contract": CONTRACT_VERSION,
        "backend": "netzsim",
        "api": API_VERSION,
        "solver": f"pandapower {pandapower.__version__}",
        "external_clock": settings.external_clock,
    }


@router.post("/step")
async def step():
    """Advance exactly one simulation step under the external clock and return
    the published result (same projection as ``/state`` — strict-observability
    stripping included). 409 while the internal clock is running."""
    try:
        await runtime.engine.external_step()
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc
    return runtime.store.latest
