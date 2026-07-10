"""Day sweeps & profile curves: the three view layers of the day graphs.

Extracted from ``Simulator`` (2026-07-10); every function takes the live
``sim`` as first argument and the ``Simulator`` keeps thin delegate methods,
so all call sites (API, tests) are unchanged. The caches stay ON the
Simulator (``_daily_by_day``, ``_est_sweep_min``): DER/estimation
invalidation and the bulk exporter's deepcopy of the live Simulator keep
working without special handling here.

Layer design (customer requirements 2026-07-07): ``daily_curves`` = TRUTH
only, solved at every input step on an isolated net copy (recycled solves);
``measured_curves`` = derived array math on the truth sweep, only metered
elements in their device's TAF raster; ``daily_est`` = lazy WLS mini-sweep
at the pinned estimate raster, cached inside the truth entry keyed on
placement/mode/policy — only the Schätzung view pays for WLS runs."""
from __future__ import annotations

import copy
import dataclasses

import numpy as np
import pandapower as pp

from . import battery as bat
from .battery import Battery
from .measurements import MeasurementSet, _r


def daily_curves(sim, day: int | None = None) -> dict:
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
    d = sim.day if day is None else day
    cached = sim._daily_by_day.get(d)
    if cached is not None:
        return cached
    # Snapshot the LIVE net (not a rebuild from the input data): runtime
    # DER changes — added/resized PV, EV windows, removals — exist only on
    # the live net/profiles, and sim._loads_at/_sgens_at row indices refer
    # to sim.prof. The sweep only reads prof and re-solves on its copy.
    net = copy.deepcopy(sim.net)
    net.storage.drop(net.storage.index, inplace=True)  # batteries recreated fresh below
    spd = sim.steps_per_day
    bs = _sweep_batteries(sim, net)
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
    for t, solved in _sweep_solves(sim, net, bs, d, range(spd)):
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
    sim._daily_by_day[d] = result
    return result


def _sweep_batteries(sim, net) -> list[Battery]:
    """Recreate the live batteries on an isolated sweep net (fresh SOC 50 %)."""
    return [Battery(bus=b.bus, capacity_mwh=b.capacity_mwh, power_mw=b.power_mw,
                    mode=b.mode, eff=b.eff, soc_min=b.soc_min, soc_max=b.soc_max,
                    soc_mwh=0.5 * b.capacity_mwh, name=b.name,
                    storage_idx=int(pp.create_storage(net, bus=b.bus, p_mw=0.0,
                                                      max_e_mwh=b.capacity_mwh,
                                                      soc_percent=50.0, name=b.name)))
            for b in sim.batteries]


def _sweep_solves(sim, net, bs: list[Battery], d: int, solve_at):
    """Drive one day on the sweep net: integrate battery SOC at EVERY step,
    solve the power flow at the steps in ``solve_at`` (recycled after the
    first success — Ybus unchanged, only bus injections move — with the
    run_step retry ladder as fallback) and yield ``(t, solved)`` there."""
    prof = sim.prof
    spd = sim.steps_per_day
    dt_h = 24.0 / spd
    sgen_peak = prof.sgen_p.max(axis=1) if prof.sgen_p.size else np.zeros(0)
    want = set(int(t) for t in solve_at)
    # a varying slack setpoint is NOT covered by the bus-P/Q recycle path
    use_recycle = not ((prof.ext_vm.size and float(np.ptp(prof.ext_vm, axis=1).max()) > 1e-12)
                       or (prof.ext_va.size and float(np.ptp(prof.ext_va, axis=1).max()) > 1e-12))
    warm = False
    for t in range(spd):
        col = prof.sgen_p[:, t]
        if sim.pv_days is not None:
            col = col.copy()
            real = sgen_peak * sim.pv_days[d % sim.pv_days.shape[0], t]
            col[sim._sgen_is_pv] = real[sim._sgen_is_pv]
        if bs:  # advance SOC every step + set storage setpoints on the net
            sim._apply_batteries(net, bs, prof, col, d, t, dt_h,
                                 sim._loads_at, sim._sgens_at, do_integrate=True)
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


