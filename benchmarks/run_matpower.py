"""T-series: IEEE reference cases, pandapower vs REAL MATPOWER 8.1 (Octave).

Byte-identical case data on both sides: each case is loaded by MATPOWER's
own ``loadcase`` (the authoritative .m file), saved as a .mat struct and
imported into pandapower via ``converter.matpower.from_mpc`` — so the two
solvers consume the same numbers and the comparison isolates the SOLVERS,
not the data lineage. Gate: max |dVm| <= 1e-6 pu (docs/BENCHMARKS.md §6.1).

Run inside the benchmark venv (OCTAVE_EXECUTABLE set):
    .venv-bench\\Scripts\\python benchmarks\\run_matpower.py
"""
from __future__ import annotations

import csv
import json
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pandapower as pp
from pandapower.converter.matpower import from_mpc
from scipy.io import savemat

# MATPOWER 1-based columns (caseformat): bus VM=8, VA=9 -> numpy 7, 8
VM, VA = 7, 8

CASES = [
    ("T4", "case9", "Chow/WSCC 9-bus (textbook, not IEEE)"),
    ("T1", "case14", "IEEE 14-bus"),
    ("T2", "case_ieee30", "IEEE 30-bus (the true IEEE case, not case30)"),
    ("T3", "case118", "IEEE 118-bus"),
]

GATE_VM_PU = 1e-6
GATE_VA_DEG = 0.01     # pandapower's own PowerFactory validation gate


def run(out_dir: Path) -> int:
    from matpower import start_instance

    print("starting Octave/MATPOWER instance ...", flush=True)
    m = start_instance()
    mpopt = m.mpoption("verbose", 0, "out.all", 0)
    rows = []
    failed = []
    try:
        for bid, case, label in CASES:
            raw = m.loadcase(case)
            bus = np.asarray(raw["bus"], dtype=float)
            # Classic IEEE CDF conversions carry BASE_KV=0 (col 10). MATPOWER's
            # solver never uses BASE_KV (everything is pu), but pandapower's
            # from_mpc converts pu branches to physical ohms via Zbase=kV²/MVA
            # and back — a zero base divides by zero. Patch it to a uniform
            # dummy in the ONE struct BOTH solvers consume: the pu physics is
            # unchanged, the data stays byte-identical across the comparison.
            bus[bus[:, 9] == 0, 9] = 100.0
            branch = np.asarray(raw["branch"], dtype=float)
            # RATE_A=0 means "unlimited" in MATPOWER (runpf ignores ratings);
            # pandapower's from_ppc hits an indexing bug on zero ratings —
            # patch to MATPOWER's conventional 9900-MVA placeholder, again in
            # the one struct both solvers consume. No effect on the solution.
            branch[branch[:, 5] == 0, 5] = 9900.0
            mpc = {"version": "2",
                   "baseMVA": float(np.asarray(raw["baseMVA"]).squeeze()),
                   "bus": bus,
                   "gen": np.asarray(raw["gen"], dtype=float),
                   "branch": branch}

            # -- MATPOWER side ------------------------------------------- #
            t0 = time.perf_counter()
            res = m.runpf(mpc, mpopt)
            t_mp = (time.perf_counter() - t0) * 1000
            if not bool(res["success"]):
                failed.append(f"{bid} {case}: MATPOWER did not converge")
                continue
            mp_vm = np.asarray(res["bus"])[:, VM]
            mp_va = np.asarray(res["bus"])[:, VA]

            # -- pandapower side, SAME case struct ------------------------ #
            with tempfile.TemporaryDirectory() as td:
                mat = Path(td) / f"{case}.mat"
                savemat(str(mat), {"mpc": mpc})
                net = from_mpc(str(mat), f_hz=50)
            t0 = time.perf_counter()
            pp.runpp(net, calculate_voltage_angles=True, init="flat",
                     tolerance_mva=1e-8)
            t_pp = (time.perf_counter() - t0) * 1000
            pp_vm = net.res_bus.vm_pu.to_numpy()
            pp_va = net.res_bus.va_degree.to_numpy()

            # from_mpc keeps the mpc bus order -> positional comparison
            dvm = np.abs(pp_vm - mp_vm)
            dva = np.abs(pp_va - mp_va)
            ok = bool(dvm.max() <= GATE_VM_PU and dva.max() <= GATE_VA_DEG)
            rows.append({
                "id": bid, "case": case, "label": label,
                "n_bus": len(mp_vm),
                "max_dvm_pu": f"{dvm.max():.3e}",
                "mean_dvm_pu": f"{dvm.mean():.3e}",
                "max_dva_deg": f"{dva.max():.3e}",
                "t_matpower_ms": f"{t_mp:.1f}", "t_pandapower_ms": f"{t_pp:.1f}",
                "gate_vm_pu": GATE_VM_PU, "pass": ok,
            })
            if not ok:
                failed.append(f"{bid} {case}: max|dVm|={dvm.max():.3e} pu, "
                              f"max|dVa|={dva.max():.3e} deg")
            print(f"  {bid} {case:12s} n={len(mp_vm):4d}  "
                  f"max|dVm|={dvm.max():.3e} pu  max|dVa|={dva.max():.3e} deg  "
                  f"{'PASS' if ok else 'FAIL'}", flush=True)
    finally:
        m.exit()

    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "t_series.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    (out_dir / "t_series.json").write_text(json.dumps(rows, indent=2),
                                           encoding="utf-8")
    print(f"\nwrote {out_dir / 't_series.csv'}")
    if failed:
        print("FAILED gates:")
        for f_ in failed:
            print(" -", f_)
        return 1
    print(f"all {len(rows)} T-series benchmarks within the "
          f"{GATE_VM_PU} pu / {GATE_VA_DEG} deg gates")
    return 0


if __name__ == "__main__":
    sys.exit(run(Path(__file__).parent / "out"))
