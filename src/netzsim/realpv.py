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
