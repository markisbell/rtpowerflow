"""One-command benchmark run (docs/BENCHMARKS.md §7.1).

Runs the whole validation — T-series (pandapower vs MATPOWER), G-series
(netzsim vs OpenDSS daily + vs MATPOWER, aligned gate runs + full-model
supplements) — then renders the figures and regenerates the public report
under docs/benchmarks/. Idempotent.

    .venv-bench\\Scripts\\python benchmarks\\run_all.py [--skip-matpower]
        [--steps 1440] [--fixtures g1_lv_rural g2_lv_suburban g3_mv_district]
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
FIXTURES = ["g1_lv_rural", "g2_lv_suburban", "g3_mv_district"]


def sh(script: str, *args: str) -> None:
    cmd = [sys.executable, str(HERE / script), *args]
    print(f"\n=== {script} {' '.join(args)}", flush=True)
    subprocess.run(cmd, check=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--steps", type=int, default=1440)
    ap.add_argument("--skip-matpower", action="store_true",
                    help="no Octave available: OpenDSS legs only")
    ap.add_argument("--fixtures", nargs="+", default=FIXTURES)
    a = ap.parse_args()
    steps = str(a.steps)

    sh("check_env.py")
    if not a.skip_matpower:
        sh("run_matpower.py")                          # T-series
    for f in a.fixtures:
        sh("netzsim_runner.py", f, "--steps", steps, "--align-trafo")
        sh("netzsim_runner.py", f, "--steps", steps)   # full-model reference
        sh("to_dss.py", f, "--align-trafo")
        sh("to_dss.py", f)
        sh("run_opendss.py", f, "--steps", steps, "--aligned")
        sh("run_opendss.py", f, "--steps", steps)      # supplementary
        if not a.skip_matpower:
            sh("run_matpower_g.py", f, "--steps", steps)
    sh("plots.py")
    sh("make_report.py")
    print("\nbenchmark run complete — report: docs/benchmarks/README.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
