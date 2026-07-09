"""State estimation — the operator's *calculated* view of the grid.

Beside reality (the solved power flow) and observation (raw meter readings)
this adds the classic third layer: weighted-least-squares state estimation
(``pandapower.estimation``). It uses only what a distribution operator really
has:

- the **grid model** — lines and transformers are assumed known exactly,
- the **placed measurements** — smart meters (V, P, Q at a bus), transformer
  meters (HV-side P, Q) and the slack voltage *setpoint*,
- **structural knowledge** — buses without any customer inject nothing
  (junctions, cable cabinets → exact zero-injection pseudo-measurements),
- **coarse load knowledge** — each customer's *average* consumption over the
  day (standard-load-profile style), entered as low-confidence
  pseudo-measurements so the sparse system becomes observable.

From that it reconstructs voltage at every bus and the flow on every line —
including everything no meter can see. Batteries are the honest blind spot:
the operator knows one exists (its rating bounds the pseudo-measurement) but
not its current setpoint, so estimation error concentrates around unmetered
storage. Estimation runs on a dedicated copy of the net; the result arrays
mirror the truth arrays so the UI can render either interchangeably.
"""
from __future__ import annotations

import copy
import math
import time
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandapower as pp

from .measurements import _r

# ``select_subnet`` lives in ``pandapower.toolbox``; older releases re-export it
# at the top level, newer ones (that CI installs from requirements.txt) do NOT.
# Import from the stable location, falling back to the top-level name.
try:
    from pandapower.toolbox import select_subnet as _select_subnet
except ImportError:  # pragma: no cover - very old pandapower
    _select_subnet = pp.select_subnet

# standard deviations = the operator's trust in each information source
STD_V_METER = 0.003      # smart-meter voltage, pu
STD_PQ_METER = 0.0005    # smart-meter P/Q, MW / MVar
STD_PQ_TRAFO = 0.002     # transformer-meter HV-side P/Q, MW / MVar
STD_V_SLACK = 0.001      # slack busbar voltage setpoint, pu
STD_ZERO_INJ = 1e-4      # structural zero injection (junctions/cabinets), MW
STD_PSEUDO_FLOOR = 1e-3  # pseudo std never tighter than 1 kW / 1 kvar

_TAN_PHI = math.tan(math.acos(0.95))   # SLP reactive-power assumption


@dataclass
class EstConfig:
    """What the operator's estimation is allowed to use (UI: tab "Schätzung").

    Defaults mirror real DSO practice: no pseudo-measurement for PV (plants
    differ in orientation, so a fleet-wide day shape is not trusted) and none
    for EV charging (stochastic); household loads enter with profile
    knowledge. ``load_basis`` picks that knowledge: ``"profile"`` is the
    idealized per-customer daily mean of the *actual* profile, ``"slp"`` the
    realistic standard-load-profile assumption — every household the same
    annual consumption, no individual knowledge.
    """

    pv_pseudo: bool = False        # subtract the PV day-mean from the pseudo value
    ev_pseudo: bool = False        # include EV charging in the pseudo value
    load_basis: str = "profile"    # "profile" | "slp"
    slp_annual_kwh: float = 4000.0     # SLP assumption per household
    pseudo_std_pct: float = 50.0   # pseudo std = pct% × the bus's daily peak
    zero_injection: bool = True    # structural knowledge at junctions/cabinets
    # vertical MV/LV estimation (docs/VERTIKALE_INTEGRATION.md, Phase 1.2):
    # "hierarchical" = per-cell WLS whose boundary flows feed a reduced MV-level
    # WLS (DSO practice, scales); "monolithic" = one WLS over the whole net;
    # "auto" = hierarchical as soon as the grid has spliced ONS cells AND a
    # real MV level above them.
    hierarchy: str = "auto"        # "auto" | "monolithic" | "hierarchical"


