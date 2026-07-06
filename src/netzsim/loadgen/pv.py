"""Synthetic rooftop-PV generation assignment.

PV output is well described by a clear-sky bell curve (zero at night, peak at
solar noon), so we don't need the LPG engine for it. A fraction of the grid's
load buses get a PV ``sgen`` sized to a peak kWp, with small per-system scatter.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


@dataclass
class PvPolicy:
    penetration: float = 0.0    # fraction of load buses that host a PV system
    kwp: float = 5.0            # peak DC power per system [kW]
    mix: bool = False           # random size (0.5–1.5x kwp) + orientation per system
    seed: int = 0
    peak_hour: float = 12.0     # solar noon
    width_hours: float = 3.5    # bell half-width


def _clearsky(steps: int, peak_hour: float, width_hours: float) -> np.ndarray:
    t = np.arange(steps)
    peak = peak_hour / 24.0 * steps
    width = width_hours / 24.0 * steps
    day_start, day_end = 6.0 / 24.0 * steps, 18.0 / 24.0 * steps
    bell = np.exp(-((t - peak) ** 2) / (2.0 * width ** 2))
    # The raw Gaussian is still well above zero at sunrise/sunset, which made PV
    # jump from 0 to ~20 % at 06:00. Subtract the daylight-boundary value and
    # renormalise so the curve ramps from exactly 0 at sunrise to 1 at noon.
    edge = max(math.exp(-((day_start - peak) ** 2) / (2.0 * width ** 2)),
               math.exp(-((day_end - peak) ** 2) / (2.0 * width ** 2)))
    bell = np.clip((bell - edge) / (1.0 - edge), 0.0, 1.0)
    bell[(t < day_start) | (t > day_end)] = 0.0
    return bell


def assign_pv(loads: list[dict], policy: PvPolicy, *, steps: int = 1440) -> dict:
    """Return a netzsim ``generation.json`` doc (sgen) for the PV systems."""
    rng = np.random.default_rng(policy.seed + 99)
    # separate stream for the size/orientation mix so enabling it does NOT
    # shift the main stream: the same buses get PV, only the systems differ
    mix_rng = np.random.default_rng(policy.seed + 211)
    shape = _clearsky(steps, policy.peak_hour, policy.width_hours)
    gens: list[dict] = []
    for i, ld in enumerate(loads):
        if rng.random() >= policy.penetration:
            continue
        # small scatter for orientation/derating (0.8–1.0); always drawn so the
        # selection stream stays identical whether or not the mix is active
        base_scatter = 0.8 + 0.2 * rng.random()
        if policy.mix:
            # random size around the chosen kWp + east/west roof orientation
            # (the midday peak shifts per system by up to ±1.5 h)
            size_mw = policy.kwp / 1000.0 * float(mix_rng.uniform(0.5, 1.5))
            shape_i = _clearsky(steps, policy.peak_hour + float(mix_rng.uniform(-1.5, 1.5)),
                                policy.width_hours)
        else:
            size_mw = policy.kwp / 1000.0 * base_scatter
            shape_i = shape
        p = np.round(shape_i * size_mw, 6)
        gens.append({
            "name": f"PV_{ld.get('name') or i}",
            "bus": ld["bus"],
            "p_mw": p.tolist(),
            "q_mvar": [0.0] * steps,  # unity power factor
        })
    return {
        "resolution_minutes": 1440 // steps if steps else 1,
        "steps": steps,
        "generation": gens,
    }
