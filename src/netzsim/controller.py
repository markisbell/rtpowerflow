"""Placeable overload controller (netzdienliche Steuerung).

Modelled after German practice — §14a EnWG (dimming controllable loads such
as wallboxes) and Einspeisemanagement (PV feed-in curtailment): a controller
watches the loading of its domain and throttles EV charging or PV feed-in
STEPWISE when a limit is exceeded, releasing again with hysteresis once the
loading has fallen well below the limit.

The control loop is deliberately a real closed loop: factors computed from
the CURRENT solved step act on the NEXT step — a field controller also only
reacts after measuring. With one step per simulated minute the default rates
mean: full curtailment within ~4 minutes of sustained overload, full release
within ~20 minutes of healthy loading.

The controller is fed ONLY from the operator's view — meter readings
(``StepResult.measurements``) and the WLS state estimate
(``StepResult.estimated``), see ``Simulator._controller_update``. The true
power flow never reaches the control law: without meters the controller is
blind (``seen_pct is None``) and holds its factors, and an overload the
estimate cannot reconstruct (reference scenario 3 without plant SMGWs) is
not acted upon. That is the point: control quality equals observability.

Which lever the controller pulls follows the flow direction of its domain:
a net-exporting domain (midday PV) throttles generation, a net-importing one
(evening EV charging) throttles the controllable loads.

Vertical integration (Phase 2): scope ``cell`` limits the domain to ONE
spliced ONS cell (its station trafo + cell lines + the DERs behind them);
scope ``mv`` is the MV COORDINATOR — it watches only the MV level (MV lines,
HV/MV trafo) and never throttles DERs itself. Instead its factors are
broadcast as a grid-traffic-light SIGNAL to every placed cell controller,
which applies it as an upper bound on top of its own local law
(``min(local, signal)``). A cell without a controller has no Steuerbox and
stays uncoordinated; a locally blind cell controller still executes the
coordinator's signal — the command path needs a device, not a meter.
"""
from __future__ import annotations

from dataclasses import dataclass, field

SCOPES = ("station", "bus", "cell", "mv")


@dataclass
class Controller:
    """One placed controller; ``station`` covers the whole grid, ``bus`` the
    DERs at a single node (its lever reacts to the lines touching that bus),
    ``cell`` one ONS cell, ``mv`` the coordinating MV level (signal only)."""

    cid: int
    scope: str = "station"        # "station" | "bus" | "cell" | "mv"
    bus: int | None = None        # for scope "bus"
    cell: str | None = None       # for scope "cell"
    limit_pct: float = 100.0      # curtail while loading is above this
    release_pct: float = 80.0     # ramp back below this (hysteresis band)
    step_down: float = 0.25       # factor cut per violating step
    step_up: float = 0.05         # factor recovery per healthy step
    ev_factor: float = 1.0        # applied to EV charging loads in scope
    pv_factor: float = 1.0        # applied to PV feed-in in scope
    # grid-traffic-light bound received from an MV coordinator (cell scope
    # only; 1.0 = green). Applied as min(local factor, signal).
    signal_ev: float = 1.0
    signal_pv: float = 1.0
    # an MV coordinator's last broadcast, per cell id (UI signal table)
    signals: dict = field(default_factory=dict)
    # what the controller last saw of its domain (None = blind, no data)
    seen_pct: float | None = None
    seen_src: str | None = None   # "meter" | "estimate" | None

    @property
    def effective_ev(self) -> float:
        return min(self.ev_factor, self.signal_ev)

    @property
    def effective_pv(self) -> float:
        return min(self.pv_factor, self.signal_pv)

    @property
    def active(self) -> bool:
        return self.effective_ev < 1.0 or self.effective_pv < 1.0

    def update(self, max_loading_pct: float | None, exporting: bool) -> None:
        """One control step from the OBSERVED loading of the domain. ``None``
        means the controller has no data — it holds its factors (a field
        device without a measurement neither dims nor releases)."""
        if max_loading_pct is None:
            return
        if max_loading_pct > self.limit_pct:
            if exporting:
                self.pv_factor = max(0.0, round(self.pv_factor - self.step_down, 6))
            else:
                self.ev_factor = max(0.0, round(self.ev_factor - self.step_down, 6))
        elif max_loading_pct < self.release_pct:
            self.ev_factor = min(1.0, round(self.ev_factor + self.step_up, 6))
            self.pv_factor = min(1.0, round(self.pv_factor + self.step_up, 6))

    def as_dict(self) -> dict:
        d = {"id": self.cid, "scope": self.scope, "bus": self.bus,
             "cell": self.cell,
             "limit_pct": self.limit_pct, "release_pct": self.release_pct,
             "ev_factor": round(self.effective_ev, 4),
             "pv_factor": round(self.effective_pv, 4),
             "active": self.active,
             "seen_pct": round(self.seen_pct, 2) if self.seen_pct is not None else None,
             "seen_src": self.seen_src}
        if self.scope == "cell" and (self.signal_ev < 1.0 or self.signal_pv < 1.0):
            d["signal"] = {"ev": round(self.signal_ev, 4), "pv": round(self.signal_pv, 4)}
        if self.scope == "mv":
            d["signals"] = {cid: {"ev": round(s[0], 4), "pv": round(s[1], 4)}
                            for cid, s in self.signals.items()}
        return d