class Estimator:
    """WLS state estimation on a dedicated copy of the simulator's net."""

    def __init__(self, net, prof, loads_at: dict[int, list[int]],
                 sgens_at: dict[int, list[int]], *,
                 ev_rows: set[int] | None = None,
                 household_rows: set[int] | None = None,
                 household_counts: dict[int, int] | None = None,
                 config: EstConfig | None = None):
        self._net = copy.deepcopy(net)                    # measurements live here
        if len(self._net.measurement):
            self._net.measurement.drop(self._net.measurement.index, inplace=True)
        # the estimator's model doesn't carry storage: the operator can't see
        # battery setpoints (their effect enters only through measurements)
        if len(self._net.storage):
            self._net.storage.drop(self._net.storage.index, inplace=True)
        self._prof = prof
        self._loads_at = loads_at                         # bus -> load profile rows
        self._sgens_at = sgens_at                         # bus -> sgen profile rows
        self._ext_buses = {int(b) for b in net.ext_grid.bus}
        self._ev_rows = ev_rows or set()                  # rows that are EV charging
        self._household_rows = household_rows if household_rows is not None \
            else set(range(prof.load_p.shape[0]) if prof.load_p.size else [])
        # metering points per row (multi-family buildings): the DSO knows its
        # meter counts, so the SLP basis scales with them
        self._household_counts = household_counts or {}
        self.set_config(config or EstConfig())

    def set_config(self, cfg: EstConfig) -> None:
        """(Re)compute the per-bus profile knowledge under the given policy.

        Pseudo *values* follow the policy (EV excluded unless allowed; SLP
        basis replaces each household's true mean with the uniform standard
        assumption). Pseudo *widths* always come from the true daily peaks —
        they describe uncertainty, not knowledge, and keep the ±pct knob
        meaningful across bases.
        """
        self._cfg = cfg
        self._std_frac = max(cfg.pseudo_std_pct, 1.0) / 100.0
        slp_p = cfg.slp_annual_kwh / 8760.0 / 1000.0      # MW mean over the year
        slp_q = slp_p * _TAN_PHI
        self._p_mean, self._q_mean, self._p_peak = {}, {}, {}
        for b, rows in self._loads_at.items():
            p = q = peak = 0.0
            for i in rows:
                peak += float(self._prof.load_p[i].max())
                if i in self._ev_rows:
                    if cfg.ev_pseudo:
                        p += float(self._prof.load_p[i].mean())
                        q += float(self._prof.load_q[i].mean())
                    continue
                if cfg.load_basis == "slp" and i in self._household_rows:
                    n = self._household_counts.get(i, 1)
                    p += slp_p * n
                    q += slp_q * n
                else:
                    p += float(self._prof.load_p[i].mean())
                    q += float(self._prof.load_q[i].mean())
            self._p_mean[b], self._q_mean[b], self._p_peak[b] = p, q, peak

    # -- one estimation run ------------------------------------------------ #
    def run(self, source_net, observed: dict[str, Any], sgen_day_mean: np.ndarray,
            battery_buses: dict[int, float],
            boundary: dict[int, tuple[float, float, float]] | None = None,
            ) -> dict[str, Any] | None:
        """Estimate the grid state from the current meter readings.

        ``observed`` is the meters' projection (``StepResult.measurements``) —
        the estimator sees exactly what the devices deliver: full V/P/Q at every
        step, or only the 15-min mean P in standard metering mode (missing
        quantities fall back to profile knowledge with wide uncertainty).
        ``source_net`` supplies the slack setpoints and the truth for the error
        metric (skipped when its solved results don't match this net — the
        hierarchical stages compute their errors at composition);
        ``battery_buses`` maps a battery's bus to its power rating (MW)
        — the operator's only knowledge about it. ``boundary`` (hierarchical
        MV stage) maps a bus to ``(p_mw, q_mvar, std)`` — the known boundary
        flow of a collapsed ONS cell, added on top of the bus's own pseudo
        knowledge instead of leaving the bus purely profile-guessed.
        """
        net = self._net
        net.measurement.drop(net.measurement.index, inplace=True)
        t0 = time.perf_counter()

        # transformer tap positions are the operator's OWN setpoints (rONT):
        # keep the estimation model's ratios in sync with the live net
        for col in ("tap_side", "tap_neutral", "tap_min", "tap_max",
                    "tap_step_percent", "tap_pos"):
            if col in source_net.trafo.columns and len(net.trafo):
                net.trafo[col] = source_net.trafo[col].reindex(net.trafo.index)

        # slack voltage: the operator's own setpoint, always known
        for _, row in source_net.ext_grid.iterrows():
            pp.create_measurement(net, "v", "bus", float(row["vm_pu"]), STD_V_SLACK,
                                  element=int(row["bus"]))

        # node meters: whatever quantities the device delivered. A device that
        # delivered NOTHING (a TAF-7 Lastgang meter before its first completed
        # 15-min window) counts as unmetered — it keeps its pseudo-load below,
        # otherwise the WLS loses that bus's injection knowledge entirely and
        # the system can turn unobservable.
        metered: set[int] = set()
        for nm in observed.get("nodes", []):
            b = int(nm["bus"])
            if b not in net.bus.index:
                continue
            if nm.get("vm_pu") is None and nm.get("p_mw") is None:
                continue
            metered.add(b)
            full = nm.get("q_mvar") is not None
            if nm.get("vm_pu") is not None:
                pp.create_measurement(net, "v", "bus", float(nm["vm_pu"]), STD_V_METER, element=b)
            if nm.get("p_mw") is not None:
                # a 15-min mean is stale within the window -> trust it less
                std = STD_PQ_METER if full else max(abs(float(nm["p_mw"])) * 0.3, STD_PSEUDO_FLOOR)
                pp.create_measurement(net, "p", "bus", float(nm["p_mw"]), std, element=b)
            if full:
                pp.create_measurement(net, "q", "bus", float(nm["q_mvar"]), STD_PQ_METER, element=b)
            else:
                # P-only meter: reactive power from profile knowledge instead
                std_q = max(self._p_peak.get(b, 0.0) * self._std_frac, STD_PSEUDO_FLOOR)
                pp.create_measurement(net, "q", "bus", self._q_mean.get(b, 0.0), std_q, element=b)

        # transformer meters: HV-side flow readings the device delivered
        for tm in observed.get("trafos", []):
            tr = int(tm["trafo"])
            if tr not in net.trafo.index:
                continue
            full = tm.get("q_hv_mvar") is not None
            if tm.get("p_hv_mw") is not None:
                std = STD_PQ_TRAFO if full else max(abs(float(tm["p_hv_mw"])) * 0.3, STD_PSEUDO_FLOOR)
                pp.create_measurement(net, "p", "trafo", float(tm["p_hv_mw"]), std, element=tr, side="hv")
            if full:
                pp.create_measurement(net, "q", "trafo", float(tm["q_hv_mvar"]),
                                      STD_PQ_TRAFO, element=tr, side="hv")

        # unmetered buses: structural zero injection or profile-based pseudo load
        for b in net.bus.index:
            b = int(b)
            if b in metered or b in self._ext_buses:
                continue
            p = self._p_mean.get(b, 0.0)
            q = self._q_mean.get(b, 0.0)
            peak = self._p_peak.get(b, 0.0)
            for rows in (self._sgens_at.get(b),):
                if rows and sgen_day_mean.size:
                    gen = float(sgen_day_mean[rows].sum())
                    if self._cfg.pv_pseudo:
                        p -= gen               # fleet day-shape trusted as value
                    # either way the plant's size is known (register data) and
                    # widens the uncertainty at this bus
                    peak = max(peak, gen)
            if boundary and b in boundary:
                # collapsed ONS cell: its boundary flow (measured or estimated
                # by the cell stage) plus whatever else sits at this MV bus
                bp, bq, bstd = boundary[b]
                std = max(math.hypot(bstd, peak * self._std_frac), STD_PSEUDO_FLOOR)
                pp.create_measurement(net, "p", "bus", p + bp, std, element=b)
                pp.create_measurement(net, "q", "bus", q + bq, std, element=b)
            elif b in battery_buses:               # rating known, setpoint not
                std = max(battery_buses[b], peak * self._std_frac, STD_PSEUDO_FLOOR)
                pp.create_measurement(net, "p", "bus", p, std, element=b)
                pp.create_measurement(net, "q", "bus", q, std, element=b)
            elif b in self._loads_at or b in self._sgens_at:
                std = max(peak * self._std_frac, STD_PSEUDO_FLOOR)
                pp.create_measurement(net, "p", "bus", p, std, element=b)
                pp.create_measurement(net, "q", "bus", q, std, element=b)
            elif self._cfg.zero_injection:         # junction / cabinet: injects nothing
                pp.create_measurement(net, "p", "bus", 0.0, STD_ZERO_INJ, element=b)
                pp.create_measurement(net, "q", "bus", 0.0, STD_ZERO_INJ, element=b)
            # zero_injection off: the operator claims nothing about these buses
            # (the system may turn unobservable -> the estimate simply vanishes)

        try:
            result = pp.estimation.estimate(net, algorithm="wls", zero_injection=None)
        except Exception:  # noqa: BLE001 — unobservable/singular system
            return None
        ok = result.get("success") if isinstance(result, dict) else bool(result)
        if not ok:
            return None
        solve_ms = (time.perf_counter() - t0) * 1000.0

        est: dict[str, Any] = {
            "buses": [{"index": int(b),
                       "vm_pu": _r(net.res_bus_est.at[b, "vm_pu"]),
                       "va_degree": _r(net.res_bus_est.at[b, "va_degree"]),
                       "p_mw": _r(net.res_bus_est.at[b, "p_mw"]),
                       "q_mvar": _r(net.res_bus_est.at[b, "q_mvar"])}
                      for b in net.bus.index],
            "lines": [{"index": int(l),
                       "loading_percent": _r(net.res_line_est.at[l, "loading_percent"]),
                       "i_ka": _r(net.res_line_est.at[l, "i_ka"]),
                       "p_from_mw": _r(net.res_line_est.at[l, "p_from_mw"]),
                       "pl_mw": _r(net.res_line_est.at[l, "pl_mw"])}
                      for l in net.line.index],
            "trafos": [{"index": int(tr),
                        "loading_percent": _r(net.res_trafo_est.at[tr, "loading_percent"]),
                        "p_hv_mw": _r(net.res_trafo_est.at[tr, "p_hv_mw"]),
                        "q_hv_mvar": _r(net.res_trafo_est.at[tr, "q_hv_mvar"]),
                        "i_hv_ka": _r(net.res_trafo_est.at[tr, "i_hv_ka"]),
                        "pl_mw": _r(net.res_trafo_est.at[tr, "pl_mw"])}
                       for tr in net.trafo.index],
            "solve_ms": round(solve_ms, 3),
            "mode": "monolithic",
        }

        # estimation quality vs the true power flow (stripped in strict mode).
        # Skipped when source_net isn't this net's solved truth (the
        # hierarchical stages run on subnets and compute errors at composition).
        if len(source_net.res_bus.index) == len(net.bus.index):
            dv = (net.res_bus_est.vm_pu - source_net.res_bus.vm_pu).abs()
            di = (net.res_line_est.i_ka - source_net.res_line.i_ka).abs()
            est["error"] = {
                "max_dv_pu": _r(dv.max()),
                "mean_dv_pu": _r(dv.mean()),
                "max_di_ka": _r(di.max()) if len(di) else None,
            }
        return est


