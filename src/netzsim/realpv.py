"""Load cached real-PV daily shapes for the multi-day / day-switcher feature.

The cache (``data/real_pv_days.json``, produced by ``scripts/fetch_real_pv.py``)
holds a list of clean measured PV days, each a normalised 0..1 shape (clear-day
peak ≈ 1.0) in local time. The simulator applies day ``d`` as a per-PV scale so
switching days shows a different real solar profile.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np


@dataclass
class PvDays:
    shapes: np.ndarray      # [n_days, steps], normalised 0..1
    dates: list[str]
    peak_w: float

    @property
    def n_days(self) -> int:
        return int(self.shapes.shape[0])


def _resample(arr: np.ndarray, steps: int) -> np.ndarray:
    m = arr.shape[1]
    xi = np.linspace(0, m - 1, steps)
    return np.vstack([np.interp(xi, np.arange(m), row) for row in arr])


def load_prices(path: str | Path, dates: list[str]) -> np.ndarray | None:
    """Load cached hourly aWATTar prices aligned 1:1 with ``dates`` (the PV days),
    so day index d → prices[d]. Missing days fall back to the mean day. Returns
    ``[n_days, 24]`` EUR/MWh or ``None``."""
    p = Path(path)
    if not p.exists() or not dates:
        return None
    try:
        pr = (json.loads(p.read_text()).get("prices") or {})
    except Exception:  # noqa: BLE001
        return None
    have = [pr[d] for d in dates if d in pr and len(pr[d]) == 24]
    if not have:
        return None
    mean_day = np.mean(np.asarray(have, dtype=float), axis=0)
    rows = [pr[d] if (d in pr and len(pr[d]) == 24) else mean_day for d in dates]
    return np.asarray(rows, dtype=float)


def load_pv_days(path: str | Path, steps: int = 1440) -> PvDays | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        doc = json.loads(p.read_text())
    except Exception:  # noqa: BLE001 — a bad cache should not break startup
        return None
    days = [d for d in (doc.get("days") or []) if d.get("shape")]
    if not days:
        return None
    arr = np.asarray([d["shape"] for d in days], dtype=float)
    if arr.shape[1] != steps:
        arr = _resample(arr, steps)
    return PvDays(shapes=arr, dates=[d.get("date", str(i)) for i, d in enumerate(days)],
                 peak_w=float(doc.get("peak_w", 0.0)))
