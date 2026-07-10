"""The observability layer: what the operator can actually *see*.

Reality is the full power flow that pandapower solves every step. In the real
world you don't get to see all of it — you only know a quantity where a
**measurement device** has been installed. This module models that device layer
and *projects* the true solved net down to the observed subset.

Two device kinds are modelled:

- **Smart meter (node)** — installed at a bus. A smart meter + gateway measures
  voltage magnitude, current, active power and reactive power (per phase, and the
  sum). The current model is a **balanced single-phase-equivalent** power flow, so
  the three phases are symmetric: the reported ``p_mw`` / ``q_mvar`` are the
  three-phase sums, per-phase = sum / 3, and the current is the (equal) per-phase
  line current ``I = S / (√3 · V_LL)``. True unbalanced per-phase values would
  need pandapower's ``runpp_3ph`` (a separate simulation mode — see CLAUDE.md).
- **Transformer meter** — installed at a transformer. Reveals its loading and
  HV-side power/current. Without it the transformer's loading is simply unknown.

A grid with no meters is therefore almost entirely unobservable — which is the
whole point: it shows how little of "reality" a real operator sees.

The set of placed devices is grid-specific (bus / transformer indices change with
the grid), so it is held per-``Simulator`` and reset whenever the grid is swapped.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

# Presets accepted by ``MeasurementSet.apply_preset`` / the ``/measurements/preset``
# endpoint. Kept here so the API and the set agree on the vocabulary. The cell
# presets (vertical MV/LV integration) need the grid's ONS cells: one digital
# secondary substation per cell, or the full SMGW rollout of a single cell.
PRESETS = ("all_nodes", "all_trafos", "substation_trafos", "digital_stations",
           "cell_full", "clear")

# Meter fidelity. "full": every quantity (V, P, Q, I) every simulation step —
# TAF 9/10/14 grid-state telemetry. "standard": German standard metering
# ("Lastgang", TAF 7): only the ACTIVE power, as the mean over each completed
# 15-minute window. The mode is PER DEVICE (a street of TAF-7 household meters
# can coexist with 1-min SMGWs at plants and wallboxes); ``mode`` on the set is
# the default for newly placed devices, and ``set_mode`` doubles as the bulk
# "switch everything" action.
METER_MODES = ("full", "standard")


def _r(value, ndigits: int = 6):
    """Round to a JSON-safe float; non-finite → ``None``. The CANONICAL copy —
    simulator.py and sweeps.py import it from here (all result floats must go
    through this, or Python's json emits literal NaN and browsers drop frames)."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return round(f, ndigits) if math.isfinite(f) else None


@dataclass
class MeasurementSet:
    """Which buses / transformers carry a measurement device, plus the projection
    from a solved pandapower net to the observed readings."""

    node_buses: set[int] = field(default_factory=set)
    trafo_idxs: set[int] = field(default_factory=set)
    mode: str = "full"                    # DEFAULT for new devices, see METER_MODES
    # per-device mode OVERRIDES (device without an entry follows the default)
    node_modes: dict[int, str] = field(default_factory=dict)
    trafo_modes: dict[int, str] = field(default_factory=dict)
    # standard-mode window state: running accumulator + last completed window
    _win: int = field(default=-1, repr=False)
    _acc: dict = field(default_factory=dict, repr=False)   # key -> (sum, n)
    _held: dict = field(default_factory=dict, repr=False)  # key -> window mean

    # -- per-device mode ------------------------------------------------------ #
    def mode_of_node(self, bus: int) -> str:
        return self.node_modes.get(int(bus), self.mode)

    def mode_of_trafo(self, trafo: int) -> str:
        return self.trafo_modes.get(int(trafo), self.mode)

    def set_node_mode(self, bus: int, name: str) -> None:
        if name not in METER_MODES:
            raise ValueError(f"unknown meter mode '{name}'")
        if int(bus) not in self.node_buses:
            raise KeyError(bus)
        if name == self.mode:
            self.node_modes.pop(int(bus), None)
        else:
            self.node_modes[int(bus)] = name
        self._win, self._acc, self._held = -1, {}, {}

    def set_trafo_mode(self, trafo: int, name: str) -> None:
        if name not in METER_MODES:
            raise ValueError(f"unknown meter mode '{name}'")
        if int(trafo) not in self.trafo_idxs:
            raise KeyError(trafo)
        if name == self.mode:
            self.trafo_modes.pop(int(trafo), None)
        else:
            self.trafo_modes[int(trafo)] = name
        self._win, self._acc, self._held = -1, {}, {}

    @property
    def all_standard(self) -> bool:
        """True when at least one device is placed and EVERY device delivers
        only the 15-min Lastgang — then nothing in the system carries 1-min
        information and consumers (live estimation raster) may slow down."""
        if not self.node_buses and not self.trafo_idxs:
            return False
        return (all(self.mode_of_node(b) == "standard" for b in self.node_buses)
                and all(self.mode_of_trafo(tr) == "standard" for tr in self.trafo_idxs))

    def modes_signature(self) -> tuple:
        """Hashable fingerprint of default + per-device modes (cache keys)."""
        return (self.mode, tuple(sorted(self.node_modes.items())),
                tuple(sorted(self.trafo_modes.items())))

    # -- placement mutators (all return True if the set actually changed) ----- #
    def add_node(self, bus: int) -> bool:
        if bus in self.node_buses:
            return False
        self.node_buses.add(int(bus))
        return True

    def remove_node(self, bus: int) -> bool:
        if bus not in self.node_buses:
            return False
        self.node_buses.discard(int(bus))
        self.node_modes.pop(int(bus), None)
        return True

    def add_trafo(self, trafo: int) -> bool:
        if trafo in self.trafo_idxs:
            return False
        self.trafo_idxs.add(int(trafo))
        return True

    def remove_trafo(self, trafo: int) -> bool:
        if trafo not in self.trafo_idxs:
            return False
        self.trafo_idxs.discard(int(trafo))
        self.trafo_modes.pop(int(trafo), None)
        return True

    def clear(self) -> None:
        self.node_buses.clear()
        self.trafo_idxs.clear()
        self.node_modes.clear()
        self.trafo_modes.clear()
        self._win, self._acc, self._held = -1, {}, {}

    def set_mode(self, name: str) -> None:
        """Set the default AND switch every placed device (the bulk action of
        the Messungen menu); per-device overrides are cleared."""
        if name not in METER_MODES:
            raise ValueError(f"unknown meter mode '{name}'")
        self.mode = name
        self.node_modes.clear()
        self.trafo_modes.clear()
        self._win, self._acc, self._held = -1, {}, {}

    def apply_preset(self, name: str, net, cells: list[dict] | None = None,
                     cell: str | None = None) -> None:
        """Bulk placement helper. ``net`` supplies the element indices; the
        cell presets additionally need the grid's ONS ``cells``."""
        if name == "clear":
            self.clear()
        elif name == "all_nodes":
            self.node_buses = {int(b) for b in net.bus.index}
        elif name == "all_trafos":
            self.trafo_idxs = {int(t) for t in net.trafo.index}
        elif name == "substation_trafos":
            # a "substation" here = a transformer's LV busbar meter + the trafo meter
            self.trafo_idxs = {int(t) for t in net.trafo.index}
            self.node_buses |= {int(net.trafo.at[t, "lv_bus"]) for t in net.trafo.index}
        elif name == "digital_stations":
            # one digital ONS per cell: the station-trafo meter. Model stand-ins
            # where no trafo exists: a lumped station is only its aggregate load
            # at the feeding MV bus -> node meter there; a trafo-less standalone
            # LV cell (ding0 "lv" scope) is fed at its busbar -> node meter.
            for c in cells or []:
                if c.get("station_trafos"):
                    self.trafo_idxs |= {int(t) for t in c["station_trafos"]}
                elif c.get("lumped") and c.get("mv_bus") is not None:
                    self.node_buses.add(int(c["mv_bus"]))
                elif c.get("lv_busbar") is not None:
                    self.node_buses.add(int(c["lv_busbar"]))
        elif name == "cell_full":
            # full SMGW rollout of ONE cell (incl. its digital station)
            match = next((c for c in cells or [] if c.get("id") == cell), None)
            if match is None:
                raise KeyError(cell)
            self.node_buses |= {int(b) for b in match.get("buses", [])}
            self.trafo_idxs |= {int(t) for t in match.get("station_trafos", [])}
        else:  # pragma: no cover - guarded by the API layer
            raise ValueError(f"unknown preset '{name}'")

    def prune(self, net) -> None:
        """Drop placements whose element no longer exists (after a grid swap)."""
        self.node_buses &= {int(b) for b in net.bus.index}
        self.trafo_idxs &= {int(t) for t in net.trafo.index}
        self.node_modes = {b: m for b, m in self.node_modes.items() if b in self.node_buses}
        self.trafo_modes = {t: m for t, m in self.trafo_modes.items() if t in self.trafo_idxs}

    # -- the projection: solved net → observed readings ----------------------- #
    def placement(self, net, cells: list[dict] | None = None) -> dict[str, Any]:
        """Static placement + coverage (no results needed) for the /measurements
        endpoint and the UI's meter markers. With ``cells`` given, the coverage
        is additionally broken down per ONS cell — the vertical view: does a
        cell have its station measurement, how many of its buses carry SMGWs?"""
        n_bus = int(len(net.bus))
        n_trafo = int(len(net.trafo))
        n_node_meter = len(self.node_buses)
        n_trafo_meter = len(self.trafo_idxs)
        cell_cov = []
        for c in cells or []:
            member = set(int(b) for b in c.get("buses", []))
            trafos = [int(t) for t in c.get("station_trafos", [])]
            if trafos:
                station = all(t in self.trafo_idxs for t in trafos)
            elif c.get("lumped") and c.get("mv_bus") is not None:
                station = int(c["mv_bus"]) in self.node_buses
            else:
                station = (c.get("lv_busbar") is not None
                           and int(c["lv_busbar"]) in self.node_buses)
            cell_cov.append({
                "id": c["id"], "lumped": bool(c.get("lumped")),
                "n_buses": len(member),
                "n_node_meter": len(member & self.node_buses),
                "station_metered": bool(station),
            })
        return {
            "node_buses": sorted(self.node_buses),
            "trafo_idxs": sorted(self.trafo_idxs),
            "mode": self.mode,
            # full per-device map (default resolved), for the UI's TAF switches
            "node_modes": {b: self.mode_of_node(b) for b in sorted(self.node_buses)},
            "trafo_modes": {tr: self.mode_of_trafo(tr) for tr in sorted(self.trafo_idxs)},
            "cells": cell_cov,
            "coverage": {
                "n_bus": n_bus,
                "n_node_meter": n_node_meter,
                "n_trafo": n_trafo,
                "n_trafo_meter": n_trafo_meter,
                "node_fraction": round(n_node_meter / n_bus, 4) if n_bus else 0.0,
                "trafo_fraction": round(n_trafo_meter / n_trafo, 4) if n_trafo else 0.0,
            },
        }

    def observe(self, net, t: int = 0) -> dict[str, Any]:
        """Project the just-solved ``net`` down to what the meters can see.

        Returns ``{nodes, trafos, coverage}``. ``nodes`` / ``trafos`` are present
        only for placed devices; everything else is, by construction, unknown.
        Devices are read PER MODE: a "full" device (TAF 9/10/14) delivers
        V/P/Q/I of the current step, a "standard" device (TAF 7 Lastgang)
        only the ACTIVE power as the mean over the last COMPLETED 15-minute
        window — no voltage, no reactive power, and strictly no intra-window
        updates: until the first window completes (also right after placing
        or re-switching a device) the reading is simply absent. That keeps
        the granularity honest — a Lastgang meter never emits 1-min data.
        Both kinds mix freely in one system: TAF-7 household meters next to
        1-min SMGWs at plants and wallboxes.
        Assumes the net has fresh ``res_*`` tables (call after a converged
        solve); ``t`` is the step within the day (window bookkeeping)."""
        w = t // 15
        if w != self._win:                      # window boundary: publish means
            if self._acc:
                self._held = {k: s / n for k, (s, n) in self._acc.items()}
            self._acc = {}
            self._win = w

        def tick(key, value: float):
            s_, n_ = self._acc.get(key, (0.0, 0))
            self._acc[key] = (s_ + value, n_ + 1)
            return self._held.get(key)          # None until a window completed

        nodes: list[dict[str, Any]] = []
        for bus in sorted(self.node_buses):
            if bus not in net.res_bus.index:
                continue
            if self.mode_of_node(bus) == "standard":
                p = tick(("n", bus), float(net.res_bus.at[bus, "p_mw"]))
                nodes.append({"bus": int(bus), "name": str(net.bus.at[bus, "name"]),
                              "vm_pu": None, "v_ll_kv": None, "p_mw": _r(p),
                              "q_mvar": None, "s_mva": None, "i_ka": None})
                continue
            vm_pu = float(net.res_bus.at[bus, "vm_pu"])
            p_mw = float(net.res_bus.at[bus, "p_mw"])
            q_mvar = float(net.res_bus.at[bus, "q_mvar"])
            vn_kv = float(net.bus.at[bus, "vn_kv"])
            v_ll_kv = vm_pu * vn_kv                       # line-to-line voltage
            s_mva = math.hypot(p_mw, q_mvar)              # three-phase apparent power
            # balanced three-phase current magnitude I = S / (√3 · V_LL)
            i_ka = s_mva / (math.sqrt(3.0) * v_ll_kv) if v_ll_kv > 1e-9 else 0.0
            nodes.append({
                "bus": int(bus),
                "name": str(net.bus.at[bus, "name"]),
                "vm_pu": _r(vm_pu),
                "v_ll_kv": _r(v_ll_kv),
                "p_mw": _r(p_mw),           # Σ three phases
                "q_mvar": _r(q_mvar),       # Σ three phases
                "s_mva": _r(s_mva),
                "i_ka": _r(i_ka),           # per-phase line current (balanced)
            })
        trafos: list[dict[str, Any]] = []
        for tr in sorted(self.trafo_idxs):
            if tr not in net.res_trafo.index:
                continue
            base = {"trafo": int(tr), "name": str(net.trafo.at[tr, "name"]),
                    "hv_bus": int(net.trafo.at[tr, "hv_bus"]),
                    "lv_bus": int(net.trafo.at[tr, "lv_bus"])}
            if self.mode_of_trafo(tr) == "standard":
                p = tick(("t", tr), float(net.res_trafo.at[tr, "p_hv_mw"]))
                trafos.append({**base, "loading_percent": None, "p_hv_mw": _r(p),
                               "q_hv_mvar": None, "i_hv_ka": None, "pl_mw": None})
            else:
                trafos.append({**base,
                               "loading_percent": _r(net.res_trafo.at[tr, "loading_percent"]),
                               "p_hv_mw": _r(net.res_trafo.at[tr, "p_hv_mw"]),
                               "q_hv_mvar": _r(net.res_trafo.at[tr, "q_hv_mvar"]),
                               "i_hv_ka": _r(net.res_trafo.at[tr, "i_hv_ka"]),
                               "pl_mw": _r(net.res_trafo.at[tr, "pl_mw"])})
        return {
            "nodes": nodes,
            "trafos": trafos,
            "coverage": self.placement(net)["coverage"],
            # metadata: reminds consumers the per-phase model is balanced
            "phases": 3,
            "balanced": True,
        }

    def observed_summary(self, observed: dict[str, Any]) -> dict[str, Any]:
        """Aggregate over *observed* elements only — the operator's view of the
        system, which may differ sharply from the true system-wide summary."""
        nodes = observed["nodes"]
        trafos = observed["trafos"]
        vms = [n["vm_pu"] for n in nodes if n["vm_pu"] is not None]
        tls = [t["loading_percent"] for t in trafos if t["loading_percent"] is not None]
        ps = [n["p_mw"] for n in nodes if n["p_mw"] is not None]
        return {
            "vm_pu_min": min(vms) if vms else None,
            "vm_pu_max": max(vms) if vms else None,
            "max_trafo_loading_percent": max(tls) if tls else None,
            "measured_node_p_mw": _r(sum(ps)) if ps else None,
            **observed["coverage"],
        }
