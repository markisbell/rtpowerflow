"""rONT — regelbarer Ortsnetztransformator (on-load tap changer per station).

The classic vertical voltage asset: an OLTC on the MV/LV distribution
transformer that keeps its LV busbar inside a voltage band, decoupling the
cell's voltage level from the MV profile above it. Activating an rONT
upgrades the station transformer's tap data in place (default ±4 × 1.5 %,
the usual rONT range — wider and finer than the off-load taps the standard
units ship with) and steps ``tap_pos`` in a real closed loop: the position
chosen from the CURRENT solved step acts on the NEXT one.

Like the overload controllers, the rONT sees only the operator's view —
the busbar's meter reading if a smart meter delivers voltage there, else
the state estimate at the busbar. An estimate-fed rONT acts once per NEW
telegram (the estimate can refresh much slower than the simulation steps);
without any data it is blind and holds its position. pandapower sign
convention (tap on the HV side): a HIGHER ``tap_pos`` LOWERS the LV
voltage, so the regulation steps ``-1`` to raise and ``+1`` to lower.

The daily-sweep curves (day graphs) show the UNREGULATED day — like the
controllers, the rONT acts only in the live loop.
"""
from __future__ import annotations

from dataclasses import dataclass

# rONT tap range applied to the transformer on activation
RONT_TAP_MIN = -4
RONT_TAP_MAX = 4
RONT_STEP_PERCENT = 1.5


@dataclass
class Ront:
    """One activated rONT; regulates its transformer's LV busbar voltage."""

    rid: int
    trafo: int                    # transformer index (pandapower)
    busbar: int                   # the regulated LV busbar bus
    cell: str | None = None       # owning ONS cell (info, may be None)
    v_target: float = 1.0         # setpoint, pu
    deadband: float = 0.015       # half-width of the healthy band, pu
    tap_pos: int = 0
    # what the regulator last saw of its busbar (None = blind, no data)
    seen_v: float | None = None
    seen_src: str | None = None   # "meter" | "estimate" | None
    # telegram id of the last acted-on estimate (see Controller.est_stamp)
    est_stamp: tuple | None = None

    def update(self, v: float | None) -> bool:
        """One regulation step from the OBSERVED busbar voltage. Returns True
        when the tap actually moved (one mechanical step per action)."""
        if v is None:
            return False
        if v > self.v_target + self.deadband and self.tap_pos < RONT_TAP_MAX:
            self.tap_pos += 1      # higher tap = lower LV voltage
            return True
        if v < self.v_target - self.deadband and self.tap_pos > RONT_TAP_MIN:
            self.tap_pos -= 1
            return True
        return False

    def as_dict(self) -> dict:
        return {"id": self.rid, "trafo": self.trafo, "busbar": self.busbar,
                "cell": self.cell,
                "v_target": self.v_target, "deadband": self.deadband,
                "tap_pos": self.tap_pos,
                "tap_min": RONT_TAP_MIN, "tap_max": RONT_TAP_MAX,
                "tap_step_percent": RONT_STEP_PERCENT,
                "seen_v": round(self.seen_v, 5) if self.seen_v is not None else None,
                "seen_src": self.seen_src}
