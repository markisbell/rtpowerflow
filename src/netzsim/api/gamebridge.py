"""Gamebridge: the co-simulation contract v1 surface for an external game clock.

Spec: simgames ``docs/contract/v1.md`` (authoritative) + the JSON Schemas in
``docs/contract/schemas/``. The game owns time; netzsim never self-advances in
puppet mode (``NETZSIM_EXTERNAL_CLOCK=true``). Divergence is data, never an
HTTP error — a failed solve returns ``status: "failed"`` with HTTP 200 and the
game maps it to gameplay events. The one 4xx in the step path is an
out-of-order ``t`` (HTTP 409 / WS ``error: "out_of_order"`` frame).

Surface (contract §1): control plane over HTTP — ``/gb/version``,
``/gb/net/reset``, ``/gb/net/patch``, ``/gb/result/latest`` — plus the step
channel WebSocket ``/gb/ws`` (one text frame in = one step request, one text
frame out = the step result, strictly sequential). ``POST /gb/step`` is the
identically-behaving debugging fallback; called with an EMPTY body it keeps
the pre-contract behavior (advance one step, return the native StepResult).

How the contract maps onto netzsim:

- ``/gb/net/reset`` takes the topology document, EXTENDS its ``native``
  five-doc GridInputs by one zero-profile load row per ZONE (the supply-zone
  demand rows, id-mapped) and by the game-controlled DEVICES (slack →
  ext_grid, created if native lacks one; generator/pv/wind → zero-profile
  sgen rows; coupling_load → zero-profile load row), then swaps the grid via
  ``engine.reconfigure(data, autostart=False)``. ``steps_per_day`` comes from
  the document and is authoritative — every native profile array must match
  it (the pydantic models enforce that, → HTTP 400).
- Stepping is sample-and-hold (§4): each request's ``zone_demand`` /
  ``device_setpoints`` / ``coupling_in`` update HELD values; every held value
  is then written into the profile arrays at the CURRENT engine step index
  (clamped to device bounds, reported as ``clamped`` violations) before
  ``engine.external_step()`` solves and publishes. The native StepResult
  stream (``/state``, ``/ws``, ``/history``) keeps working unchanged.
- Warmup (contract §0.5, ADR-003): reset fires one THROWAWAY
  ``sim.run_step(0, 0)`` — it JIT-compiles numba and primes the warm start,
  but does NOT advance the engine clock and is NOT published to the store
  (the store was just cleared by ``reconfigure`` anyway), so the first real
  step still solves index 0 with the game's actual boundary conditions.
- Real-PV day shapes are detached from the game grid (``set_pv_days(None)``):
  the game computes PV/wind availability itself and sends setpoints —
  generation *models* live game-side, network *physics* backend-side.
"""
from __future__ import annotations

import asyncio
import copy
import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np
import pandapower as pp
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .. import der
from ..config import settings
from ..data_loader import input_data_from_dicts
from ..measurements import _r  # canonical JSON-safe rounding
from ..simulator import StepResult
from .runtime import API_VERSION, runtime

router = APIRouter(prefix="/gb", tags=["gamebridge"])

CONTRACT_VERSION = "1.0"
NETWORK_KIND = "power"

# Device kinds this backend accepts (contract §3.1 table, "power" rows).
# battery is DECLARED in v1 but implemented in Phase 3 — accepted, idle.
_GEN_KINDS = ("generator", "pv", "wind")
_POWER_KINDS = ("slack", *_GEN_KINDS, "coupling_load", "battery")
_ALL_KINDS = (*_POWER_KINDS, "chp", "heat_pump", "boiler", "storage_heat")
_CAP_PARAM = {"generator": "p_max_kw", "pv": "p_rated_kw", "wind": "p_rated_kw"}

# Violation thresholds (contract §4): line/trafo loading % and vm_pu band.
_OVERLOAD_WARN = 100.0
_OVERLOAD_CRIT = 120.0
_V_BAND = (0.95, 1.05)
_V_CRIT = (0.90, 1.10)


# ------------------------------------------------------------------ gb state

