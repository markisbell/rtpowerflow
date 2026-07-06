"""Compose an interconnected MV district: the MV ring + street-routed LV subgrids.

``scope="mv"`` folds every LV grid into a lumped load at its feeding MV bus.
This importer goes one step further: for each MV/LV station whose LV grid has a
street-routed OSM version in the library, it SPLICES that full LV grid into the
district — connected through the real ding0 station transformer from
``transformers.csv`` — instead of the lumped load. Stations without an OSM LV
grid stay lumped, so the district always solves as one 20 kV + 0.4 kV network.

Spliced elements are name-prefixed ``lv{grid_id}:``. Building loads carry
``household: true`` so the LPG/EV/PV assignment targets real households only and
leaves the lumped station loads on their aggregate profiles.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from .ding0_import import _lvg, _num, _real, _solarish, convert_ding0_csv, trafo_spec_from_row
from .grid_inputs import GridInputs
from .osm_lv_import import convert_osm_lv


def convert_district(grid_dir: str | Path, lv_grids: list[dict], *,
                     name: str | None = None, steps: int = 1440,
                     power_factor: float = 0.95) -> GridInputs:
    """Build one interconnected district from a ding0 CSV dir plus OSM LV grids.

    ``lv_grids`` entries are ``{"lv_grid_id": str, "path": str}`` (the manifest's
    LV entries of this district that carry an ``osm_grid``). A grid is only
    spliced when its station transformer can be located; otherwise it stays
    lumped like any other LV grid.
    """
    d = Path(grid_dir)
    name = name or d.name

    buses = pd.read_csv(d / "buses.csv")
    trafos = pd.read_csv(d / "transformers.csv") if (d / "transformers.csv").exists() else pd.DataFrame()
    gens = pd.read_csv(d / "generators.csv") if (d / "generators.csv").exists() else pd.DataFrame()
    lvof: dict[str, str] = {}
    vnof: dict[str, float] = {}
    for _, r in buses.iterrows():
        nm = str(r["name"])
        lvof[nm] = _lvg(r.get("lv_grid_id"))
        vnof[nm] = _num(r.get("v_nom"), 0.4)

    # locate each candidate's station transformer row(s) + feeding MV bus; only
    # grids with a resolvable station get spliced (the rest stay lumped)
    station: dict[str, list] = {}   # lv grid id -> [(trafo row, mv bus name, lv vn)]
    for _, r in trafos.iterrows():
        a, b = _real(r["bus0"]), _real(r["bus1"])
        ga, gb = lvof.get(a, ""), lvof.get(b, "")
        mv_side = a if ga == "" and vnof.get(a, 0.0) > 1.0 else b if gb == "" and vnof.get(b, 0.0) > 1.0 else None
        lv_side = b if mv_side == a else a if mv_side == b else None
        if mv_side is None or lv_side is None:
            continue
        g = lvof.get(lv_side, "")
        if g:
            station.setdefault(g, []).append((r, mv_side, vnof.get(lv_side, 0.4)))
    splice = [g for g in lv_grids if _lvg(g["lv_grid_id"]) in station]
    skipped = [g for g in lv_grids if _lvg(g["lv_grid_id"]) not in station]

    # MV graph with the spliced grids excluded from lumping
    mv = convert_ding0_csv(d, name=name, steps=steps, power_factor=power_factor,
                           scope="mv", exclude_lv={_lvg(g["lv_grid_id"]) for g in splice})
    bus_specs = mv.grid_structure["buses"]
    line_specs = mv.lines["lines"]
    trafo_specs = mv.lines["transformers"]
    load_specs = mv.load["loads"]
    gen_specs = mv.generation["generation"]
    notes = list(mv.notes)
    bus_index = {b["name"]: i for i, b in enumerate(bus_specs)}
    for ld in load_specs:
        ld["household"] = False        # MV-level + lumped station loads: no LPG households

    for g in splice:
        lvid = _lvg(g["lv_grid_id"])
        lv = convert_osm_lv(g["path"], name=g.get("id") or f"lv_{lvid}",
                            steps=steps, power_factor=power_factor)
        lv_buses = lv.grid_structure["buses"]
        # convert_osm_lv appends a synthetic MS-Netz bus + auto-sized trafo +
        # slack; the district replaces all three with the real MV bus + station trafo
        assert lv_buses[-1]["name"] == "MS-Netz"
        busbar_local = lv.lines["transformers"][0]["lv_bus"]
        offset = len(bus_specs)
        for b in lv_buses[:-1]:
            bus_specs.append({**b, "name": f"lv{lvid}:{b['name']}"})
        for ln in lv.lines["lines"]:
            line_specs.append({**ln, "name": f"lv{lvid}:{ln.get('name')}",
                               "from_bus": ln["from_bus"] + offset,
                               "to_bus": ln["to_bus"] + offset})
        for ld in lv.load["loads"]:
            load_specs.append({**ld, "name": f"lv{lvid}:{ld.get('name')}",
                               "bus": ld["bus"] + offset, "household": True})

        busbar = busbar_local + offset
        rows = station[lvid]
        mv_bus = bus_index[rows[0][1]]
        for r, mv_name, vn_lv in rows:  # several rows = parallel station transformers
            trafo_specs.append(trafo_spec_from_row(
                r, bus_index[mv_name], busbar, vnof.get(mv_name, 20.0), vn_lv))

        n_gen = 0                       # ding0's LV generators re-attach at the busbar
        if not gens.empty:
            for _, r in gens.iterrows():
                if lvof.get(_real(r["bus"]), "") != lvid:
                    continue
                subtype = str(r.get("subtype") or r.get("type") or "")
                gen_specs.append({"name": f"lv{lvid}:{r['name']}", "bus": busbar,
                                  "p_mw": _solarish(steps, _num(r.get("p_nom")), subtype),
                                  "q_mvar": [0.0] * steps})
                n_gen += 1
        notes.append(f"spliced LV grid {lvid} at MV bus '{rows[0][1]}': "
                     f"{len(lv_buses) - 1} buses, {len(lv.load['loads'])} household loads, "
                     f"{len(rows)} station trafo(s)" + (f", {n_gen} gen(s) at busbar" if n_gen else ""))

    for g in skipped:
        notes.append(f"LV grid {_lvg(g['lv_grid_id'])}: station transformer not found — kept lumped")
    notes.append(f"interconnected district '{name}': {len(bus_specs)} buses, "
                 f"{len(line_specs)} lines, {len(trafo_specs)} trafos, "
                 f"{len(load_specs)} loads ({len(splice)} LV subgrids spliced)")

    return GridInputs(
        grid_structure={**mv.grid_structure, "buses": bus_specs},
        lines={"lines": line_specs, "transformers": trafo_specs},
        load={**mv.load, "loads": load_specs},
        generation={**mv.generation, "generation": gen_specs},
        substation=mv.substation, notes=notes,
    )
