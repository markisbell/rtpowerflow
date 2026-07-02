"""Local battery storage: element state + per-step control strategies.

A battery is a pandapower ``storage`` at a bus. Sign convention follows
pandapower: ``p_mw > 0`` charges (draws from the grid), ``p_mw < 0`` discharges
(injects). State of charge is integrated across steps by the simulator; the
power flow itself is stateless.

Three strategies (chosen per battery, run simultaneously):
* ``self``  — household self-sufficiency: soak up local PV surplus, cover local demand.
* ``peak``  — shave the transformer peak: discharge above a loading band, refill below it.
* ``price`` — arbitrage: charge in the cheapest hours, discharge in the priciest.
"""
from __future__ import annotations

from dataclasses import dataclass

MODES = ("self", "peak", "price")


@dataclass
class Battery:
    bus: int
    capacity_mwh: float
    power_mw: float
    mode: str = "self"
    eff: float = 0.95          # one-way efficiency (√ round-trip)
    soc_min: float = 0.10
    soc_max: float = 1.00
    soc_mwh: float = 0.0       # state of charge (energy)
    name: str = ""
    storage_idx: int | None = None   # pandapower storage index

    def soc_frac(self) -> float:
        return self.soc_mwh / self.capacity_mwh if self.capacity_mwh else 0.0

    def room_mwh(self) -> float:
        return max(0.0, self.soc_max * self.capacity_mwh - self.soc_mwh)

    def avail_mwh(self) -> float:
        return max(0.0, self.soc_mwh - self.soc_min * self.capacity_mwh)


def setpoint(b: Battery, ctx: dict, dt_h: float) -> float:
    """Charge(+)/discharge(-) power [MW] for this step, clamped by the power
    rating and the remaining SOC head-/foot-room."""
    p = 0.0
    if b.mode == "self":
        net = ctx.get("load_mw", 0.0) - ctx.get("pv_mw", 0.0)
        p = -net                                    # surplus → charge, demand → discharge
    elif b.mode == "peak":
        through = ctx.get("through_mw", 0.0)
        hi = ctx.get("peak_hi_mw")
        lo = ctx.get("peak_lo_mw")
        if hi is not None and through > hi:
            p = -(through - hi)                     # shave above the band
        elif lo is not None and through < lo:
            p = b.power_mw                          # refill below the band
    elif b.mode == "price":
        pr, lo, hi = ctx.get("price"), ctx.get("price_lo"), ctx.get("price_hi")
        if pr is not None and lo is not None and hi is not None:
            if pr <= lo:
                p = b.power_mw
            elif pr >= hi:
                p = -b.power_mw
    # clamp by rating, then by SOC band
    p = max(-b.power_mw, min(b.power_mw, p))
    if dt_h > 0:
        if p > 0:
            p = min(p, b.room_mwh() / (b.eff * dt_h))
        elif p < 0:
            p = max(p, -b.avail_mwh() * b.eff / dt_h)
    return p


def integrate(b: Battery, p_mw: float, dt_h: float) -> None:
    """Advance SOC by holding ``p_mw`` for ``dt_h`` hours (with efficiency)."""
    b.soc_mwh += (p_mw * b.eff if p_mw > 0 else p_mw / b.eff) * dt_h
    b.soc_mwh = min(b.soc_max * b.capacity_mwh, max(b.soc_min * b.capacity_mwh, b.soc_mwh))
