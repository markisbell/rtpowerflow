"""The core: apply one time step's profiles to the net and solve the power flow."""
from __future__ import annotations

import copy
import math
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandapower as pp

from . import battery as bat
from .battery import MODES, Battery
from .data_loader import InputData
from .estimator import Estimator
from .measurements import MeasurementSet
from .network_builder import ProfileArrays, build_network


@dataclass
class StepResult:
    """Serializable summary of one solved time step."""

    step: int
    day: int
    time_of_day: str          # "HH:MM"
    converged: bool
    solve_ms: float
    timestamp: float          # wall-clock unix time the step was solved
    buses: list[dict[str, Any]] = field(default_factory=list)
    lines: list[dict[str, Any]] = field(default_factory=list)
    trafos: list[dict[str, Any]] = field(default_factory=list)
    ext_grids: list[dict[str, Any]] = field(default_factory=list)
    batteries: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    # Observability projection: what the placed measurement devices reveal (see
    # measurements.py). Always present; the truth fields above are stripped from
    # the wire when NETZSIM_EXPOSE_GROUND_TRUTH is off (state.StateStore).
    measurements: dict[str, Any] = field(default_factory=dict)
    observed_summary: dict[str, Any] | None = None
    # WLS state estimation from the placed meters + grid model (estimator.py).
    # Present only while at least one meter is placed; derived purely from the
    # observed readings, so it survives strict mode (except its error metric).
    estimated: dict[str, Any] | None = None


def _hhmm(step: int, steps_per_day: int) -> str:
    minutes_per_step = 24 * 60 // steps_per_day
    total = step * minutes_per_step
    return f"{total // 60:02d}:{total % 60:02d}"