def _raster_steps(sim, minutes: float) -> int:
    return max(1, round(minutes / (1440.0 / sim.steps_per_day)))


def measured_curves(sim, day: int | None = None) -> dict:
    """The MEASURED layer of the day graphs, derived from the truth sweep:
    only elements that carry a device, only the quantities THAT device
    delivers, in ITS metering raster — a full-mode device (TAF 9/10/14)
    passes the 1-min values through, a standard-mode device (TAF 7
    Lastgang) reduces to the 15-min window mean of the ACTIVE power
    (voltage/loading unknown). Modes mix freely per device. Cheap array
    math on the cached truth arrays — never cached itself, so
    placement/mode changes take effect immediately."""
    d = sim.day if day is None else day
    truth = daily_curves(sim, d)
    spd = sim.steps_per_day
    win = _raster_steps(sim, 15)
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
    for b in sorted(sim.meters.node_buses):
        p = truth["bus_p"].get(b)
        if p is None:
            continue
        if sim.meters.mode_of_node(b) == "standard":
            nodes[b] = {"p_mw": window_mean(p), "vm": None, "raster_min": 15}
        else:
            nodes[b] = {"p_mw": p, "vm": truth["bus_vm"].get(b),
                        "raster_min": full_raster}
    trafos: dict[int, dict] = {}
    for tr in sorted(sim.meters.trafo_idxs):
        p = truth["trafo_p_hv"].get(tr)
        if p is None:
            continue
        if sim.meters.mode_of_trafo(tr) == "standard":
            trafos[tr] = {"p_hv": window_mean(p), "loading": None, "raster_min": 15}
        else:
            trafos[tr] = {"p_hv": p, "loading": truth["trafo_loading"].get(tr),
                          "raster_min": full_raster}
    return {"nodes": nodes, "trafos": trafos}


def daily_est(sim, day: int | None = None) -> dict:
    """The ESTIMATED layer of the day graphs: a lazy WLS mini-sweep that
    re-solves the day only at the estimate raster (pinned per-grid cost
    tier, never finer than the metering raster) and runs the estimator
    there. Stored inside the truth cache entry keyed on the meter
    placement/mode AND the estimation policy — so it is computed only when
    the Schätzung view actually asks for it, and the pure-Lastfluss /
    Gemessen views never pay for WLS runs."""
    d = sim.day if day is None else day
    truth = daily_curves(sim, d)
    sig = (frozenset(sim.meters.node_buses), frozenset(sim.meters.trafo_idxs),
           sim.meters.modes_signature(), dataclasses.astuple(sim.est_config))
    cached = truth.get("_est")
    if cached is not None and cached.get("_sig") == sig:
        return cached
    spd = sim.steps_per_day
    # without meters there is nothing to estimate: empty layer, so the
    # profile endpoints report the estimate as absent (None), not all-null
    est_vm: dict[int, list] = {}
    est_i: dict[int, list] = {}
    est_p_hv: dict[int, list] = {}
    result = {"est_bus_vm": est_vm, "est_line_i_ka": est_i,
              "est_trafo_p_hv": est_p_hv, "_sig": sig}
    if sim.meters.node_buses or sim.meters.trafo_idxs:
        est_vm.update({int(b): [None] * spd for b in sim.net.bus.index})
        est_i.update({int(l): [None] * spd for l in sim.net.line.index})
        est_p_hv.update({int(tr): [None] * spd for tr in sim.net.trafo.index})
        net = copy.deepcopy(sim.net)
        net.storage.drop(net.storage.index, inplace=True)
        bs = _sweep_batteries(sim, net)
        estimator = sim._make_estimator(net)
        # its own MeasurementSet copy: standard-mode window accumulators
        # must not disturb the live meters' state
        sweep_meters = MeasurementSet(node_buses=set(sim.meters.node_buses),
                                      trafo_idxs=set(sim.meters.trafo_idxs),
                                      mode=sim.meters.mode,
                                      node_modes=dict(sim.meters.node_modes),
                                      trafo_modes=dict(sim.meters.trafo_modes))
        est_bats = {b.bus: b.power_mw for b in sim.batteries}
        meter_raster = _raster_steps(sim, 15) if sim.meters.all_standard else 1
        est_steps = max(_raster_steps(sim, sim._est_sweep_min or 15), meter_raster)
        for t, solved in _sweep_solves(sim, net, bs, d,
                                       range(0, spd, est_steps)):
            if not solved:
                continue
            est = estimator.run(net, sweep_meters.observe(net, t),
                                sim._sgen_day_mean(d), est_bats)
            if est is None:
                continue
            if sim._est_sweep_min is None:
                # decide the tier ONCE per grid: big nets sample the
                # estimate coarser so a sweep stays interactive. Use the
                # cheaper of this run and the live loop's smoothed cost
                # (the first run may be a cold-start outlier), and snap to
                # clean resolutions: 15/30/60/120 min.
                ms = est["solve_ms"]
                if sim._est_ms > 0:
                    ms = min(ms, sim._est_ms)
                sim._est_sweep_min = (15 if ms <= 120 else 30 if ms <= 240
                                      else 60 if ms <= 480 else 120)
                coarser = max(_raster_steps(sim, sim._est_sweep_min), meter_raster)
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


