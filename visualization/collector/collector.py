"""Collector: poll netzsim's REST API and write each solved step into InfluxDB.

Reads ``GET /state`` on the realtime power-flow service, deduplicates by
(day, step) so every simulated 1-minute step is written exactly once, and
stores buses / lines / trafos / ext_grids / summary as InfluxDB measurements.
The point timestamp is the wall-clock time the step was solved, so a Grafana
"last 5 minutes" view follows the accelerated realtime simulation live.
"""
from __future__ import annotations

import logging
import os
import time

import requests
from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import SYNCHRONOUS

log = logging.getLogger("collector")

NETZSIM_URL = os.getenv("NETZSIM_URL", "http://netzsim:8000").rstrip("/")
INFLUX_URL = os.getenv("INFLUX_URL", "http://influxdb:8086")
INFLUX_TOKEN = os.getenv("INFLUX_TOKEN", "netzsim-dev-token")
INFLUX_ORG = os.getenv("INFLUX_ORG", "netzsim")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "powerflow")
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL_SECONDS", "0.5"))


def wait_for(url: str, label: str, timeout: float = 120.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if requests.get(url, timeout=3).status_code < 500:
                log.info("%s is up.", label)
                return
        except requests.RequestException:
            pass
        time.sleep(2)
    raise RuntimeError(f"Timed out waiting for {label} at {url}")


def build_points(state: dict) -> list[Point]:
    ts = int(float(state["timestamp"]) * 1e9)  # unix seconds -> ns
    day = int(state["day"])
    step = int(state["step"])
    tod = state["time_of_day"]
    pts: list[Point] = []

    def base(measurement: str) -> Point:
        return (
            Point(measurement)
            .field("day", day)
            .field("step", step)
            .tag("time_of_day", tod)
            .time(ts, WritePrecision.NS)
        )

    summary = base("summary").field("converged", int(bool(state["converged"])))
    summary = summary.field("solve_ms", float(state.get("solve_ms") or 0.0))
    for k, v in (state.get("summary") or {}).items():
        if v is not None:
            summary = summary.field(k, float(v))
    pts.append(summary)

    for b in state.get("buses", []):
        p = base("bus").tag("bus_index", str(b["index"])).tag("bus_name", b["name"])
        for f in ("vm_pu", "va_degree", "p_mw", "q_mvar"):
            if b.get(f) is not None:
                p = p.field(f, float(b[f]))
        pts.append(p)

    for ln in state.get("lines", []):
        p = base("line").tag("line_index", str(ln["index"])).tag("line_name", ln["name"])
        for f in ("loading_percent", "i_ka", "p_from_mw", "pl_mw"):
            if ln.get(f) is not None:
                p = p.field(f, float(ln[f]))
        pts.append(p)

    for tr in state.get("trafos", []):
        p = base("trafo").tag("trafo_index", str(tr["index"])).tag("trafo_name", tr["name"])
        for f in ("loading_percent", "p_hv_mw", "q_hv_mvar", "i_hv_ka", "pl_mw"):
            if tr.get(f) is not None:
                p = p.field(f, float(tr[f]))
        pts.append(p)

    for eg in state.get("ext_grids", []):
        p = base("ext_grid").tag("eg_index", str(eg["index"])).tag("eg_name", eg["name"])
        for f in ("p_mw", "q_mvar"):
            if eg.get(f) is not None:
                p = p.field(f, float(eg[f]))
        pts.append(p)

    return pts


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    wait_for(f"{INFLUX_URL}/health", "InfluxDB")
    wait_for(f"{NETZSIM_URL}/health", "netzsim")

    client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
    write_api = client.write_api(write_options=SYNCHRONOUS)
    log.info("Collecting %s/state -> InfluxDB bucket '%s' every %.2fs",
             NETZSIM_URL, INFLUX_BUCKET, POLL_INTERVAL)

    last_key: tuple[int, int] | None = None
    while True:
        try:
            resp = requests.get(f"{NETZSIM_URL}/state", timeout=5)
            if resp.status_code == 404:
                time.sleep(POLL_INTERVAL)  # no step solved yet
                continue
            resp.raise_for_status()
            state = resp.json()
            key = (int(state["day"]), int(state["step"]))
            if key != last_key:
                write_api.write(bucket=INFLUX_BUCKET, record=build_points(state))
                last_key = key
                log.debug("wrote day=%s step=%s (%s)", key[0], key[1],
                          state["time_of_day"])
        except Exception as exc:  # keep the collector resilient
            log.warning("poll/write failed: %s", exc)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
