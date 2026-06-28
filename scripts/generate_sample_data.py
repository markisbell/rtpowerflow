"""Generate a small, valid example dataset (5 buses, 1440 steps) into ./data.

Run:  python scripts/generate_sample_data.py
Creates the five JSON files the simulator expects so the app runs out of the box.

Profile shapes are anchored to clock *hours* (not raw step indices) so the
resolution can be changed by editing ``STEPS``/``RES_MIN`` alone.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

STEPS = 1440      # 24h @ 1 min
RES_MIN = 1       # minutes per step  (STEPS * RES_MIN must equal 1440)
DATA = Path(__file__).resolve().parents[1] / "data"


def _step_of_hour(hour: float) -> float:
    """Fractional step index corresponding to a time of day in hours [0, 24)."""
    return hour / 24.0 * STEPS


def daily(base: float, amp: float, peak_hour: float, floor: float = 0.0) -> list[float]:
    """A smooth sinusoidal daily shape peaking at ``peak_hour`` (clock hours)."""
    peak_step = _step_of_hour(peak_hour)
    out = []
    for t in range(STEPS):
        phase = 2 * math.pi * (t - peak_step) / STEPS
        val = base + amp * math.cos(phase)
        out.append(round(max(floor, val), 4))
    return out


def solar(peak: float, peak_hour: float = 12.0, width_hours: float = 5.5) -> list[float]:
    """Bell-shaped PV generation, zero outside daylight (06:00-18:00)."""
    peak_step = _step_of_hour(peak_hour)
    width = width_hours / 24.0 * STEPS
    day_start, day_end = _step_of_hour(6.0), _step_of_hour(18.0)
    out = []
    for t in range(STEPS):
        val = peak * math.exp(-((t - peak_step) ** 2) / (2 * width ** 2))
        out.append(round(val if day_start <= t <= day_end else 0.0, 4))
    return out


def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)

    grid = {
        "name": "Example LV feeder",
        "f_hz": 50.0,
        "buses": [
            {"name": "Substation (MV/LV)", "vn_kv": 0.4, "zone": "LV"},
            {"name": "Bus 1", "vn_kv": 0.4, "zone": "LV"},
            {"name": "Bus 2", "vn_kv": 0.4, "zone": "LV"},
            {"name": "Bus 3", "vn_kv": 0.4, "zone": "LV"},
            {"name": "Bus 4", "vn_kv": 0.4, "zone": "LV"},
        ],
    }

    lines = {
        "lines": [
            {"name": "L0-1", "from_bus": 0, "to_bus": 1, "length_km": 0.12,
             "std_type": "NAYY 4x150 SE"},
            {"name": "L1-2", "from_bus": 1, "to_bus": 2, "length_km": 0.10,
             "std_type": "NAYY 4x150 SE"},
            {"name": "L2-3", "from_bus": 2, "to_bus": 3, "length_km": 0.08,
             "std_type": "NAYY 4x150 SE"},
            {"name": "L1-4", "from_bus": 1, "to_bus": 4, "length_km": 0.15,
             "std_type": "NAYY 4x150 SE"},
        ],
        "transformers": [],
    }

    load = {
        "resolution_minutes": RES_MIN, "steps": STEPS,
        "loads": [
            {"name": "Load B1", "bus": 1, "p_mw": daily(0.030, 0.020, 19.0),
             "q_mvar": daily(0.010, 0.006, 19.0)},
            {"name": "Load B2", "bus": 2, "p_mw": daily(0.025, 0.018, 19.5),
             "q_mvar": daily(0.008, 0.005, 19.5)},
            {"name": "Load B3", "bus": 3, "p_mw": daily(0.040, 0.025, 20.0),
             "q_mvar": daily(0.013, 0.008, 20.0)},
            {"name": "Load B4", "bus": 4, "p_mw": daily(0.020, 0.015, 18.5),
             "q_mvar": daily(0.006, 0.004, 18.5)},
        ],
    }

    generation = {
        "resolution_minutes": RES_MIN, "steps": STEPS,
        "generation": [
            {"name": "PV B3", "bus": 3, "p_mw": solar(0.050),
             "q_mvar": [0.0] * STEPS},
            {"name": "PV B4", "bus": 4, "p_mw": solar(0.035),
             "q_mvar": [0.0] * STEPS},
        ],
    }

    substation = {
        "resolution_minutes": RES_MIN, "steps": STEPS,
        "substations": [
            # Upper-grid voltage set-point sags slightly during the evening peak.
            {"name": "MV slack", "bus": 0,
             "vm_pu": daily(1.02, -0.02, 19.0, floor=0.96),
             "va_degree": [0.0] * STEPS},
        ],
    }

    files = {
        "grid_structure.json": grid,
        "lines.json": lines,
        "load.json": load,
        "generation.json": generation,
        "substation.json": substation,
    }
    for fname, obj in files.items():
        (DATA / fname).write_text(json.dumps(obj, indent=2), encoding="utf-8")
        print(f"wrote {DATA / fname}")


if __name__ == "__main__":
    main()
