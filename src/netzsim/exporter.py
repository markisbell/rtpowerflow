"""Bulk export: simulate whole days as fast as possible into a recording pack.

The live recorder (recorder.py) captures what happens while the accelerated
clock ticks — fine for interactive sessions, but waiting 24 wall-clock
minutes per simulated day is pointless when one just wants "3 days of data
for the exercise group". The bulk exporter REPLAYS the current setup
offline: a deep copy of the live simulator (grid, loadgen profiles, runtime
DERs, batteries, controllers, meter placement, estimation policy) is driven
through ``run_step`` for every step of the requested days, back to back,
and every result is fed into a private ``Recorder`` — so the output pack is
byte-compatible with a live recording (same CSVs, same metadata.json, same
/recordings listing and ZIP download).

Replay semantics (deliberately the LIVE physics, not the day-graph sweep):
batteries start the export at 50 % SOC (the scenario convention) and then
integrate CONTINUOUSLY across day boundaries; controllers really regulate
(closed loop); the state estimate runs in the metering raster — forced
fresh at every raster step, because in an offline replay there is no live
loop to protect (``estimate=False`` skips it entirely, which is much faster
on TAF-9/10/14 setups where WLS dominates the runtime).

One export at a time; progress is polled via ``status()`` and a run can be
cancelled between steps (the partial pack is finalized and marked).
"""
from __future__ import annotations

import copy
import logging
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .ext import reset_ext_values
from .recorder import Recorder

log = logging.getLogger("netzsim.exporter")


class BulkExporter:
    """Replays whole days of the CURRENT configuration into a recording pack."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._state: dict[str, Any] = {"active": False}

    # -- control ------------------------------------------------------------ #
    def start(self, sim_copy, meta: dict[str, Any], days: list[int],
              estimate: bool = True, name: str | None = None) -> dict:
        """Start the replay on an already ISOLATED simulator copy (the caller
        deep-copies while the engine is briefly paused, so the copy is clean)."""
        if self._state.get("active"):
            raise RuntimeError("a bulk export is already running")
        rec = Recorder(self.root)
        meta = {**meta, "export": {"days": days, "estimate": estimate}}
        rec.start(meta, name=name or f"export-{len(days)}-tage")
        spd = sim_copy.steps_per_day
        self._state = {
            "active": True,
            "id": rec.status()["id"],
            "days": days,
            "estimate": estimate,
            "steps_total": len(days) * spd,
            "steps_done": 0,
            "day": days[0] if days else None,
            "started": time.time(),
            "error": None,
            "cancelled": False,
        }
        self._cancel.clear()
        self._thread = threading.Thread(
            target=self._run, args=(sim_copy, rec, days, estimate),
            name="netzsim-exporter", daemon=True)
        self._thread.start()
        return self.status()

    def cancel(self) -> dict:
        """Request a stop between steps; the partial pack is kept + finalized."""
        if not self._state.get("active"):
            raise RuntimeError("no bulk export is running")
        self._cancel.set()
        if self._thread is not None:
            self._thread.join(timeout=60)
        return self.status()

    def status(self) -> dict:
        s = dict(self._state)
        if s.get("active") and s.get("steps_done"):
            rate = s["steps_done"] / max(time.time() - s["started"], 1e-6)
            s["eta_seconds"] = round((s["steps_total"] - s["steps_done"]) / max(rate, 1e-6))
        return s

    @property
    def active_id(self) -> str | None:
        """The pack currently being written (guards download/delete)."""
        return self._state.get("id") if self._state.get("active") else None

    # -- replay thread -------------------------------------------------------- #
    def _run(self, sim, rec: Recorder, days: list[int], estimate: bool) -> None:
        t0 = time.time()
        try:
            self._prepare(sim, estimate)
            spd = sim.steps_per_day
            for d in days:
                self._state["day"] = d
                for t in range(spd):
                    if self._cancel.is_set():
                        self._state["cancelled"] = True
                        raise _Cancelled()
                    if estimate:
                        sim._est_wall = 0.0     # offline: no live loop to protect
                    rec.record(asdict(sim.run_step(t, d)))
                    self._state["steps_done"] += 1
        except _Cancelled:
            log.info("Bulk export cancelled after %d steps.",
                     self._state["steps_done"])
        except Exception as exc:  # noqa: BLE001 — surface via status, keep partial
            log.exception("Bulk export failed.")
            self._state["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            rec._meta = {**rec._meta,
                         "export": {**rec._meta.get("export", {}),
                                    "cancelled": self._state.get("cancelled", False),
                                    "error": self._state.get("error"),
                                    "duration_seconds": round(time.time() - t0, 1)}}
            rec.stop()
            self._state["active"] = False
            log.info("Bulk export finished: %d steps in %.1f s.",
                     self._state["steps_done"], time.time() - t0)

    @staticmethod
    def _prepare(sim, estimate: bool) -> None:
        """Normalize the copy for a from-midnight replay: batteries at the
        scenario convention (50 % SOC), controllers released, estimator state
        fresh; the day-graph caches of the live sim are dead weight here."""
        sim._daily_by_day.clear()
        sim._estimator = None
        sim._est_last = None
        sim._est_ms = 0.0
        sim._est_wall = float("inf") if not estimate else 0.0
        # external nodes: forget the live mailbox so a replay never depends
        # on what a feed happened to send (nodes start silent = 0 kW)
        reset_ext_values(sim)
        for b in sim.batteries:
            b.soc_mwh = 0.5 * b.capacity_mwh
        for c in sim.controllers:
            c.ev_factor = c.pv_factor = 1.0
            c.seen_pct = c.seen_src = None


class _Cancelled(Exception):
    pass