class Simulator:
    """Holds the pandapower net + profiles and solves any single step on demand."""

    def __init__(self, data: InputData, warm_start: bool = True):
        self.data = data
        self.warm_start = warm_start
        self.net, self.prof = build_network(data)
        self.steps_per_day = data.steps_per_day
        self._solved_once = False
        self._coords: tuple[dict[int, list[float]], dict[int, list[float]]] | None = None
        # multi-day real PV (optional): per-day normalised 0..1 shapes applied as a
        # scale on each PV sgen. `day` selects the active day; graphs cache per day.
        self.day = 0
        self.pv_days: np.ndarray | None = None
        self.sgen_peak = self.prof.sgen_p.max(axis=1) if self.prof.sgen_p.size else np.zeros(0)
        self._daily_by_day: dict[int, dict] = {}

        # Observability: which buses / transformers carry a measurement device.
        # Empty by default — a fresh grid is almost entirely unobservable until
        # meters are placed. Grid-specific, so it resets when the grid is swapped
        # (a new Simulator is built by engine.reconfigure).
        self.meters = MeasurementSet()
        # WLS state estimation from those meters; built lazily on first use
        # (it deep-copies the net) and only run while meters are placed. On big
        # district nets one WLS run costs ~1-2 s, so estimation is re-run
        # adaptively (spaced by 2× its own runtime) and the latest estimate is
        # carried on every step in between.
        self._estimator: Estimator | None = None
        self._sgen_day_mean_cache: dict[int, np.ndarray] = {}
        self._est_last: dict | None = None
        self._est_wall = 0.0            # monotonic time of the last estimation run
        self._est_ms = 0.0              # its duration (drives the adaptive spacing)

        # local battery storage (added at runtime); prices drive the "price" mode.
        self.batteries: list[Battery] = []
        self.prices: np.ndarray | None = None   # [n_days, 24] EUR/MWh, aligned to pv days
        self._loads_at: dict[int, list[int]] = {}
        for i, li in enumerate(self.prof.load_idx):
            self._loads_at.setdefault(int(self.net.load.at[li, "bus"]), []).append(i)
        self._sgens_at: dict[int, list[int]] = {}
        for i, si in enumerate(self.prof.sgen_idx):
            self._sgens_at.setdefault(int(self.net.sgen.at[si, "bus"]), []).append(i)
        trafo_r = float((self.net.trafo["sn_mva"] * self.net.trafo["parallel"]).sum()) if len(self.net.trafo) else 0.0
        if trafo_r <= 0:  # no transformer → reference the grid's peak load for peak-shaving
            trafo_r = float(self.prof.load_p.sum(axis=0).max()) if self.prof.load_p.size else 1.0
        self._peak_ref_mw = trafo_r

    def set_pv_days(self, shapes: np.ndarray | None) -> None:
        """Attach per-day PV shapes ([n_days, steps], 0..1). Applied only if the
        grid actually has PV sgens; clears the per-day graph cache."""
        self.pv_days = shapes if (shapes is not None and len(shapes) and self.sgen_peak.size) else None
        self._daily_by_day.clear()

    @property
    def n_days(self) -> int:
        return int(self.pv_days.shape[0]) if self.pv_days is not None else 1

    def _sgen_p_col(self, day: int, t: int) -> "np.ndarray":
        """PV/sgen active power at step ``t`` — the real day's shape × each
        system's peak when real PV is loaded, else the built-in profile."""
        if self.pv_days is not None:
            return self.sgen_peak * self.pv_days[day % self.pv_days.shape[0], t]
        return self.prof.sgen_p[:, t]

    def _sgen_day_mean(self, day: int) -> "np.ndarray":
        """Per-sgen mean generation of the active day — the operator's coarse PV
        knowledge, used as pseudo-measurement input by the state estimator."""
        d = day % self.n_days
        if d not in self._sgen_day_mean_cache:
            if not self.prof.sgen_p.size:
                self._sgen_day_mean_cache[d] = np.zeros(0)
            elif self.pv_days is not None:
                self._sgen_day_mean_cache[d] = self.sgen_peak * float(self.pv_days[d].mean())
            else:
                self._sgen_day_mean_cache[d] = self.prof.sgen_p.mean(axis=1)
        return self._sgen_day_mean_cache[d]

    # -- battery storage ------------------------------------------------- #
    def set_prices(self, prices) -> None:
        self.prices = np.asarray(prices, dtype=float) if prices is not None and len(prices) else None
        self._daily_by_day.clear()

    def add_battery(self, bus: int, capacity_kwh: float, power_kw: float,
                    mode: str = "self", soc0: float = 0.5) -> Battery:
        cap = max(0.0, capacity_kwh) / 1000.0
        b = Battery(bus=int(bus), capacity_mwh=cap, power_mw=max(0.0, power_kw) / 1000.0,
                    mode=mode if mode in MODES else "self", soc_mwh=soc0 * cap,
                    name=f"BAT_{bus}")
        self.batteries.append(b)
        self._rebuild_storage()
        self._daily_by_day.clear()
        return b

    def set_battery_mode(self, storage_idx: int, mode: str) -> bool:
        """Switch a deployed battery's operating strategy in place. Clears the
        daily-curve cache — the battery-aware sweeps change with the strategy."""
        if mode not in MODES:
            raise ValueError(f"unknown battery mode '{mode}'")
        for b in self.batteries:
            if b.storage_idx == storage_idx:
                if b.mode != mode:
                    b.mode = mode
                    self._daily_by_day.clear()
                return True
        return False

    def remove_battery(self, storage_idx: int) -> bool:
        n = len(self.batteries)
        self.batteries = [b for b in self.batteries if b.storage_idx != storage_idx]
        if len(self.batteries) != n:
            self._rebuild_storage()
            self._daily_by_day.clear()
            return True
        return False

    # -- runtime DER configuration (PV / EV per node) ---------------------- #
    def _der_invalidate(self) -> None:
        """Refresh everything derived from the load/sgen tables + profiles after
        a runtime DER change (same hygiene as a battery add/remove)."""
        self.sgen_peak = self.prof.sgen_p.max(axis=1) if self.prof.sgen_p.size else np.zeros(0)
        self._loads_at = {}
        for i, li in enumerate(self.prof.load_idx):
            self._loads_at.setdefault(int(self.net.load.at[li, "bus"]), []).append(i)
        self._sgens_at = {}
        for i, si in enumerate(self.prof.sgen_idx):
            self._sgens_at.setdefault(int(self.net.sgen.at[si, "bus"]), []).append(i)
        self._daily_by_day.clear()
        self._sgen_day_mean_cache.clear()
        self._estimator = None          # cached per-bus profile stats are stale
        self._est_last = None

    def node_der(self, bus: int) -> dict:
        """The bus's configurable DERs. Parameters are derived from the profile
        rows themselves (PV kWp = row peak; EV start/duration/power = the
        nonzero charging window), so LoadStudio-assigned and runtime-added
        systems are equally editable."""
        if bus not in self.net.bus.index:
            raise KeyError(bus)
        mps = 1440 // self.steps_per_day            # minutes per step
        pv = None
        for i, si in enumerate(self.prof.sgen_idx):
            if int(self.net.sgen.at[si, "bus"]) != bus:
                continue
            if "PV_" not in str(self.net.sgen.at[si, "name"] or ""):
                continue
            pv = {"sgen": int(si), "kwp": round(float(self.prof.sgen_p[i].max()) * 1000.0, 2)}
            break
        ev = None
        for i, li in enumerate(self.prof.load_idx):
            if int(self.net.load.at[li, "bus"]) != bus:
                continue
            if "EV_" not in str(self.net.load.at[li, "name"] or ""):
                continue
            row = self.prof.load_p[i]
            on = np.flatnonzero(row > 1e-9)
            start = int(on[0]) if on.size else 18 * 60 // mps
            if on.size and on[0] == 0 and on[-1] == len(row) - 1:
                gaps = np.flatnonzero(np.diff(on) > 1)   # window wraps midnight
                if gaps.size:
                    start = int(on[gaps[0] + 1])
            ev = {"load": int(li),
                  "kw": round(float(row.max()) * 1000.0, 2) if on.size else 11.0,
                  "start_min": start * mps,
                  "dur_min": int(on.size) * mps if on.size else 120}
            break
        return {"bus": int(bus), "pv": pv, "ev": ev}

    def add_pv(self, bus: int, kwp: float = 5.0) -> dict:
        """Add a rooftop-PV system (clear-sky shape × kWp) at a bus at runtime.
        Like batteries, runtime DERs live in this simulator only (reset on swap)."""
        if bus not in self.net.bus.index:
            raise KeyError(bus)
        from .loadgen.pv import PvPolicy, _clearsky
        kwp = float(min(max(kwp, 0.5), 100.0))
        pol = PvPolicy()
        row = _clearsky(self.steps_per_day, pol.peak_hour, pol.width_hours) * (kwp / 1000.0)
        si = int(pp.create_sgen(self.net, bus=bus, p_mw=0.0, q_mvar=0.0, name=f"PV_cfg_{bus}"))
        self.prof.sgen_idx.append(si)
        self.prof.sgen_p = np.vstack([self.prof.sgen_p, row[None, :]])
        self.prof.sgen_q = np.vstack([self.prof.sgen_q, np.zeros((1, self.steps_per_day))])
        self._der_invalidate()
        return self.node_der(bus)

    def set_pv_kwp(self, sgen: int, kwp: float) -> bool:
        """Rescale a PV system to a new peak. Works in real-PV mode too — the
        measured day shapes scale with ``sgen_peak``."""
        if sgen not in self.prof.sgen_idx:
            return False
        i = self.prof.sgen_idx.index(sgen)
        kwp = float(min(max(kwp, 0.5), 100.0))
        row = self.prof.sgen_p[i]
        peak = float(row.max())
        if peak > 1e-9:
            self.prof.sgen_p[i] = row * (kwp / 1000.0 / peak)
        else:
            from .loadgen.pv import PvPolicy, _clearsky
            pol = PvPolicy()
            self.prof.sgen_p[i] = _clearsky(self.steps_per_day, pol.peak_hour,
                                            pol.width_hours) * (kwp / 1000.0)
        self._der_invalidate()
        return True

    def add_ev(self, bus: int, kw: float = 11.0, start_min: int = 18 * 60,
               dur_min: int = 120) -> dict:
        """Add an EV home-charging load at a bus at runtime (wallbox kW held for
        the charge window; duration clamped to 1-4 h; wraps past midnight)."""
        if bus not in self.net.bus.index:
            raise KeyError(bus)
        li = int(pp.create_load(self.net, bus=bus, p_mw=0.0, q_mvar=0.0, name=f"EV_cfg_{bus}"))
        self.prof.load_idx.append(li)
        self.prof.load_p = np.vstack([self.prof.load_p, np.zeros((1, self.steps_per_day))])
        self.prof.load_q = np.vstack([self.prof.load_q, np.zeros((1, self.steps_per_day))])
        self._set_ev_row(len(self.prof.load_idx) - 1, kw, start_min, dur_min)
        self._der_invalidate()
        return self.node_der(bus)

    def set_ev(self, load: int, start_min: int, dur_min: int) -> bool:
        """Move an EV's charge window (start instant + 1-4 h duration); the
        wallbox power is kept from the existing profile."""
        if load not in self.prof.load_idx:
            return False
        i = self.prof.load_idx.index(load)
        kw = float(self.prof.load_p[i].max()) * 1000.0
        self._set_ev_row(i, kw if kw > 0.1 else 11.0, start_min, dur_min)
        self._der_invalidate()
        return True

    def remove_pv(self, sgen: int) -> bool:
        """Remove a PV system (LoadStudio-assigned or runtime-added)."""
        if sgen not in self.prof.sgen_idx:
            return False
        i = self.prof.sgen_idx.index(sgen)
        self.prof.sgen_idx.pop(i)
        self.prof.sgen_p = np.delete(self.prof.sgen_p, i, axis=0)
        self.prof.sgen_q = np.delete(self.prof.sgen_q, i, axis=0)
        self.net.sgen.drop(sgen, inplace=True)
        self._der_invalidate()
        return True

    def remove_ev(self, load: int) -> bool:
        """Remove an EV charging load (LoadStudio-assigned or runtime-added)."""
        if load not in self.prof.load_idx:
            return False
        i = self.prof.load_idx.index(load)
        self.prof.load_idx.pop(i)
        self.prof.load_p = np.delete(self.prof.load_p, i, axis=0)
        self.prof.load_q = np.delete(self.prof.load_q, i, axis=0)
        self.net.load.drop(load, inplace=True)
        self._der_invalidate()
        return True

    def _set_ev_row(self, i: int, kw: float, start_min: int, dur_min: int) -> None:
        from .loadgen.ev import _charge_profile
        dur_min = int(min(max(dur_min, 60), 240))        # 1-4 h charging
        start_min = int(start_min) % 1440
        row = _charge_profile(self.steps_per_day, start_min / 60.0, dur_min / 60.0,
                              max(kw, 0.1)) / 1000.0
        self.prof.load_p[i] = row
        self.prof.load_q[i] = row * math.tan(math.acos(0.98))

    def _rebuild_storage(self) -> None:
        """Recreate the pandapower storage table from ``self.batteries`` (keeps
        indices in sync after add/remove)."""
        self.net.storage.drop(self.net.storage.index, inplace=True)
        for b in self.batteries:
            b.storage_idx = int(pp.create_storage(
                self.net, bus=b.bus, p_mw=0.0, max_e_mwh=b.capacity_mwh,
                soc_percent=b.soc_frac() * 100.0, name=b.name))

    # -- observability / measurement placement --------------------------- #
    def measurement_placement(self) -> dict:
        """Current meter placement + coverage (no power-flow results needed)."""
        return self.meters.placement(self.net)

    def place_node_meter(self, bus: int) -> bool:
        if bus not in self.net.bus.index:
            raise KeyError(bus)
        return self.meters.add_node(bus)

    def remove_node_meter(self, bus: int) -> bool:
        return self.meters.remove_node(bus)

    def place_trafo_meter(self, trafo: int) -> bool:
        if trafo not in self.net.trafo.index:
            raise KeyError(trafo)
        return self.meters.add_trafo(trafo)

    def remove_trafo_meter(self, trafo: int) -> bool:
        return self.meters.remove_trafo(trafo)

    def apply_meter_preset(self, name: str) -> None:
        self.meters.apply_preset(name, self.net)

    def set_meter_mode(self, name: str) -> None:
        """Meter fidelity: "full" (V/P/Q/I per step) or "standard" (15-min mean
        P only). The daily-sweep cache re-keys on it automatically."""
        self.meters.set_mode(name)

    def _price_ctx(self, day: int, t: int) -> dict:
        if self.prices is None or not len(self.prices):
            return {}
        pday = self.prices[day % self.prices.shape[0]]
        hour = min(23, int(t / (self.steps_per_day / 24)))
        return {"price": float(pday[hour]),
                "price_lo": float(np.percentile(pday, 33)),
                "price_hi": float(np.percentile(pday, 66))}

    def _apply_batteries(self, net, batteries, prof, sgen_col, day, t, dt_h,
                         loads_at, sgens_at, do_integrate: bool) -> None:
        """Compute each battery's charge/discharge setpoint from the step's local
        load/PV, transformer through-power and price; write it into the net's
        storage and (optionally) advance SOC."""
        if not batteries:
            return
        total_load = float(prof.load_p[:, t].sum()) if prof.load_p.size else 0.0
        total_gen = float(sgen_col.sum()) if sgen_col is not None and sgen_col.size else 0.0
        base = {"through_mw": total_load - total_gen,
                "peak_hi_mw": self._peak_ref_mw * 0.6,
                "peak_lo_mw": self._peak_ref_mw * 0.3,
                **self._price_ctx(day, t)}
        for b in batteries:
            ctx = dict(base)
            if b.mode == "self":
                ctx["load_mw"] = sum(float(prof.load_p[i, t]) for i in loads_at.get(b.bus, []))
                ctx["pv_mw"] = (sum(float(sgen_col[i]) for i in sgens_at.get(b.bus, []))
                                if sgen_col is not None and sgen_col.size else 0.0)
            p = bat.setpoint(b, ctx, dt_h)
            if b.storage_idx is not None and b.storage_idx in net.storage.index:
                net.storage.at[b.storage_idx, "p_mw"] = p
            if do_integrate:
                bat.integrate(b, p, dt_h)

    # -- profile application -------------------------------------------- #
    def _apply_step(self, t: int) -> None:
        p = self.prof
        sgen_col = None
        if p.load_idx:
            self.net.load.loc[p.load_idx, "p_mw"] = p.load_p[:, t]
            self.net.load.loc[p.load_idx, "q_mvar"] = p.load_q[:, t]
        if p.sgen_idx:
            sgen_col = self._sgen_p_col(self.day, t)
            self.net.sgen.loc[p.sgen_idx, "p_mw"] = sgen_col
            self.net.sgen.loc[p.sgen_idx, "q_mvar"] = p.sgen_q[:, t]
        if p.ext_idx:
            self.net.ext_grid.loc[p.ext_idx, "vm_pu"] = p.ext_vm[:, t]
            self.net.ext_grid.loc[p.ext_idx, "va_degree"] = p.ext_va[:, t]
        if self.batteries:
            self._apply_batteries(self.net, self.batteries, p, sgen_col, self.day, t,
                                  24.0 / self.steps_per_day, self._loads_at, self._sgens_at,
                                  do_integrate=True)

    # -- the power flow -------------------------------------------------- #
    def run_step(self, step: int, day: int = 0) -> StepResult:
        t = step % self.steps_per_day
        self.day = day
        self._apply_step(t)

        init = "results" if (self.warm_start and self._solved_once) else "auto"
        t0 = time.perf_counter()
        converged, error = False, None
        # Newton-Raphson can stall on healthy operating points of these nets
        # (zero-impedance ding0 busbar links make the Jacobian ill-conditioned,
        # and the dc-based "auto" init then oscillates at certain load patterns).
        # Retry with a flat start, then damped (Iwamoto) Newton, before
        # reporting the step as non-converged.
        for kw in (dict(init=init),
                   dict(init="flat"),
                   dict(init="flat", algorithm="iwamoto_nr", max_iteration=30)):
            try:
                pp.runpp(self.net, calculate_voltage_angles=True, **kw)
                converged = True
                self._solved_once = True
                break
            except pp.LoadflowNotConverged as exc:  # type: ignore[attr-defined]
                error = f"Load flow did not converge: {exc}"
        if converged:
            error = None
        solve_ms = (time.perf_counter() - t0) * 1000.0

        res = self._collect(step, day, t, converged, solve_ms, error)
        # state estimation (the operator's calculated view) — only meaningful
        # once at least one meter is placed, and only from a converged truth
        if converged and (self.meters.node_buses or self.meters.trafo_idxs):
            if self._estimator is None:
                self._estimator = Estimator(self.net, self.prof, self._loads_at, self._sgens_at)
            now = time.monotonic()
            if now - self._est_wall >= 2.0 * self._est_ms / 1000.0:
                bats = {b.bus: b.power_mw for b in self.batteries}
                est = self._estimator.run(self.net, res.measurements,
                                          self._sgen_day_mean(day), bats)
                self._est_wall = time.monotonic()
                if est is not None:
                    est["step"], est["day"] = t, day   # which step it estimated
                    self._est_ms = est["solve_ms"]
                    self._est_last = est
            res.estimated = self._est_last
        else:
            self._est_last = None
        return res

    # -- result extraction ---------------------------------------------- #
    def _collect(self, step, day, t, converged, solve_ms, error) -> StepResult:
        res = StepResult(
            step=t,
            day=day,
            time_of_day=_hhmm(t, self.steps_per_day),
            converged=converged,
            solve_ms=round(solve_ms, 3),
            timestamp=time.time(),
            error=error,
        )
        if not converged:
            # No readings without a solve, but keep coverage so the UI can still
            # show where meters are placed.
            res.measurements = {"nodes": [], "trafos": [],
                                "coverage": self.meters.placement(self.net)["coverage"],
                                "phases": 3, "balanced": True}
            return res

        net = self.net
        for idx, row in net.res_bus.iterrows():
            res.buses.append({
                "index": int(idx),
                "name": str(net.bus.at[idx, "name"]),
                "vm_pu": _r(row.vm_pu),
                "va_degree": _r(row.va_degree),
                "p_mw": _r(row.p_mw),
                "q_mvar": _r(row.q_mvar),
            })
        for idx, row in net.res_line.iterrows():
            res.lines.append({
                "index": int(idx),
                "name": str(net.line.at[idx, "name"]),
                "from_bus": int(net.line.at[idx, "from_bus"]),
                "to_bus": int(net.line.at[idx, "to_bus"]),
                "loading_percent": _r(row.loading_percent),
                "i_ka": _r(row.i_ka),
                "p_from_mw": _r(row.p_from_mw),
                "pl_mw": _r(row.pl_mw),
            })
        for idx, row in net.res_trafo.iterrows():
            res.trafos.append({
                "index": int(idx),
                "name": str(net.trafo.at[idx, "name"]),
                "hv_bus": int(net.trafo.at[idx, "hv_bus"]),
                "lv_bus": int(net.trafo.at[idx, "lv_bus"]),
                "loading_percent": _r(row.loading_percent),
                "p_hv_mw": _r(row.p_hv_mw),
                "q_hv_mvar": _r(row.q_hv_mvar),
                "i_hv_ka": _r(row.i_hv_ka),
                "pl_mw": _r(row.pl_mw),
            })
        for idx, row in net.res_ext_grid.iterrows():
            res.ext_grids.append({
                "index": int(idx),
                "name": str(net.ext_grid.at[idx, "name"]),
                "p_mw": _r(row.p_mw),
                "q_mvar": _r(row.q_mvar),
            })
        for b in self.batteries:
            p_mw = float(net.storage.at[b.storage_idx, "p_mw"]) if b.storage_idx in net.storage.index else 0.0
            res.batteries.append({
                "index": b.storage_idx, "bus": b.bus, "name": b.name, "mode": b.mode,
                "soc_percent": _r(b.soc_frac() * 100.0), "p_mw": _r(p_mw),
                "capacity_kwh": _r(b.capacity_mwh * 1000.0), "power_kw": _r(b.power_mw * 1000.0),
            })

        vm = net.res_bus.vm_pu
        loadings = net.res_line.loading_percent
        trafo_loadings = net.res_trafo.loading_percent
        res.summary = {
            "n_bus": int(len(net.res_bus)),
            "n_line": int(len(net.res_line)),
            "n_trafo": int(len(net.res_trafo)),
            "vm_pu_min": _r(vm.min()),
            "vm_pu_max": _r(vm.max()),
            "max_line_loading_percent": _r(loadings.max()) if len(loadings) else None,
            "max_trafo_loading_percent": (
                _r(trafo_loadings.max()) if len(trafo_loadings) else None
            ),
            "total_load_mw": _r(net.res_load.p_mw.sum()) if len(net.res_load) else 0.0,
            "total_gen_mw": _r(net.res_sgen.p_mw.sum()) if len(net.res_sgen) else 0.0,
            "total_ext_grid_mw": _r(net.res_ext_grid.p_mw.sum()),
            "total_losses_mw": _r(net.res_line.pl_mw.sum()) if len(net.res_line) else 0.0,
        }
        # Observability projection: reduce the solved net to what the placed
        # measurement devices actually reveal, plus the operator-visible summary.
        res.measurements = self.meters.observe(net, t)
        res.observed_summary = self.meters.observed_summary(res.measurements)
        return res

    # -- static topology for the API ------------------------------------ #
    def topology(self) -> dict[str, Any]:
        net = self.net
        layout_geo, tree = self._bus_coordinates()
        # Real WGS84 coords (ding0 grids): {bus_index: [lon, lat]}
        real = {i: b.geo for i, b in enumerate(self.data.grid.buses) if b.geo}
        has_geo = len(real) >= max(1, len(self.data.grid.buses)) * 0.5
        if has_geo:
            lons = [g[0] for g in real.values()]
            lats = [g[1] for g in real.values()]
            mnlon, mxlon, mnlat, mxlat = min(lons), max(lons), min(lats), max(lats)
            dlon, dlat = (mxlon - mnlon) or 1.0, (mxlat - mnlat) or 1.0

        buses = net.bus.reset_index().rename(columns={"index": "id"}).to_dict(
            orient="records")
        for b in buses:
            i = int(b["id"])
            t = tree.get(i, [0.0, 0.0])
            b["tx"], b["ty"] = t[0], t[1]      # tidy tree (toggle)
            gg = real.get(i)
            b["geo"] = [gg[0], gg[1]] if gg else None   # raw lon/lat for the map
            if has_geo and gg:
                # geographic layout = real positions (lat inverted: north is up)
                b["x"] = round((gg[0] - mnlon) / dlon, 5)
                b["y"] = round(1.0 - (gg[1] - mnlat) / dlat, 5)
            else:
                g = layout_geo.get(i, [0.0, 0.0])
                b["x"], b["y"] = g[0], g[1]    # length-aware synthetic layout
        # lines + optional per-line geometry (OSM-routed cables follow the streets);
        # net.line index == position in data.lines.lines, so geometry maps by id.
        specs = self.data.lines.lines
        lines_out = net.line.reset_index().rename(columns={"index": "id"})[
            ["id", "name", "from_bus", "to_bus", "length_km"]].to_dict(orient="records")
        for ln in lines_out:
            i = int(ln["id"])
            ln["geometry"] = specs[i].geometry if i < len(specs) else None
            # normally-open ring ties are laid but out of service → drawn open on the map
            ln["in_service"] = bool(net.line.at[i, "in_service"])
        return {
            "name": net.name,
            "f_hz": float(net.f_hz),
            "steps_per_day": self.steps_per_day,
            "has_geo": has_geo,
            "buses": buses,
            "lines": lines_out,
            "trafos": net.trafo.reset_index().rename(columns={"index": "id"})
                [["id", "name", "hv_bus", "lv_bus", "sn_mva"]]
                .to_dict(orient="records"),
            "ext_grids": net.ext_grid.reset_index().rename(columns={"index": "id"})
                [["id", "name", "bus"]].to_dict(orient="records"),
            # bus indices hosting loads / PV — for the map underlay (houses, panels)
            "load_buses": sorted({int(b) for b in net.load["bus"].tolist()}),
            "sgen_buses": sorted({int(b) for b in net.sgen["bus"].tolist()}),
            # equipment icons: buses with EV charging loads / rooftop-PV systems
            "ev_buses": sorted({int(net.load.at[li, "bus"]) for li in net.load.index
                                if "EV_" in str(net.load.at[li, "name"] or "")}),
            "pv_buses": sorted({int(net.sgen.at[si, "bus"]) for si in net.sgen.index
                                if "PV_" in str(net.sgen.at[si, "name"] or "")}),
            # LV cable cabinets (where service cables join the main line) → green circles
            "cabinet_buses": [i for i, b in enumerate(self.data.grid.buses)
                              if getattr(b, "kind", None) == "cabinet"],
            "n_load": int(len(net.load)),
            "n_sgen": int(len(net.sgen)),
            "n_trafo": int(len(net.trafo)),
        }

    def _bus_coordinates(self) -> tuple[dict[int, list[float]], dict[int, list[float]]]:
        if self._coords is None:
            from .layout import compute_layouts
            self._coords = compute_layouts(self.net)
        return self._coords

    def daily_curves(self, day: int | None = None, samples: int = 97) -> dict:
        """Sweep a whole day once (on an ISOLATED net, so the live engine is not
        disturbed) and cache per-bus voltage + per-line current/loading + per-trafo
        power, sampled ~evenly across the day. Cached per day, since real-PV days
        differ; a grid without real PV only ever uses day 0.

        While meters are placed the sweep also runs the state estimator at the
        samples (decimated on big nets, where one WLS run costs ~1 s), so the
        daily graphs can overlay the operator's estimate on the truth — placing
        more meters visibly locks the estimated curve onto the real one. The
        cache is keyed on the meter placement, so changing it re-sweeps."""
        d = self.day if day is None else day
        sig = (frozenset(self.meters.node_buses), frozenset(self.meters.trafo_idxs),
               self.meters.mode)
        cached = self._daily_by_day.get(d)
        if cached is not None and cached.get("_sig") == sig:
            return cached
        # Snapshot the LIVE net (not a rebuild from the input data): runtime
        # DER changes — added/resized PV, EV windows, removals — exist only on
        # the live net/profiles, and self._loads_at/_sgens_at row indices refer
        # to self.prof. The sweep only reads prof and re-solves on its copy.
        net = copy.deepcopy(self.net)
        net.storage.drop(net.storage.index, inplace=True)  # batteries recreated fresh below
        prof = self.prof
        sgen_peak = prof.sgen_p.max(axis=1) if prof.sgen_p.size else np.zeros(0)
        spd = self.steps_per_day
        n = max(2, min(samples, spd))
        sample_at = {round(i * (spd - 1) / (n - 1)) for i in range(n)}
        dt_h = 24.0 / spd
        # Recreate the batteries on the isolated net (fresh SOC = 50 %) so the swept
        # curves reflect their charge/discharge; SOC is integrated at full 1-min
        # resolution while the power flow is solved only at the samples.
        bs = [Battery(bus=b.bus, capacity_mwh=b.capacity_mwh, power_mw=b.power_mw, mode=b.mode,
                      eff=b.eff, soc_min=b.soc_min, soc_max=b.soc_max, soc_mwh=0.5 * b.capacity_mwh,
                      name=b.name,
                      storage_idx=int(pp.create_storage(net, bus=b.bus, p_mw=0.0,
                                                        max_e_mwh=b.capacity_mwh, soc_percent=50.0, name=b.name)))
              for b in self.batteries]

        def sgen_col_at(t):
            return (sgen_peak * self.pv_days[d % self.pv_days.shape[0], t]
                    if self.pv_days is not None else prof.sgen_p[:, t])

        vm = {int(b): [] for b in net.bus.index}
        i_ka = {int(l): [] for l in net.line.index}
        loading = {int(l): [] for l in net.line.index}
        tr_p_hv = {int(t): [] for t in net.trafo.index}
        tr_loading = {int(t): [] for t in net.trafo.index}
        # estimated curves (only while meters are placed). A dedicated Estimator
        # instance: the live engine's runs concurrently on another thread.
        do_est = bool(self.meters.node_buses or self.meters.trafo_idxs)
        est_vm = {int(b): [] for b in net.bus.index}
        est_i = {int(l): [] for l in net.line.index}
        est_p_hv = {int(t): [] for t in net.trafo.index}
        estimator = Estimator(net, prof, self._loads_at, self._sgens_at) if do_est else None
        # the sweep gets its own MeasurementSet copy: standard-mode window
        # accumulators must not disturb the live meters' state
        sweep_meters = MeasurementSet(node_buses=set(self.meters.node_buses),
                                      trafo_idxs=set(self.meters.trafo_idxs),
                                      mode=self.meters.mode) if do_est else None
        est_bats = {b.bus: b.power_mw for b in self.batteries}
        est_every, si = 1, -1           # decimation adapts to the first run's cost
        # "auto" (not "flat") so multi-voltage-level grids with a transformer
        # converge on the first solve, matching the live engine; then warm-start.
        init = "auto"
        for t in range(spd):
            sgen_col = sgen_col_at(t) if (bs or t in sample_at) else None
            if bs:  # advance SOC every minute + set storage setpoints on the net
                self._apply_batteries(net, bs, prof, sgen_col, d, t, dt_h,
                                      self._loads_at, self._sgens_at, do_integrate=True)
            if t not in sample_at:
                continue
            net.load.loc[prof.load_idx, "p_mw"] = prof.load_p[:, t]
            net.load.loc[prof.load_idx, "q_mvar"] = prof.load_q[:, t]
            net.sgen.loc[prof.sgen_idx, "p_mw"] = sgen_col
            net.sgen.loc[prof.sgen_idx, "q_mvar"] = prof.sgen_q[:, t]
            net.ext_grid.loc[prof.ext_idx, "vm_pu"] = prof.ext_vm[:, t]
            net.ext_grid.loc[prof.ext_idx, "va_degree"] = prof.ext_va[:, t]
            solved = False
            # same retry ladder as run_step: flat init rescues the steps where
            # the warm/dc-init Newton stalls on ill-conditioned district nets
            for kw in (dict(init=init),
                       dict(init="flat"),
                       dict(init="flat", algorithm="iwamoto_nr", max_iteration=30)):
                try:
                    pp.runpp(net, calculate_voltage_angles=True, **kw)
                    solved = True
                    break
                except Exception:  # noqa: BLE001
                    continue
            si += 1
            est = None
            if solved:
                init = "results"
                for b in net.bus.index:
                    vm[int(b)].append(_r(net.res_bus.at[b, "vm_pu"]))
                for l in net.line.index:
                    i_ka[int(l)].append(_r(net.res_line.at[l, "i_ka"]))
                    loading[int(l)].append(_r(net.res_line.at[l, "loading_percent"]))
                for tr in net.trafo.index:
                    tr_p_hv[int(tr)].append(_r(net.res_trafo.at[tr, "p_hv_mw"]))
                    tr_loading[int(tr)].append(_r(net.res_trafo.at[tr, "loading_percent"]))
                if estimator is not None and si % est_every == 0:
                    est = estimator.run(net, sweep_meters.observe(net, t),
                                        self._sgen_day_mean(d), est_bats)
                    if est is not None and est_every == 1 and est["solve_ms"] > 120:
                        # big net: one WLS run is expensive — sample the estimate
                        # coarser so a sweep stays interactive (~a dozen runs)
                        est_every = max(1, round(est["solve_ms"] / 120))
            else:  # non-convergence at this step → gaps
                init = "auto"
                for b in net.bus.index:
                    vm[int(b)].append(None)
                for l in net.line.index:
                    i_ka[int(l)].append(None); loading[int(l)].append(None)
                for tr in net.trafo.index:
                    tr_p_hv[int(tr)].append(None); tr_loading[int(tr)].append(None)
            eb = {e["index"]: e["vm_pu"] for e in est["buses"]} if est else {}
            el = {e["index"]: e["i_ka"] for e in est["lines"]} if est else {}
            et = {e["index"]: e["p_hv_mw"] for e in est["trafos"]} if est else {}
            for b in net.bus.index:
                est_vm[int(b)].append(eb.get(int(b)))
            for l in net.line.index:
                est_i[int(l)].append(el.get(int(l)))
            for tr in net.trafo.index:
                est_p_hv[int(tr)].append(et.get(int(tr)))
        result = {"n": n, "bus_vm": vm, "line_i_ka": i_ka, "line_loading": loading,
                  "trafo_p_hv": tr_p_hv, "trafo_loading": tr_loading,
                  "est_bus_vm": est_vm if do_est else {},
                  "est_line_i_ka": est_i if do_est else {},
                  "est_trafo_p_hv": est_p_hv if do_est else {},
                  "_sig": sig}
        self._daily_by_day[d] = result
        return result

    def node_profiles(self, bus: int) -> dict:
        """A bus's daily curves: input p_mw split into residential / EV loads and PV
        generation (``EV_*`` loads, ``PV_*`` sgen), plus its voltage over the day."""
        p, net = self.prof, self.net
        cats: dict[str, "np.ndarray | None"] = {}
        def add(key, row):
            cats[key] = row.copy() if cats.get(key) is None else cats[key] + row
        for i, li in enumerate(p.load_idx):
            if int(net.load.at[li, "bus"]) != bus:
                continue
            name = str(net.load.at[li, "name"] or "")
            add("ev" if name.startswith("EV_") else "residential", p.load_p[i])
        for i, si in enumerate(p.sgen_idx):
            if int(net.sgen.at[si, "bus"]) != bus:
                continue
            # real-PV day → this system's peak × the day's shape; else built-in
            row = (self.sgen_peak[i] * self.pv_days[self.day % self.pv_days.shape[0]]
                   if self.pv_days is not None else p.sgen_p[i])
            add("pv", row)
        order = ["residential", "ev", "pv"]
        series = [{"kind": k, "p_mw": [_r(v) for v in cats[k]]}
                  for k in order if cats.get(k) is not None]
        name = str(net.bus.at[bus, "name"]) if bus in net.bus.index else str(bus)
        d = self.daily_curves(self.day)
        return {"bus": bus, "name": name, "steps_per_day": self.steps_per_day,
                "series": series, "voltage": d["bus_vm"].get(bus, []),
                "est_voltage": d.get("est_bus_vm", {}).get(bus)}

    def line_profiles(self, line: int) -> dict:
        """A line's daily current + loading curves, with its rated current (the
        ampacity limit = ``max_i_ka × parallel``)."""
        net = self.net
        d = self.daily_curves(self.day)
        rated = float(net.line.at[line, "max_i_ka"]) * int(net.line.at[line, "parallel"])
        return {
            "line": line, "name": str(net.line.at[line, "name"] or line),
            "from_bus": int(net.line.at[line, "from_bus"]), "to_bus": int(net.line.at[line, "to_bus"]),
            "steps_per_day": self.steps_per_day, "rated_i_ka": _r(rated),
            "current": d["line_i_ka"].get(line, []), "loading": d["line_loading"].get(line, []),
            "est_current": d.get("est_line_i_ka", {}).get(line),
        }

    def trafo_profiles(self, trafo: int) -> dict:
        """A transformer's daily power exchange (HV-side P) + loading curves, with
        its rated apparent power (``sn_mva × parallel``) as the capacity limit."""
        net = self.net
        d = self.daily_curves(self.day)
        rated = float(net.trafo.at[trafo, "sn_mva"]) * int(net.trafo.at[trafo, "parallel"])
        return {
            "trafo": trafo, "name": str(net.trafo.at[trafo, "name"] or trafo),
            "hv_bus": int(net.trafo.at[trafo, "hv_bus"]), "lv_bus": int(net.trafo.at[trafo, "lv_bus"]),
            "steps_per_day": self.steps_per_day, "sn_mva": _r(rated),
            "power": d["trafo_p_hv"].get(trafo, []), "loading": d["trafo_loading"].get(trafo, []),
            "est_power": d.get("est_trafo_p_hv", {}).get(trafo),
        }

    def battery_profiles(self, storage_idx: int, samples: int = 97) -> dict | None:
        """A battery's daily SOC + charge/discharge curve for the current day. The
        controllers read profiles/price only (no power flow), so this integrates
        SOC over the full day cheaply — on throwaway battery copies starting at
        50 %, so the live SOC is untouched. Includes the price curve for context."""
        target = next((b for b in self.batteries if b.storage_idx == storage_idx), None)
        if target is None:
            return None
        day, spd = self.day, self.steps_per_day
        dt_h = 24.0 / spd
        n = max(2, min(samples, spd))
        sample_at = {round(i * (spd - 1) / (n - 1)) for i in range(n)}
        bs = [Battery(bus=b.bus, capacity_mwh=b.capacity_mwh, power_mw=b.power_mw, mode=b.mode,
                      eff=b.eff, soc_min=b.soc_min, soc_max=b.soc_max,
                      soc_mwh=0.5 * b.capacity_mwh, name=b.name, storage_idx=b.storage_idx)
              for b in self.batteries]
        tgt = next(b for b in bs if b.storage_idx == storage_idx)
        soc: list = []; power: list = []; price: list = []
        price_lo = price_hi = None
        for t in range(spd):
            sgen_col = self._sgen_p_col(day, t) if self.prof.sgen_idx else None
            total_load = float(self.prof.load_p[:, t].sum()) if self.prof.load_p.size else 0.0
            total_gen = float(sgen_col.sum()) if sgen_col is not None and sgen_col.size else 0.0
            pctx = self._price_ctx(day, t)
            base = {"through_mw": total_load - total_gen,
                    "peak_hi_mw": self._peak_ref_mw * 0.6, "peak_lo_mw": self._peak_ref_mw * 0.3, **pctx}
            p_tgt = 0.0
            for b in bs:
                ctx = dict(base)
                if b.mode == "self":
                    ctx["load_mw"] = sum(float(self.prof.load_p[i, t]) for i in self._loads_at.get(b.bus, []))
                    ctx["pv_mw"] = (sum(float(sgen_col[i]) for i in self._sgens_at.get(b.bus, []))
                                    if sgen_col is not None and sgen_col.size else 0.0)
                p = bat.setpoint(b, ctx, dt_h)
                bat.integrate(b, p, dt_h)
                if b is tgt:
                    p_tgt = p
            if t in sample_at:
                soc.append(_r(tgt.soc_frac() * 100.0)); power.append(_r(p_tgt))
                price.append(_r(pctx["price"]) if "price" in pctx else None)
                price_lo, price_hi = pctx.get("price_lo", price_lo), pctx.get("price_hi", price_hi)
        return {
            "index": storage_idx, "bus": target.bus, "name": target.name, "mode": target.mode,
            "steps_per_day": spd, "capacity_kwh": _r(target.capacity_mwh * 1000.0),
            "power_kw": _r(target.power_mw * 1000.0), "soc": soc, "power": power,
            "price": price, "price_lo": _r(price_lo) if price_lo is not None else None,
            "price_hi": _r(price_hi) if price_hi is not None else None,
        }


def _r(value, ndigits: int = 6):
    """Round to JSON-safe floats. Non-finite (NaN/±Inf — e.g. isolated buses)
    becomes ``None`` so the payload is valid JSON for strict parsers (browsers)."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return round(f, ndigits) if math.isfinite(f) else None