def node_profiles(sim, bus: int, view: str = "est") -> dict:
    """A bus's daily curves. ``view`` picks the layers the caller may see:
    ``truth`` = input p_mw split into residential / EV / PV (grid-model
    knowledge) + the solved voltage; ``measured`` = ONLY the meter's own
    quantities in the metering raster (net P, V in full mode — never the
    composition, which no meter can know); ``est`` = all layers overlaid."""
    p, net = sim.prof, sim.net
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
        kind = sim.sgen_kind[i] if i < len(sim.sgen_kind) else "pv"
        # real-PV day → a PV system's peak × the day's shape; wind/biogas
        # (and any other kind) always keep their built-in profile
        row = (sim.sgen_peak[i] * sim.pv_days[sim.day % sim.pv_days.shape[0]]
               if sim.pv_days is not None and kind == "pv" else p.sgen_p[i])
        add(kind, row)
    order = ["residential", "ev", "pv", "wind", "biogas", "gen"]
    series = [{"kind": k, "p_mw": [_r(v) for v in cats[k]]}
              for k in order if cats.get(k) is not None]
    name = str(net.bus.at[bus, "name"]) if bus in net.bus.index else str(bus)
    out = {"bus": bus, "name": name, "steps_per_day": sim.steps_per_day,
           "view": view, "series": [], "voltage": [],
           "est_voltage": None, "measured": None}
    if view in ("truth", "est"):
        d = daily_curves(sim, sim.day)
        out["series"] = series
        out["voltage"] = d["bus_vm"].get(bus, [])
    if view in ("measured", "est"):
        m = measured_curves(sim, sim.day)
        node = m["nodes"].get(bus)
        if node is not None:
            out["measured"] = dict(node)
    if view == "est":
        out["est_voltage"] = daily_est(sim, sim.day)["est_bus_vm"].get(bus)
    return out


def line_profiles(sim, line: int, view: str = "est") -> dict:
    """A line's daily current + loading curves, with its rated current (the
    ampacity limit = ``max_i_ka × parallel``). ``view`` picks the layers:
    lines carry no meters, so the measured view is deliberately empty."""
    net = sim.net
    rated = float(net.line.at[line, "max_i_ka"]) * int(net.line.at[line, "parallel"])
    out = {
        "line": line, "name": str(net.line.at[line, "name"] or line),
        "from_bus": int(net.line.at[line, "from_bus"]), "to_bus": int(net.line.at[line, "to_bus"]),
        "steps_per_day": sim.steps_per_day, "rated_i_ka": _r(rated),
        "view": view, "current": [], "loading": [], "est_current": None,
    }
    if view in ("truth", "est"):
        d = daily_curves(sim, sim.day)
        out["current"] = d["line_i_ka"].get(line, [])
        out["loading"] = d["line_loading"].get(line, [])
    if view == "est":
        out["est_current"] = daily_est(sim, sim.day)["est_line_i_ka"].get(line)
    return out


