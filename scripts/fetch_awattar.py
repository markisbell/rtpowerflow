"""Fetch hourly day-ahead electricity prices from aWATTar (Austria) and cache them.

Prices drive the battery "price" strategy. We fetch the same calendar days as the
cached real-PV data (data/real_pv_days.json) so the day slider selects a matching
PV shape *and* price curve. Prices are EUR/MWh, local time (Europe/Vienna ==
Europe/Berlin offset). netzsim loads the cached JSON at runtime.

Usage:  python scripts/fetch_awattar.py
"""
from __future__ import annotations

import datetime as dt
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.awattar.at/v1/marketdata"
ROOT = Path(__file__).resolve().parents[1]
PV = ROOT / "data" / "real_pv_days.json"
OUT = ROOT / "data" / "awattar_prices.json"
TZ = dt.timezone(dt.timedelta(hours=2))   # CEST (May–June); matches the PV local time


def _fetch(day: dt.date) -> list[float] | None:
    start = int(dt.datetime(day.year, day.month, day.day, tzinfo=TZ).timestamp() * 1000)
    end = start + 24 * 3600 * 1000
    url = f"{API}?start={start}&end={end}"
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                data = json.load(r).get("data", [])
            break
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                time.sleep(2.0 * (attempt + 1))   # back off on rate limit
                continue
            raise
    if len(data) < 24:
        return None
    # 24 hourly prices, local-hour ordered
    return [round(float(d["marketprice"]), 2) for d in data[:24]]


def main() -> None:
    dates = [d["date"] for d in json.loads(PV.read_text())["days"]] if PV.exists() else []
    if not dates:
        # fall back to a recent 30-day window
        today = dt.date.today()
        dates = [(today - dt.timedelta(days=k)).isoformat() for k in range(30, 0, -1)]
    prices = {}
    for iso in dates:
        day = dt.date.fromisoformat(iso)
        try:
            hourly = _fetch(day)
        except Exception as exc:  # noqa: BLE001
            print(f"{iso}  ERROR {exc}")
            continue
        if hourly:
            prices[iso] = hourly
            print(f"{iso}  min={min(hourly):.0f} max={max(hourly):.0f} EUR/MWh")
        else:
            print(f"{iso}  (no data)")
        time.sleep(0.4)   # be gentle with the public API
    OUT.write_text(json.dumps({
        "source": API, "unit": "EUR/MWh", "tz": "Europe/Vienna",
        "generated": dt.datetime.utcnow().isoformat() + "Z", "prices": prices,
    }, indent=0))
    print(f"\nWrote {len(prices)} day(s) to {OUT}")


if __name__ == "__main__":
    main()
