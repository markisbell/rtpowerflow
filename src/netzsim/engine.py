"""Realtime engine: drives the simulator forward one step per accelerated tick.

Wall-clock semantics (accelerated tick): every ``interval`` real seconds the
engine advances exactly one 15-minute step. After ``steps_per_day`` steps it
wraps around to step 0 and increments the day counter, repeating indefinitely
with the same daily profiles.
"""
from __future__ import annotations

import asyncio
import logging

from .data_loader import InputData
from .simulator import Simulator
from .state import StateStore

log = logging.getLogger("netzsim.engine")


class RealtimeEngine:
    def __init__(
        self,
        simulator: Simulator,
        store: StateStore,
        interval_seconds: float = 1.0,
    ):
        self.sim = simulator
        self.store = store
        self.interval = interval_seconds
        self.steps_per_day = simulator.steps_per_day

        self.step = 0
        self.day = 0
        self._task: asyncio.Task | None = None
        self._running = asyncio.Event()
        self._stopped = False

    # -- lifecycle ------------------------------------------------------- #
    def start_loop(self) -> None:
        if self._task is None or self._task.done():
            self._stopped = False
            self._task = asyncio.create_task(self._run(), name="netzsim-loop")
        self._running.set()

    def pause(self) -> None:
        self._running.clear()

    def resume(self) -> None:
        self._running.set()

    async def stop(self) -> None:
        self._stopped = True
        self._running.set()  # release the wait so the loop can exit
        if self._task:
            await self._task

    async def reconfigure(
        self, data: InputData, *, autostart: bool | None = None
    ) -> None:
        """Swap in a different grid at runtime without a process restart.

        Halts the loop, rebuilds the pandapower net + profiles from ``data``,
        resets the clock to day 0 / step 0, and clears the published history
        (bus/line indices and topology change with the grid). Resumes the loop
        if it was running (override with ``autostart``).
        """
        was_running = self._running.is_set() and not self._stopped
        await self.stop()  # awaits the in-flight step, if any

        # Building the net is CPU-bound; keep it off the event loop.
        self.sim = await asyncio.to_thread(
            Simulator, data, warm_start=self.sim.warm_start
        )
        self.steps_per_day = self.sim.steps_per_day
        self.step = 0
        self.day = 0
        self.store.reset()

        self._stopped = False
        self._running.clear()
        if autostart if autostart is not None else was_running:
            self.start_loop()
        log.info("Reconfigured grid: %d buses, %d steps/day.",
                 len(self.sim.net.bus), self.steps_per_day)

    def seek(self, step: int) -> None:
        self.step = step % self.steps_per_day

    def set_interval(self, seconds: float) -> None:
        """Change the accelerated-tick interval (real seconds per step). The
        running loop picks up the new value on its next sleep."""
        self.interval = max(0.01, float(seconds))

    @property
    def status(self) -> dict:
        return {
            "running": self._running.is_set() and not self._stopped,
            "step": self.step,
            "day": self.day,
            "steps_per_day": self.steps_per_day,
            "interval_seconds": self.interval,
        }

    # -- main loop ------------------------------------------------------- #
    async def _run(self) -> None:
        log.info("Realtime engine started (%.3fs/step).", self.interval)
        while not self._stopped:
            await self._running.wait()
            if self._stopped:
                break

            result = await asyncio.to_thread(self.sim.run_step, self.step, self.day)
            await self.store.publish(result)

            if not result.converged:
                log.warning("Step %s (day %s) did not converge.", self.step, self.day)

            self.step += 1
            if self.step >= self.steps_per_day:
                self.step = 0
                self.day += 1
                log.info("Day %s complete; repeating profiles.", self.day)

            try:
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                break
        log.info("Realtime engine stopped.")
