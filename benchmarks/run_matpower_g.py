"""G-series MATPOWER leg: a fixture's full day as 1440 runpf solves.

The pandapower net (trafo-aligned, plan §5.2) is exported once via
``pandapower.converter.matpower.to_mpc``; the per-step bus injections
(loads + sgens aggregated per bus, netzsim's profile arrays) are pushed to
Octave as matrices and the WHOLE day loops INSIDE Octave — one oct2py call
instead of 1440 round-trips. Compared against the aligned netzsim
reference in pu (MATPOWER VM is pu on BASE_KV = pandapower vn_kv).

Usage:
    .venv-bench\\Scripts\\python benchmarks\\run_matpower_g.py g1_lv_rural
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from netzsim.data_loader import load_inputs   # noqa: E402
from netzsim.simulator import Simulator       # noqa: E402

GATE_DV_PU = 1e-5
FAIL_DV_PU = 1e-4


def compare(fixture: str, steps: int) -> int:
    from pandapower.converter.matpower import to_mpc
    from scipy.io import loadmat
    from matpower import start_instance

    fdir = ROOT / "benchmarks" / "fixtures" / fixture
    sim = Simulator(load_inputs(fdir))
    net = sim.net
    if len(net.trafo):
        net.trafo.pfe_kw = 0.0        # plan §5.2 alignment (branch has no shunt)
        net.trafo.i0_percent = 0.0

    # -- one mpc from the aligned net; bus lookup pandapower -> ppc row ----- #
    with tempfile.TemporaryDirectory() as td:
        mat = Path(td) / "case.mat"
        to_mpc(net, filename=str(mat), init="flat")
        mpc_raw = loadmat(str(mat), simplify_cells=True)["mpc"]
    lookup = net._pd2ppc_lookups["bus"]           # pandapower bus -> ppc row
    # simplify_cells collapses single-row matrices to 1-D — Octave then sees
    # a vector where runpf needs a matrix; force 2-D on all three tables
    mpc = {"version": "2", "baseMVA": float(mpc_raw["baseMVA"]),
           "bus": np.atleast_2d(np.asarray(mpc_raw["bus"], dtype=float)),
           "gen": np.atleast_2d(np.asarray(mpc_raw["gen"], dtype=float)),
           "branch": np.atleast_2d(np.asarray(mpc_raw["branch"], dtype=float))}
    # The Dyn vector-group shift (150 deg from the std types) is a pure
    # angle rotation of the LV side — magnitude-neutral in a radial net —
    # but it puts Newton's FLAT start 150 deg away from the solution and
    # the solver walks into a wrong basin (observed: 1 MW phantom losses
    # at zero load). Plan §5.2: zero the shift, compare magnitudes.
    mpc["branch"][:, 9] = 0.0
    n_row = mpc["bus"].shape[0]

    # -- per-step PD/QD matrices (MW/MVAr), loads minus sgens per bus ------- #
    pd = np.zeros((n_row, steps))
    qd = np.zeros((n_row, steps))
    for i, li in enumerate(sim.prof.load_idx):
        row = lookup[int(net.load.at[li, "bus"])]
        pd[row] += sim.prof.load_p[i][:steps]
        qd[row] += sim.prof.load_q[i][:steps]
    for i, si in enumerate(sim.prof.sgen_idx):
        row = lookup[int(net.sgen.at[si, "bus"])]
        pd[row] -= sim.prof.sgen_p[i][:steps]
        qd[row] -= sim.prof.sgen_q[i][:steps]

    # -- the whole day inside ONE Octave session ----------------------------- #
    print("starting Octave/MATPOWER ...", flush=True)
    m = start_instance()
    try:
        m.push("mpc", mpc)
        m.push("PD", pd)
        m.push("QD", qd)
        t0 = time.perf_counter()
        m.eval("""
            mpopt = mpoption('verbose', 0, 'out.all', 0);
            T = size(PD, 2);
            VM = zeros(size(mpc.bus, 1), T);
            OK = zeros(1, T);
            for t = 1:T
                mpc.bus(:, 3) = PD(:, t);
                mpc.bus(:, 4) = QD(:, t);
                r = runpf(mpc, mpopt);
                OK(t) = r.success;
                VM(:, t) = r.bus(:, 8);
            end
        """)
        vm_mp = np.asarray(m.pull("VM"))
        ok = np.asarray(m.pull("OK")).ravel()
        wall = time.perf_counter() - t0
    finally:
        m.exit()
    if not ok.all():
        raise SystemExit(f"MATPOWER did not converge at steps "
                         f"{np.where(ok == 0)[0][:5].tolist()}")

    # -- compare against the aligned netzsim reference ----------------------- #
    ref = np.load(ROOT / "benchmarks" / "out" / f"{fixture}_netzsim_aligned.npz")
    ref_vm = ref["vm_pu"][:, :steps]
    n_bus = ref_vm.shape[0]
    vm_mp_by_bus = np.empty_like(ref_vm)
    for b in range(n_bus):
        vm_mp_by_bus[b] = vm_mp[lookup[b], :steps]

    dv = np.abs(vm_mp_by_bus - ref_vm)
    max_dv = float(np.nanmax(dv))
    result = {
        "fixture": fixture, "engine": "matpower-8.1-octave", "steps": steps,
        "trafo_model": "aligned (magnetizing zeroed, plan §5.2)",
        "max_dv_pu": max_dv, "mean_dv_pu": float(np.nanmean(dv)),
        "worst_bus": int(np.unravel_index(np.nanargmax(dv), dv.shape)[0]),
        "worst_step": int(np.unravel_index(np.nanargmax(dv), dv.shape)[1]),
        "wall_s": round(wall, 1),
        "gate_dv_pu": GATE_DV_PU, "pass": bool(max_dv <= GATE_DV_PU),
    }
    out = ROOT / "benchmarks" / "out"
    (out / f"{fixture}_compare_matpower.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8")
    np.savez_compressed(out / f"{fixture}_matpower.npz", vm_pu=vm_mp_by_bus)

    verdict = ("PASS" if result["pass"]
               else "FAIL" if max_dv > FAIL_DV_PU else "SOFT-MISS")
    print(f"{fixture} vs MATPOWER 8.1: {steps} runpf in {wall:.1f} s\n"
          f"  max|dV| = {max_dv:.3e} pu at bus {result['worst_bus']} "
          f"step {result['worst_step']}\n"
          f"  mean|dV| = {result['mean_dv_pu']:.3e} pu\n"
          f"  gate: {GATE_DV_PU} pu -> {verdict}")
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("fixture")
    ap.add_argument("--steps", type=int, default=1440)
    a = ap.parse_args()
    sys.exit(compare(a.fixture, a.steps))
