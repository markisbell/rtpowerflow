"""The core: apply one time step's profiles to the net and solve the power flow."""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any

import pandapower as pp

from .data_loader import InputData
from .network_builder import ProfileArrays, build_network


@dataclass
class StepResult:
    """Serializable summary of one solved time step."""

    step: int
    day: int
    time_of_day: str          # "HH:MM"
    converged: bool
    solve_ms: float
    timestamp: float          # wall-clock unix time the step was solved
    buses: list[dict[str, Any]] = field(default_factory=list)
    lines: list[dict[str, Any]] = field(default_factory=list)
    trafos: list[dict[str, Any]] = field(default_factory=list)
    ext_grids: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


def _hhmm(step: int, steps_per_day: int) -> str:
    minutes_per_step = 24 * 60 // steps_per_day
    total = step * minutes_per_step
    return f"{total // 60:02d}:{total % 60:02d}"


class Simulator:
    """Holds the pandapower net + profiles and solves any single step on demand."""

    def __init__(self, data: InputData, warm_start: bool = True):
        self.data = data
        self.warm_start = warm_start
        self.net, self.prof = build_network(data)
        self.steps_per_day = data.steps_per_day
        self._solved_once = False
        self._coords: tuple[dict[int, list[float]], dict[int, list[float]]] | None = None

    # -- profile application -------------------------------------------- #
    def _apply_step(self, t: int) -> None:
        p = self.prof
        if p.load_idx:
            self.net.load.loc[p.load_idx, "p_mw"] = p.load_p[:, t]
            self.net.load.loc[p.load_idx, "q_mvar"] = p.load_q[:, t]
        if p.sgen_idx:
            self.net.sgen.loc[p.sgen_idx, "p_mw"] = p.sgen_p[:, t]
            self.net.sgen.loc[p.sgen_idx, "q_mvar"] = p.sgen_q[:, t]
        if p.ext_idx:
            self.net.ext_grid.loc[p.ext_idx, "vm_pu"] = p.ext_vm[:, t]
            self.net.ext_grid.loc[p.ext_idx, "va_degree"] = p.ext_va[:, t]

    # -- the power flow -------------------------------------------------- #
    def run_step(self, step: int, day: int = 0) -> StepResult:
        t = step % self.steps_per_day
        self._apply_step(t)

        init = "results" if (self.warm_start and self._solved_once) else "auto"
        t0 = time.perf_counter()
        try:
            pp.runpp(self.net, init=init, calculate_voltage_angles=True)
            converged = True
            error = None
            self._solved_once = True
        except pp.LoadflowNotConverged as exc:  # type: ignore[attr-defined]
            converged = False
            error = f"Load flow did not converge: {exc}"
        solve_ms = (time.perf_counter() - t0) * 1000.0

        return self._collect(step, day, t, converged, solve_ms, error)

    # -- result extraction ---------------------------------------------- #
    def _collect(self, step, day, t, converged, solve_ms, error) -> StepResult:
        res = StepResult(
            step=t,
            day=day,
            time_of_day=_hhmm(t, self.steps_per_day),
            converged=converged,
            solve_ms=round(solve_ms, 3),
            timestamp=time.time(),
            error=error,
        )
        if not converged:
            return res

        net = self.net
        for idx, row in net.res_bus.iterrows():
            res.buses.append({
                "index": int(idx),
                "name": str(net.bus.at[idx, "name"]),
                "vm_pu": _r(row.vm_pu),
                "va_degree": _r(row.va_degree),
                "p_mw": _r(row.p_mw),
                "q_mvar": _r(row.q_mvar),
            })
        for idx, row in net.res_line.iterrows():
            res.lines.append({
                "index": int(idx),
                "name": str(net.line.at[idx, "name"]),
                "from_bus": int(net.line.at[idx, "from_bus"]),
                "to_bus": int(net.line.at[idx, "to_bus"]),
                "loading_percent": _r(row.loading_percent),
                "i_ka": _r(row.i_ka),
                "p_from_mw": _r(row.p_from_mw),
                "pl_mw": _r(row.pl_mw),
            })
        for idx, row in net.res_trafo.iterrows():
            res.trafos.append({
                "index": int(idx),
                "name": str(net.trafo.at[idx, "name"]),
                "hv_bus": int(net.trafo.at[idx, "hv_bus"]),
                "lv_bus": int(net.trafo.at[idx, "lv_bus"]),
                "loading_percent": _r(row.loading_percent),
                "p_hv_mw": _r(row.p_hv_mw),
                "q_hv_mvar": _r(row.q_hv_mvar),
                "i_hv_ka": _r(row.i_hv_ka),
                "pl_mw": _r(row.pl_mw),
            })
        for idx, row in net.res_ext_grid.iterrows():
            res.ext_grids.append({
                "index": int(idx),
                "name": str(net.ext_grid.at[idx, "name"]),
                "p_mw": _r(row.p_mw),
                "q_mvar": _r(row.q_mvar),
            })

        vm = net.res_bus.vm_pu
        loadings = net.res_line.loading_percent
        trafo_loadings = net.res_trafo.loading_percent
        res.summary = {
            "n_bus": int(len(net.res_bus)),
            "n_line": int(len(net.res_line)),
            "n_trafo": int(len(net.res_trafo)),
            "vm_pu_min": _r(vm.min()),
            "vm_pu_max": _r(vm.max()),
            "max_line_loading_percent": _r(loadings.max()) if len(loadings) else None,
            "max_trafo_loading_percent": (
                _r(trafo_loadings.max()) if len(trafo_loadings) else None
            ),
            "total_load_mw": _r(net.res_load.p_mw.sum()) if len(net.res_load) else 0.0,
            "total_gen_mw": _r(net.res_sgen.p_mw.sum()) if len(net.res_sgen) else 0.0,
            "total_ext_grid_mw": _r(net.res_ext_grid.p_mw.sum()),
            "total_losses_mw": _r(net.res_line.pl_mw.sum()) if len(net.res_line) else 0.0,
        }
        return res

    # -- static topology for the API ------------------------------------ #
    def topology(self) -> dict[str, Any]:
        net = self.net
        force, tree = self._bus_coordinates()
        buses = net.bus.reset_index().rename(columns={"index": "id"}).to_dict(
            orient="records")
        for b in buses:
            f = force.get(int(b["id"]), [0.0, 0.0])
            t = tree.get(int(b["id"]), [0.0, 0.0])
            b["x"], b["y"] = f[0], f[1]       # force-directed (default)
            b["tx"], b["ty"] = t[0], t[1]     # tidy tree (toggle)
        return {
            "name": net.name,
            "f_hz": float(net.f_hz),
            "steps_per_day": self.steps_per_day,
            "buses": buses,
            "lines": net.line.reset_index().rename(columns={"index": "id"})
                [["id", "name", "from_bus", "to_bus", "length_km"]]
                .to_dict(orient="records"),
            "trafos": net.trafo.reset_index().rename(columns={"index": "id"})
                [["id", "name", "hv_bus", "lv_bus", "sn_mva"]]
                .to_dict(orient="records"),
            "ext_grids": net.ext_grid.reset_index().rename(columns={"index": "id"})
                [["id", "name", "bus"]].to_dict(orient="records"),
            "n_load": int(len(net.load)),
            "n_sgen": int(len(net.sgen)),
            "n_trafo": int(len(net.trafo)),
        }

    def _bus_coordinates(self) -> tuple[dict[int, list[float]], dict[int, list[float]]]:
        if self._coords is None:
            from .layout import compute_layouts
            self._coords = compute_layouts(self.net)
        return self._coords


def _r(value, ndigits: int = 6):
    """Round to JSON-safe floats. Non-finite (NaN/±Inf — e.g. isolated buses)
    becomes ``None`` so the payload is valid JSON for strict parsers (browsers)."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return round(f, ndigits) if math.isfinite(f) else None
