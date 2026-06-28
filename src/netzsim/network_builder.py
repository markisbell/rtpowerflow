"""Translate the validated input data into a pandapower network + profile arrays.

The static topology (buses, lines, transformers) is built once. Loads, sgens and
ext_grids are created as pandapower elements and their per-step values are stored
as dense numpy arrays of shape (n_elements, n_steps) for fast time stepping.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandapower as pp

from .data_loader import InputData


@dataclass
class ProfileArrays:
    """Dense per-step arrays aligned with pandapower element indices."""

    load_idx: list[int] = field(default_factory=list)
    load_p: np.ndarray = field(default_factory=lambda: np.empty((0, 0)))
    load_q: np.ndarray = field(default_factory=lambda: np.empty((0, 0)))

    sgen_idx: list[int] = field(default_factory=list)
    sgen_p: np.ndarray = field(default_factory=lambda: np.empty((0, 0)))
    sgen_q: np.ndarray = field(default_factory=lambda: np.empty((0, 0)))

    ext_idx: list[int] = field(default_factory=list)
    ext_vm: np.ndarray = field(default_factory=lambda: np.empty((0, 0)))
    ext_va: np.ndarray = field(default_factory=lambda: np.empty((0, 0)))

    steps: int = 0


def _stack(rows: list[list[float]], steps: int) -> np.ndarray:
    if not rows:
        return np.empty((0, steps), dtype=float)
    return np.asarray(rows, dtype=float)


def build_network(data: InputData) -> tuple[pp.pandapowerNet, ProfileArrays]:
    grid = data.grid
    net = pp.create_empty_network(name=grid.name, f_hz=grid.f_hz)

    # --- Buses (index == position in grid_structure.buses) ---------------- #
    for b in grid.buses:
        pp.create_bus(
            net, vn_kv=b.vn_kv, name=b.name, type=b.type,
            zone=b.zone, in_service=b.in_service,
        )

    # --- Lines ------------------------------------------------------------ #
    for ln in data.lines.lines:
        if ln.std_type:
            pp.create_line(
                net, from_bus=ln.from_bus, to_bus=ln.to_bus,
                length_km=ln.length_km, std_type=ln.std_type,
                name=ln.name, parallel=ln.parallel, in_service=ln.in_service,
            )
        else:
            pp.create_line_from_parameters(
                net, from_bus=ln.from_bus, to_bus=ln.to_bus,
                length_km=ln.length_km,
                r_ohm_per_km=ln.r_ohm_per_km, x_ohm_per_km=ln.x_ohm_per_km,
                c_nf_per_km=ln.c_nf_per_km, max_i_ka=ln.max_i_ka,
                name=ln.name, parallel=ln.parallel, in_service=ln.in_service,
            )

    # --- Transformers (optional) ----------------------------------------- #
    for tr in data.lines.transformers:
        if tr.std_type:
            pp.create_transformer(
                net, hv_bus=tr.hv_bus, lv_bus=tr.lv_bus,
                std_type=tr.std_type, name=tr.name, parallel=tr.parallel,
                in_service=tr.in_service,
            )
        else:
            pp.create_transformer_from_parameters(
                net, hv_bus=tr.hv_bus, lv_bus=tr.lv_bus,
                sn_mva=tr.sn_mva, vn_hv_kv=tr.vn_hv_kv, vn_lv_kv=tr.vn_lv_kv,
                vk_percent=tr.vk_percent, vkr_percent=tr.vkr_percent,
                pfe_kw=tr.pfe_kw, i0_percent=tr.i0_percent,
                shift_degree=tr.shift_degree, name=tr.name,
                parallel=tr.parallel, in_service=tr.in_service,
            )

    steps = data.steps_per_day
    prof = ProfileArrays(steps=steps)

    # --- Loads ------------------------------------------------------------ #
    p_rows, q_rows = [], []
    for el in data.load.loads:
        idx = pp.create_load(net, bus=el.bus, p_mw=0.0, q_mvar=0.0, name=el.name)
        prof.load_idx.append(idx)
        p_rows.append(el.p_mw)
        q_rows.append(el.q_mvar if el.q_mvar is not None else [0.0] * steps)
    prof.load_p = _stack(p_rows, steps)
    prof.load_q = _stack(q_rows, steps)

    # --- Static generators ------------------------------------------------ #
    p_rows, q_rows = [], []
    for el in data.generation.gens:
        idx = pp.create_sgen(net, bus=el.bus, p_mw=0.0, q_mvar=0.0, name=el.name)
        prof.sgen_idx.append(idx)
        p_rows.append(el.p_mw)
        q_rows.append(el.q_mvar if el.q_mvar is not None else [0.0] * steps)
    prof.sgen_p = _stack(p_rows, steps)
    prof.sgen_q = _stack(q_rows, steps)

    # --- External grids (substations / upper grid layers) ----------------- #
    vm_rows, va_rows = [], []
    for el in data.substation.substations:
        idx = pp.create_ext_grid(
            net, bus=el.bus, vm_pu=el.vm_pu[0],
            va_degree=(el.va_degree[0] if el.va_degree else 0.0), name=el.name,
        )
        prof.ext_idx.append(idx)
        vm_rows.append(el.vm_pu)
        va_rows.append(el.va_degree if el.va_degree is not None else [0.0] * steps)
    prof.ext_vm = _stack(vm_rows, steps)
    prof.ext_va = _stack(va_rows, steps)

    return net, prof
