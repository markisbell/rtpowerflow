"""External nodes: a live P/Q feed for individual buses (fully controlled
nodes) — design in ``docs/EXTERNAL_NODES.md`` (v1 decisions 2026-07-10).

External clients PUSH setpoints (``PUT /ext/{id}/value``); the engine reads
the LATEST value per tick, non-blocking (mailbox + sample-and-hold — plain
attribute writes, no locks per project convention: a torn frame heals on
the next step). An external node is a pandapower load row with a ZEROED
profile row (the ``der.add_ev`` pattern) that every step overrides from the
mailbox — signed P (+ = consumption, − = feed-in), optional Q.

Every function takes the live ``sim`` as first argument; the ``Simulator``
keeps thin delegates and owns all state (``sim.ext_nodes``), so grid swaps
reset placements, the bulk exporter's deepcopy carries them, and scenario
recipes can persist placements (not values) later."""
from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np
import pandapower as pp

from . import der

TIMEOUT_POLICIES = ("hold", "zero")


@dataclass
class ExternalNode:
    eid: int
    bus: int
    name: str
    hold_s: float = 30.0            # telegram older than this => stale
    on_timeout: str = "hold"        # "hold" keeps the last value, "zero" drops to 0
    # value bound (kW): the API rejects beyond it AND the estimator uses it
    # as the width of this bus's rating-bounded pseudo-measurement (a zeroed
    # profile would otherwise pin the WLS near 0 kW, overconfident + wrong)
    p_max_kw: float = 50.0
    # -- mailbox: latest received setpoint (sample-and-hold) --------------- #
    p_mw: float = 0.0
    q_mvar: float = 0.0
    t_received: float | None = None      # time.monotonic() of the last push
    # full-day ring of APPLIED values in kW (decision #3: 1440 slots, last
    # value per step) — the node's day graph in phase 2 renders this
    history: list = field(default_factory=list)
    load_idx: int | None = None          # pandapower load row ("EXT_<bus>")

    def age_s(self, now: float | None = None) -> float | None:
        if self.t_received is None:
            return None
        return (time.monotonic() if now is None else now) - self.t_received

    def is_stale(self, now: float | None = None) -> bool:
        """Never-fed counts as stale — a silent feed must be visible."""
        age = self.age_s(now)
        return age is None or age > self.hold_s

    def applied(self, now: float | None = None) -> tuple[float, float, bool]:
        """The (p_mw, q_mvar, stale) this step actually uses."""
        stale = self.is_stale(now)
        if stale and self.on_timeout == "zero":
            return 0.0, 0.0, True
        return self.p_mw, self.q_mvar, stale

    def as_dict(self, now: float | None = None) -> dict:
        p, q, stale = self.applied(now)
        age = self.age_s(now)
        return {"id": self.eid, "bus": self.bus, "name": self.name,
                "p_kw": round(p * 1000.0, 3), "q_kvar": round(q * 1000.0, 3),
                "age_s": round(age, 1) if age is not None else None,
                "stale": stale, "hold_s": self.hold_s,
                "on_timeout": self.on_timeout, "p_max_kw": self.p_max_kw}


def add_ext_node(sim, bus: int, name: str | None = None, hold_s: float = 30.0,
                 on_timeout: str = "hold", p_max_kw: float = 50.0) -> ExternalNode:
    """Attach an external node at a bus: a load row with a zeroed profile
    row, overridden from the mailbox each step. One per bus."""
    if bus not in sim.net.bus.index:
        raise KeyError(bus)
    if any(x.bus == int(bus) for x in sim.ext_nodes):
        raise ValueError(f"bus {bus} already has an external node")
    if on_timeout not in TIMEOUT_POLICIES:
        raise ValueError(f"on_timeout must be one of {TIMEOUT_POLICIES}")
    li = int(pp.create_load(sim.net, bus=bus, p_mw=0.0, q_mvar=0.0,
                            name=f"EXT_{bus}"))
    sim.prof.load_idx.append(li)
    sim.prof.load_p = np.vstack([sim.prof.load_p, np.zeros((1, sim.steps_per_day))])
    sim.prof.load_q = np.vstack([sim.prof.load_q, np.zeros((1, sim.steps_per_day))])
    sim._load_household.append(False)    # externally driven, not a household
    sim._load_households.append(1)
    x = ExternalNode(eid=sim._next_ext_id, bus=int(bus),
                     name=name or f"EXT_{bus}", hold_s=float(hold_s),
                     on_timeout=on_timeout, p_max_kw=float(p_max_kw),
                     history=[None] * sim.steps_per_day, load_idx=li)
    sim._next_ext_id += 1
    sim.ext_nodes.append(x)
    der._der_invalidate(sim)
    return x


def remove_ext_node(sim, eid: int) -> bool:
    x = next((n for n in sim.ext_nodes if n.eid == eid), None)
    if x is None:
        return False
    if x.load_idx is not None and x.load_idx in sim.prof.load_idx:
        i = sim.prof.load_idx.index(x.load_idx)
        sim.prof.load_idx.pop(i)
        sim.prof.load_p = np.delete(sim.prof.load_p, i, axis=0)
        sim.prof.load_q = np.delete(sim.prof.load_q, i, axis=0)
        if i < len(sim._load_household):
            sim._load_household.pop(i)
        if i < len(sim._load_households):
            sim._load_households.pop(i)
        sim.net.load.drop(x.load_idx, inplace=True)
    sim.ext_nodes.remove(x)
    der._der_invalidate(sim)
    return True


def set_ext_value(sim, eid: int, p_kw: float, q_kvar: float = 0.0) -> ExternalNode | None:
    """The hot path: store the pushed setpoint in the mailbox (latest wins)."""
    x = next((n for n in sim.ext_nodes if n.eid == eid), None)
    if x is None:
        return None
    if abs(float(p_kw)) > x.p_max_kw:
        raise ValueError(f"|p_kw| exceeds the node's bound of {x.p_max_kw} kW")
    x.p_mw = float(p_kw) / 1000.0
    x.q_mvar = float(q_kvar or 0.0) / 1000.0
    x.t_received = time.monotonic()
    return x


def apply_ext_nodes(sim, t: int, now: float | None = None) -> None:
    """Override each external node's load row with its mailbox value —
    called by ``_apply_step`` AFTER the profile columns (which zero the row)
    and BEFORE controller factors (which must not touch external nodes)."""
    now = time.monotonic() if now is None else now
    for x in sim.ext_nodes:
        if x.load_idx is None or x.load_idx not in sim.net.load.index:
            continue
        p, q, _stale = x.applied(now)
        sim.net.load.at[x.load_idx, "p_mw"] = p
        sim.net.load.at[x.load_idx, "q_mvar"] = q
        if 0 <= t < len(x.history):
            x.history[t] = round(p * 1000.0, 3)


def reset_ext_values(sim) -> None:
    """Deterministic replay (bulk exporter): forget the live mailbox — the
    nodes start silent (stale) and hold/zero their 0-kW initial value, so an
    export never depends on what a live feed happened to send."""
    for x in sim.ext_nodes:
        x.p_mw = x.q_mvar = 0.0
        x.t_received = None
        x.history = [None] * sim.steps_per_day
