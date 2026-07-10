"""The core: apply one time step's profiles to the net and solve the power flow."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandapower as pp

from . import battery as bat
from . import control_runtime
from . import der
from . import sweeps
from .battery import MODES, Battery
from .controller import SCOPES, Controller
from .ront import RONT_STEP_PERCENT, RONT_TAP_MAX, RONT_TAP_MIN, Ront
from .data_loader import InputData
from .estimator import EstConfig, Estimator, HierarchicalEstimator, wants_hierarchy
from .measurements import MeasurementSet, _r  # _r: canonical JSON-safe rounding
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
    # activated rONTs (on-load tap changers) with their live tap positions
    ronts: list[dict[str, Any]] = field(default_factory=list)
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
        # static per-cell domains for cell controllers / the MV coordinator:
        # a cell owns its member buses, the lines between them (all its lines,
        # by construction) and its station trafo(s); everything else is the
        # MV level. A LUMPED cell is representable too — its "members" are
        # just its feeding MV bus, so a Steuerbox there can throttle the
        # aggregate DERs modelled at that bus (locally blind, but it executes
        # coordinator signals). Empty on grids without cells.
        self._cell_buses: dict[str, set[int]] = {}
        for c in self.cells:
            if not c.get("lumped") and c.get("buses"):
                self._cell_buses[c["id"]] = {int(b) for b in c["buses"]}
            elif c.get("lumped") and c.get("mv_bus") is not None:
                self._cell_buses[c["id"]] = {int(c["mv_bus"])}
        # lines/trafos belong only to SPLICED cells (a lumped cell's MV bus
        # sits on the ring — its adjacent lines stay in the coordinator's
        # domain, and the missing entry keeps its controller locally blind)
        spliced_members = {c["id"]: {int(b) for b in c["buses"]}
                           for c in self.cells
                           if not c.get("lumped") and c.get("buses")}
        self._cell_lines: dict[str, set[int]] = {
            cid: {int(li) for li in self.net.line.index
                  if int(self.net.line.at[li, "from_bus"]) in member}
            for cid, member in spliced_members.items()}
        self._cell_trafos: dict[str, set[int]] = {
            c["id"]: {int(t) for t in c.get("station_trafos", [])}
            for c in self.cells if not c.get("lumped") and c.get("buses")}
        in_cells_l = set().union(*self._cell_lines.values()) if self._cell_lines else set()
        in_cells_t = set().union(*self._cell_trafos.values()) if self._cell_trafos else set()
        self._mv_lines: set[int] = {int(li) for li in self.net.line.index} - in_cells_l
        self._mv_trafos: set[int] = {int(t) for t in self.net.trafo.index} - in_cells_t
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
        self._est_seq = 0               # counts estimation runs (telegram id)
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
        # activated rONTs (per-simulator, reset on swap); originals keep the
        # transformer's shipped tap data for a clean removal
        self.ronts: list[Ront] = []
        self._next_rid = 1
        self._ront_orig: dict[int, dict[str, Any]] = {}
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
                       limit_pct: float = 100.0,
                       cell: str | None = None) -> Controller:
        """Place an overload controller — ``station`` throttles all EV/PV of
        the grid on trafo/line overload, ``bus`` only the DERs at one node
        (reacting to the lines that touch it), ``cell`` one spliced ONS cell,
        ``mv`` the coordinating MV level (grid-traffic-light signals only)."""
        if scope not in SCOPES:
            raise ValueError(f"scope must be one of {SCOPES}")
        if scope == "bus" and (bus is None or bus not in self.net.bus.index):
            raise KeyError(bus)
        if scope == "cell" and cell not in self._cell_buses:
            raise KeyError(cell)
        if scope == "mv" and not (self._cell_buses
                                  and (self._mv_lines or self._mv_trafos)):
            raise ValueError("the MV coordinator needs spliced ONS cells "
                             "below a real MV level")
        c = Controller(cid=self._next_cid, scope=scope,
                       bus=int(bus) if bus is not None else None,
                       cell=cell if scope == "cell" else None,
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

    # ---- controller/rONT runtime (control_runtime.py) --------------------- #
    # Domain views + the per-step control passes live in control_runtime.py;
    # placement CRUD stays here (id counters, tap originals, domain sets).

    def _controller_rows(self, c: Controller) -> tuple[list[int], list[int]]:
        return control_runtime.controller_rows(self, c)

    def _apply_controller_factors(self) -> None:
        control_runtime.apply_controller_factors(self)

    def _controller_update(self, res: "StepResult") -> None:
        control_runtime.controller_update(self, res)

    # -- rONT: on-load tap changer per station transformer ------------------ #
    def add_ront(self, trafo: int, v_target: float = 1.0,
                 deadband: float = 0.015) -> Ront:
        """Activate an rONT on a transformer: upgrade its tap data in place
        (±4 × 1.5 %, neutral start) and regulate the LV busbar from now on."""
        if trafo not in self.net.trafo.index:
            raise KeyError(trafo)
        if any(r.trafo == trafo for r in self.ronts):
            raise ValueError(f"trafo {trafo} already has an rONT")
        tap_cols = ("tap_side", "tap_neutral", "tap_min", "tap_max",
                    "tap_step_percent", "tap_pos")
        self._ront_orig[int(trafo)] = {
            c: self.net.trafo.at[trafo, c] for c in tap_cols
            if c in self.net.trafo.columns}
        self.net.trafo.at[trafo, "tap_side"] = "hv"
        self.net.trafo.at[trafo, "tap_neutral"] = 0
        self.net.trafo.at[trafo, "tap_min"] = RONT_TAP_MIN
        self.net.trafo.at[trafo, "tap_max"] = RONT_TAP_MAX
        self.net.trafo.at[trafo, "tap_step_percent"] = RONT_STEP_PERCENT
        self.net.trafo.at[trafo, "tap_pos"] = 0
        cell = next((cid for cid, ts in self._cell_trafos.items() if trafo in ts), None)
        r = Ront(rid=self._next_rid, trafo=int(trafo),
                 busbar=int(self.net.trafo.at[trafo, "lv_bus"]), cell=cell,
                 v_target=float(v_target), deadband=float(deadband))
        self._next_rid += 1
        self.ronts.append(r)
        self._solved_once = False       # ratio changed: solve cold next step
        self._estimator = None          # est model copies the tap data lazily
        self._est_last = None
        return r

    def remove_ront(self, rid: int) -> bool:
        r = next((x for x in self.ronts if x.rid == rid), None)
        if r is None:
            return False
        for c, v in self._ront_orig.pop(r.trafo, {}).items():
            self.net.trafo.at[r.trafo, c] = v
        self.ronts = [x for x in self.ronts if x.rid != rid]
        self._solved_once = False
        self._estimator = None
        self._est_last = None
        return True

    def set_ront(self, rid: int, v_target: float | None = None,
                 deadband: float | None = None) -> bool:
        for r in self.ronts:
            if r.rid == rid:
                if v_target is not None:
                    r.v_target = float(v_target)
                if deadband is not None:
                    r.deadband = float(deadband)
                return True
        return False

    def _ront_update(self, res: "StepResult") -> None:
        control_runtime.ront_update(self, res)

    # ---- runtime-configurable DERs (der.py) ------------------------------- #
    # PV size + EV charge window per node, plus the bus-addressed DER journal
    # scenarios replay. The logic lives in der.py; the journal (`der_log`) and
    # the mutated profile rows stay HERE on the Simulator, so scenario recipes,
    # sweep-cache invalidation and the exporter's deepcopy keep working.

    def apply_der_op(self, op: dict) -> bool:
        return der.apply_der_op(self, op)

    def node_der(self, bus: int) -> dict:
        return der.node_der(self, bus)

    def add_pv(self, bus: int, kwp: float = 5.0) -> dict:
        return der.add_pv(self, bus, kwp)

    def set_pv_kwp(self, sgen: int, kwp: float) -> bool:
        return der.set_pv_kwp(self, sgen, kwp)

    def add_ev(self, bus: int, kw: float = 11.0, start_min: int = 18 * 60,
               dur_min: int = 120) -> dict:
        return der.add_ev(self, bus, kw, start_min, dur_min)

    def set_ev(self, load: int, start_min: int, dur_min: int) -> bool:
        return der.set_ev(self, load, start_min, dur_min)

    def remove_pv(self, sgen: int) -> bool:
        return der.remove_pv(self, sgen)

    def remove_ev(self, load: int) -> bool:
        return der.remove_ev(self, load)

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
                    self._est_seq += 1
                    est["seq"] = self._est_seq         # telegram id (controllers)
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
        # the rONT closes its loop the same way: observed busbar voltage of
        # THIS step decides the tap position of the NEXT one
        if converged and self.ronts:
            self._ront_update(res)
            res.ronts = [r.as_dict() for r in self.ronts]
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
        res.ronts = [r.as_dict() for r in self.ronts]

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

    # ---- day sweeps & profile curves (sweeps.py) -------------------------- #
    # The sweep layer solves whole days on isolated net copies and serves the
    # three view layers of the day graphs (truth / measured / estimated). It
    # lives in sweeps.py; the caches stay HERE on the Simulator
    # (`_daily_by_day`, `_est_sweep_min`) so DER/estimation invalidation and
    # the bulk exporter's deepcopy of the live Simulator keep working.

    def daily_curves(self, day: int | None = None) -> dict:
        return sweeps.daily_curves(self, day)

    def measured_curves(self, day: int | None = None) -> dict:
        return sweeps.measured_curves(self, day)

    def daily_est(self, day: int | None = None) -> dict:
        return sweeps.daily_est(self, day)

    def node_profiles(self, bus: int, view: str = "est") -> dict:
        return sweeps.node_profiles(self, bus, view)

    def line_profiles(self, line: int, view: str = "est") -> dict:
        return sweeps.line_profiles(self, line, view)

    def trafo_profiles(self, trafo: int, view: str = "est") -> dict:
        return sweeps.trafo_profiles(self, trafo, view)

    def battery_profiles(self, storage_idx: int) -> dict | None:
        return sweeps.battery_profiles(self, storage_idx)
