"""Runtime-configurable DERs: rooftop PV (kWp) and EV charge windows per node,
plus the bus-addressed DER journal scenarios replay.

Extracted from ``Simulator`` (2026-07-10); every function takes the live
``sim`` as first argument and the ``Simulator`` keeps thin delegate methods,
so all call sites (API, scenarios, tests) are unchanged. The journal
(``sim.der_log``) and the mutated profile rows stay ON the Simulator —
scenario recipes, the sweep caches and the exporter's deepcopy read them
there. Parameters are derived from the profile rows themselves (PV kWp =
row peak; EV window = the nonzero charging stretch), so LoadStudio-assigned
and runtime-added systems are equally editable."""
from __future__ import annotations

import math

import numpy as np
import pandapower as pp


def _der_log_put(sim, entry: dict, replaces: tuple[str, ...]) -> None:
    """Append a journal entry, dropping superseded ops for the same bus."""
    sim.der_log = [e for e in sim.der_log
                   if not (e["bus"] == entry["bus"] and e["op"] in replaces)]
    sim.der_log.append(entry)


def apply_der_op(sim, op: dict) -> bool:
    """Replay one bus-addressed DER op (scenario load). Tolerant: an op that
    no longer applies (e.g. remove on an absent system) is a no-op."""
    kind, bus = op.get("op"), int(op.get("bus", -1))
    if bus not in sim.net.bus.index:
        return False
    der = node_der(sim, bus)
    if kind == "add_pv" or kind == "set_pv":
        if der["pv"] is None:
            add_pv(sim, bus, float(op.get("kwp", 5.0)))
        else:
            set_pv_kwp(sim, der["pv"]["sgen"], float(op.get("kwp", 5.0)))
        return True
    if kind == "remove_pv":
        return der["pv"] is not None and remove_pv(sim, der["pv"]["sgen"])
    if kind == "add_ev" or kind == "set_ev":
        start = int(op.get("start_min", 18 * 60))
        dur = int(op.get("dur_min", 120))
        if der["ev"] is None:
            add_ev(sim, bus, float(op.get("kw", 11.0)), start, dur)
        else:
            set_ev(sim, der["ev"]["load"], start, dur)
        return True
    if kind == "remove_ev":
        return der["ev"] is not None and remove_ev(sim, der["ev"]["load"])
    return False


def _der_invalidate(sim) -> None:
    """Refresh everything derived from the load/sgen tables + profiles after
    a runtime DER change (same hygiene as a battery add/remove)."""
    sim.sgen_peak = sim.prof.sgen_p.max(axis=1) if sim.prof.sgen_p.size else np.zeros(0)
    sim._sgen_is_pv = np.array([k == "pv" for k in sim.sgen_kind], dtype=bool)
    sim._loads_at = {}
    for i, li in enumerate(sim.prof.load_idx):
        sim._loads_at.setdefault(int(sim.net.load.at[li, "bus"]), []).append(i)
    sim._sgens_at = {}
    for i, si in enumerate(sim.prof.sgen_idx):
        sim._sgens_at.setdefault(int(sim.net.sgen.at[si, "bus"]), []).append(i)
    sim._daily_by_day.clear()
    sim._sgen_day_mean_cache.clear()
    sim._estimator = None          # cached per-bus profile stats are stale
    sim._est_last = None
    sim._solved_once = False       # element tables changed: solve cold next


def node_der(sim, bus: int) -> dict:
    """The bus's configurable DERs. Parameters are derived from the profile
    rows themselves (PV kWp = row peak; EV start/duration/power = the
    nonzero charging window), so LoadStudio-assigned and runtime-added
    systems are equally editable."""
    if bus not in sim.net.bus.index:
        raise KeyError(bus)
    mps = 1440 // sim.steps_per_day            # minutes per step
    pv = None
    for i, si in enumerate(sim.prof.sgen_idx):
        if int(sim.net.sgen.at[si, "bus"]) != bus:
            continue
        if "PV_" not in str(sim.net.sgen.at[si, "name"] or ""):
            continue
        pv = {"sgen": int(si), "kwp": round(float(sim.prof.sgen_p[i].max()) * 1000.0, 2)}
        break
    ev = None
    for i, li in enumerate(sim.prof.load_idx):
        if int(sim.net.load.at[li, "bus"]) != bus:
            continue
        if "EV_" not in str(sim.net.load.at[li, "name"] or ""):
            continue
        row = sim.prof.load_p[i]
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


