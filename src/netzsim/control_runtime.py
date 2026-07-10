"""The controllers' and rONTs' per-step runtime: domain views, factor
application and the control passes.

Extracted from ``Simulator`` (2026-07-10); every function takes the live
``sim`` as first argument and the ``Simulator`` keeps thin delegate methods.
The placement CRUD (add/remove/set controller and rONT) stays on the
Simulator — it owns the id counters, the rONT tap-data originals and the
domain index sets (``_cell_buses``/``_cell_lines``/``_cell_trafos``/
``_mv_lines``/``_mv_trafos``) these passes read.

The control law is deliberately fed ONLY from the operator's view — meter
readings and the WLS estimate, never the truth arrays: control quality =
observability (customer feedback 2026-07-07)."""
from __future__ import annotations

from .controller import Controller


def controller_rows(sim, c: Controller) -> tuple[list[int], list[int]]:
    """Profile row indices (EV loads, PV sgens) in the controller's scope.
    The MV coordinator throttles nothing itself — it only signals."""
    if c.scope == "mv":
        return [], []
    member = sim._cell_buses.get(c.cell or "", set()) if c.scope == "cell" else None

    def in_scope(bus: int) -> bool:
        if c.scope == "station":
            return True
        if c.scope == "cell":
            return bus in member
        return bus == c.bus

    ev_rows = [i for i, li in enumerate(sim.prof.load_idx)
               if "EV_" in str(sim.net.load.at[li, "name"] or "")
               and in_scope(int(sim.net.load.at[li, "bus"]))]
    pv_rows = [i for i, si in enumerate(sim.prof.sgen_idx)
               if i < len(sim._sgen_is_pv) and sim._sgen_is_pv[i]
               and in_scope(int(sim.net.sgen.at[si, "bus"]))]
    return ev_rows, pv_rows


def apply_controller_factors(sim) -> None:
    """Scale EV loads / PV sgens by the strictest covering controller —
    called after the step profiles are written, before the batteries.
    Cell controllers apply min(local law, coordinator signal)."""
    ev_f: dict[int, float] = {}
    pv_f: dict[int, float] = {}
    for c in sim.controllers:
        ev_rows, pv_rows = controller_rows(sim, c)
        for i in ev_rows:
            ev_f[i] = min(ev_f.get(i, 1.0), c.effective_ev)
        for i in pv_rows:
            pv_f[i] = min(pv_f.get(i, 1.0), c.effective_pv)
    for i, f in ev_f.items():
        if f < 1.0:
            li = sim.prof.load_idx[i]
            sim.net.load.at[li, "p_mw"] *= f
            sim.net.load.at[li, "q_mvar"] *= f
    for i, f in pv_f.items():
        if f < 1.0:
            si = sim.prof.sgen_idx[i]
            sim.net.sgen.at[si, "p_mw"] *= f
            sim.net.sgen.at[si, "q_mvar"] *= f


