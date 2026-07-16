"""Freeze the G-series benchmark fixtures (docs/BENCHMARKS.md §6.4).

Each fixture is the FULLY RESOLVED simulation input as the 5 netzsim data_dir
JSONs — grid structure and lines straight from the catalog import, loads and
generation dumped from the *effective* profile arrays of a Simulator that was
configured exactly like the published teaching scenario (seeded LPG loads +
the scenario's runtime DER ops baked in). This removes every source of
nondeterminism: an external person reruns bit-identical inputs, and the
benchmark exercises netzsim's real pipeline (data_loader -> network_builder
-> Simulator) on load.

Run ONCE from the repo root (both venvs work; pandapower version must match):
    .venv-bench\\Scripts\\python benchmarks\\make_fixtures.py
"""
from __future__ import annotations

import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from netzsim.config import settings                    # noqa: E402
from netzsim.api.runtime import runtime                # noqa: E402
from netzsim.grid_catalog import GridCatalog           # noqa: E402
from netzsim.loadgen import LoadLibrary                # noqa: E402
from netzsim.data_loader import input_data_from_dicts  # noqa: E402
from netzsim.simulator import Simulator                # noqa: E402

FIXTURES = ROOT / "benchmarks" / "fixtures"
SCENARIOS = ROOT / "data" / "scenarios"

# fixture id -> (scenario file, human label)
SOURCES = {
    "g1_lv_rural": ("1-bauernhof-pv-75-kw-spannungs-berh-hung.json",
                    "scenario 1: rural LV feeder, 75-kWp farm PV at bus 24"),
    "g2_lv_suburban": ("2-feierabend-laden-strang-berlast-nh-sicherung-l-st-aus.json",
                       "scenario 2: suburban LV feeder, 12 staggered 11-kW EVs"),
    "g3_mv_district": ("4-feierabend-im-bezirk-engpass-unsichtbar-f-r-jede-zelle.json",
                       "scenario 4: MV district, 42 aggregate 200-kW wallbox blocks"),
}


def _round(arr) -> list:
    return [round(float(v), 6) for v in arr]


def build_fixture(fid: str, scenario_file: str, label: str) -> dict:
    from netzsim.api.grids import (LoadgenPolicy, _assigned_load_doc,
                                   _grid_character, _pv_gen_doc)

    doc = json.loads((SCENARIOS / scenario_file).read_text(encoding="utf-8"))
    gid = doc["grid_id"]
    g = runtime.catalog.get_inputs(gid, steps=settings.steps_per_day)

    # the scenario's seeded load generation (deterministic per seed)
    gen_doc = g.generation
    policy = LoadgenPolicy(**doc["loadgen"]) if doc.get("loadgen") else None
    if policy is not None:
        assigned = _assigned_load_doc(g, policy, _grid_character(gid))
        load_doc = {k: assigned[k] for k in ("resolution_minutes", "steps", "loads")}
        pv = _pv_gen_doc(g, policy)
        if pv is not None:
            gen_doc = pv
    else:
        load_doc = g.load

    data = input_data_from_dicts(g.grid_structure, g.lines, load_doc,
                                 gen_doc, g.substation, cells=g.cells)
    sim = Simulator(data)
    for op in doc.get("der_ops", []):
        sim.apply_der_op(op)

    # -- dump the EFFECTIVE profiles back into the 5-JSON data_dir format -- #
    out = FIXTURES / fid
    out.mkdir(parents=True, exist_ok=True)
    steps = sim.steps_per_day

    (out / "grid_structure.json").write_text(
        json.dumps(g.grid_structure, indent=1), encoding="utf-8")
    (out / "lines.json").write_text(
        json.dumps(g.lines, indent=1), encoding="utf-8")
    (out / "substation.json").write_text(
        json.dumps(g.substation, indent=1), encoding="utf-8")

    loads = []
    for i, li in enumerate(sim.prof.load_idx):
        loads.append({"name": str(sim.net.load.at[li, "name"]),
                      "bus": int(sim.net.load.at[li, "bus"]),
                      "p_mw": _round(sim.prof.load_p[i]),
                      "q_mvar": _round(sim.prof.load_q[i])})
    (out / "load.json").write_text(json.dumps(
        {"resolution_minutes": 1, "steps": steps, "loads": loads}),
        encoding="utf-8")

    gens = []
    for i, si in enumerate(sim.prof.sgen_idx):
        gens.append({"name": str(sim.net.sgen.at[si, "name"]),
                     "bus": int(sim.net.sgen.at[si, "bus"]),
                     "p_mw": _round(sim.prof.sgen_p[i]),
                     "q_mvar": _round(sim.prof.sgen_q[i])})
    (out / "generation.json").write_text(json.dumps(
        {"resolution_minutes": 1, "steps": steps, "generation": gens}),
        encoding="utf-8")

    # solve-smoke + noon snapshot so the manifest carries a physics anchor
    res = sim.run_step(720)
    assert res.converged, f"{fid}: fixture does not converge at noon"
    hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()[:16]
              for p in sorted(out.glob("*.json"))}
    return {"id": fid, "label": label, "grid_id": gid,
            "scenario": scenario_file, "loadgen": doc.get("loadgen"),
            "der_ops_count": len(doc.get("der_ops", [])),
            "n_bus": len(sim.net.bus), "n_load": len(loads),
            "n_sgen": len(gens), "steps": steps,
            "noon_vm_pu_min": round(min(b["vm_pu"] for b in res.buses), 6),
            "sha256_16": hashes}


def main() -> int:
    runtime.catalog = GridCatalog(ding0_dir=settings.ding0_dir,
                                  library_manifest=settings.grid_library,
                                  user_dir=settings.user_grids_dir)
    runtime.library = LoadLibrary(settings.lpg_library_dir)

    manifest = {"created": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "generator": "benchmarks/make_fixtures.py",
                "fixtures": []}
    for fid, (scen, label) in SOURCES.items():
        print(f"building {fid} ...", flush=True)
        info = build_fixture(fid, scen, label)
        manifest["fixtures"].append(info)
        print(f"  {info['n_bus']} buses, {info['n_load']} loads, "
              f"{info['n_sgen']} sgens, noon vm_min={info['noon_vm_pu_min']}",
              flush=True)
    (FIXTURES / "MANIFEST.json").write_text(json.dumps(manifest, indent=2),
                                            encoding="utf-8")
    print(f"wrote {FIXTURES / 'MANIFEST.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
