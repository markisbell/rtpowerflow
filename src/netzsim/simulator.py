"""The core: apply one time step's profiles to the net and solve the power flow."""
from __future__ import annotations

import copy
import dataclasses
import math
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandapower as pp

from . import battery as bat
from .battery import MODES, Battery
from .controller import SCOPES, Controller
from .data_loader import InputData
from .estimator import EstConfig, Estimator, HierarchicalEstimator, wants_hierarchy
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
    # placed overload controllers with their live curtailment factors
    controllers: list[dict[str, Any]] = field(default_factory=list)
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


def _gen_kind(kind: str | None) -> str:
    """Normalize a generation spec's ``kind``. ``None`` -> "pv" (legacy LV
    grids: every sgen is rooftop PV); recognizes wind/biogas (gridedit MV
    grids, ding0 MV generators); anything else -> "gen"."""
    k = (kind or "pv").lower()
    if "pv" in k or "solar" in k:
        return "pv"
    if "wind" in k:
        return "wind"
    if "bio" in k:
        return "biogas"
    return "gen"


class Simulator:
    """Holds the pandapower net + profiles and solves any single step on demand."""

    def __init__(self, data: InputData, warm_start: bool = True):
        self.data = data
        self.warm_start = warm_start
        self.net, self.prof = build_network(data)
        self.steps_per_day = data.steps_per_day
        # vertical MV/LV structure: ONS cells from the importer (may be empty —
        # file-based grids carry none). `cell_of_bus` is the derived membership
        # map every later phase (hierarchical estimation, cell controllers)
        # builds on; lumped cells have no member buses.
        self.cells: list[dict[str, Any]] = [dict(c) for c in data.cells]
        self.cell_of_bus: dict[int, str] = {
            int(b): c["id"] for c in self.cells for b in c.get("buses", [])}
        self._solved_once = False
        self._coords: tuple[dict[int, list[float]], dict[int, list[float]]] | None = None
        # multi-day real PV (optional): per-day normalised 0..1 shapes applied as a
        # scale on each PV sgen. `day` selects the active day; graphs cache per day.
        self.day = 0
        self.pv_days: np.ndarray | None = None
        self.sgen_peak = self.prof.sgen_p.max(axis=1) if self.prof.sgen_p.size else np.zeros(0)
        # per-sgen kind ("pv" | "wind" | "biogas" | "gen"): only PV systems
        # follow the real measured solar day; wind/biogas keep their profiles
        self.sgen_kind = [_gen_kind(g.kind) for g in data.generation.gens]
        self._sgen_is_pv = np.array([k == "pv" for k in self.sgen_kind], dtype=bool)
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
        self._estimator: Estimator | HierarchicalEstimator | None = None
        self._sgen_day_mean_cache: dict[int, np.ndarray] = {}
        self._est_last: dict | None = None
        self._est_wall = 0.0            # monotonic time of the last estimation run
        self._est_ms = 0.0              # its duration (drives the adaptive spacing)
        # daily-sweep estimate raster in MINUTES (15/30/60/120): decided ONCE
        # per grid from a robust cost measurement, then pinned, so the
        # estimated curves keep one consistent resolution for the session
        self._est_sweep_min: int | None = None
        # estimation policy (config tab); which load rows are real households
        self.est_config = EstConfig()
        self._load_household: list[bool] = [
            getattr(ld, "household", None) is not False for ld in data.load.loads]
        # metering points per building (multi-family houses sum n profiles);
        # the SLP pseudo basis scales with it — the DSO knows its meter counts
        self._load_households: list[int] = [
            int(getattr(ld, "households", None) or 1) for ld in data.load.loads]

        # bus-addressed journal of runtime DER changes — the delta a scenario
        # file stores on top of the deterministic grid + loadgen recipe.
        self.der_log: list[dict] = []

        # local battery storage (added at runtime); prices drive the "price" mode.
        self.batteries: list[Battery] = []
        # placed overload controllers (like batteries: per-simulator, reset on swap)
        self.controllers: list[Controller] = []
        self._next_cid = 1
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

    def set_est_config(self, cfg: EstConfig) -> None:
        """Swap the estimation policy (config tab); the estimator is rebuilt
        with fresh per-bus profile knowledge on the next solved step, and the
        ESTIMATED day-curve layer is dropped so the day graphs re-estimate
        under the new policy — the truth sweep does not depend on it and
        stays cached (its layer sig would reject stale estimates anyway)."""
        self.est_config = cfg
        self._estimator = None
        self._est_last = None
        self._est_wall = 0.0
        for entry in self._daily_by_day.values():
            entry.pop("_est", None)

    def _make_estimator(self, net) -> "Estimator | HierarchicalEstimator":
        """The estimation policy's ``hierarchy`` knob decides the machinery:
        two-stage cell/MV WLS on districts with spliced ONS cells, the classic
        monolithic WLS everywhere else (incl. standalone LV grids, whose one
        cell has no real MV level above it)."""
        ev_rows, household_rows, hh_counts = self._est_row_sets()
        kw = dict(ev_rows=ev_rows, household_rows=household_rows,
                  household_counts=hh_counts, config=self.est_config)
        if wants_hierarchy(self.est_config, self.cells, int(len(net.bus))):
            return HierarchicalEstimator(net, self.prof, self._loads_at,
                                         self._sgens_at, self.cells, **kw)
        return Estimator(net, self.prof, self._loads_at, self._sgens_at, **kw)

    def _est_row_sets(self) -> tuple[set[int], set[int], dict[int, int]]:
        """Which load-profile rows are EV charging / real households — the
        row-level knowledge the estimation policy filters on — plus each row's
        metering-point count (multi-family buildings scale the SLP basis)."""
        ev_rows = {i for i, li in enumerate(self.prof.load_idx)
                   if "EV_" in str(self.net.load.at[li, "name"] or "")}
        household_rows = {i for i in range(len(self.prof.load_idx))
                          if i >= len(self._load_household)     # runtime rows
                          or self._load_household[i]}
        counts = {i: self._load_households[i]
                  for i in range(min(len(self.prof.load_idx),
                                     len(self._load_households)))
                  if self._load_households[i] > 1}
        return ev_rows, household_rows, counts

    def set_pv_days(self, shapes: np.ndarray | None) -> None:
        """Attach per-day PV shapes ([n_days, steps], 0..1). Applied only if the
        grid actually has PV sgens (wind/biogas-only grids stay on their built-in
        profiles); clears the per-day graph cache."""
        self.pv_days = shapes if (shapes is not None and len(shapes)
                                  and bool(self._sgen_is_pv.any())) else None
        self._daily_by_day.clear()

    @property
    def n_days(self) -> int:
        return int(self.pv_days.shape[0]) if self.pv_days is not None else 1

    def _sgen_p_col(self, day: int, t: int) -> "np.ndarray":
        """sgen active power at step ``t`` — PV systems get the real day's shape
        × their peak when real PV is loaded; everything else (wind, biogas)
        always keeps its built-in profile."""
        if self.pv_days is not None:
            col = self.prof.sgen_p[:, t].copy()
            real = self.sgen_peak * self.pv_days[day % self.pv_days.shape[0], t]
            col[self._sgen_is_pv] = real[self._sgen_is_pv]
            return col
        return self.prof.sgen_p[:, t]

    def _sgen_day_mean(self, day: int) -> "np.ndarray":
        """Per-sgen mean generation of the active day — the operator's coarse PV
        knowledge, used as pseudo-measurement input by the state estimator."""
        d = day % self.n_days
        if d not in self._sgen_day_mean_cache:
            if not self.prof.sgen_p.size:
                self._sgen_day_mean_cache[d] = np.zeros(0)
            elif self.pv_days is not None:
                m = self.prof.sgen_p.mean(axis=1)
                m[self._sgen_is_pv] = (self.sgen_peak[self._sgen_is_pv]
                                       * float(self.pv_days[d].mean()))
                self._sgen_day_mean_cache[d] = m
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

    def set_battery_size(self, storage_idx: int, capacity_kwh: float,
                         power_kw: float) -> bool:
        """Resize a deployed battery in place (capacity + power rating). The
        SOC keeps its *fraction*; the storage table and the battery-aware
        daily sweeps are rebuilt."""
        for b in self.batteries:
            if b.storage_idx == storage_idx:
                frac = b.soc_frac()
                b.capacity_mwh = max(0.0, capacity_kwh) / 1000.0
                b.power_mw = max(0.0, power_kw) / 1000.0
                b.soc_mwh = frac * b.capacity_mwh
                self._rebuild_storage()
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

    # -- overload controllers (netzdienliche Steuerung) -------------------- #
    def add_controller(self, scope: str = "station", bus: int | None = None,
                       limit_pct: float = 100.0) -> Controller:
        """Place an overload controller — ``station`` throttles all EV/PV of
        the grid on trafo/line overload, ``bus`` only the DERs at one node
        (reacting to the lines that touch it)."""
        if scope not in SCOPES:
            raise ValueError(f"scope must be one of {SCOPES}")
        if scope == "bus" and (bus is None or bus not in self.net.bus.index):
            raise KeyError(bus)
        c = Controller(cid=self._next_cid, scope=scope,
                       bus=int(bus) if bus is not None else None,
                       limit_pct=float(limit_pct))
        self._next_cid += 1
        self.controllers.append(c)
        return c

    def remove_controller(self, cid: int) -> bool:
        n = len(self.controllers)
        self.controllers = [c for c in self.controllers if c.cid != cid]
        return len(self.controllers) != n

    def set_controller(self, cid: int, limit_pct: float) -> bool:
        for c in self.controllers:
            if c.cid == cid:
                c.limit_pct = float(limit_pct)
                c.release_pct = min(c.release_pct, c.limit_pct - 5.0)
                return True
        return False

    def _controller_rows(self, c: Controller) -> tuple[list[int], list[int]]:
        """Profile row indices (EV loads, PV sgens) in the controller's scope."""
        ev_rows = [i for i, li in enumerate(self.prof.load_idx)
                   if "EV_" in str(self.net.load.at[li, "name"] or "")
                   and (c.scope == "station"
                        or int(self.net.load.at[li, "bus"]) == c.bus)]
        pv_rows = [i for i, si in enumerate(self.prof.sgen_idx)
                   if i < len(self._sgen_is_pv) and self._sgen_is_pv[i]
                   and (c.scope == "station"
                        or int(self.net.sgen.at[si, "bus"]) == c.bus)]
        return ev_rows, pv_rows

    def _apply_controller_factors(self) -> None:
        """Scale EV loads / PV sgens by the strictest covering controller —
        called after the step profiles are written, before the batteries."""
        ev_f: dict[int, float] = {}
        pv_f: dict[int, float] = {}
        for c in self.controllers:
            ev_rows, pv_rows = self._controller_rows(c)
            for i in ev_rows:
                ev_f[i] = min(ev_f.get(i, 1.0), c.ev_factor)
            for i in pv_rows:
                pv_f[i] = min(pv_f.get(i, 1.0), c.pv_factor)
        for i, f in ev_f.items():
            if f < 1.0:
                li = self.prof.load_idx[i]
                self.net.load.at[li, "p_mw"] *= f
                self.net.load.at[li, "q_mvar"] *= f
        for i, f in pv_f.items():
            if f < 1.0:
                si = self.prof.sgen_idx[i]
                self.net.sgen.at[si, "p_mw"] *= f
                self.net.sgen.at[si, "q_mvar"] *= f

    def _controller_update(self, res: "StepResult") -> None:
        """One control step per controller — fed ONLY from the operator's view:
        meter readings (``res.measurements``) and the WLS state estimate
        (``res.estimated``, the last available run; a field controller also
        works on the latest received telegram). The true power flow never
        reaches the control law, so an overload that neither a meter nor the
        estimate reveals is NOT acted upon: control quality = observability.
        Without any data a controller is blind and holds its factors."""
        meas = res.measurements or {}
        est = res.estimated if isinstance(res.estimated, dict) else {}
        est_lines = est.get("lines") or []
        est_trafos = est.get("trafos") or []
        for c in self.controllers:
            seen: list[tuple[float, str]] = []
            if c.scope == "station":
                # measured transformer loadings are direct device readings ...
                for tm in meas.get("trafos", []):
                    if tm.get("loading_percent") is not None:
                        seen.append((float(tm["loading_percent"]), "meter"))
                # ... everything else (line loadings!) exists only estimated
                for row in (*est_lines, *est_trafos):
                    if row.get("loading_percent") is not None:
                        seen.append((float(row["loading_percent"]), "estimate"))
                # flow direction from the HV-side trafo flow (measured before
                # estimated): backfeed into the upper grid = domain exports
                p_hv = [tm["p_hv_mw"] for tm in meas.get("trafos", [])
                        if tm.get("p_hv_mw") is not None]
                if not p_hv:
                    p_hv = [tr["p_hv_mw"] for tr in est_trafos
                            if tr.get("p_hv_mw") is not None]
                exporting = bool(p_hv) and sum(p_hv) < 0.0
            else:
                ln_df = self.net.line
                adj = {int(li) for li in ln_df.index
                       if c.bus in (int(ln_df.at[li, "from_bus"]),
                                    int(ln_df.at[li, "to_bus"]))}
                # lines carry no meters in this model — adjacent loadings are
                # only knowable through the state estimate
                for ln in est_lines:
                    if ln.get("index") in adj and ln.get("loading_percent") is not None:
                        seen.append((float(ln["loading_percent"]), "estimate"))
                # direction from the node's own meter, else its estimated
                # injection (res_bus convention: p > 0 consumes, p < 0 feeds in)
                p_bus = next((n["p_mw"] for n in meas.get("nodes", [])
                              if n.get("bus") == c.bus and n.get("p_mw") is not None),
                             None)
                if p_bus is None:
                    p_bus = next((b["p_mw"] for b in est.get("buses") or []
                                  if b.get("index") == c.bus and b.get("p_mw") is not None),
                                 None)
                exporting = p_bus is not None and float(p_bus) < 0.0
            if seen:
                c.seen_pct, c.seen_src = max(seen, key=lambda v: v[0])
                c.update(c.seen_pct, exporting)
            else:
                c.seen_pct = c.seen_src = None
                c.update(None, False)

    # -- runtime DER configuration (PV / EV per node) ---------------------- #
    def _der_log_put(self, entry: dict, replaces: tuple[str, ...]) -> None:
        """Append a journal entry, dropping superseded ops for the same bus."""
        self.der_log = [e for e in self.der_log
                        if not (e["bus"] == entry["bus"] and e["op"] in replaces)]
        self.der_log.append(entry)

    def apply_der_op(self, op: dict) -> bool:
        """Replay one bus-addressed DER op (scenario load). Tolerant: an op that
        no longer applies (e.g. remove on an absent system) is a no-op."""
        kind, bus = op.get("op"), int(op.get("bus", -1))
        if bus not in self.net.bus.index:
            return False
        der = self.node_der(bus)
        if kind == "add_pv" or kind == "set_pv":
            if der["pv"] is None:
                self.add_pv(bus, float(op.get("kwp", 5.0)))
            else:
                self.set_pv_kwp(der["pv"]["sgen"], float(op.get("kwp", 5.0)))
            return True
        if kind == "remove_pv":
            return der["pv"] is not None and self.remove_pv(der["pv"]["sgen"])
        if kind == "add_ev" or kind == "set_ev":
            start = int(op.get("start_min", 18 * 60))
            dur = int(op.get("dur_min", 120))
            if der["ev"] is None:
                self.add_ev(bus, float(op.get("kw", 11.0)), start, dur)
            else:
                self.set_ev(der["ev"]["load"], start, dur)
            return True
        if kind == "remove_ev":
            return der["ev"] is not None and self.remove_ev(der["ev"]["load"])
        return False

    def _der_invalidate(self) -> None:
        """Refresh everything derived from the load/sgen tables + profiles after
        a runtime DER change (same hygiene as a battery add/remove)."""
        self.sgen_peak = self.prof.sgen_p.max(axis=1) if self.prof.sgen_p.size else np.zeros(0)
        self._sgen_is_pv = np.array([k == "pv" for k in self.sgen_kind], dtype=bool)
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
        self._solved_once = False       # element tables changed: solve cold next

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
        self.sgen_kind.append("pv")
        self._der_log_put({"op": "add_pv", "bus": int(bus), "kwp": kwp},
                          ("add_pv", "set_pv", "remove_pv"))
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
        bus = int(self.net.sgen.at[sgen, "bus"])
        # an earlier add_pv keeps its place in the log; only its size updates
        if any(e["op"] == "add_pv" and e["bus"] == bus for e in self.der_log):
            self._der_log_put({"op": "add_pv", "bus": bus, "kwp": kwp},
                              ("add_pv", "set_pv"))
        else:
            self._der_log_put({"op": "set_pv", "bus": bus, "kwp": kwp}, ("set_pv",))
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
        self._load_household.append(False)     # EV charging is not a household
        self._load_households.append(1)
        self._set_ev_row(len(self.prof.load_idx) - 1, kw, start_min, dur_min)
        self._der_log_put({"op": "add_ev", "bus": int(bus), "kw": float(kw),
                           "start_min": int(start_min) % 1440,
                           "dur_min": int(min(max(dur_min, 60), 240))},
                          ("add_ev", "set_ev", "remove_ev"))
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
        bus = int(self.net.load.at[load, "bus"])
        clamped = int(min(max(dur_min, 60), 240))
        if any(e["op"] == "add_ev" and e["bus"] == bus for e in self.der_log):
            self._der_log_put({"op": "add_ev", "bus": bus, "kw": kw if kw > 0.1 else 11.0,
                               "start_min": int(start_min) % 1440, "dur_min": clamped},
                              ("add_ev", "set_ev"))
        else:
            self._der_log_put({"op": "set_ev", "bus": bus,
                               "start_min": int(start_min) % 1440, "dur_min": clamped},
                              ("set_ev",))
        self._der_invalidate()
        return True

    def remove_pv(self, sgen: int) -> bool:
        """Remove a PV system (LoadStudio-assigned or runtime-added)."""
        if sgen not in self.prof.sgen_idx:
            return False
        bus = int(self.net.sgen.at[sgen, "bus"])
        was_added = any(e["op"] == "add_pv" and e["bus"] == bus for e in self.der_log)
        self._der_log_put({"op": "remove_pv", "bus": bus},
                          ("add_pv", "set_pv", "remove_pv"))
        if was_added:                       # runtime add + remove = no delta
            self.der_log = [e for e in self.der_log
                            if not (e["op"] == "remove_pv" and e["bus"] == bus)]
        i = self.prof.sgen_idx.index(sgen)
        self.prof.sgen_idx.pop(i)
        self.prof.sgen_p = np.delete(self.prof.sgen_p, i, axis=0)
        self.prof.sgen_q = np.delete(self.prof.sgen_q, i, axis=0)
        if i < len(self.sgen_kind):
            self.sgen_kind.pop(i)
        self.net.sgen.drop(sgen, inplace=True)
        self._der_invalidate()
        return True

    def remove_ev(self, load: int) -> bool:
        """Remove an EV charging load (LoadStudio-assigned or runtime-added)."""
        if load not in self.prof.load_idx:
            return False
        bus = int(self.net.load.at[load, "bus"])
        was_added = any(e["op"] == "add_ev" and e["bus"] == bus for e in self.der_log)
        self._der_log_put({"op": "remove_ev", "bus": bus},
                          ("add_ev", "set_ev", "remove_ev"))
        if was_added:                       # runtime add + remove = no delta
            self.der_log = [e for e in self.der_log
                            if not (e["op"] == "remove_ev" and e["bus"] == bus)]
        i = self.prof.load_idx.index(load)
        self.prof.load_idx.pop(i)
        self.prof.load_p = np.delete(self.prof.load_p, i, axis=0)
        self.prof.load_q = np.delete(self.prof.load_q, i, axis=0)
        if i < len(self._load_household):
            self._load_household.pop(i)
        if i < len(self._load_households):
            self._load_households.pop(i)
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
        self._solved_once = False       # element tables changed: solve cold next
        self.net.storage.drop(self.net.storage.index, inplace=True)
        for b in self.batteries:
            b.storage_idx = int(pp.create_storage(
                self.net, bus=b.bus, p_mw=0.0, max_e_mwh=b.capacity_mwh,
                soc_percent=b.soc_frac() * 100.0, name=b.name))

    # -- observability / measurement placement --------------------------- #
    def measurement_placement(self) -> dict:
        """Current meter placement + coverage (no power-flow results needed)."""
        return self.meters.placement(self.net, cells=self.cells)

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

    def apply_meter_preset(self, name: str, cell: str | None = None) -> None:
        self.meters.apply_preset(name, self.net, cells=self.cells, cell=cell)

    def set_meter_mode(self, name: str) -> None:
        """Bulk meter fidelity: switch EVERY placed device and the default for
        new ones — "full" (TAF 9/10/14: V/P/Q/I per step) or "standard"
        (TAF 7 Lastgang: 15-min mean P only). Per-device overrides via
        ``set_node_meter_mode`` / ``set_trafo_meter_mode``; the daily-sweep
        estimate cache re-keys on the modes automatically."""
        self.meters.set_mode(name)

    def set_node_meter_mode(self, bus: int, name: str) -> None:
        self.meters.set_node_mode(bus, name)

    def set_trafo_meter_mode(self, trafo: int, name: str) -> None:
        self.meters.set_trafo_mode(trafo, name)

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
        # overload controllers throttle EV/PV before the batteries react, so a
        # battery sees the actually available (curtailed) local generation/load
        if self.controllers:
            self._apply_controller_factors()
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
            except Exception as exc:  # noqa: BLE001
                # e.g. a runtime mutation (DER/battery via the API) racing this
                # solve leaves pandapower's internals momentarily inconsistent.
                # Never let a single step kill the engine loop — report the
                # step as failed and start cold next time; the next solve
                # rebuilds the internals and self-heals.
                error = f"{type(exc).__name__}: {exc}"
                self._solved_once = False
        if converged:
            error = None
        solve_ms = (time.perf_counter() - t0) * 1000.0

        res = self._collect(step, day, t, converged, solve_ms, error)
        # state estimation (the operator's calculated view) — only meaningful
        # once at least one meter is placed, and only from a converged truth
        if converged and (self.meters.node_buses or self.meters.trafo_idxs):
            if self._estimator is None:
                self._estimator = self._make_estimator(self.net)
            # the estimate can only be as fine as the METERING raster: when
            # EVERY device is a TAF-7 Lastgang meter, new readings arrive only
            # once per 15-min window, so a new estimate is only due at window
            # boundaries; a single TAF-9/10/14 device (1-min) makes every step
            # worth estimating. The wall-clock spacing below is purely a
            # compute guard for very large grids.
            raster = (max(1, round(15 * self.steps_per_day / 1440))
                      if self.meters.all_standard else 1)
            now = time.monotonic()
            if t % raster == 0 and now - self._est_wall >= 2.0 * self._est_ms / 1000.0:
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
        # closed control loop: factors computed from THIS step's OBSERVED view
        # (meters + estimate, never the truth) throttle the NEXT step — runs
        # after the estimation so the controller sees the freshest estimate
        if converged and self.controllers:
            self._controller_update(res)
            res.controllers = [c.as_dict() for c in self.controllers]
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
        res.controllers = [c.as_dict() for c in self.controllers]

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
            # vertical MV/LV structure: ONS cells (empty for legacy/file grids)
            "cells": [dict(c) for c in self.cells],
            "n_load": int(len(net.load)),
            "n_sgen": int(len(net.sgen)),
            "n_trafo": int(len(net.trafo)),
        }

    def _bus_coordinates(self) -> tuple[dict[int, list[float]], dict[int, list[float]]]:
        if self._coords is None:
            from .layout import compute_layouts
            self._coords = compute_layouts(self.net)
        return self._coords

    def daily_curves(self, day: int | None = None) -> dict:
        """Sweep a whole day once (on an ISOLATED net, so the live engine is not
        disturbed) and cache per-bus voltage/injection + per-line current/loading
        + per-trafo power — the TRUTH layer of the day graphs. The power flow is
        solved at EVERY step, so the curves keep the full input raster (1 minute
        on the committed grids) — affordable because consecutive solves are
        recycled (only the bus P/Q injections are rebuilt; validated
        bit-identical to full solves). Deliberately WITHOUT the state estimator:
        the pure-Lastfluss view must not pay for WLS runs — the estimated layer
        is computed lazily by ``daily_est`` and the measured layer is derived
        from these arrays by ``measured_curves``. Cached per day, since real-PV
        days differ; a grid without real PV only ever uses day 0."""
        d = self.day if day is None else day
        cached = self._daily_by_day.get(d)
        if cached is not None:
            return cached
        # Snapshot the LIVE net (not a rebuild from the input data): runtime
        # DER changes — added/resized PV, EV windows, removals — exist only on
        # the live net/profiles, and self._loads_at/_sgens_at row indices refer
        # to self.prof. The sweep only reads prof and re-solves on its copy.
        net = copy.deepcopy(self.net)
        net.storage.drop(net.storage.index, inplace=True)  # batteries recreated fresh below
        prof = self.prof
        spd = self.steps_per_day
        bs = self._sweep_batteries(net)
        bus_ids = [int(b) for b in net.bus.index]
        line_ids = [int(l) for l in net.line.index]
        trafo_ids = [int(tr) for tr in net.trafo.index]
        # truth curves as [n_elem, spd] arrays: unsolved steps stay NaN → null
        vm_a = np.full((len(bus_ids), spd), np.nan)
        busp_a = np.full((len(bus_ids), spd), np.nan)
        ika_a = np.full((len(line_ids), spd), np.nan)
        load_a = np.full((len(line_ids), spd), np.nan)
        trp_a = np.full((len(trafo_ids), spd), np.nan)
        trl_a = np.full((len(trafo_ids), spd), np.nan)
        for t, solved in self._sweep_solves(net, bs, d, range(spd)):
            if not solved:
                continue
            vm_a[:, t] = net.res_bus["vm_pu"].to_numpy()
            busp_a[:, t] = net.res_bus["p_mw"].to_numpy()
            if line_ids:
                ika_a[:, t] = net.res_line["i_ka"].to_numpy()
                load_a[:, t] = net.res_line["loading_percent"].to_numpy()
            if trafo_ids:
                trp_a[:, t] = net.res_trafo["p_hv_mw"].to_numpy()
                trl_a[:, t] = net.res_trafo["loading_percent"].to_numpy()
        result = {"n": spd,
                  "bus_vm": {b: [_r(x) for x in vm_a[i]] for i, b in enumerate(bus_ids)},
                  "bus_p": {b: [_r(x) for x in busp_a[i]] for i, b in enumerate(bus_ids)},
                  "line_i_ka": {l: [_r(x) for x in ika_a[i]] for i, l in enumerate(line_ids)},
                  "line_loading": {l: [_r(x) for x in load_a[i]] for i, l in enumerate(line_ids)},
                  "trafo_p_hv": {tr: [_r(x) for x in trp_a[i]] for i, tr in enumerate(trafo_ids)},
                  "trafo_loading": {tr: [_r(x) for x in trl_a[i]] for i, tr in enumerate(trafo_ids)}}
        self._daily_by_day[d] = result
        return result

    def _sweep_batteries(self, net) -> list[Battery]:
        """Recreate the live batteries on an isolated sweep net (fresh SOC 50 %)."""
        return [Battery(bus=b.bus, capacity_mwh=b.capacity_mwh, power_mw=b.power_mw,
                        mode=b.mode, eff=b.eff, soc_min=b.soc_min, soc_max=b.soc_max,
                        soc_mwh=0.5 * b.capacity_mwh, name=b.name,
                        storage_idx=int(pp.create_storage(net, bus=b.bus, p_mw=0.0,
                                                          max_e_mwh=b.capacity_mwh,
                                                          soc_percent=50.0, name=b.name)))
                for b in self.batteries]

    def _sweep_solves(self, net, bs: list[Battery], d: int, solve_at):
        """Drive one day on the sweep net: integrate battery SOC at EVERY step,
        solve the power flow at the steps in ``solve_at`` (recycled after the
        first success — Ybus unchanged, only bus injections move — with the
        run_step retry ladder as fallback) and yield ``(t, solved)`` there."""
        prof = self.prof
        spd = self.steps_per_day
        dt_h = 24.0 / spd
        sgen_peak = prof.sgen_p.max(axis=1) if prof.sgen_p.size else np.zeros(0)
        want = set(int(t) for t in solve_at)
        # a varying slack setpoint is NOT covered by the bus-P/Q recycle path
        use_recycle = not ((prof.ext_vm.size and float(np.ptp(prof.ext_vm, axis=1).max()) > 1e-12)
                           or (prof.ext_va.size and float(np.ptp(prof.ext_va, axis=1).max()) > 1e-12))
        warm = False
        for t in range(spd):
            col = prof.sgen_p[:, t]
            if self.pv_days is not None:
                col = col.copy()
                real = sgen_peak * self.pv_days[d % self.pv_days.shape[0], t]
                col[self._sgen_is_pv] = real[self._sgen_is_pv]
            if bs:  # advance SOC every step + set storage setpoints on the net
                self._apply_batteries(net, bs, prof, col, d, t, dt_h,
                                      self._loads_at, self._sgens_at, do_integrate=True)
            if t not in want:
                continue
            net.load.loc[prof.load_idx, "p_mw"] = prof.load_p[:, t]
            net.load.loc[prof.load_idx, "q_mvar"] = prof.load_q[:, t]
            net.sgen.loc[prof.sgen_idx, "p_mw"] = col
            net.sgen.loc[prof.sgen_idx, "q_mvar"] = prof.sgen_q[:, t]
            net.ext_grid.loc[prof.ext_idx, "vm_pu"] = prof.ext_vm[:, t]
            net.ext_grid.loc[prof.ext_idx, "va_degree"] = prof.ext_va[:, t]
            solved = False
            if warm and use_recycle:
                try:
                    pp.runpp(net, calculate_voltage_angles=True, init="results",
                             recycle={"bus_pq": True, "trafo": False, "gen": False})
                    solved = True
                except Exception:  # noqa: BLE001
                    solved = False
            if not solved:
                # same retry ladder as run_step: flat init rescues the steps where
                # the warm/dc-init Newton stalls on ill-conditioned district nets
                # ("auto", not "flat", first so multi-voltage-level grids with a
                # transformer converge on the first solve, matching the live loop)
                for kw in (dict(init="results" if warm else "auto"),
                           dict(init="flat"),
                           dict(init="flat", algorithm="iwamoto_nr", max_iteration=30)):
                    try:
                        pp.runpp(net, calculate_voltage_angles=True, **kw)
                        solved = True
                        break
                    except Exception:  # noqa: BLE001
                        continue
            warm = solved
            yield t, solved

    def _raster_steps(self, minutes: float) -> int:
        return max(1, round(minutes / (1440.0 / self.steps_per_day)))

    def measured_curves(self, day: int | None = None) -> dict:
        """The MEASURED layer of the day graphs, derived from the truth sweep:
        only elements that carry a device, only the quantities THAT device
        delivers, in ITS metering raster — a full-mode device (TAF 9/10/14)
        passes the 1-min values through, a standard-mode device (TAF 7
        Lastgang) reduces to the 15-min window mean of the ACTIVE power
        (voltage/loading unknown). Modes mix freely per device. Cheap array
        math on the cached truth arrays — never cached itself, so
        placement/mode changes take effect immediately."""
        d = self.day if day is None else day
        truth = self.daily_curves(d)
        spd = self.steps_per_day
        win = self._raster_steps(15)
        full_raster = round(1440 / spd)

        def window_mean(row: list) -> list:
            out: list = [None] * spd
            for w0 in range(0, spd, win):
                vals = [v for v in row[w0:w0 + win] if v is not None]
                if vals:
                    mean = _r(sum(vals) / len(vals))
                    for i in range(w0, min(w0 + win, spd)):
                        out[i] = mean
            return out

        nodes: dict[int, dict] = {}
        for b in sorted(self.meters.node_buses):
            p = truth["bus_p"].get(b)
            if p is None:
                continue
            if self.meters.mode_of_node(b) == "standard":
                nodes[b] = {"p_mw": window_mean(p), "vm": None, "raster_min": 15}
            else:
                nodes[b] = {"p_mw": p, "vm": truth["bus_vm"].get(b),
                            "raster_min": full_raster}
        trafos: dict[int, dict] = {}
        for tr in sorted(self.meters.trafo_idxs):
            p = truth["trafo_p_hv"].get(tr)
            if p is None:
                continue
            if self.meters.mode_of_trafo(tr) == "standard":
                trafos[tr] = {"p_hv": window_mean(p), "loading": None, "raster_min": 15}
            else:
                trafos[tr] = {"p_hv": p, "loading": truth["trafo_loading"].get(tr),
                              "raster_min": full_raster}
        return {"nodes": nodes, "trafos": trafos}

    def daily_est(self, day: int | None = None) -> dict:
        """The ESTIMATED layer of the day graphs: a lazy WLS mini-sweep that
        re-solves the day only at the estimate raster (pinned per-grid cost
        tier, never finer than the metering raster) and runs the estimator
        there. Stored inside the truth cache entry keyed on the meter
        placement/mode AND the estimation policy — so it is computed only when
        the Schätzung view actually asks for it, and the pure-Lastfluss /
        Gemessen views never pay for WLS runs."""
        d = self.day if day is None else day
        truth = self.daily_curves(d)
        sig = (frozenset(self.meters.node_buses), frozenset(self.meters.trafo_idxs),
               self.meters.modes_signature(), dataclasses.astuple(self.est_config))
        cached = truth.get("_est")
        if cached is not None and cached.get("_sig") == sig:
            return cached
        spd = self.steps_per_day
        # without meters there is nothing to estimate: empty layer, so the
        # profile endpoints report the estimate as absent (None), not all-null
        est_vm: dict[int, list] = {}
        est_i: dict[int, list] = {}
        est_p_hv: dict[int, list] = {}
        result = {"est_bus_vm": est_vm, "est_line_i_ka": est_i,
                  "est_trafo_p_hv": est_p_hv, "_sig": sig}
        if self.meters.node_buses or self.meters.trafo_idxs:
            est_vm.update({int(b): [None] * spd for b in self.net.bus.index})
            est_i.update({int(l): [None] * spd for l in self.net.line.index})
            est_p_hv.update({int(tr): [None] * spd for tr in self.net.trafo.index})
            net = copy.deepcopy(self.net)
            net.storage.drop(net.storage.index, inplace=True)
            bs = self._sweep_batteries(net)
            estimator = self._make_estimator(net)
            # its own MeasurementSet copy: standard-mode window accumulators
            # must not disturb the live meters' state
            sweep_meters = MeasurementSet(node_buses=set(self.meters.node_buses),
                                          trafo_idxs=set(self.meters.trafo_idxs),
                                          mode=self.meters.mode,
                                          node_modes=dict(self.meters.node_modes),
                                          trafo_modes=dict(self.meters.trafo_modes))
            est_bats = {b.bus: b.power_mw for b in self.batteries}
            meter_raster = self._raster_steps(15) if self.meters.all_standard else 1
            est_steps = max(self._raster_steps(self._est_sweep_min or 15), meter_raster)
            redo: list[int] = []
            for t, solved in self._sweep_solves(net, bs, d,
                                                range(0, spd, est_steps)):
                if not solved:
                    continue
                est = estimator.run(net, sweep_meters.observe(net, t),
                                    self._sgen_day_mean(d), est_bats)
                if est is None:
                    continue
                if self._est_sweep_min is None:
                    # decide the tier ONCE per grid: big nets sample the
                    # estimate coarser so a sweep stays interactive. Use the
                    # cheaper of this run and the live loop's smoothed cost
                    # (the first run may be a cold-start outlier), and snap to
                    # clean resolutions: 15/30/60/120 min.
                    ms = est["solve_ms"]
                    if self._est_ms > 0:
                        ms = min(ms, self._est_ms)
                    self._est_sweep_min = (15 if ms <= 120 else 30 if ms <= 240
                                           else 60 if ms <= 480 else 120)
                    coarser = max(self._raster_steps(self._est_sweep_min), meter_raster)
                    if coarser > est_steps:
                        # keep only the samples on the coarser raster; the
                        # generator keeps yielding the fine ones — skip them
                        est_steps = coarser
                if t % est_steps != 0:
                    continue
                for e in est["buses"]:
                    est_vm[e["index"]][t] = e["vm_pu"]
                for e in est["lines"]:
                    est_i[e["index"]][t] = e["i_ka"]
                for e in est["trafos"]:
                    est_p_hv[e["index"]][t] = e["p_hv_mw"]
        truth["_est"] = result
        return result

    def node_profiles(self, bus: int, view: str = "est") -> dict:
        """A bus's daily curves. ``view`` picks the layers the caller may see:
        ``truth`` = input p_mw split into residential / EV / PV (grid-model
        knowledge) + the solved voltage; ``measured`` = ONLY the meter's own
        quantities in the metering raster (net P, V in full mode — never the
        composition, which no meter can know); ``est`` = all layers overlaid."""
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
            kind = self.sgen_kind[i] if i < len(self.sgen_kind) else "pv"
            # real-PV day → a PV system's peak × the day's shape; wind/biogas
            # (and any other kind) always keep their built-in profile
            row = (self.sgen_peak[i] * self.pv_days[self.day % self.pv_days.shape[0]]
                   if self.pv_days is not None and kind == "pv" else p.sgen_p[i])
            add(kind, row)
        order = ["residential", "ev", "pv", "wind", "biogas", "gen"]
        series = [{"kind": k, "p_mw": [_r(v) for v in cats[k]]}
                  for k in order if cats.get(k) is not None]
        name = str(net.bus.at[bus, "name"]) if bus in net.bus.index else str(bus)
        out = {"bus": bus, "name": name, "steps_per_day": self.steps_per_day,
               "view": view, "series": [], "voltage": [],
               "est_voltage": None, "measured": None}
        if view in ("truth", "est"):
            d = self.daily_curves(self.day)
            out["series"] = series
            out["voltage"] = d["bus_vm"].get(bus, [])
        if view in ("measured", "est"):
            m = self.measured_curves(self.day)
            node = m["nodes"].get(bus)
            if node is not None:
                out["measured"] = dict(node)
        if view == "est":
            out["est_voltage"] = self.daily_est(self.day)["est_bus_vm"].get(bus)
        return out

    def line_profiles(self, line: int, view: str = "est") -> dict:
        """A line's daily current + loading curves, with its rated current (the
        ampacity limit = ``max_i_ka × parallel``). ``view`` picks the layers:
        lines carry no meters, so the measured view is deliberately empty."""
        net = self.net
        rated = float(net.line.at[line, "max_i_ka"]) * int(net.line.at[line, "parallel"])
        out = {
            "line": line, "name": str(net.line.at[line, "name"] or line),
            "from_bus": int(net.line.at[line, "from_bus"]), "to_bus": int(net.line.at[line, "to_bus"]),
            "steps_per_day": self.steps_per_day, "rated_i_ka": _r(rated),
            "view": view, "current": [], "loading": [], "est_current": None,
        }
        if view in ("truth", "est"):
            d = self.daily_curves(self.day)
            out["current"] = d["line_i_ka"].get(line, [])
            out["loading"] = d["line_loading"].get(line, [])
        if view == "est":
            out["est_current"] = self.daily_est(self.day)["est_line_i_ka"].get(line)
        return out

    def trafo_profiles(self, trafo: int, view: str = "est") -> dict:
        """A transformer's daily power exchange (HV-side P) + loading curves, with
        its rated apparent power (``sn_mva × parallel``) as the capacity limit.
        ``view`` picks the layers; the measured layer appears only for a metered
        transformer, in the metering raster."""
        net = self.net
        rated = float(net.trafo.at[trafo, "sn_mva"]) * int(net.trafo.at[trafo, "parallel"])
        out = {
            "trafo": trafo, "name": str(net.trafo.at[trafo, "name"] or trafo),
            "hv_bus": int(net.trafo.at[trafo, "hv_bus"]), "lv_bus": int(net.trafo.at[trafo, "lv_bus"]),
            "steps_per_day": self.steps_per_day, "sn_mva": _r(rated),
            "view": view, "power": [], "loading": [], "est_power": None, "measured": None,
        }
        if view in ("truth", "est"):
            d = self.daily_curves(self.day)
            out["power"] = d["trafo_p_hv"].get(trafo, [])
            out["loading"] = d["trafo_loading"].get(trafo, [])
        if view in ("measured", "est"):
            m = self.measured_curves(self.day)
            tm = m["trafos"].get(trafo)
            if tm is not None:
                out["measured"] = dict(tm)
        if view == "est":
            out["est_power"] = self.daily_est(self.day)["est_trafo_p_hv"].get(trafo)
        return out

    def battery_profiles(self, storage_idx: int) -> dict | None:
        """A battery's daily SOC + charge/discharge curve for the current day, in
        the full input raster (1 minute on the committed grids). The battery
        strategies read profiles/price only (no power flow), so this integrates
        SOC over the full day cheaply — on throwaway battery copies starting at
        50 %, so the live SOC is untouched. Includes the price curve for context."""
        target = next((b for b in self.batteries if b.storage_idx == storage_idx), None)
        if target is None:
            return None
        day, spd = self.day, self.steps_per_day
        dt_h = 24.0 / spd
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
