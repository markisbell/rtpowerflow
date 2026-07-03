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
import time
from typing import Any

import numpy as np
import pandapower as pp

from .measurements import _r

# standard deviations = the operator's trust in each information source
STD_V_METER = 0.003      # smart-meter voltage, pu
STD_PQ_METER = 0.0005    # smart-meter P/Q, MW / MVar
STD_PQ_TRAFO = 0.002     # transformer-meter HV-side P/Q, MW / MVar
STD_V_SLACK = 0.001      # slack busbar voltage setpoint, pu
STD_ZERO_INJ = 1e-4      # structural zero injection (junctions/cabinets), MW
PSEUDO_STD_FRAC = 0.5    # pseudo-measurement std = frac × the bus's daily peak
STD_PSEUDO_FLOOR = 1e-3  # ...but never tighter than 1 kW / 1 kvar


class Estimator:
    """WLS state estimation on a dedicated copy of the simulator's net."""

    def __init__(self, net, prof, loads_at: dict[int, list[int]],
                 sgens_at: dict[int, list[int]]):
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
        # per-bus daily load statistics (the operator's profile knowledge)
        self._p_mean = {b: float(prof.load_p[rows].mean(axis=1).sum()) for b, rows in loads_at.items()}
        self._p_peak = {b: float(prof.load_p[rows].max(axis=1).sum()) for b, rows in loads_at.items()}
        self._q_mean = {b: float(prof.load_q[rows].mean(axis=1).sum()) for b, rows in loads_at.items()}

    # -- one estimation run ------------------------------------------------ #
    def run(self, source_net, observed: dict[str, Any], sgen_day_mean: np.ndarray,
            battery_buses: dict[int, float]) -> dict[str, Any] | None:
        """Estimate the grid state from the current meter readings.

        ``observed`` is the meters' projection (``StepResult.measurements``) —
        the estimator sees exactly what the devices deliver: full V/P/Q at every
        step, or only the 15-min mean P in standard metering mode (missing
        quantities fall back to profile knowledge with wide uncertainty).
        ``source_net`` supplies the slack setpoints and the truth for the error
        metric; ``battery_buses`` maps a battery's bus to its power rating (MW)
        — the operator's only knowledge about it.
        """
        net = self._net
        net.measurement.drop(net.measurement.index, inplace=True)
        t0 = time.perf_counter()

        # slack voltage: the operator's own setpoint, always known
        for _, row in source_net.ext_grid.iterrows():
            pp.create_measurement(net, "v", "bus", float(row["vm_pu"]), STD_V_SLACK,
                                  element=int(row["bus"]))

        # node meters: whatever quantities the device delivered
        metered: set[int] = set()
        for nm in observed.get("nodes", []):
            b = int(nm["bus"])
            if b not in net.bus.index:
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
                std_q = max(self._p_peak.get(b, 0.0) * PSEUDO_STD_FRAC, STD_PSEUDO_FLOOR)
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
                    p -= gen
                    peak = max(peak, gen)
            if b in battery_buses:                 # rating known, setpoint not
                std = max(battery_buses[b], peak * PSEUDO_STD_FRAC, STD_PSEUDO_FLOOR)
                pp.create_measurement(net, "p", "bus", p, std, element=b)
                pp.create_measurement(net, "q", "bus", q, std, element=b)
            elif b in self._loads_at or b in self._sgens_at:
                std = max(peak * PSEUDO_STD_FRAC, STD_PSEUDO_FLOOR)
                pp.create_measurement(net, "p", "bus", p, std, element=b)
                pp.create_measurement(net, "q", "bus", q, std, element=b)
            else:                                  # junction / cabinet: injects nothing
                pp.create_measurement(net, "p", "bus", 0.0, STD_ZERO_INJ, element=b)
                pp.create_measurement(net, "q", "bus", 0.0, STD_ZERO_INJ, element=b)

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
        }

        # estimation quality vs the true power flow (stripped in strict mode)
        dv = (net.res_bus_est.vm_pu - source_net.res_bus.vm_pu).abs()
        di = (net.res_line_est.i_ka - source_net.res_line.i_ka).abs()
        est["error"] = {
            "max_dv_pu": _r(dv.max()),
            "mean_dv_pu": _r(dv.mean()),
            "max_di_ka": _r(di.max()) if len(di) else None,
        }
        return est
