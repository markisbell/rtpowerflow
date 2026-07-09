"""In-memory state store + WebSocket broadcast hub."""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import asdict
from typing import Any

from fastapi import WebSocket

from .simulator import StepResult


# Ground-truth (full power-flow) keys stripped from the wire when observability
# is enforced strictly (expose_ground_truth=False). The observed projection
# (`measurements` / `observed_summary`) and the scalar step fields always remain.
_TRUTH_KEYS = ("buses", "lines", "trafos", "ext_grids", "summary")


class StateStore:
    """Keeps the latest result, a bounded history, and notifies subscribers."""

    def __init__(self, history_size: int = 288, expose_ground_truth: bool = True):
        self._latest: StepResult | None = None
        self._history: deque[StepResult] = deque(maxlen=history_size)
        self._subscribers: set[WebSocket] = set()
        self._lock = asyncio.Lock()
        self._expose = expose_ground_truth
        # optional non-blocking consumer of every published (projected) payload
        # — the session recorder taps in here, so it records exactly what the
        # wire carries (strict mode included)
        self._sink = None

    def set_sink(self, fn) -> None:
        self._sink = fn

    def _project(self, payload: dict[str, Any]) -> dict[str, Any]:
        """What actually goes on the wire: full truth when exposed, else only the
        observed measurement projection + scalar fields. The state estimate is
        derived purely from measurements and stays — but its error-vs-truth
        metric would leak ground truth, so it is stripped too."""
        if self._expose:
            return payload
        out = {k: v for k, v in payload.items() if k not in _TRUTH_KEYS}
        est = out.get("estimated")
        if isinstance(est, dict):
            est = {k: v for k, v in est.items() if k != "error"}
            # hierarchical estimates carry per-cell error metrics — same leak
            if isinstance(est.get("cells"), list):
                est["cells"] = [{k: v for k, v in c.items() if k != "error"}
                                for c in est["cells"]]
            out["estimated"] = est
        return out

    # -- writes ---------------------------------------------------------- #
    async def publish(self, result: StepResult) -> None:
        self._latest = result
        self._history.append(result)
        if self._sink is None and not self._subscribers:
            return
        payload = self._project(asdict(result))
        if self._sink is not None:
            try:
                self._sink(payload)
            except Exception:  # noqa: BLE001 — recording must never stall the loop
                pass
        await self._broadcast(payload)

    def reset(self) -> None:
        """Drop the latest result and history (topology/indices changed).

        Subscribers stay connected; they simply receive the next grid's results.
        """
        self._latest = None
        self._history.clear()

    # -- reads ----------------------------------------------------------- #
    @property
    def latest(self) -> dict[str, Any] | None:
        return self._project(asdict(self._latest)) if self._latest else None

    def history(self, limit: int | None = None) -> list[dict[str, Any]]:
        items = list(self._history)
        if limit is not None:
            items = items[-limit:]
        return [self._project(asdict(r)) for r in items]

    # -- websocket pub/sub ---------------------------------------------- #
    async def subscribe(self, ws: WebSocket) -> None:
        async with self._lock:
            self._subscribers.add(ws)

    async def unsubscribe(self, ws: WebSocket) -> None:
        async with self._lock:
            self._subscribers.discard(ws)

    async def _broadcast(self, payload: dict[str, Any]) -> None:
        if not self._subscribers:
            return
        dead: list[WebSocket] = []
        for ws in list(self._subscribers):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._subscribers.discard(ws)
