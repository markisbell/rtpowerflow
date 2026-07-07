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

Which lever the controller pulls follows the flow direction of its domain:
a net-exporting domain (midday PV) throttles generation, a net-importing one
(evening EV charging) throttles the controllable loads.
"""
from __future__ import annotations

from dataclasses import dataclass

SCOPES = ("station", "bus")


@dataclass
class Controller:
    """One placed controller; ``station`` covers the whole grid, ``bus`` the
    DERs at a single node (its lever reacts to the lines touching that bus)."""

    cid: int
    scope: str = "station"        # "station" | "bus"
    bus: int | None = None        # for scope "bus"
    limit_pct: float = 100.0      # curtail while loading is above this
    release_pct: float = 80.0     # ramp back below this (hysteresis band)
    step_down: float = 0.25       # factor cut per violating step
    step_up: float = 0.05         # factor recovery per healthy step
    ev_factor: float = 1.0        # applied to EV charging loads in scope
    pv_factor: float = 1.0        # applied to PV feed-in in scope

    @property
    def active(self) -> bool:
        return self.ev_factor < 1.0 or self.pv_factor < 1.0

    def update(self, max_loading_pct: float | None, exporting: bool) -> None:
        """One control step from the freshly solved loading of the domain."""
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
        return {"id": self.cid, "scope": self.scope, "bus": self.bus,
                "limit_pct": self.limit_pct, "release_pct": self.release_pct,
                "ev_factor": round(self.ev_factor, 4),
                "pv_factor": round(self.pv_factor, 4),
                "active": self.active}
