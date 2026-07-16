"""netzsim reference runner: a frozen fixture through the REAL pipeline.

Loads a fixture (the 5 data_dir JSONs) via ``data_loader.load_inputs`` and
steps a ``Simulator`` offline over the day — the exact production code path
(std-type resolution, profile packing, warm start, derived quantities), no
REST server. Writes the per-step reference arrays the comparators consume:

    benchmarks/out/<fixture>_netzsim.npz
        vm_pu   [n_bus,  steps]   bus voltage magnitude (pu)
        i_ka    [n_line, steps]   line current (kA), netzsim's i_ka convention
        loading [n_line, steps]   line loading (%)
        p_slack [steps]           total ext-grid P (MW)
        + bus_names/vn_kv/line_names/from_bus/to_bus, solve_ms stats

Usage:
    .venv-bench\\Scripts\\python benchmarks\\netzsim_runner.py g1_lv_rural [--steps 1440]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from netzsim.data_loader import load_inputs   # noqa: E402
from netzsim.simulator import Simulator       # noqa: E402


def run(fixture: str, steps: int) -> Path:
    fdir = ROOT / "benchmarks" / "fixtures" / fixture
    if not fdir.exists():
        raise SystemExit(f"unknown fixture '{fixture}' — run make_fixtures.py")
    data = load_inputs(fdir)
    sim = Simulator(data, warm_start=True)
    n_bus, n_line = len(sim.net.bus), len(sim.net.line)

    vm = np.full((n_bus, steps), np.nan)
    ika = np.full((n_line, steps), np.nan)
    loading = np.full((n_line, steps), np.nan)
    p_slack = np.full(steps, np.nan)
    solve_ms = np.full(steps, np.nan)

    t0 = time.perf_counter()
    for t in range(steps):
        res = sim.run_step(t)
        if not res.converged:
            raise SystemExit(f"{fixture}: step {t} did not converge")
        for b in res.buses:
            vm[b["index"], t] = b["vm_pu"]
        for ln in res.lines:
            ika[ln["index"], t] = ln["i_ka"]
            loading[ln["index"], t] = ln["loading_percent"]
        p_slack[t] = sum(e["p_mw"] for e in res.ext_grids)
        solve_ms[t] = res.solve_ms
    wall = time.perf_counter() - t0

    out = ROOT / "benchmarks" / "out"
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{fixture}_netzsim.npz"
    np.savez_compressed(
        path, vm_pu=vm, i_ka=ika, loading=loading, p_slack=p_slack,
        solve_ms=solve_ms,
        bus_names=np.array([str(n) for n in sim.net.bus.name]),
        vn_kv=sim.net.bus.vn_kv.to_numpy(),
        line_names=np.array([str(n) for n in sim.net.line.name]),
        from_bus=sim.net.line.from_bus.to_numpy(),
        to_bus=sim.net.line.to_bus.to_numpy(),
    )
    print(f"{fixture}: {steps} steps, {n_bus} buses, {n_line} lines — "
          f"{wall:.1f} s wall, mean solve {np.nanmean(solve_ms):.1f} ms, "
          f"vm range [{np.nanmin(vm):.4f}, {np.nanmax(vm):.4f}] pu "
          f"-> {path.name}")
    return path


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("fixture")
    ap.add_argument("--steps", type=int, default=1440)
    a = ap.parse_args()
    run(a.fixture, a.steps)