def trafo_profiles(sim, trafo: int, view: str = "est") -> dict:
    """A transformer's daily power exchange (HV-side P) + loading curves, with
    its rated apparent power (``sn_mva × parallel``) as the capacity limit.
    ``view`` picks the layers; the measured layer appears only for a metered
    transformer, in the metering raster."""
    net = sim.net
    rated = float(net.trafo.at[trafo, "sn_mva"]) * int(net.trafo.at[trafo, "parallel"])
    out = {
        "trafo": trafo, "name": str(net.trafo.at[trafo, "name"] or trafo),
        "hv_bus": int(net.trafo.at[trafo, "hv_bus"]), "lv_bus": int(net.trafo.at[trafo, "lv_bus"]),
        "steps_per_day": sim.steps_per_day, "sn_mva": _r(rated),
        "view": view, "power": [], "loading": [], "est_power": None, "measured": None,
    }
    if view in ("truth", "est"):
        d = daily_curves(sim, sim.day)
        out["power"] = d["trafo_p_hv"].get(trafo, [])
        out["loading"] = d["trafo_loading"].get(trafo, [])
    if view in ("measured", "est"):
        m = measured_curves(sim, sim.day)
        tm = m["trafos"].get(trafo)
        if tm is not None:
            out["measured"] = dict(tm)
    if view == "est":
        out["est_power"] = daily_est(sim, sim.day)["est_trafo_p_hv"].get(trafo)
    return out


def battery_profiles(sim, storage_idx: int) -> dict | None:
    """A battery's daily SOC + charge/discharge curve for the current day, in
    the full input raster (1 minute on the committed grids). The battery
    strategies read profiles/price only (no power flow), so this integrates
    SOC over the full day cheaply — on throwaway battery copies starting at
    50 %, so the live SOC is untouched. Includes the price curve for context."""
    target = next((b for b in sim.batteries if b.storage_idx == storage_idx), None)
    if target is None:
        return None
    day, spd = sim.day, sim.steps_per_day
    dt_h = 24.0 / spd
    bs = [Battery(bus=b.bus, capacity_mwh=b.capacity_mwh, power_mw=b.power_mw, mode=b.mode,
                  eff=b.eff, soc_min=b.soc_min, soc_max=b.soc_max,
                  soc_mwh=0.5 * b.capacity_mwh, name=b.name, storage_idx=b.storage_idx)
          for b in sim.batteries]
    tgt = next(b for b in bs if b.storage_idx == storage_idx)
    soc: list = []; power: list = []; price: list = []
    price_lo = price_hi = None
    for t in range(spd):
        sgen_col = sim._sgen_p_col(day, t) if sim.prof.sgen_idx else None
        total_load = float(sim.prof.load_p[:, t].sum()) if sim.prof.load_p.size else 0.0
        total_gen = float(sgen_col.sum()) if sgen_col is not None and sgen_col.size else 0.0
        pctx = sim._price_ctx(day, t)
        base = {"through_mw": total_load - total_gen,
                "peak_hi_mw": sim._peak_ref_mw * 0.6, "peak_lo_mw": sim._peak_ref_mw * 0.3, **pctx}
        p_tgt = 0.0
        for b in bs:
            ctx = dict(base)
            if b.mode == "self":
                ctx["load_mw"] = sum(float(sim.prof.load_p[i, t]) for i in sim._loads_at.get(b.bus, []))
                ctx["pv_mw"] = (sum(float(sgen_col[i]) for i in sim._sgens_at.get(b.bus, []))
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