def controller_update(sim, res) -> None:
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
    # identity of the current estimate: estimate-fed controllers act once
    # per NEW telegram (see Controller.est_stamp)
    est_stamp = est.get("seq") if est else None

    def _act(c: Controller, seen: list, exporting: bool) -> None:
        if seen:
            c.seen_pct, c.seen_src = max(seen, key=lambda v: v[0])
            if c.seen_src == "estimate":
                if c.est_stamp == est_stamp:
                    return                      # stale telegram: hold
                c.est_stamp = est_stamp
            c.update(c.seen_pct, exporting)
        else:
            c.seen_pct = c.seen_src = None
            c.update(None, False)

    # -- pass 1: MV coordinators (grid traffic light) ------------------- #
    # They see only the MV level and ratchet their factors like any
    # controller; the factors are then broadcast as per-cell SIGNALS to
    # every placed cell controller (min-bound on top of its local law).
    # Executing a received signal needs a device, not a meter — a locally
    # blind cell controller still dims on command.
    coords = [c for c in sim.controllers if c.scope == "mv"]
    cell_ctrls = [c for c in sim.controllers if c.scope == "cell"]
    for c in coords:
        seen: list[tuple[float, str]] = []
        for tm in meas.get("trafos", []):
            tr = tm.get("trafo")
            if tr in sim._mv_trafos and tm.get("loading_percent") is not None:
                seen.append((float(tm["loading_percent"]), "meter"))
        for ln in est_lines:
            if ln.get("index") in sim._mv_lines and ln.get("loading_percent") is not None:
                seen.append((float(ln["loading_percent"]), "estimate"))
        for tr in est_trafos:
            if tr.get("index") in sim._mv_trafos and tr.get("loading_percent") is not None:
                seen.append((float(tr["loading_percent"]), "estimate"))
        # direction from the HV/MV import (measured before estimated):
        # the MV level exporting into the HV grid = PV is the lever
        p_hv = [tm["p_hv_mw"] for tm in meas.get("trafos", [])
                if tm.get("trafo") in sim._mv_trafos and tm.get("p_hv_mw") is not None]
        if not p_hv:
            p_hv = [tr["p_hv_mw"] for tr in est_trafos
                    if tr.get("index") in sim._mv_trafos and tr.get("p_hv_mw") is not None]
        exporting = bool(p_hv) and sum(p_hv) < 0.0
        _act(c, seen, exporting)
        c.signals = {cc.cell: (c.ev_factor, c.pv_factor) for cc in cell_ctrls}
    # the strictest coordinator wins per cell (usually there is one)
    for cc in cell_ctrls:
        sig_ev = min((c.ev_factor for c in coords), default=1.0)
        sig_pv = min((c.pv_factor for c in coords), default=1.0)
        cc.signal_ev, cc.signal_pv = sig_ev, sig_pv

    # -- pass 2: local controllers -------------------------------------- #
    for c in sim.controllers:
        if c.scope == "mv":
            continue
        seen = []
        if c.scope == "cell":
            member_t = sim._cell_trafos.get(c.cell or "", set())
            member_l = sim._cell_lines.get(c.cell or "", set())
            # the cell's own station meter is a direct reading ...
            for tm in meas.get("trafos", []):
                if tm.get("trafo") in member_t and tm.get("loading_percent") is not None:
                    seen.append((float(tm["loading_percent"]), "meter"))
            # ... its lines (and an unmetered station trafo) only estimated
            for ln in est_lines:
                if ln.get("index") in member_l and ln.get("loading_percent") is not None:
                    seen.append((float(ln["loading_percent"]), "estimate"))
            for tr in est_trafos:
                if tr.get("index") in member_t and tr.get("loading_percent") is not None:
                    seen.append((float(tr["loading_percent"]), "estimate"))
            # direction from the cell's boundary flow (station trafo)
            p_hv = [tm["p_hv_mw"] for tm in meas.get("trafos", [])
                    if tm.get("trafo") in member_t and tm.get("p_hv_mw") is not None]
            if not p_hv:
                p_hv = [tr["p_hv_mw"] for tr in est_trafos
                        if tr.get("index") in member_t and tr.get("p_hv_mw") is not None]
            exporting = bool(p_hv) and sum(p_hv) < 0.0
        elif c.scope == "station":
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
            ln_df = sim.net.line
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
        _act(c, seen, exporting)


def ront_update(sim, res) -> None:
    """One regulation step per rONT — fed ONLY from the operator's view of
    its busbar: the smart-meter voltage where one delivers V, else the
    state estimate. Estimate-fed regulators act once per new telegram;
    without data the rONT is blind and holds. The chosen tap acts on the
    NEXT step (written to the net here)."""
    est = res.estimated if isinstance(res.estimated, dict) else {}
    est_stamp = est.get("seq") if est else None
    for r in sim.ronts:
        v = next((n["vm_pu"] for n in (res.measurements or {}).get("nodes", [])
                  if n.get("bus") == r.busbar and n.get("vm_pu") is not None),
                 None)
        src = "meter" if v is not None else None
        if v is None:
            v = next((b["vm_pu"] for b in est.get("buses") or []
                      if b.get("index") == r.busbar and b.get("vm_pu") is not None),
                     None)
            src = "estimate" if v is not None else None
        r.seen_v, r.seen_src = (float(v) if v is not None else None), src
        if src == "estimate":
            if r.est_stamp == est_stamp:
                continue                     # stale telegram: hold
            r.est_stamp = est_stamp
        if r.update(r.seen_v):
            sim.net.trafo.at[r.trafo, "tap_pos"] = r.tap_pos
