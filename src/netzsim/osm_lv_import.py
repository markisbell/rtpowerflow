"""Load an OSM-routed LV grid into netzsim inputs.

These grids are built offline by ``scripts/build_lv_osm_grids.py`` from real
OpenStreetMap data: every load sits at a building footprint, the cable backbone
follows the street network (sized by downstream load), and each line carries a
``geometry`` polyline so the live map draws the cable along the actual roads.
The file is a small JSON (buses + lines + loads + slack); see the build script.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .grid_inputs import GridInputs, _daily


def convert_osm_lv(path: str | Path, *, name: str | None = None,
                   steps: int = 1440, power_factor: float = 0.95) -> GridInputs:
    g = json.loads(Path(path).read_text())
    name = name or g.get("name", Path(path).stem)

    buses = [{"name": b["name"], "vn_kv": b["vn_kv"], "type": "b", "zone": "LV",
              "in_service": True, "geo": b["geo"],
              "kind": "cabinet" if b.get("role") == "cabinet" else None}
             for b in g["buses"]]

    line_specs: list[dict[str, Any]] = []
    for i, l in enumerate(g["lines"]):
        line_specs.append({
            "name": l.get("name", f"L{i}"),
            "from_bus": l["from"], "to_bus": l["to"], "length_km": l["length_km"],
            "r_ohm_per_km": l["r_ohm_per_km"], "x_ohm_per_km": l["x_ohm_per_km"],
            "c_nf_per_km": l.get("c_nf_per_km", 0.0), "max_i_ka": l["max_i_ka"],
            "parallel": int(l.get("parallel", 1)), "geometry": l.get("geometry"),
        })
    lines_doc = {"lines": line_specs, "transformers": []}

    tan_phi = math.tan(math.acos(max(min(power_factor, 1.0), 1e-3)))
    load_specs: list[dict[str, Any]] = []
    for i, ld in enumerate(g["loads"]):
        peak = float(ld["peak_mw"])
        p = _daily(steps, base=peak * 0.4, amp=peak * 0.6, peak_hour=19.0 + (i % 4) * 0.3)
        load_specs.append({"name": f"load_{i}", "bus": ld["bus"], "p_mw": p,
                           "q_mvar": [round(v * tan_phi, 6) for v in p]})
    load_doc = {"resolution_minutes": 1440 // steps, "steps": steps, "loads": load_specs}
    gen_doc = {"resolution_minutes": 1440 // steps, "steps": steps, "generation": []}

    slack_name = g["buses"][g["slack_bus"]]["name"]
    sub_doc = {"resolution_minutes": 1440 // steps, "steps": steps,
               "substations": [{"name": slack_name, "bus": g["slack_bus"],
                                "vm_pu": [1.0] * steps, "va_degree": [0.0] * steps}]}

    notes = [f"OSM-routed LV grid '{name}': {len(buses)} buses, {len(line_specs)} "
             f"lines, {len(load_specs)} loads; cables follow real streets"]
    return GridInputs(
        grid_structure={"name": name, "f_hz": 50.0, "buses": buses},
        lines=lines_doc, load=load_doc, generation=gen_doc,
        substation=sub_doc, notes=notes,
    )
