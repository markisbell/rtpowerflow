"""Synthetic EV home-charging assignment.

LPG cannot produce a controllable, additive home-charging load for these
archetypes (its agents prefer transit, so the car barely charges), so EV charging
is modelled directly: a fraction of homes get an EV that plugs in on a diversified
evening arrival and draws its wallbox power until the day's energy is delivered
("uncontrolled" charging — the grid-relevant worst case). Each EV becomes an extra
load element at its home bus, so it strictly *adds* to the household load.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


MIX_KW = (3.7, 11.0, 22.0)         # the common home-wallbox ratings


@dataclass
class EvPolicy:
    penetration: float = 0.0       # fraction of load buses with an EV
    charger_kw: float = 11.0       # wallbox power [kW]
    charger_mix: bool = False      # draw each wallbox from MIX_KW instead
    daily_kwh: float = 8.0         # mean energy charged per day (~40 km @ 0.2 kWh/km)
    arrival_hour: float = 18.0     # mean evening plug-in time
    arrival_spread_h: float = 2.0  # std of plug-in time across EVs
    seed: int = 0
    power_factor: float = 0.98


def _charge_profile(steps: int, arrive_h: float, dur_h: float, kw: float) -> np.ndarray:
    prof = np.zeros(steps)
    start = int(arrive_h / 24.0 * steps) % steps
    length = max(1, int(round(dur_h / 24.0 * steps)))
    idx = (start + np.arange(length)) % steps  # wrap past midnight
    prof[idx] = kw
    return prof


def assign_ev(loads: list[dict], policy: EvPolicy, *, steps: int = 1440) -> dict:
    """Return a ``load.json``-shaped doc of additional EV charging loads."""
    rng = np.random.default_rng(policy.seed + 13)
    # separate stream for the wallbox-rating mix, so enabling the mix does NOT
    # shift the main stream: the same homes get an EV, only the ratings differ
    mix_rng = np.random.default_rng(policy.seed + 101)
    pf = max(min(policy.power_factor, 1.0), 1e-3)
    tan_phi = math.tan(math.acos(pf))
    specs: list[dict] = []
    for i, ld in enumerate(loads):
        if rng.random() >= policy.penetration:
            continue
        energy = policy.daily_kwh * (0.5 + rng.random())          # 0.5–1.5× daily spread
        arrive = float(rng.normal(policy.arrival_hour, policy.arrival_spread_h)) % 24.0
        # per-EV wallbox rating: fixed, or a random pick from the common units
        kw_rating = float(mix_rng.choice(MIX_KW)) if policy.charger_mix else policy.charger_kw
        dur_h = max(energy / max(kw_rating, 0.1), 1.0 / 60)
        kw = _charge_profile(steps, arrive, dur_h, kw_rating)
        p_mw = np.round(kw / 1000.0, 6)
        specs.append({
            "name": f"EV_{ld.get('name') or i}",
            "bus": ld["bus"],
            "p_mw": p_mw.tolist(),
            "q_mvar": np.round(p_mw * tan_phi, 6).tolist(),
        })
    return {
        "resolution_minutes": 1440 // steps if steps else 1,
        "steps": steps,
        "loads": specs,
    }