@dataclass
class GbZone:
    """One supply zone (ROADMAP §2.4) mapped onto its zero-profile load row."""

    id: str
    node: str
    bus: int
    load_idx: int                     # pandapower load element index


@dataclass
class GbDevice:
    """One game-controlled device mapped onto its pandapower element."""

    id: str
    kind: str
    node: str
    bus: int
    params: dict
    table: Optional[str]              # "sgen" | "load" | "ext_grid" | None
    elem: Optional[int]               # pandapower element index (None: battery)


@dataclass
class GbState:
    """Everything the step path needs, held module-level on the runtime App
    (``runtime.gb``). Rebuilt by every ``/gb/net/reset``; profile ROW indices
    are resolved per step from the element indices, so ``/gb/net/patch``
    add/remove (which shifts rows) can never desynchronize the mapping."""

    doc: dict                         # the topology document, verbatim
    bus_of: "dict[str, int]"          # native node name -> bus index
    zones: "dict[str, GbZone]"
    devices: "dict[str, GbDevice]"
    zone_demand_kw: "dict[str, float]"        # sample-and-hold, default 0.0
    held: "dict[str, dict]" = field(default_factory=dict)  # device setpoints
    last_t: Optional[int] = None      # idempotency cache (contract §0.3)
    last_result: Optional[dict] = None


# ----------------------------------------------------------------- validation

def _bad(msg: str) -> None:
    raise HTTPException(status_code=400, detail=msg)


