"""Reference feeders (IEEE / CIGRE / Kerber) -> :class:`GridInputs`.

pandapower ships the classic distribution reference networks as ready-built
nets; this module converts them into netzsim's five-document form so they
appear in the ``/grids`` catalog next to the ding0 library and user grids.

The conversion is **exact** where the source is: buses, lines and transformers
carry their explicit physical parameters (``create_*_from_parameters`` path in
the network builder — no snapping to std types), open switches are honored,
closed bus-bus switches are merged, and tap positions are folded into the
voltage ratio. Only the TIME dimension is added: every static case load
becomes a smooth daily profile whose EVENING PEAK (19:00) equals the case's
nominal value — so a solve at the peak step reproduces the published case
exactly (pinned in ``tests/test_reference_import.py``).

LV loads are flagged ``household: true``: the LPG load generator, EV/PV
seeding and the estimator's SLP basis work on the reference feeders exactly
like on the library grids. The IEEE European LV Test Feeder's asymmetric
loads are folded balanced (netzsim solves the balanced single-phase
equivalent; the per-phase spread is noted, not modelled).
"""
from __future__ import annotations

from typing import Any, Callable

import math

from .grid_inputs import GridInputs, _daily

# Evening peak hour of the synthetic daily shape. 19:00 lands exactly on a
# step for the common rasters (1440 -> step 1140), which keeps the
# peak-step-equals-published-case property testable.
PEAK_HOUR = 19.0
PV_PEAK_HOUR = 13.0
BASE_SHARE = 0.3          # nightly base as share of the case's nominal load


def _factories() -> dict[str, Callable[[], Any]]:
    """Lazy import: pandapower.networks pulls optional deps on import."""
    import pandapower.networks as pn

    return {
        "ieee_33bw": pn.case33bw,
        # scenario = minute-566 snapshot of the published day (the on-peak case)
        "ieee_european_lv": lambda: pn.ieee_european_lv_asymmetric("on_peak_566"),
        "cigre_mv": lambda: pn.create_cigre_network_mv(with_der=False),
        "cigre_lv": pn.create_cigre_network_lv,
        "kerber_landnetz": pn.create_kerber_landnetz_kabel_2,
        "kerber_dorfnetz": pn.create_kerber_dorfnetz,
        "kerber_vorstadtnetz": pn.create_kerber_vorstadtnetz_kabel_1,
    }


# Catalog metadata. ``nodes`` is the converted bus count, pinned in the test
# suite so a pandapower upgrade that reshapes a reference net trips visibly.
REFERENCE_GRIDS: dict[str, dict[str, Any]] = {
    "ieee_33bw": {
        "name": "IEEE 33-Bus Test Feeder (12,66 kV)",
        "voltage": "MV", "origin": "ieee", "nodes": 33,
    },
    "ieee_european_lv": {
        "name": "IEEE European LV Test Feeder (416 V)",
        "voltage": "LV", "origin": "ieee", "nodes": 907,
    },
    "cigre_mv": {
        # 15 Knoten + 3 Stichknoten an den offenen Ringtrennstellen S1-S3
        "name": "CIGRE Mittelspannungsnetz (20 kV)",
        "voltage": "MV", "origin": "cigre", "nodes": 18,
    },
    "cigre_lv": {
        "name": "CIGRE Niederspannungsnetz (400 V)",
        "voltage": "LV", "origin": "cigre", "nodes": 41,
    },
    "kerber_landnetz": {
        "name": "Kerber Landnetz (Kabel, 14 Haushalte)",
        "voltage": "LV", "origin": "kerber", "nodes": 30,
    },
    "kerber_dorfnetz": {
        "name": "Kerber Dorfnetz (57 Haushalte)",
        "voltage": "LV", "origin": "kerber", "nodes": 116,
    },
    "kerber_vorstadtnetz": {
        "name": "Kerber Vorstadtnetz (Kabel, 146 Haushalte)",
        "voltage": "LV", "origin": "kerber", "nodes": 294,
    },
}


def convert_reference(key: str, *, steps: int = 1440) -> GridInputs:
    """Build the pandapower reference net ``key`` and convert it."""
    try:
        factory = _factories()[key]
    except KeyError:
        raise KeyError(f"unknown reference grid '{key}'") from None
    meta = REFERENCE_GRIDS[key]
    return convert_pandapower_net(factory(), name=meta["name"], steps=steps)


def _val(row: Any, col: str, default: float | None = None) -> float | None:
    """A row value as float, mapping missing columns and NaN to ``default``."""
    if col not in row:
        return default
    v = row[col]
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return default if math.isnan(f) else f


