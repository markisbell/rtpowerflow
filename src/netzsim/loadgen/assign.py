"""Assign archetype daily profiles onto a grid's load elements.

Each load element keeps its name and bus; only the time series is (re)generated
from the library. Diversity comes from cycling distinct ``(archetype, variant)``
pairs across loads, with an optional per-load time jitter.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .library import LoadLibrary


@dataclass
class AssignPolicy:
    archetypes: list[str] | None = None   # subset of library ids; None -> all
    mode: str = "round_robin"             # "round_robin" | "random"
    seed: int = 0
    scale: float = 1.0                    # global multiplier on real household kW
    power_factor: float = 0.95            # -> q_mvar from p_mw
    jitter_minutes: int = 0               # circular shift per load (decorrelate repeats)
    # multi-family buildings: each load element sums this many household
    # profiles (drawn per building). None -> single-family (one profile).
    households_range: tuple[int, int] | None = None


def _resample(profile_kw: list[float], steps: int) -> np.ndarray:
    arr = np.asarray(profile_kw, dtype=float)
    if len(arr) == steps:
        return arr
    # area-preserving-ish resample by linear interpolation onto the new grid
    src = np.linspace(0.0, 1.0, len(arr), endpoint=False)
    dst = np.linspace(0.0, 1.0, steps, endpoint=False)
    return np.interp(dst, src, arr)


def _pool(library: LoadLibrary, ids: list[str]) -> list[tuple[str, int]]:
    """Interleave archetypes so consecutive loads differ as much as possible."""
    per = {aid: library.get(aid).n_variants for aid in ids}
    pool: list[tuple[str, int]] = []
    for vi in range(max(per.values())):
        for aid in ids:
            if vi < per[aid]:
                pool.append((aid, vi))
    return pool


def assign_to_loads(
    loads: list[dict],
    library: LoadLibrary,
    policy: AssignPolicy | None = None,
    *,
    steps: int = 1440,
) -> dict:
    """Return a netzsim ``load.json`` doc with LPG-derived household profiles.

    ``loads`` is a list of ``{name, bus, ...}`` (e.g. a converted grid's loads).
    The returned doc carries an ``assignments`` list mapping each load to the
    archetype + variant it received. (EV charging is layered on separately via
    :func:`netzsim.loadgen.ev.assign_ev`.)
    """
    policy = policy or AssignPolicy()
    if not library.available:
        raise ValueError("load library is empty - build it with build_lpg_library.py")

    ids = policy.archetypes or library.ids()
    unknown = [a for a in ids if not library.has(a)]
    if unknown:
        raise ValueError(f"unknown archetype(s): {unknown}")

    pool = _pool(library, ids)
    rng = np.random.default_rng(policy.seed)
    tan_phi = math.tan(math.acos(max(min(policy.power_factor, 1.0), 1e-3)))

    load_specs: list[dict] = []
    assignments: list[dict] = []
    cursor = 0                       # round-robin position (advances per household)
    for i, ld in enumerate(loads):
        lo, hi = policy.households_range or (1, 1)
        n = int(rng.integers(lo, hi + 1)) if hi > lo else int(lo)
        kw = np.zeros(steps)
        aid = vi = None
        for _ in range(max(n, 1)):
            if policy.mode == "random":
                a, v = pool[int(rng.integers(len(pool)))]
            else:
                a, v = pool[cursor % len(pool)]
                cursor += 1
            if aid is None:
                aid, vi = a, v           # first household labels the building
            hh = _resample(library.get(a).variants_kw[v], steps) * policy.scale
            if policy.jitter_minutes:
                shift = int(rng.integers(-policy.jitter_minutes,
                                         policy.jitter_minutes + 1))
                hh = np.roll(hh, shift)
            kw = kw + hh
        p_mw = np.round(kw / 1000.0, 6)
        q_mvar = np.round(p_mw * tan_phi, 6)
        load_specs.append({
            "name": ld.get("name") or f"load{i}",
            "bus": ld["bus"],
            "p_mw": p_mw.tolist(),
            "q_mvar": q_mvar.tolist(),
            "households": n,
        })
        assignments.append({"name": ld.get("name"), "bus": ld["bus"],
                            "archetype": aid, "variant": vi, "households": n})

    return {
        "resolution_minutes": 1440 // steps if steps else 1,
        "steps": steps,
        "loads": load_specs,
        "assignments": assignments,
    }
