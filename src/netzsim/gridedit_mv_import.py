"""Load a gridedit MV-layer export into netzsim inputs.

The gridedit editor's MS layer exports as ``format: "gridedit-mv"`` — one file
per Umspannwerk: buses (``uw | station | consumer | gen | junction``) with real
WGS84 coordinates, free-flow MV lines with explicit parameters and geometry.

netzsim models the full 110-kV feed explicitly: ext_grid → standard HV/MV
transformer (pandapower std unit, appended HV bus so the file's bus indices
stay unchanged) → the drawn MV net. MV/LV stations arrive as *lumped* LV
demand at their MV bus (exactly like the ding0 ``"mv"`` scope), so no LPG
households attach to them (``household: false``). Daily profiles are
synthesized per element type:

* station    — evening-peak residential shape (the lumped LV grid)
* consumer   — ``mall`` peaks early afternoon, ``charge`` (HPC park) late
               afternoon; both idle at a small floor overnight
* generation — ``pv`` midday bell, ``wind`` gusty deterministic shape,
               ``biogas`` steady base-load
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .grid_inputs import GridInputs, _daily

_HV_KV = 110.0
_HV_TRAFO_MVA = [25.0, 40.0, 63.0]      # pandapower std units 110/20 + 110/10


def _pick_hv_trafo(sn_mva: float, mv_kv: float) -> tuple[str, float]:
    """Smallest standard 110/x-kV unit covering the requested rating."""
    mv = 10 if abs(mv_kv - 10.0) < 1e-6 else 20
    for sn in _HV_TRAFO_MVA:
        if sn >= sn_mva - 1e-9:
            return f"{sn:g} MVA 110/{mv} kV", sn
    sn = _HV_TRAFO_MVA[-1]
    return f"{sn:g} MVA 110/{mv} kV", sn


def _pv_bell(steps: int, peak: float) -> list[float]:
    """Clear-sky midday bell (mirrors the ding0 importer's PV shape)."""
    peak_step, width = steps / 2.0, steps / 7.0
    return [round(peak * math.exp(-((t - peak_step) ** 2) / (2 * width ** 2))
                  if steps * 0.25 <= t <= steps * 0.75 else 0.0, 6)
            for t in range(steps)]


def _windy(steps: int, peak: float, phase: float) -> list[float]:
    """A deterministic 'gusty' wind shape: two incommensurate sinusoids around
    ~50 % capacity factor, clipped well inside [0, rated]."""
    out = []
    for t in range(steps):
        f = (0.50 + 0.32 * math.sin(2 * math.pi * 3 * t / steps + phase)
             + 0.12 * math.sin(2 * math.pi * 8 * t / steps + 2.0 * phase))
        out.append(round(peak * min(max(f, 0.05), 0.95), 6))
    return out


def convert_gridedit_mv(path: str | Path, *, name: str | None = None,
                        steps: int = 1440, power_factor: float = 0.95) -> GridInputs:
    g = json.loads(Path(path).read_text(encoding="utf-8"))
    if g.get("format") != "gridedit-mv":
        raise ValueError(f"{path}: not a gridedit-mv document")
    name = name or g.get("name", Path(path).stem)
    mv_kv = float(g.get("mv_kv", 20))
    slack = int(g.get("slack", 0))
    src_buses = g["buses"]
    tan_phi = math.tan(math.acos(max(min(power_factor, 1.0), 1e-3)))

    # readable bus names per element type (the editor exports node ids)
    counters: dict[str, int] = {}
    def _bn(kind: str, subtype: str | None) -> str:
        label = {"uw": "UW", "station": "Station", "junction": "K"}.get(kind)
        if label is None:
            label = {"mall": "EKZ", "charge": "HPC", "wind": "Wind",
                     "biogas": "Biogas", "pv": "PV"}.get(subtype or "", "Last")
        if kind == "uw":
            return label
        counters[label] = counters.get(label, 0) + 1
        return f"{label}_{counters[label]}"

    buses: list[dict[str, Any]] = []
    for b in src_buses:
        buses.append({"name": _bn(b.get("kind", "junction"), b.get("subtype")),
                      "vn_kv": mv_kv, "type": "b", "zone": "MV",
                      "in_service": True, "geo": b["geo"], "kind": None})

    line_specs: list[dict[str, Any]] = []
    for i, l in enumerate(g.get("lines", [])):
        line_specs.append({
            "name": l.get("type", f"L{i}"),
            "from_bus": l["from"], "to_bus": l["to"],
            "length_km": max(float(l["length_km"]), 1e-4),
            "r_ohm_per_km": float(l["r_ohm_per_km"]),
            "x_ohm_per_km": float(l["x_ohm_per_km"]),
            "c_nf_per_km": float(l.get("c_nf_per_km", 0.0)),
            "max_i_ka": float(l.get("max_i_ka", 0.3)) or 0.3,
            "parallel": 1, "geometry": l.get("geometry"), "in_service": True,
        })

    # loads: lumped stations + large consumers, never LPG households
    load_specs: list[dict[str, Any]] = []
    gen_specs: list[dict[str, Any]] = []
    n_station = n_consumer = 0
    for i, b in enumerate(src_buses):
        kind = b.get("kind")
        if b.get("p_kw"):
            p = float(b["p_kw"]) / 1000.0
            if kind == "station":
                shape = _daily(steps, base=0.4 * p, amp=0.6 * p,
                               peak_hour=19.0 + (n_station % 4) * 0.3)
                n_station += 1
            elif b.get("subtype") == "charge":
                shape = _daily(steps, base=0.25 * p, amp=0.75 * p,
                               peak_hour=17.5, floor=0.05 * p)
                n_consumer += 1
            else:                                    # mall & unknown consumers
                shape = _daily(steps, base=0.30 * p, amp=0.70 * p,
                               peak_hour=14.0, floor=0.05 * p)
                n_consumer += 1
            load_specs.append({"name": buses[i]["name"], "bus": i, "p_mw": shape,
                               "q_mvar": [round(v * tan_phi, 6) for v in shape],
                               "household": False})
        if b.get("gen_kw"):
            p = float(b["gen_kw"]) / 1000.0
            subtype = b.get("subtype", "pv")
            if subtype == "wind":
                shape = _windy(steps, p, phase=2.4 * len(gen_specs))
            elif subtype == "biogas":
                shape = [round(0.7 * p, 6)] * steps
            else:
                shape = _pv_bell(steps, p)
            gen_specs.append({"name": buses[i]["name"], "bus": i, "kind": subtype,
                              "p_mw": shape, "q_mvar": [0.0] * steps})

    # the 110-kV feed: appended HV bus -> std transformer -> the UW busbar
    uw = src_buses[slack]
    std_type, sn_mva = _pick_hv_trafo(float(uw.get("sn_mva", 40)), mv_kv)
    hv_bus = len(buses)
    buses.append({"name": "HV_Netz", "vn_kv": _HV_KV, "type": "b", "zone": "HV",
                  "in_service": True, "geo": uw["geo"], "kind": None})
    trafo_specs = [{"name": f"UW {std_type}", "hv_bus": hv_bus, "lv_bus": slack,
                    "std_type": std_type, "parallel": 1}]

    res = 1440 // steps
    load_doc = {"resolution_minutes": res, "steps": steps, "loads": load_specs}
    gen_doc = {"resolution_minutes": res, "steps": steps, "generation": gen_specs}
    lines_doc = {"lines": line_specs, "transformers": trafo_specs}
    sub_doc = {"resolution_minutes": res, "steps": steps,
               "substations": [{"name": "HV_Netz", "bus": hv_bus,
                                "vm_pu": [1.0] * steps,
                                "va_degree": [0.0] * steps}]}

    notes = [f"gridedit MV grid '{name}': {len(buses)} buses, {len(line_specs)} "
             f"lines, {n_station} lumped MV/LV stations, {n_consumer} large "
             f"consumers, {len(gen_specs)} generation units",
             f"110-kV feed via {std_type} ({sn_mva:g} MVA) at the Umspannwerk"]
    echeck = g.get("echeck")
    if echeck and not echeck.get("ok", True):
        fails = ", ".join(echeck.get("failures", [])) or "unknown"
        notes.append(f"E-Check FAIL: {fails}")
    # every drawn MV/LV station is a degenerate (lumped) ONS cell — Phase 5 of
    # the vertical integration will let a station reference a drawn LV grid
    cells = [{"id": buses[i]["name"], "name": buses[i]["name"],
              "buses": [], "lv_busbar": None, "mv_bus": i,
              "station_trafos": [], "lumped": True}
             for i, b in enumerate(src_buses) if b.get("kind") == "station"]
    return GridInputs(
        grid_structure={"name": name, "f_hz": 50.0, "buses": buses},
        lines=lines_doc, load=load_doc, generation=gen_doc,
        substation=sub_doc, notes=notes, cells=cells,
    )
