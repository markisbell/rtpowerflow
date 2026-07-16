"""G-series: the exported OpenDSS model over the day, compared to netzsim.

Drives ``benchmarks/out/dss/<fixture>/circuit.dss`` (from ``to_dss.py``) in
OpenDSS daily mode — one Solve per 1-minute step — and compares every step
against the netzsim reference arrays (``netzsim_runner.py``):

- bus voltage as RAW VOLTS L-L (the base-proof comparison; the pu error is
  derived over netzsim's per-bus vn_kv), gate 1e-5 pu / fail 1e-4
- line current in A (terminal 1, phase 1; balanced), gate 0.1 A (LV)

Engines: ``--engine altdss`` (OpenDSSDirect.py, default) — the
py-dss-interface (official EPRI) leg is wired behind ``--engine epri`` but
its ``text()`` hangs in non-interactive shells (Phase-0 finding).

Usage:
    .venv-bench\\Scripts\\python benchmarks\\run_opendss.py g1_lv_rural
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]

GATE_DV_PU = 1e-5
FAIL_DV_PU = 1e-4
GATE_DI_A = 0.1


def run_altdss(dss_dir: Path, steps: int):
    from opendssdirect import dss

    mapping = json.loads((dss_dir / "mapping.json").read_text())
    dss.Text.Command("Clear")
    dss.Text.Command(f'Compile "{(dss_dir / "circuit.dss").as_posix()}"')
    dss.Text.Command("Set Mode=daily StepSize=1m Number=1")

    bus_ids = mapping["buses"]                 # name -> pandapower index
    line_ids = mapping["lines"]
    n_bus = len(bus_ids)
    n_line = len(line_ids)
    vll = np.full((n_bus, steps), np.nan)      # volts L-L
    i_a = np.full((n_line, steps), np.nan)     # ampere

    t0 = time.perf_counter()
    for t in range(steps):
        dss.Solution.Solve()
        if not dss.Solution.Converged():
            raise SystemExit(f"OpenDSS did not converge at step {t}")
        for name, idx in bus_ids.items():
            dss.Circuit.SetActiveBus(name)
            mags = dss.Bus.VMagAngle()[::2]    # volts L-N per node
            vll[idx, t] = mags[0] * np.sqrt(3.0)
        k = dss.Lines.First()
        while k > 0:
            idx = line_ids.get(dss.Lines.Name().lower())
            if idx is not None:
                cur = dss.CktElement.CurrentsMagAng()
                i_a[idx, t] = cur[0]           # terminal 1, phase 1
            k = dss.Lines.Next()
    wall = time.perf_counter() - t0
    return vll, i_a, wall


def compare(fixture: str, steps: int, engine: str,
            aligned: bool = False) -> int:
    out = ROOT / "benchmarks" / "out"
    suffix = "_aligned" if aligned else ""
    dss_dir = out / "dss" / (fixture + suffix)
    ref = np.load(out / f"{fixture}_netzsim{suffix}.npz")
    if engine != "altdss":
        raise SystemExit("only the altdss engine leg is implemented so far "
                         "(the EPRI text() hang is a documented open item)")
    vll, i_a, wall = run_altdss(dss_dir, steps)

    vn_kv = ref["vn_kv"]                               # netzsim bus base, LL
    ref_vll = ref["vm_pu"][:, :steps] * vn_kv[:, None] * 1000.0
    ref_ia = ref["i_ka"][:, :steps] * 1000.0

    dv_v = np.abs(vll - ref_vll)
    dv_pu = dv_v / (vn_kv[:, None] * 1000.0)
    # netzsim reports NaN current on out-of-service lines; mask both sides
    mask = ~np.isnan(ref_ia)
    di_a = np.abs(np.where(mask, i_a, np.nan) - ref_ia)

    max_dv_pu = float(np.nanmax(dv_pu))
    max_di = float(np.nanmax(di_a))
    result = {
        "fixture": fixture, "engine": engine, "steps": steps,
        "trafo_model": "aligned (magnetizing zeroed, plan §5.2)"
                       if aligned else "full mapping",
        "max_dv_pu": max_dv_pu, "mean_dv_pu": float(np.nanmean(dv_pu)),
        "max_dv_volt": float(np.nanmax(dv_v)),
        "max_di_a": max_di, "mean_di_a": float(np.nanmean(di_a)),
        "worst_bus": int(np.unravel_index(np.nanargmax(dv_pu), dv_pu.shape)[0]),
        "worst_step": int(np.unravel_index(np.nanargmax(dv_pu), dv_pu.shape)[1]),
        "wall_s": round(wall, 1),
        "gate_dv_pu": GATE_DV_PU, "fail_dv_pu": FAIL_DV_PU,
        "gate_di_a": GATE_DI_A,
        "pass": bool(max_dv_pu <= GATE_DV_PU and max_di <= GATE_DI_A),
    }
    np.savez_compressed(out / f"{fixture}_opendss_{engine}{suffix}.npz",
                        vll_v=vll, i_a=i_a)
    (out / f"{fixture}_compare_{engine}{suffix}.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8")

    verdict = ("PASS" if result["pass"]
               else "FAIL" if max_dv_pu > FAIL_DV_PU else "SOFT-MISS")
    print(f"{fixture}{suffix} vs OpenDSS ({engine}): {steps} steps in {wall:.1f} s\n"
          f"  max|dV| = {max_dv_pu:.3e} pu ({result['max_dv_volt']:.4f} V) "
          f"at bus {result['worst_bus']} step {result['worst_step']}\n"
          f"  mean|dV| = {result['mean_dv_pu']:.3e} pu\n"
          f"  max|dI| = {max_di:.4f} A   mean|dI| = {result['mean_di_a']:.5f} A\n"
          f"  gates: {GATE_DV_PU} pu / {GATE_DI_A} A -> {verdict}")
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("fixture")
    ap.add_argument("--steps", type=int, default=1440)
    ap.add_argument("--engine", choices=("altdss", "epri"), default="altdss")
    ap.add_argument("--aligned", action="store_true",
                    help="compare the trafo-aligned pair (plan §5.2 gate run)")
    a = ap.parse_args()
    sys.exit(compare(a.fixture, a.steps, a.engine, a.aligned))
