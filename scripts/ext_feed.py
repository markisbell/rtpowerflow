"""Demo feeder client for EXTERNAL NODES (docs/EXTERNAL_NODES.md, phase 3).

Pushes a live value into a netzsim external node once per interval — the
reference implementation of the mailbox contract (``PUT /ext/{id}/value``,
latest wins, the engine samples non-blocking). Two sources:

  sine   synthetic PV half-wave (no hardware needed) — the teaching default:
             p_kw = -peak * max(0, sin(2*pi * t / period))
  pi     a real rooftop-PV feed from an InfluxDB 1.8 (the Raspberry Pi
         setup: db ``griddata``, measurement ``grid_data``, field
         ``active_power_a`` in watts, positive = PV generation). The watt
         value is scaled and pushed as FEED-IN (negative, signed P).

The node is resolved per ``--bus``: an existing external node at that bus is
reused, otherwise one is created (``--hold-s/--on-timeout/--p-max-kw``).
Stopping the script is part of the demo: after ``hold_s`` the node turns
stale and the UI shows the policy consequence (hold last value / drop to 0).

Examples:
    python scripts/ext_feed.py --bus 3                       # synthetic PV
    python scripts/ext_feed.py --bus 3 --source pi --scale 25
        # the 600-W Pi plant scaled up 25x -> a ~15-kW rooftop system

Deliberately stdlib-only (urllib) so it doubles as copy-paste starting point
for students' own feeder clients (EMS, lab bench, co-simulation).
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


def _http(method: str, url: str, body: dict | None = None, timeout: float = 5.0) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def ensure_node(base: str, args: argparse.Namespace) -> int:
    """Reuse the external node at --bus or create one there."""
    for x in _http("GET", f"{base}/ext")["ext_nodes"]:
        if x["bus"] == args.bus:
            print(f"using existing external node {x['id']} at bus {args.bus}")
            return int(x["id"])
    x = _http("POST", f"{base}/ext", {
        "bus": args.bus, "name": args.name,
        "hold_s": args.hold_s, "on_timeout": args.on_timeout,
        "p_max_kw": args.p_max_kw,
    })
    print(f"created external node {x['id']} at bus {args.bus} "
          f"(hold {args.hold_s:g} s, on timeout {args.on_timeout}, "
          f"bound +-{args.p_max_kw:g} kW)")
    return int(x["id"])


def read_pi_watts(args: argparse.Namespace) -> float:
    """last(active_power_a) from the InfluxDB 1.8 (positive = PV watts)."""
    q = urllib.parse.urlencode({
        "db": args.influx_db,
        "q": f"SELECT last({args.influx_field}) FROM {args.influx_measurement}",
    })
    doc = _http("GET", f"{args.influx_url}/query?{q}")
    series = doc["results"][0].get("series")
    if not series:
        raise ValueError("no data in the InfluxDB series")
    return float(series[0]["values"][0][1])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--netzsim", default="http://localhost:8000",
                    help="netzsim base URL (default %(default)s)")
    ap.add_argument("--bus", type=int, required=True,
                    help="bus to feed (node is created there if missing)")
    ap.add_argument("--source", choices=("sine", "pi"), default="sine",
                    help="value source (default %(default)s)")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="seconds between pushes (default %(default)s)")
    ap.add_argument("--scale", type=float, default=1.0,
                    help="multiply the source value (pi: scale the 600-W plant)")
    # node creation parameters (ignored when the bus already has a node)
    ap.add_argument("--name", default=None, help="node name (default EXT_<bus>)")
    ap.add_argument("--hold-s", type=float, default=30.0)
    ap.add_argument("--on-timeout", choices=("hold", "zero"), default="hold")
    ap.add_argument("--p-max-kw", type=float, default=50.0)
    # sine source
    ap.add_argument("--peak-kw", type=float, default=5.0,
                    help="sine: feed-in peak in kW (default %(default)s)")
    ap.add_argument("--period", type=float, default=120.0,
                    help="sine: full period in seconds (default %(default)s)")
    # pi source (InfluxDB 1.8)
    ap.add_argument("--influx-url", default="http://192.168.178.23:8086")
    ap.add_argument("--influx-db", default="griddata")
    ap.add_argument("--influx-measurement", default="grid_data")
    ap.add_argument("--influx-field", default="active_power_a")
    args = ap.parse_args()
    base = args.netzsim.rstrip("/")

    try:
        eid = ensure_node(base, args)
    except urllib.error.URLError as exc:
        print(f"cannot reach netzsim at {base}: {exc}", file=sys.stderr)
        return 1

    t0 = time.monotonic()
    print(f"feeding node {eid} from source '{args.source}' every "
          f"{args.interval:g} s - Ctrl+C to stop (the node then turns stale)")
    try:
        while True:
            try:
                if args.source == "pi":
                    watts = read_pi_watts(args)
                    p_kw = -(watts / 1000.0) * args.scale   # PV watts -> feed-in
                else:
                    t = time.monotonic() - t0
                    p_kw = -args.peak_kw * max(0.0, math.sin(2 * math.pi * t / args.period))
                    p_kw *= args.scale
                x = _http("PUT", f"{base}/ext/{eid}/value", {"p_kw": round(p_kw, 3)})
                print(f"  -> {x['p_kw']:8.3f} kW  (applied, stale={x['stale']})",
                      flush=True)
            except Exception as exc:  # noqa: BLE001 — keep feeding; a gap = stale demo
                print(f"  !! push failed ({exc}) - retrying", file=sys.stderr)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nstopped - the node goes stale after its hold_s now")
        return 0


if __name__ == "__main__":
    sys.exit(main())
