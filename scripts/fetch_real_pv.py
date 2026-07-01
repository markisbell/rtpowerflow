"""Fetch real PV daily shapes from the Raspberry Pi InfluxDB and cache them.

The Pi at 192.168.178.23 runs InfluxDB 1.8 (db ``griddata``, measurement
``grid_data``). Field ``active_power_a`` is a real rooftop-PV feed-in meter
(positive = PV, negative = an air-conditioner that was connected later). We use
the clean window (May–early June, before the AC) and store each day as a
normalised 0..1 shape (clear-day peak ≈ 1.0), converted from UTC to local time
so the PV peak sits near local noon. netzsim loads the cached JSON at runtime;
re-run this script (with the Pi reachable) to refresh it.

Usage:  python scripts/fetch_real_pv.py
"""
from __future__ import annotations

import datetime as dt
import json
import urllib.parse
import urllib.request
from pathlib import Path

INFLUX = "http://192.168.178.23:8086"
DB = "griddata"
FIELD = "active_power_a"
REF_MAX = 65.0          # clear-day peak in raw units ≈ the 600 W PV rating
PEAK_W = 600.0
TZ_SHIFT_MIN = 120      # CEST = UTC+2 in May/June → shift UTC series to local time
START = dt.date(2026, 5, 1)
END = dt.date(2026, 6, 7)   # exclusive
STEPS = 1440

OUT = Path(__file__).resolve().parents[1] / "data" / "real_pv_days.json"


def _query(q: str) -> dict:
    url = INFLUX + "/query?" + urllib.parse.urlencode({"db": DB, "q": q})
    with urllib.request.urlopen(url, timeout=25) as r:
        return json.load(r)


def _day_shape(day: dt.date) -> list[float] | None:
    nxt = day + dt.timedelta(days=1)
    q = (f"SELECT MEAN({FIELD}) FROM grid_data "
         f"WHERE time >= '{day}T00:00:00Z' AND time < '{nxt}T00:00:00Z' "
         f"GROUP BY time(1m) fill(0)")
    series = _query(q)["results"][0].get("series")
    if not series:
        return None
    vals = [float(v[1]) if v[1] is not None else 0.0 for v in series[0]["values"]]
    vals = (vals + [0.0] * STEPS)[:STEPS]
    # UTC → local (CEST +2h): local[l] = utc[l-120]
    rolled = vals[-TZ_SHIFT_MIN:] + vals[:-TZ_SHIFT_MIN]
    shape = [round(max(0.0, min(1.0, x / REF_MAX)), 4) for x in rolled]
    return shape if max(shape) > 0.05 else None   # drop empty/missing days


def main() -> None:
    days = []
    d = START
    while d < END:
        shape = _day_shape(d)
        if shape is not None:
            days.append({"date": d.isoformat(), "shape": shape})
            print(f"{d}  peak={max(shape):.2f}  energy={sum(shape)/60:.1f} h·pu")
        else:
            print(f"{d}  (skipped: no/empty data)")
        d += dt.timedelta(days=1)
    OUT.write_text(json.dumps({
        "source": f"{INFLUX} {DB}.grid_data.{FIELD}",
        "generated": dt.datetime.utcnow().isoformat() + "Z",
        "peak_w": PEAK_W, "ref_max": REF_MAX, "tz": "Europe/Berlin",
        "steps": STEPS, "days": days,
    }, indent=0))
    print(f"\nWrote {len(days)} days to {OUT}")


if __name__ == "__main__":
    main()
