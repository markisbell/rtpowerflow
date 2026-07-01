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
        layout_geo, tree = self._bus_coordinates()
        # Real WGS84 coords (ding0 grids): {bus_index: [lon, lat]}
        real = {i: b.geo for i, b in enumerate(self.data.grid.buses) if b.geo}
        has_geo = len(real) >= max(1, len(self.data.grid.buses)) * 0.5
        if has_geo:
            lons = [g[0] for g in real.values()]
            lats = [g[1] for g in real.values()]
            mnlon, mxlon, mnlat, mxlat = min(lons), max(lons), min(lats), max(lats)
            dlon, dlat = (mxlon - mnlon) or 1.0, (mxlat - mnlat) or 1.0

        buses = net.bus.reset_index().rename(columns={"index": "id"}).to_dict(
            orient="records")
        for b in buses:
            i = int(b["id"])
            t = tree.get(i, [0.0, 0.0])
            b["tx"], b["ty"] = t[0], t[1]      # tidy tree (toggle)
            gg = real.get(i)
            b["geo"] = [gg[0], gg[1]] if gg else None   # raw lon/lat for the map
            if has_geo and gg:
                # geographic layout = real positions (lat inverted: north is up)
                b["x"] = round((gg[0] - mnlon) / dlon, 5)
                b["y"] = round(1.0 - (gg[1] - mnlat) / dlat, 5)
            else:
                g = layout_geo.get(i, [0.0, 0.0])
                b["x"], b["y"] = g[0], g[1]    # length-aware synthetic layout
        # lines + optional per-line geometry (OSM-routed cables follow the streets);
        # net.line index == position in data.lines.lines, so geometry maps by id.
        specs = self.data.lines.lines
        lines_out = net.line.reset_index().rename(columns={"index": "id"})[
            ["id", "name", "from_bus", "to_bus", "length_km"]].to_dict(orient="records")
        for ln in lines_out:
            i = int(ln["id"])
            ln["geometry"] = specs[i].geometry if i < len(specs) else None
            # normally-open ring ties are laid but out of service → drawn open on the map
            ln["in_service"] = bool(net.line.at[i, "in_service"])
        return {
            "name": net.name,
            "f_hz": float(net.f_hz),
            "steps_per_day": self.steps_per_day,
            "has_geo": has_geo,
            "buses": buses,
            "lines": lines_out,
            "trafos": net.trafo.reset_index().rename(columns={"index": "id"})
                [["id", "name", "hv_bus", "lv_bus", "sn_mva"]]
                .to_dict(orient="records"),
            "ext_grids": net.ext_grid.reset_index().rename(columns={"index": "id"})
                [["id", "name", "bus"]].to_dict(orient="records"),
            # bus indices hosting loads / PV — for the map underlay (houses, panels)
            "load_buses": sorted({int(b) for b in net.load["bus"].tolist()}),
            "sgen_buses": sorted({int(b) for b in net.sgen["bus"].tolist()}),
            # LV cable cabinets (where service cables join the main line) → green circles
            "cabinet_buses": [i for i, b in enumerate(self.data.grid.buses)
                              if getattr(b, "kind", None) == "cabinet"],
            "n_load": int(len(net.load)),
            "n_sgen": int(len(net.sgen)),
            "n_trafo": int(len(net.trafo)),
        }

    def _bus_coordinates(self) -> tuple[dict[int, list[float]], dict[int, list[float]]]:
        if self._coords is None:
            from .layout import compute_layouts
            self._coords = compute_layouts(self.net)
        return self._coords

    def node_profiles(self, bus: int) -> dict:
        """The daily p_mw profiles feeding one bus, split into residential / EV
        loads and PV generation (EV loads are named ``EV_*``, PV sgen ``PV_*``).
        Each series is the sum over that category's elements at the bus."""
        p, net = self.prof, self.net
        cats: dict[str, "np.ndarray | None"] = {}
        def add(key, row):
            cats[key] = row.copy() if cats.get(key) is None else cats[key] + row
        for i, li in enumerate(p.load_idx):
            if int(net.load.at[li, "bus"]) != bus:
                continue
            name = str(net.load.at[li, "name"] or "")
            add("ev" if name.startswith("EV_") else "residential", p.load_p[i])
        for i, si in enumerate(p.sgen_idx):
            if int(net.sgen.at[si, "bus"]) != bus:
                continue
            add("pv", p.sgen_p[i])
        order = ["residential", "ev", "pv"]
        series = [{"kind": k, "p_mw": [_r(v) for v in cats[k]]}
                  for k in order if cats.get(k) is not None]
        name = str(net.bus.at[bus, "name"]) if bus in net.bus.index else str(bus)
        return {"bus": bus, "name": name, "steps_per_day": self.steps_per_day, "series": series}


def _r(value, ndigits: int = 6):
    """Round to JSON-safe floats. Non-finite (NaN/±Inf — e.g. isolated buses)
    becomes ``None`` so the payload is valid JSON for strict parsers (browsers)."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return round(f, ndigits) if math.isfinite(f) else None