def add_pv(sim, bus: int, kwp: float = 5.0) -> dict:
    """Add a rooftop-PV system (clear-sky shape × kWp) at a bus at runtime.
    Like batteries, runtime DERs live in this simulator only (reset on swap)."""
    if bus not in sim.net.bus.index:
        raise KeyError(bus)
    from .loadgen.pv import PvPolicy, _clearsky
    kwp = float(min(max(kwp, 0.5), 100.0))
    pol = PvPolicy()
    row = _clearsky(sim.steps_per_day, pol.peak_hour, pol.width_hours) * (kwp / 1000.0)
    si = int(pp.create_sgen(sim.net, bus=bus, p_mw=0.0, q_mvar=0.0, name=f"PV_cfg_{bus}"))
    sim.prof.sgen_idx.append(si)
    sim.prof.sgen_p = np.vstack([sim.prof.sgen_p, row[None, :]])
    sim.prof.sgen_q = np.vstack([sim.prof.sgen_q, np.zeros((1, sim.steps_per_day))])
    sim.sgen_kind.append("pv")
    _der_log_put(sim, {"op": "add_pv", "bus": int(bus), "kwp": kwp},
                 ("add_pv", "set_pv", "remove_pv"))
    _der_invalidate(sim)
    return node_der(sim, bus)


def set_pv_kwp(sim, sgen: int, kwp: float) -> bool:
    """Rescale a PV system to a new peak. Works in real-PV mode too — the
    measured day shapes scale with ``sgen_peak``."""
    if sgen not in sim.prof.sgen_idx:
        return False
    i = sim.prof.sgen_idx.index(sgen)
    kwp = float(min(max(kwp, 0.5), 100.0))
    row = sim.prof.sgen_p[i]
    peak = float(row.max())
    if peak > 1e-9:
        sim.prof.sgen_p[i] = row * (kwp / 1000.0 / peak)
    else:
        from .loadgen.pv import PvPolicy, _clearsky
        pol = PvPolicy()
        sim.prof.sgen_p[i] = _clearsky(sim.steps_per_day, pol.peak_hour,
                                       pol.width_hours) * (kwp / 1000.0)
    bus = int(sim.net.sgen.at[sgen, "bus"])
    # an earlier add_pv keeps its place in the log; only its size updates
    if any(e["op"] == "add_pv" and e["bus"] == bus for e in sim.der_log):
        _der_log_put(sim, {"op": "add_pv", "bus": bus, "kwp": kwp},
                     ("add_pv", "set_pv"))
    else:
        _der_log_put(sim, {"op": "set_pv", "bus": bus, "kwp": kwp}, ("set_pv",))
    _der_invalidate(sim)
    return True


def add_ev(sim, bus: int, kw: float = 11.0, start_min: int = 18 * 60,
           dur_min: int = 120) -> dict:
    """Add an EV home-charging load at a bus at runtime (wallbox kW held for
    the charge window; duration clamped to 1-4 h; wraps past midnight)."""
    if bus not in sim.net.bus.index:
        raise KeyError(bus)
    li = int(pp.create_load(sim.net, bus=bus, p_mw=0.0, q_mvar=0.0, name=f"EV_cfg_{bus}"))
    sim.prof.load_idx.append(li)
    sim.prof.load_p = np.vstack([sim.prof.load_p, np.zeros((1, sim.steps_per_day))])
    sim.prof.load_q = np.vstack([sim.prof.load_q, np.zeros((1, sim.steps_per_day))])
    sim._load_household.append(False)     # EV charging is not a household
    sim._load_households.append(1)
    _set_ev_row(sim, len(sim.prof.load_idx) - 1, kw, start_min, dur_min)
    _der_log_put(sim, {"op": "add_ev", "bus": int(bus), "kw": float(kw),
                       "start_min": int(start_min) % 1440,
                       "dur_min": int(min(max(dur_min, 60), 240))},
                 ("add_ev", "set_ev", "remove_ev"))
    _der_invalidate(sim)
    return node_der(sim, bus)