async def _json_body(request: Request) -> Any:
    try:
        return json.loads(await request.body())
    except json.JSONDecodeError:
        _bad("body is not valid JSON")


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _as_int(v: Any) -> Optional[int]:
    """JSON-semantics integer: a real int, or a float with zero fractional
    part (JSON Schema "integer"; e.g. Godot's JSON layer serializes every
    number as a float, so 96 arrives as 96.0). None if not an integer."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return None


def _device_error(dev: Any, bus_of: "dict[str, int] | None" = None) -> str | None:
    """Error string if ``dev`` is not a valid POWER device spec, else None."""
    if not isinstance(dev, dict):
        return "device must be an object"
    if not isinstance(dev.get("id"), str) or not dev["id"]:
        return "device needs a non-empty string 'id'"
    kind = dev.get("kind")
    if kind not in _ALL_KINDS:
        return f"unknown device kind {kind!r}"
    if kind not in _POWER_KINDS:
        return f"device kind {kind!r} is not a power-network device (contract §3.1)"
    if "params" in dev and dev["params"] is not None and not isinstance(dev["params"], dict):
        return "'params' must be an object"
    node = dev.get("node")
    if not isinstance(node, str) or not node:
        return "power devices need a 'node' (bus name in native.grid_structure)"
    if bus_of is not None and node not in bus_of:
        return f"unknown node {node!r}"
    return None


def _validate_topology(doc: Any) -> None:
    """Structural validation of the reset body (schemas/topology.schema.json);
    every failure is HTTP 400 with a ``detail`` (contract §3.1)."""
    if not isinstance(doc, dict):
        _bad("topology document must be a JSON object")
    for key in ("contract", "network_kind", "name", "steps_per_day",
                "native", "zones", "devices"):
        if key not in doc:
            _bad(f"topology document missing required key '{key}'")
    major, _, minor = str(doc["contract"]).partition(".")
    if major != CONTRACT_VERSION.partition(".")[0] or not minor.isdigit() \
            or int(minor) > int(CONTRACT_VERSION.partition(".")[2]):
        _bad(f"unsupported contract version {doc['contract']!r} "
             f"(this backend speaks {CONTRACT_VERSION})")
    if doc["network_kind"] != NETWORK_KIND:
        _bad(f"network_kind {doc['network_kind']!r} not served — this backend "
             f"simulates the {NETWORK_KIND!r} network")
    if not isinstance(doc["name"], str) or not doc["name"]:
        _bad("'name' must be a non-empty string")
    spd = _as_int(doc["steps_per_day"])
    if spd is None or not (24 <= spd <= 1440):
        _bad("'steps_per_day' must be an integer in [24, 1440]")
    doc["steps_per_day"] = spd  # canonical int downstream
    if not isinstance(doc["native"], dict):
        _bad("'native' must be an object (the five-doc GridInputs shape)")
    if not isinstance(doc["zones"], list):
        _bad("'zones' must be an array")
    seen: set = set()
    for i, zone in enumerate(doc["zones"]):
        if not isinstance(zone, dict) or not isinstance(zone.get("id"), str) \
                or not zone["id"]:
            _bad(f"zones[{i}] must be an object with a non-empty string 'id'")
        if zone["id"] in seen:
            _bad(f"duplicate zone id {zone['id']!r}")
        seen.add(zone["id"])
        if not isinstance(zone.get("node"), str) or not zone["node"]:
            _bad(f"zone '{zone['id']}': power zones need a 'node' "
                 f"(bus name in native.grid_structure)")
    if not isinstance(doc["devices"], list):
        _bad("'devices' must be an array")
    seen = set()
    for i, dev in enumerate(doc["devices"]):
        err = _device_error(dev)
        if err:
            _bad(f"devices[{i}]: {err}")
        if dev["id"] in seen:
            _bad(f"duplicate device id {dev['id']!r}")
        seen.add(dev["id"])


# --------------------------------------------------- native document extension

def _extend_native(doc: dict) -> "tuple[dict, dict, list, list, list]":
    """Extend the verbatim ``native`` five-doc GridInputs by the game layer.

    Returns ``(docs, bus_of, zone_rows, dev_rows, warnings)`` where ``docs``
    feeds :func:`data_loader.input_data_from_dicts`, ``zone_rows`` is
    ``[(zone_id, node, bus, load_row)]`` and ``dev_rows`` is
    ``[(device_spec, bus, table, row)]`` (row = position in the doc's element
    list == pandapower element index on the fresh build).
    """
    native = doc["native"]
    steps = doc["steps_per_day"]
    grid = copy.deepcopy(native.get("grid_structure"))
    lines = copy.deepcopy(native.get("lines"))
    if not isinstance(grid, dict) or not isinstance(grid.get("buses"), list):
        _bad("native.grid_structure with a 'buses' list is required "
             "(five-doc GridInputs, contract §3.1)")
    if not isinstance(lines, dict):
        _bad("native.lines is required (five-doc GridInputs, contract §3.1)")

    def _profile_doc(key: str, items_keys: "tuple[str, ...]") -> "tuple[dict, str]":
        d = copy.deepcopy(native.get(key))
        if d is None:
            d = {}
        if not isinstance(d, dict):
            _bad(f"native.{key} must be an object")
        # steps_per_day from the topology document is authoritative; the
        # pydantic models then enforce that every profile array matches it.
        d["steps"] = steps
        d["resolution_minutes"] = max(1, 1440 // steps)
        items_key = next((k for k in items_keys if k in d), items_keys[0])
        d.setdefault(items_key, [])
        if not isinstance(d[items_key], list):
            _bad(f"native.{key}.{items_key} must be an array")
        return d, items_key

    load_doc, load_key = _profile_doc("load", ("loads",))
    gen_doc, gen_key = _profile_doc("generation", ("generation", "gens"))
    sub_doc, sub_key = _profile_doc("substation", ("substations",))

    bus_of: "dict[str, int]" = {}
    for i, b in enumerate(grid["buses"]):
        name = b.get("name") if isinstance(b, dict) else None
        if isinstance(name, str) and name not in bus_of:
            bus_of[name] = i

    zone_rows: list = []
    for z in doc["zones"]:
        node = z["node"]
        if node not in bus_of:
            _bad(f"zone '{z['id']}': unknown node {node!r}")
        row = len(load_doc[load_key])
        load_doc[load_key].append({"name": f"GBZ_{z['id']}", "bus": bus_of[node],
                                   "p_mw": [0.0] * steps, "household": False})
        zone_rows.append((z["id"], node, bus_of[node], row))

    warnings: "list[str]" = []
    dev_rows: list = []
    claimed: "set[int]" = set()
    for dev in doc["devices"]:
        err = _device_error(dev, bus_of)
        if err:
            _bad(f"device '{dev.get('id')}': {err}")
        kind, bus = dev["kind"], bus_of[dev["node"]]
        params = dev.get("params") or {}
        if kind == "slack":
            # Map onto a native substation (ext_grid) if one exists — same
            # bus preferred — else create one from the device (contract §3.1).
            row = next((i for i, s in enumerate(sub_doc[sub_key])
                        if i not in claimed and isinstance(s, dict)
                        and s.get("bus") == bus), None)
            if row is None:
                row = next((i for i in range(len(sub_doc[sub_key]))
                            if i not in claimed), None)
                if row is not None:
                    warnings.append(
                        f"slack '{dev['id']}': no native substation at node "
                        f"{dev['node']!r} — mapped onto substation row {row}")
            if row is None:
                row = len(sub_doc[sub_key])
                sub_doc[sub_key].append({
                    "name": f"GBD_{dev['id']}", "bus": bus,
                    "vm_pu": [float(params.get("vm_pu", 1.0))] * steps})
            claimed.add(row)
            dev_rows.append((dev, bus, "ext_grid", row))
        elif kind in _GEN_KINDS:
            # GenProfile.kind "game": never follows the real-PV day shapes —
            # the game computes availability itself and sends setpoints.
            row = len(gen_doc[gen_key])
            gen_doc[gen_key].append({"name": f"GBD_{dev['id']}", "bus": bus,
                                     "p_mw": [0.0] * steps, "kind": "game"})
            dev_rows.append((dev, bus, "sgen", row))
        elif kind == "coupling_load":
            row = len(load_doc[load_key])
            load_doc[load_key].append({"name": f"GBD_{dev['id']}", "bus": bus,
                                       "p_mw": [0.0] * steps, "household": False})
            dev_rows.append((dev, bus, "load", row))
        else:  # battery — declared in v1, implemented in Phase 3
            warnings.append(f"device '{dev['id']}': kind 'battery' is declared "
                            "in contract v1 but implemented in Phase 3 — idle")
            dev_rows.append((dev, bus, None, None))

    docs = dict(grid=grid, lines=lines, load=load_doc,
                generation=gen_doc, substation=sub_doc)
    return docs, bus_of, zone_rows, dev_rows, warnings


# ------------------------------------------------------------------ endpoints

@router.get("/version")
def version():
    """Contract handshake (contract §2): the game requires identical MAJOR
    and game-minor <= backend-minor, and refuses to run on a mismatch."""
    return {
        "contract": CONTRACT_VERSION,
        "backend": "netzsim",
        "api": API_VERSION,
        "solver": f"pandapower {pp.__version__}",
        "external_clock": settings.external_clock,
    }


@router.post("/net/reset")
async def net_reset(request: Request):
    """Load a game topology (contract §3.1): validate the document, build the
    extended GridInputs, swap the grid via ``engine.reconfigure`` and fire the
    throwaway warmup solve. Clears ``last_t`` — the next step may carry any
    ``t`` (the game clock continues across resets)."""
    doc = await _json_body(request)
    _validate_topology(doc)
    docs, bus_of, zone_rows, dev_rows, warnings = _extend_native(doc)
    try:
        data = input_data_from_dicts(**docs)
    except (ValidationError, ValueError) as exc:
        _bad(f"invalid native document: {exc}")

    engine = runtime.engine
    if engine.status["running"]:
        raise HTTPException(409, "internal clock is running — pause it first")
    await engine.reconfigure(data, autostart=False)
    sim = engine.sim
    # game topologies are verbatim: no real-PV day-shape override (see module doc)
    sim.set_pv_days(None)

    zones = {zid: GbZone(id=zid, node=node, bus=bus,
                         load_idx=int(sim.prof.load_idx[row]))
             for zid, node, bus, row in zone_rows}
    devices: "dict[str, GbDevice]" = {}
    for dev, bus, table, row in dev_rows:
        elem = None
        if table == "sgen":
            elem = int(sim.prof.sgen_idx[row])
        elif table == "load":
            elem = int(sim.prof.load_idx[row])
        elif table == "ext_grid":
            elem = int(sim.prof.ext_idx[row])
        devices[dev["id"]] = GbDevice(
            id=dev["id"], kind=dev["kind"], node=dev["node"], bus=bus,
            params=dict(dev.get("params") or {}), table=table, elem=elem)

    # JIT warmup (contract §0.5): one throwaway solve so numba compilation
    # never lands on a live step. run_step is called DIRECTLY — the engine
    # clock stays at step 0 and nothing is published.
    t0 = time.perf_counter()
    await asyncio.to_thread(sim.run_step, 0, 0)
    warmup_ms = (time.perf_counter() - t0) * 1000.0

    runtime.gb = GbState(doc=doc, bus_of=bus_of, zones=zones, devices=devices,
                         zone_demand_kw={zid: 0.0 for zid in zones})
    return {"ok": True, "network_kind": NETWORK_KIND, "n_zones": len(zones),
            "n_devices": len(devices), "warmup_solve_ms": round(warmup_ms, 3),
            "warnings": warnings}


@router.post("/net/patch")
async def net_patch(request: Request):
    """Device ops (contract §3.2), tolerant per entry: applied ops stay
    applied even if later ops fail. v1 scope is device ops only — node/edge/
    zone changes go through a full reset."""
    gb: GbState | None = getattr(runtime, "gb", None)
    if gb is None:
        _bad("no game topology loaded — POST /gb/net/reset first")
    ops = await _json_body(request)
    if not isinstance(ops, list):
        _bad("patch body must be a JSON array of ops")
    applied: "list[str]" = []
    errors: "list[dict]" = []
    for i, op in enumerate(ops):
        try:
            applied.append(_apply_patch_op(gb, runtime.engine.sim, op))
        except ValueError as exc:
            errors.append({"index": i, "error": str(exc)})
    return {"applied": applied, "errors": errors}


def _apply_patch_op(gb: GbState, sim, op: Any) -> str:
    if not isinstance(op, dict):
        raise ValueError("op must be an object")
    name = op.get("op")
    if name == "add_device":
        dev = op.get("device")
        err = _device_error(dev, gb.bus_of)
        if err:
            raise ValueError(err)
        if dev["id"] in gb.devices:
            raise ValueError(f"duplicate device {dev['id']}")
        if dev["kind"] == "slack":
            raise ValueError("the slack is part of the topology — use /gb/net/reset")
        gb.devices[dev["id"]] = _create_device_element(gb, sim, dev)
        return dev["id"]
    if name == "remove_device":
        did = op.get("id")
        if not isinstance(did, str) or did not in gb.devices:
            raise ValueError(f"unknown device {did}")
        dev = gb.devices[did]
        if dev.kind == "slack":
            raise ValueError("the slack cannot be removed — use /gb/net/reset")
        _drop_device_element(sim, dev)
        del gb.devices[did]
        gb.held.pop(did, None)
        return did
    if name == "set_device":
        did = op.get("id")
        if not isinstance(did, str) or did not in gb.devices:
            raise ValueError(f"unknown device {did}")
        params = op.get("params")
        if not isinstance(params, dict):
            raise ValueError("set_device needs a 'params' object")
        dev = gb.devices[did]
        dev.params.update(params)
        if dev.kind == "slack" and _is_num(params.get("vm_pu")):
            row = sim.prof.ext_idx.index(dev.elem)
            sim.prof.ext_vm[row, :] = float(params["vm_pu"])
        return did
    raise ValueError(f"unknown op {name!r}")


def _create_device_element(gb: GbState, sim, dev: dict) -> GbDevice:
    """Append the device's zero-profile element row (the ``der.add_pv`` /
    ``add_ev`` pattern: element + aligned profile row + cache hygiene).
    pandapower reuses a dropped max index on the next create — safe here
    because the mapping goes through element indices, never row positions."""
    kind, bus = dev["kind"], gb.bus_of[dev["node"]]
    params = dict(dev.get("params") or {})
    table: str | None = None
    elem: int | None = None
    if kind in _GEN_KINDS:
        elem = int(pp.create_sgen(sim.net, bus=bus, p_mw=0.0, q_mvar=0.0,
                                  name=f"GBD_{dev['id']}"))
        sim.prof.sgen_idx.append(elem)
        sim.prof.sgen_p = np.vstack([sim.prof.sgen_p,
                                     np.zeros((1, sim.steps_per_day))])
        sim.prof.sgen_q = np.vstack([sim.prof.sgen_q,
                                     np.zeros((1, sim.steps_per_day))])
        sim.sgen_kind.append("gen")   # never follows the real-PV day shapes
        der._der_invalidate(sim)
        table = "sgen"
    elif kind == "coupling_load":
        elem = int(pp.create_load(sim.net, bus=bus, p_mw=0.0, q_mvar=0.0,
                                  name=f"GBD_{dev['id']}"))
        sim.prof.load_idx.append(elem)
        sim.prof.load_p = np.vstack([sim.prof.load_p,
                                     np.zeros((1, sim.steps_per_day))])
        sim.prof.load_q = np.vstack([sim.prof.load_q,
                                     np.zeros((1, sim.steps_per_day))])
        sim._load_household.append(False)
        sim._load_households.append(1)
        der._der_invalidate(sim)
        table = "load"
    # battery: declared in v1, implemented in Phase 3 — no element yet
    return GbDevice(id=dev["id"], kind=kind, node=dev["node"], bus=bus,
                    params=params, table=table, elem=elem)


def _drop_device_element(sim, dev: GbDevice) -> None:
    """Remove the device's element + profile row, keeping arrays index-aligned
    (mirror of ``der.remove_pv`` / ``remove_ev``)."""
    if dev.table == "sgen" and dev.elem in sim.prof.sgen_idx:
        i = sim.prof.sgen_idx.index(dev.elem)
        sim.prof.sgen_idx.pop(i)
        sim.prof.sgen_p = np.delete(sim.prof.sgen_p, i, axis=0)
        sim.prof.sgen_q = np.delete(sim.prof.sgen_q, i, axis=0)
        if i < len(sim.sgen_kind):
            sim.sgen_kind.pop(i)
        sim.net.sgen.drop(dev.elem, inplace=True)
        der._der_invalidate(sim)
    elif dev.table == "load" and dev.elem in sim.prof.load_idx:
        i = sim.prof.load_idx.index(dev.elem)
        sim.prof.load_idx.pop(i)
        sim.prof.load_p = np.delete(sim.prof.load_p, i, axis=0)
        sim.prof.load_q = np.delete(sim.prof.load_q, i, axis=0)
        if i < len(sim._load_household):
            sim._load_household.pop(i)
        if i < len(sim._load_households):
            sim._load_households.pop(i)
        sim.net.load.drop(dev.elem, inplace=True)
        der._der_invalidate(sim)


# ----------------------------------------------------------------- step path

def _apply_held(gb: GbState, sim, col: int) -> "list[dict]":
    """Write every held zone demand / device setpoint / coupling value into
    the profile arrays at the CURRENT step column (sample-and-hold), clamping
    generator setpoints to their rating; returns the ``clamped`` violations.
    Rows are resolved from element indices per call, so patched tables can
    never desynchronize the mapping."""
    prof = sim.prof
    violations: "list[dict]" = []
    for z in gb.zones.values():
        row = prof.load_idx.index(z.load_idx)
        prof.load_p[row, col] = gb.zone_demand_kw[z.id] / 1000.0
    for dev in gb.devices.values():
        held = gb.held.get(dev.id) or {}
        if dev.kind in _GEN_KINDS and dev.elem in prof.sgen_idx:
            requested = float(held.get("p_kw", 0.0))
            p_kw = max(requested, 0.0)
            cap = dev.params.get(_CAP_PARAM[dev.kind])
            if _is_num(cap):
                p_kw = min(p_kw, float(cap))
            if p_kw != requested:
                violations.append({"element": f"device:{dev.id}",
                                   "kind": "clamped", "severity": "info",
                                   "value": _r(requested)})
            prof.sgen_p[prof.sgen_idx.index(dev.elem), col] = p_kw / 1000.0
        elif dev.kind == "coupling_load" and dev.elem in prof.load_idx:
            p_kw = float(held.get("p_kw", 0.0))
            prof.load_p[prof.load_idx.index(dev.elem), col] = p_kw / 1000.0
    return violations


def _contract_result(t: int, native: StepResult, gb: GbState, sim,
                     clamped: "list[dict]") -> dict:
    """Build the contract step result (schemas/step-result.schema.json) from
    the solved native StepResult + the pandapower result tables."""
    converged = native.converged
    vm = {b["index"]: b["vm_pu"] for b in native.buses}

    zones: "dict[str, dict]" = {}
    for z in gb.zones.values():
        detail: dict = {}
        if converged and vm.get(z.bus) is not None:
            detail["v_pu"] = vm[z.bus]
        # v1 mapping (contract §4): converged -> 1.0, failed -> 0.0
        zones[z.id] = {"supplied": 1.0 if converged else 0.0, "detail": detail}

    devices: "dict[str, dict]" = {}
    for dev in gb.devices.values():
        out = soc = None
        if dev.kind == "battery":       # declared in v1, implemented Phase 3
            out, soc = 0.0, 0.5
        elif converged and dev.elem is not None:
            try:
                if dev.table == "sgen":
                    out = _r(float(sim.net.res_sgen.at[dev.elem, "p_mw"]) * 1000.0)
                elif dev.table == "load":
                    out = _r(float(sim.net.res_load.at[dev.elem, "p_mw"]) * 1000.0)
                elif dev.table == "ext_grid":   # slack: import positive
                    out = _r(float(sim.net.res_ext_grid.at[dev.elem, "p_mw"]) * 1000.0)
            except KeyError:
                out = None
        devices[dev.id] = {"output_kw": out, "soc": soc, "detail": {}}

    violations = list(clamped)
    for ln in native.lines:
        lp = ln.get("loading_percent")
        if lp is not None and lp > _OVERLOAD_WARN:
            violations.append({
                "element": f"line:{ln['index']}", "kind": "overload",
                "severity": "critical" if lp > _OVERLOAD_CRIT else "warning",
                "value": lp})
    for tr in native.trafos:
        lp = tr.get("loading_percent")
        if lp is not None and lp > _OVERLOAD_WARN:
            violations.append({
                "element": f"trafo:{tr['index']}", "kind": "overload",
                "severity": "critical" if lp > _OVERLOAD_CRIT else "warning",
                "value": lp})
    for b in native.buses:
        v = b.get("vm_pu")
        if v is not None and not (_V_BAND[0] <= v <= _V_BAND[1]):
            violations.append({
                "element": f"bus:{b['index']}", "kind": "voltage",
                "severity": "critical" if (v < _V_CRIT[0] or v > _V_CRIT[1])
                else "warning",
                "value": v})

    return {"t": t,
            "status": "converged" if converged else "failed",
            "solve_ms": native.solve_ms,
            "zones": zones,
            "devices": devices,
            "coupling_out": {},   # power has no coupling outputs in v1
            "violations": violations}


async def _gb_step(req: Any) -> "tuple[int, dict]":
    """Shared step logic for ``POST /gb/step`` and WS ``/gb/ws`` (contract §4).

    Returns ``(http_status, payload)``: 200 with a step result, 400 with
    ``{detail}``, or 409 with the out-of-order error frame.
    """
    gb: GbState | None = getattr(runtime, "gb", None)
    if gb is None:
        return 400, {"detail": "no game topology loaded — POST /gb/net/reset first"}
    if not isinstance(req, dict):
        return 400, {"detail": "step request must be a JSON object"}
    t = _as_int(req.get("t"))
    if t is None or t < 0:
        return 400, {"detail": "step request needs an integer 't' >= 0"}
    dt_s = _as_int(req.get("dt_s"))
    if dt_s is None or not (60 <= dt_s <= 3600):
        return 400, {"detail": "step request needs an integer 'dt_s' in [60, 3600]"}

    if gb.last_t is not None:
        if t == gb.last_t:
            # idempotent re-send: cached result, no re-solve (contract §0.3)
            return 200, gb.last_result  # type: ignore[return-value]
        if t != gb.last_t + 1:
            return 409, {"t": t, "status": "error", "error": "out_of_order",
                         "expected": [gb.last_t, gb.last_t + 1]}

    # -- update the held values (sample-and-hold, contract §4) ------------- #
    # `weather` is deliberately ignored: the game computes PV/wind availability
    # itself and sends setpoints (generation models live game-side).
    for zid, entry in (req.get("zone_demand") or {}).items():
        if zid in gb.zones and isinstance(entry, dict) and _is_num(entry.get("value")):
            gb.zone_demand_kw[zid] = max(0.0, float(entry["value"]))
    for did, sp in (req.get("device_setpoints") or {}).items():
        if did in gb.devices and isinstance(sp, dict):
            held = gb.held.setdefault(did, {})
            held.update({k: float(v) for k, v in sp.items() if _is_num(v)})
    for did, entry in (req.get("coupling_in") or {}).items():
        dev = gb.devices.get(did)
        if dev is not None and dev.kind == "coupling_load" \
                and isinstance(entry, dict) and _is_num(entry.get("p_kw")):
            gb.held.setdefault(did, {})["p_kw"] = float(entry["p_kw"])

    engine = runtime.engine
    sim = engine.sim
    # the column external_step is about to solve (engine.step is pre-advance)
    col = engine.step % engine.steps_per_day
    clamped = _apply_held(gb, sim, col)
    try:
        native = await engine.external_step()
    except RuntimeError as exc:   # internal clock running — two clocks never race
        return 409, {"t": t, "status": "error", "error": "internal_clock_running",
                     "detail": str(exc)}

    result = _contract_result(t, native, gb, sim, clamped)
    gb.last_t, gb.last_result = t, result
    return 200, result


@router.post("/step")
async def step(request: Request):
    """Advance exactly one step under the external clock (contract §4) —
    the debugging fallback of the WS ``/gb/ws`` step channel, identical
    behavior. With an EMPTY body: the pre-contract debug path (advance one
    step, return the native StepResult / 409 while the internal clock runs)."""
    raw = await request.body()
    if not raw:
        try:
            await runtime.engine.external_step()
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return runtime.store.latest
    try:
        req = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(400, "body is not valid JSON") from None
    status, payload = await _gb_step(req)
    if status == 200:
        return payload
    return JSONResponse(status_code=status, content=payload)


@router.get("/result/latest")
def result_latest():
    """The last contract step result (contract §4) — 404 before the first
    step. Together with idempotent re-send this is the crash-recovery path."""
    gb: GbState | None = getattr(runtime, "gb", None)
    if gb is None or gb.last_result is None:
        raise HTTPException(404, "no step result yet")
    return gb.last_result


@router.websocket("/ws")
async def step_ws(websocket: WebSocket):
    """Step channel (contract §1, ADR-003): one text frame in = one step
    request, one text frame out = the step result. Strictly sequential — the
    client keeps at most one request in flight. Out-of-order ``t`` yields the
    ``error: "out_of_order"`` frame instead of an HTTP 409."""
    await websocket.accept()
    try:
        while True:
            raw = await websocket.receive_text()
            try:
                req = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_text(json.dumps(
                    {"status": "error", "error": "bad_request",
                     "detail": "frame is not valid JSON"}))
                continue
            status, payload = await _gb_step(req)
            if status == 200 or payload.get("status") == "error":
                # a 409 payload already is the error frame (contract §4)
                await websocket.send_text(json.dumps(payload))
            else:
                await websocket.send_text(json.dumps(
                    {"t": req.get("t") if isinstance(req, dict) else None,
                     "status": "error", "error": "bad_request",
                     "detail": payload.get("detail")}))
    except WebSocketDisconnect:
        pass
