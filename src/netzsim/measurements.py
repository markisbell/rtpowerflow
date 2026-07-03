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
# endpoint. Kept here so the API and the set agree on the vocabulary.
PRESETS = ("all_nodes", "all_trafos", "substation_trafos", "clear")

# Meter fidelity. "full": every quantity (V, P, Q, I) every simulation step —
# a research-grade device. "standard": German standard metering ("Lastgang"):
# only the ACTIVE power, as the mean over each completed 15-minute window.
METER_MODES = ("full", "standard")


def _r(value, ndigits: int = 6):
    """Round to a JSON-safe float; non-finite → ``None`` (mirrors simulator._r)."""
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
    mode: str = "full"                    # see METER_MODES
    # standard-mode window state: running accumulator + last completed window
    _win: int = field(default=-1, repr=False)
    _acc: dict = field(default_factory=dict, repr=False)   # key -> (sum, n)
    _held: dict = field(default_factory=dict, repr=False)  # key -> window mean

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
        return True

    def clear(self) -> None:
        self.node_buses.clear()
        self.trafo_idxs.clear()
        self._win, self._acc, self._held = -1, {}, {}

    def set_mode(self, name: str) -> None:
        if name not in METER_MODES:
            raise ValueError(f"unknown meter mode '{name}'")
        if name != self.mode:
            self.mode = name
            self._win, self._acc, self._held = -1, {}, {}

    def apply_preset(self, name: str, net) -> None:
        """Bulk placement helper. ``net`` supplies the element indices."""
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
        else:  # pragma: no cover - guarded by the API layer
            raise ValueError(f"unknown preset '{name}'")

    def prune(self, net) -> None:
        """Drop placements whose element no longer exists (after a grid swap)."""
        self.node_buses &= {int(b) for b in net.bus.index}
        self.trafo_idxs &= {int(t) for t in net.trafo.index}

    # -- the projection: solved net → observed readings ----------------------- #
    def placement(self, net) -> dict[str, Any]:
        """Static placement + coverage (no results needed) for the /measurements
        endpoint and the UI's meter markers."""
        n_bus = int(len(net.bus))
        n_trafo = int(len(net.trafo))
        n_node_meter = len(self.node_buses)
        n_trafo_meter = len(self.trafo_idxs)
        return {
            "node_buses": sorted(self.node_buses),
            "trafo_idxs": sorted(self.trafo_idxs),
            "mode": self.mode,
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
        Assumes the net has fresh ``res_*`` tables (call after a converged solve).
        ``t`` is the step within the day — standard metering aggregates by it.
        """
        if self.mode == "standard":
            return self._observe_standard(net, t)
        nodes: list[dict[str, Any]] = []
        for bus in sorted(self.node_buses):
            if bus not in net.res_bus.index:
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
            trafos.append({
                "trafo": int(tr),
                "name": str(net.trafo.at[tr, "name"]),
                "hv_bus": int(net.trafo.at[tr, "hv_bus"]),
                "lv_bus": int(net.trafo.at[tr, "lv_bus"]),
                "loading_percent": _r(net.res_trafo.at[tr, "loading_percent"]),
                "p_hv_mw": _r(net.res_trafo.at[tr, "p_hv_mw"]),
                "q_hv_mvar": _r(net.res_trafo.at[tr, "q_hv_mvar"]),
                "i_hv_ka": _r(net.res_trafo.at[tr, "i_hv_ka"]),
                "pl_mw": _r(net.res_trafo.at[tr, "pl_mw"]),
            })
        return {
            "nodes": nodes,
            "trafos": trafos,
            "coverage": self.placement(net)["coverage"],
            # metadata: reminds consumers the per-phase model is balanced
            "phases": 3,
            "balanced": True,
        }

    def _observe_standard(self, net, t: int) -> dict[str, Any]:
        """Standard metering ("Lastgang"): each device delivers only the ACTIVE
        power as the mean over the last completed 15-minute window — no voltage,
        no reactive power, no current, no intra-window updates. Until the first
        window completes, the running mean so far is reported."""
        w = t // 15
        if w != self._win:                      # window boundary: publish means
            if self._acc:
                self._held = {k: s / n for k, (s, n) in self._acc.items()}
            self._acc = {}
            self._win = w

        def tick(key, value: float):
            s_, n_ = self._acc.get(key, (0.0, 0))
            self._acc[key] = (s_ + value, n_ + 1)
            held = self._held.get(key)
            return held if held is not None else (s_ + value) / (n_ + 1)

        nodes: list[dict[str, Any]] = []
        for bus in sorted(self.node_buses):
            if bus not in net.res_bus.index:
                continue
            p = tick(("n", bus), float(net.res_bus.at[bus, "p_mw"]))
            nodes.append({"bus": int(bus), "name": str(net.bus.at[bus, "name"]),
                          "vm_pu": None, "v_ll_kv": None, "p_mw": _r(p),
                          "q_mvar": None, "s_mva": None, "i_ka": None})
        trafos: list[dict[str, Any]] = []
        for tr in sorted(self.trafo_idxs):
            if tr not in net.res_trafo.index:
                continue
            p = tick(("t", tr), float(net.res_trafo.at[tr, "p_hv_mw"]))
            trafos.append({"trafo": int(tr), "name": str(net.trafo.at[tr, "name"]),
                           "hv_bus": int(net.trafo.at[tr, "hv_bus"]),
                           "lv_bus": int(net.trafo.at[tr, "lv_bus"]),
                           "loading_percent": None, "p_hv_mw": _r(p),
                           "q_hv_mvar": None, "i_hv_ka": None, "pl_mw": None})
        return {"nodes": nodes, "trafos": trafos,
                "coverage": self.placement(net)["coverage"],
                "phases": 3, "balanced": True}

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