def set_ev(sim, load: int, start_min: int, dur_min: int) -> bool:
    """Move an EV's charge window (start instant + 1-4 h duration); the
    wallbox power is kept from the existing profile."""
    if load not in sim.prof.load_idx:
        return False
    i = sim.prof.load_idx.index(load)
    kw = float(sim.prof.load_p[i].max()) * 1000.0
    _set_ev_row(sim, i, kw if kw > 0.1 else 11.0, start_min, dur_min)
    bus = int(sim.net.load.at[load, "bus"])
    clamped = int(min(max(dur_min, 60), 240))
    if any(e["op"] == "add_ev" and e["bus"] == bus for e in sim.der_log):
        _der_log_put(sim, {"op": "add_ev", "bus": bus, "kw": kw if kw > 0.1 else 11.0,
                           "start_min": int(start_min) % 1440, "dur_min": clamped},
                     ("add_ev", "set_ev"))
    else:
        _der_log_put(sim, {"op": "set_ev", "bus": bus,
                           "start_min": int(start_min) % 1440, "dur_min": clamped},
                     ("set_ev",))
    _der_invalidate(sim)
    return True


def remove_pv(sim, sgen: int) -> bool:
    """Remove a PV system (LoadStudio-assigned or runtime-added)."""
    if sgen not in sim.prof.sgen_idx:
        return False
    bus = int(sim.net.sgen.at[sgen, "bus"])
    was_added = any(e["op"] == "add_pv" and e["bus"] == bus for e in sim.der_log)
    _der_log_put(sim, {"op": "remove_pv", "bus": bus},
                 ("add_pv", "set_pv", "remove_pv"))
    if was_added:                       # runtime add + remove = no delta
        sim.der_log = [e for e in sim.der_log
                       if not (e["op"] == "remove_pv" and e["bus"] == bus)]
    i = sim.prof.sgen_idx.index(sgen)
    sim.prof.sgen_idx.pop(i)
    sim.prof.sgen_p = np.delete(sim.prof.sgen_p, i, axis=0)
    sim.prof.sgen_q = np.delete(sim.prof.sgen_q, i, axis=0)
    if i < len(sim.sgen_kind):
        sim.sgen_kind.pop(i)
    sim.net.sgen.drop(sgen, inplace=True)
    _der_invalidate(sim)
    return True


def remove_ev(sim, load: int) -> bool:
    """Remove an EV charging load (LoadStudio-assigned or runtime-added)."""
    if load not in sim.prof.load_idx:
        return False
    bus = int(sim.net.load.at[load, "bus"])
    was_added = any(e["op"] == "add_ev" and e["bus"] == bus for e in sim.der_log)
    _der_log_put(sim, {"op": "remove_ev", "bus": bus},
                 ("add_ev", "set_ev", "remove_ev"))
    if was_added:                       # runtime add + remove = no delta
        sim.der_log = [e for e in sim.der_log
                       if not (e["op"] == "remove_ev" and e["bus"] == bus)]
    i = sim.prof.load_idx.index(load)
    sim.prof.load_idx.pop(i)
    sim.prof.load_p = np.delete(sim.prof.load_p, i, axis=0)
    sim.prof.load_q = np.delete(sim.prof.load_q, i, axis=0)
    if i < len(sim._load_household):
        sim._load_household.pop(i)
    if i < len(sim._load_households):
        sim._load_households.pop(i)
    sim.net.load.drop(load, inplace=True)
    _der_invalidate(sim)
    return True


def _set_ev_row(sim, i: int, kw: float, start_min: int, dur_min: int) -> None:
    from .loadgen.ev import _charge_profile
    dur_min = int(min(max(dur_min, 60), 240))        # 1-4 h charging
    start_min = int(start_min) % 1440
    row = _charge_profile(sim.steps_per_day, start_min / 60.0, dur_min / 60.0,
                          max(kw, 0.1)) / 1000.0
    sim.prof.load_p[i] = row
    sim.prof.load_q[i] = row * math.tan(math.acos(0.98))