def wants_hierarchy(cfg: EstConfig, cells: list[dict], n_bus: int) -> bool:
    """Resolve the estimation-policy ``hierarchy`` knob for a concrete grid.

    Hierarchical estimation needs spliced ONS cells AND a real MV level above
    them: a standalone LV grid is one cell plus its MV feed bus — collapsing it
    would leave a one-bus "MV net", so it always estimates monolithically,
    even when forced.
    """
    if cfg.hierarchy == "monolithic":
        return False
    spliced = [c for c in cells if not c.get("lumped") and c.get("buses")]
    if not spliced:
        return False
    n_reduced = n_bus - sum(len(c["buses"]) for c in spliced)
    return n_reduced > 1


class HierarchicalEstimator:
    """Two-stage WLS mirroring DSO practice (vertical integration, Phase 1.2).

    Stage 1 — every spliced ONS cell estimates LOCALLY on its own subnet
    (member buses + the feeding MV bus; slack there, setpoint = the MV
    estimate of the previous run, 1.0 pu before the first). Stage 2 — the
    reduced MV net (spliced cell buses collapsed) estimates with each cell's
    **boundary flow** as an equivalent injection at its MV bus: the station
    trafo reading where metered, else the cell stage's estimated trafo flow,
    else the cell's aggregate profile pseudo. The composed result mirrors the
    monolithic estimator's shape (plus ``mode``/``cells``), so every consumer
    — UI layers, controllers, day sweeps — works unchanged. A failed cell
    stage leaves that cell honestly unestimated (its elements are absent);
    a failed MV stage fails the run like a monolithic non-convergence.
    """

    def __init__(self, net, prof, loads_at: dict[int, list[int]],
                 sgens_at: dict[int, list[int]], cells: list[dict], *,
                 ev_rows: set[int] | None = None,
                 household_rows: set[int] | None = None,
                 household_counts: dict[int, int] | None = None,
                 config: EstConfig | None = None):
        self._cfg = config or EstConfig()
        kw = dict(ev_rows=ev_rows, household_rows=household_rows,
                  household_counts=household_counts, config=self._cfg)
        self._cells = [c for c in cells if not c.get("lumped") and c.get("buses")]
        self._cell_net: dict[str, Any] = {}
        self._cell_est: dict[str, Estimator] = {}
        spliced: set[int] = set()
        for c in self._cells:
            member = {int(b) for b in c["buses"]}
            spliced |= member
            sub = _select_subnet(net, list(member | {int(c["mv_bus"])}))
            # the subnet keeps original indices; its slack is the feeding MV bus
            if len(sub.ext_grid):
                sub.ext_grid.drop(sub.ext_grid.index, inplace=True)
            pp.create_ext_grid(sub, bus=int(c["mv_bus"]), vm_pu=1.0,
                               name=f"cell:{c['id']}")
            self._cell_net[c["id"]] = sub
            self._cell_est[c["id"]] = Estimator(
                sub, prof,
                {b: r for b, r in loads_at.items() if b in member},
                {b: r for b, r in sgens_at.items() if b in member}, **kw)
        keep = [int(b) for b in net.bus.index if int(b) not in spliced]
        self._mv_net = _select_subnet(net, keep)
        self._mv_est = Estimator(
            self._mv_net, prof,
            {b: r for b, r in loads_at.items() if b not in spliced},
            {b: r for b, r in sgens_at.items() if b not in spliced}, **kw)
        self._last_mv_vm: dict[int, float] = {}

    def set_config(self, cfg: EstConfig) -> None:
        self._cfg = cfg
        for e in self._cell_est.values():
            e.set_config(cfg)
        self._mv_est.set_config(cfg)

    def run(self, source_net, observed: dict[str, Any], sgen_day_mean: np.ndarray,
            battery_buses: dict[int, float]) -> dict[str, Any] | None:
        t0 = time.perf_counter()
        nodes = (observed or {}).get("nodes", [])
        trafos = (observed or {}).get("trafos", [])
        boundary: dict[int, tuple[float, float, float]] = {}
        cell_results: dict[str, dict | None] = {}
        cells_meta: list[dict[str, Any]] = []

        for c in self._cells:
            cid, member = c["id"], set(c["buses"])
            st = {int(t) for t in c["station_trafos"]}
            sub = self._cell_net[cid]
            cest = self._cell_est[cid]
            sub.ext_grid["vm_pu"] = float(self._last_mv_vm.get(int(c["mv_bus"]), 1.0))
            # keep the cell model's station-trafo tap in sync (rONT setpoints)
            for col in ("tap_side", "tap_neutral", "tap_min", "tap_max",
                        "tap_step_percent", "tap_pos"):
                if col in source_net.trafo.columns and len(sub.trafo):
                    sub.trafo[col] = source_net.trafo[col].reindex(sub.trafo.index)
            c_obs = {"nodes": [n for n in nodes if int(n["bus"]) in member],
                     "trafos": [tm for tm in trafos if int(tm["trafo"]) in st]}
            r = cest.run(sub, c_obs, sgen_day_mean,
                         {b: p for b, p in (battery_buses or {}).items() if b in member})
            cell_results[cid] = r
            # boundary knowledge for the MV stage: measured > estimated > pseudo
            read = [tm for tm in c_obs["trafos"] if tm.get("p_hv_mw") is not None]
            if read:
                bp = sum(float(tm["p_hv_mw"]) for tm in read)
                bq = sum(float(tm["q_hv_mvar"] or 0.0) for tm in read)
                boundary[int(c["mv_bus"])] = (bp, bq, STD_PQ_TRAFO)
                src = "meter"
            elif r is not None:
                rows = [t for t in r["trafos"] if t["index"] in st]
                bp = sum(float(t["p_hv_mw"] or 0.0) for t in rows)
                bq = sum(float(t["q_hv_mvar"] or 0.0) for t in rows)
                if c_obs["nodes"]:      # real SMGWs inside -> a genuine estimate
                    std, src = max(0.2 * abs(bp), STD_PSEUDO_FLOOR), "estimate"
                else:                   # pseudo-only cell stage = profile guess
                    peak = sum(cest._p_peak.values())
                    std, src = max(peak * cest._std_frac, STD_PSEUDO_FLOOR), "pseudo"
                boundary[int(c["mv_bus"])] = (bp, bq, std)
            else:
                bp = sum(cest._p_mean.values())
                bq = sum(cest._q_mean.values())
                peak = sum(cest._p_peak.values())
                boundary[int(c["mv_bus"])] = (
                    bp, bq, max(peak * cest._std_frac, STD_PSEUDO_FLOOR))
                src = "pseudo"
            cells_meta.append({"id": cid, "boundary_src": src,
                               "solve_ms": r["solve_ms"] if r else None})

        mv = self._mv_est.run(source_net, {"nodes": nodes, "trafos": trafos},
                              sgen_day_mean,
                              {b: p for b, p in (battery_buses or {}).items()
                               if b in self._mv_net.bus.index},
                              boundary=boundary)
        if mv is None:
            return None
        for e in mv["buses"]:
            if e["vm_pu"] is not None:
                self._last_mv_vm[e["index"]] = float(e["vm_pu"])

        # compose: MV rows are authoritative (incl. each cell's feeding bus)
        buses = {e["index"]: e for e in mv["buses"]}
        lines = {e["index"]: e for e in mv["lines"]}
        trafs = {e["index"]: e for e in mv["trafos"]}
        for c in self._cells:
            r = cell_results[c["id"]]
            if r is None:
                continue
            member = set(c["buses"])
            for e in r["buses"]:
                if e["index"] in member:
                    buses[e["index"]] = e
            for e in r["lines"]:
                lines[e["index"]] = e
            for e in r["trafos"]:
                trafs[e["index"]] = e
        est: dict[str, Any] = {
            "buses": [buses[k] for k in sorted(buses)],
            "lines": [lines[k] for k in sorted(lines)],
            "trafos": [trafs[k] for k in sorted(trafs)],
            "solve_ms": round((time.perf_counter() - t0) * 1000.0, 3),
            "mode": "hierarchical",
            "cells": cells_meta,
        }

        # global + per-cell quality vs truth (stripped in strict mode)
        if len(source_net.res_bus.index):
            dv = {e["index"]: abs(float(e["vm_pu"]) -
                                  float(source_net.res_bus.at[e["index"], "vm_pu"]))
                  for e in est["buses"]
                  if e["vm_pu"] is not None and e["index"] in source_net.res_bus.index}
            di = [abs(float(e["i_ka"]) -
                      float(source_net.res_line.at[e["index"], "i_ka"]))
                  for e in est["lines"]
                  if e.get("i_ka") is not None and e["index"] in source_net.res_line.index]
            est["error"] = {
                "max_dv_pu": _r(max(dv.values())) if dv else None,
                "mean_dv_pu": _r(sum(dv.values()) / len(dv)) if dv else None,
                "max_di_ka": _r(max(di)) if di else None,
            }
            for meta, c in zip(cells_meta, self._cells):
                mem = [dv[b] for b in c["buses"] if b in dv]
                meta["error"] = {"max_dv_pu": _r(max(mem)) if mem else None}
        return est