def convert_pandapower_net(net: Any, *, name: str, steps: int = 1440,
                           bus_map: dict[int, int] | None = None) -> GridInputs:
    """Generic pandapower net -> GridInputs (exact params + synthetic day).

    Supports the element set the reference feeders use: bus, line, trafo,
    switch (b/l/t), load, asymmetric_load, sgen, ext_grid. Anything else
    present in the net is reported in ``notes`` and skipped. A dict passed
    as ``bus_map`` is filled with source-net bus index -> netzsim bus index
    (merged buses share a target) — the equivalence test uses it.
    """
    notes: list[str] = []

    # ---- closed bus-bus switches merge buses (pandapower fuses them) ------ #
    parent: dict[int, int] = {int(b): int(b) for b in net.bus.index}

    def find(b: int) -> int:
        while parent[b] != b:
            parent[b] = parent[parent[b]]
            b = parent[b]
        return b

    n_bb = 0
    for _, sw in net.switch.iterrows():
        if sw["et"] == "b" and bool(sw["closed"]):
            a, b = find(int(sw["bus"])), find(int(sw["element"]))
            if a != b:
                parent[max(a, b)] = min(a, b)
                n_bb += 1
    if n_bb:
        notes.append(f"{n_bb} geschlossene Sammelschienen-Schalter zu Knoten verschmolzen")

    # open element switches: (line, bus side) pairs — pandapower disconnects
    # only THAT end; the line stays energized from the other side (the cable's
    # charging current keeps flowing), which the conversion reproduces by
    # rerouting the open end onto a fresh stub bus
    open_line_ends = {(int(sw["element"]), int(sw["bus"]))
                      for _, sw in net.switch.iterrows()
                      if sw["et"] == "l" and not bool(sw["closed"])}
    open_trafos = {int(sw["element"]) for _, sw in net.switch.iterrows()
                   if sw["et"] == "t" and not bool(sw["closed"])}

    # ---- buses (positional indices; merged buses collapse to their root) -- #
    roots = sorted({find(int(b)) for b in net.bus.index})
    pos = {root: i for i, root in enumerate(roots)}

    def bus_of(old: int) -> int:
        return pos[find(int(old))]

    if bus_map is not None:
        bus_map.update({int(b): bus_of(b) for b in net.bus.index})

    buses = []
    for root in roots:
        row = net.bus.loc[root]
        bname = row["name"] if isinstance(row["name"], str) and row["name"] else f"bus_{root}"
        buses.append({
            "name": str(bname), "vn_kv": float(row["vn_kv"]),
            "type": "b", "zone": None, "in_service": bool(row["in_service"]),
        })
    vn_of = {i: b["vn_kv"] for i, b in enumerate(buses)}

    # ---- lines: exact per-km parameters ----------------------------------- #
    def stub_bus(near: int, lname: str) -> int:
        buses.append({
            "name": f"{lname}_offen", "vn_kv": vn_of[near],
            "type": "n", "zone": None, "in_service": True,
        })
        vn_of[len(buses) - 1] = buses[-1]["vn_kv"]
        return len(buses) - 1

    lines = []
    n_open = 0
    for idx, ln in net.line.iterrows():
        lname = str(ln["name"]) if isinstance(ln["name"], str) else f"line_{idx}"
        fb, tb = bus_of(ln["from_bus"]), bus_of(ln["to_bus"])
        from_open = (int(idx), int(ln["from_bus"])) in open_line_ends
        to_open = (int(idx), int(ln["to_bus"])) in open_line_ends
        in_service = bool(ln["in_service"]) and not (from_open and to_open)
        if in_service and from_open:
            fb, n_open = stub_bus(tb, lname), n_open + 1
        elif in_service and to_open:
            tb, n_open = stub_bus(fb, lname), n_open + 1
        lines.append({
            "name": lname,
            "from_bus": fb, "to_bus": tb,
            "length_km": float(ln["length_km"]),
            "r_ohm_per_km": float(ln["r_ohm_per_km"]),
            "x_ohm_per_km": float(ln["x_ohm_per_km"]),
            "c_nf_per_km": float(ln["c_nf_per_km"]),
            "max_i_ka": float(ln["max_i_ka"]),
            "parallel": int(_val(ln, "parallel", 1) or 1),
            "in_service": in_service,
        })
    if n_open:
        notes.append(
            f"{n_open} offene Trennstellen: Leitungsende auf freien Stichknoten "
            "gelegt (einseitig gespeist, Ladestrom fliesst — wie im Referenzfall)")

    # ---- transformers: exact parameters, tap folded into the ratio -------- #
    trafos = []
    for idx, tr in net.trafo.iterrows():
        vn_hv, vn_lv = float(tr["vn_hv_kv"]), float(tr["vn_lv_kv"])
        tap_pos, tap_neutral = _val(tr, "tap_pos"), _val(tr, "tap_neutral", 0.0)
        tap_step = _val(tr, "tap_step_percent")
        if tap_pos is not None and tap_step and tap_pos != tap_neutral:
            ratio = 1.0 + (tap_pos - tap_neutral) * tap_step / 100.0
            side = tr.get("tap_side")
            if side == "lv":
                vn_lv *= ratio
            else:
                vn_hv *= ratio
            notes.append(
                f"Trafo {idx}: Stufenstellung {tap_pos:g} in das "
                f"Uebersetzungsverhaeltnis eingerechnet")
        trafos.append({
            "name": str(tr["name"]) if isinstance(tr["name"], str) else f"trafo_{idx}",
            "hv_bus": bus_of(tr["hv_bus"]), "lv_bus": bus_of(tr["lv_bus"]),
            "sn_mva": float(tr["sn_mva"]),
            "vn_hv_kv": vn_hv, "vn_lv_kv": vn_lv,
            "vk_percent": float(tr["vk_percent"]),
            "vkr_percent": float(tr["vkr_percent"]),
            "pfe_kw": float(tr["pfe_kw"]), "i0_percent": float(tr["i0_percent"]),
            "shift_degree": float(_val(tr, "shift_degree", 0.0) or 0.0),
            "parallel": int(_val(tr, "parallel", 1) or 1),
            "in_service": bool(tr["in_service"]) and int(idx) not in open_trafos,
        })

    # ---- loads: nominal value becomes the 19:00 peak of a daily shape ----- #
    def load_rows(df: Any, asym: bool) -> list[dict]:
        rows = []
        for idx, ld in df.iterrows():
            if not bool(ld["in_service"]):
                continue
            scale = _val(ld, "scaling", 1.0) or 1.0
            if asym:
                p0 = (float(ld["p_a_mw"]) + float(ld["p_b_mw"]) + float(ld["p_c_mw"])) * scale
                q0 = (float(ld["q_a_mvar"]) + float(ld["q_b_mvar"]) + float(ld["q_c_mvar"])) * scale
            else:
                p0 = float(ld["p_mw"]) * scale
                q0 = float(ld["q_mvar"]) * scale
            bus = bus_of(ld["bus"])
            if p0 > 0:
                p = _daily(steps, BASE_SHARE * p0, (1 - BASE_SHARE) * p0, PEAK_HOUR)
                q = [round(v * q0 / p0, 9) for v in p]
            else:
                p = [0.0] * steps
                q = [round(q0, 9)] * steps
            lname = ld["name"] if isinstance(ld["name"], str) and ld["name"] else f"load_{idx}"
            rows.append({
                "name": str(lname), "bus": bus, "p_mw": p, "q_mvar": q,
                # LV connections are households -> LPG/EV/PV seeding + the
                # estimator's SLP basis work like on the library grids
                "household": vn_of[bus] < 1.0,
            })
        return rows

    loads = load_rows(net.load, asym=False)
    if len(net.asymmetric_load):
        loads += load_rows(net.asymmetric_load, asym=True)
        notes.append(
            f"{len(net.asymmetric_load)} unsymmetrische Lasten symmetrisch "
            "zusammengefasst (netzsim rechnet das symmetrische Ersatzsystem)")

    # ---- static generators: PV bell around midday -------------------------- #
    gens = []
    for idx, sg in net.sgen.iterrows():
        if not bool(sg["in_service"]):
            continue
        scale = _val(sg, "scaling", 1.0) or 1.0
        p0 = float(sg["p_mw"]) * scale
        gens.append({
            "name": str(sg["name"]) if isinstance(sg["name"], str) else f"sgen_{idx}",
            "bus": bus_of(sg["bus"]),
            "p_mw": _daily(steps, 0.0, p0, PV_PEAK_HOUR),
            "q_mvar": [0.0] * steps,
            "kind": "pv",
        })

    # ---- slack(s) ---------------------------------------------------------- #
    subs = []
    for idx, eg in net.ext_grid.iterrows():
        if not bool(eg["in_service"]):
            continue
        subs.append({
            "name": str(eg["name"]) if isinstance(eg["name"], str) else f"ext_grid_{idx}",
            "bus": bus_of(eg["bus"]),
            "vm_pu": [round(float(eg["vm_pu"]), 6)] * steps,
            "va_degree": [0.0] * steps,
        })

    for tbl in ("gen", "shunt", "ward", "xward", "impedance", "dcline", "storage"):
        n = len(getattr(net, tbl, []))
        if n:
            notes.append(f"{n} {tbl}-Element(e) nicht uebernommen (netzsim kennt nur "
                         "Last/Erzeuger/Trafo/Leitung/Slack)")

    notes.append(
        "Referenznetz: Impedanzen exakt uebernommen; die Zeitdimension ist "
        f"synthetisch (Tagesform mit Abendspitze {PEAK_HOUR:g} Uhr = Nennlast "
        "des Referenzfalls)")

    res_min = max(1, 1440 // steps)
    return GridInputs(
        grid_structure={"name": name, "f_hz": float(getattr(net, "f_hz", 50.0)), "buses": buses},
        lines={"lines": lines, "transformers": trafos},
        load={"resolution_minutes": res_min, "steps": steps, "loads": loads},
        generation={"resolution_minutes": res_min, "steps": steps, "generation": gens},
        substation={"resolution_minutes": res_min, "steps": steps, "substations": subs},
        notes=notes,
    )
