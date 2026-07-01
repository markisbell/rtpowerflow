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

# Standard German MV/LV distribution transformers (pandapower std_types), smallest
# first. The substation transformer is auto-sized to the smallest rating that
# covers ~1.25x the grid's daily peak load (capped at the largest standard unit).
MV_KV = 20.0
_TRAFO_LADDER = [
    (0.25, "0.25 MVA 20/0.4 kV"),
    (0.40, "0.4 MVA 20/0.4 kV"),
    (0.63, "0.63 MVA 20/0.4 kV"),
]


def _pick_trafo(peak_mw: float) -> tuple[str, float, int]:
    """Return (std_type, total sn_mva, parallel count). Pick the smallest single
    standard unit that covers ~1.25x the peak; if the peak exceeds the largest
    standard distribution transformer, use parallel units of it (a dense grid
    would in reality be fed by several substations)."""
    need = peak_mw * 1.25
    for sn, std in _TRAFO_LADDER:
        if sn >= need:
            return std, sn, 1
    sn, std = _TRAFO_LADDER[-1][0], _TRAFO_LADDER[-1][1]
    parallel = max(1, math.ceil(need / sn))
    return std, sn * parallel, parallel


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
            # normally-open ring ties (closed=false) are laid but out of service
            "in_service": bool(l.get("closed", True)),
        })

    tan_phi = math.tan(math.acos(max(min(power_factor, 1.0), 1e-3)))
    load_specs: list[dict[str, Any]] = []
    total_p = [0.0] * steps  # coincident total load per step → transformer sizing
    for i, ld in enumerate(g["loads"]):
        peak = float(ld["peak_mw"])
        p = _daily(steps, base=peak * 0.4, amp=peak * 0.6, peak_hour=19.0 + (i % 4) * 0.3)
        for t in range(steps):
            total_p[t] += p[t]
        load_specs.append({"name": f"load_{i}", "bus": ld["bus"], "p_mw": p,
                           "q_mvar": [round(v * tan_phi, 6) for v in p]})
    load_doc = {"resolution_minutes": 1440 // steps, "steps": steps, "loads": load_specs}
    gen_doc = {"resolution_minutes": 1440 // steps, "steps": steps, "generation": []}

    # Model the MV/LV substation transformer explicitly: the existing 0.4 kV
    # busbar (the grid's original slack) becomes the transformer's LV side, and a
    # new MV busbar (appended so existing bus indices are unchanged) is fed by the
    # slack. The transformer is auto-sized to the coincident daily peak load.
    lv_busbar = int(g["slack_bus"])
    peak_load_mw = max(total_p) if total_p else 0.0
    std_type, sn_mva, parallel = _pick_trafo(peak_load_mw)
    station_geo = g.get("station") or g["buses"][lv_busbar]["geo"]
    mv_bus = len(buses)
    buses.append({"name": "MV_station", "vn_kv": MV_KV, "type": "b", "zone": "MV",
                  "in_service": True, "geo": station_geo, "kind": None})
    trafo_specs = [{"name": "MV/LV substation", "hv_bus": mv_bus, "lv_bus": lv_busbar,
                    "std_type": std_type, "parallel": parallel}]
    lines_doc = {"lines": line_specs, "transformers": trafo_specs}

    sub_doc = {"resolution_minutes": 1440 // steps, "steps": steps,
               "substations": [{"name": "MV_station", "bus": mv_bus,
                                "vm_pu": [1.0] * steps, "va_degree": [0.0] * steps}]}

    unit = f"{parallel}x {std_type}" if parallel > 1 else std_type
    notes = [f"OSM-routed LV grid '{name}': {len(buses)} buses, {len(line_specs)} "
             f"lines, {len(load_specs)} loads; cables follow real streets",
             f"MV/LV substation transformer {unit} = {sn_mva * 1000:.0f} kVA "
             f"(auto-sized to {peak_load_mw * 1000:.0f} kW coincident peak)"]
    return GridInputs(
        grid_structure={"name": name, "f_hz": 50.0, "buses": buses},
        lines=lines_doc, load=load_doc, generation=gen_doc,
        substation=sub_doc, notes=notes,
    )
