"""Convert a *European Archetype* LV grid workbook into netzsim input files.

The workbooks have five sheets:

* **Nodes**        - ``Name of the Node``, ``Type`` (``Slack``/``PQ``), ``Nominal Voltage [kV]``
* **Lines**        - ``Name of the line``, ``From node``, ``To node``, ``Type``, ``Length [km]``
* **Line Types**   - ``Name of line type``, ``R [Ohm/km]``, ``X [Ohm/km]``, ``B [Mikro S/km]``, ``Nominal Current I [A]``
* **Transformers** - physical parameters (``Smax [MVA]``, ``V_P/V_S [kV]``, ``U_sc [%]`` ...)
* **Loads**        - ``Name of the load``, ``Node``, ``Pmax [MW]``, ``Sn [MVA]`` ...

Mapping decisions
-----------------
* **Bus index == row order in the Nodes sheet** (netzsim's native convention),
  so node *names* (strings) are resolved to integer indices via a name->index map.
* Line types are joined in and emitted as **explicit per-km parameters**
  (the workbooks do not use pandapower ``std_type`` names).
* The transformer is emitted with **explicit parameters**; ``tbd`` electrical
  fields fall back to :class:`TrafoDefaults`.
* The ``Slack`` node becomes the ``ext_grid`` (substation) with a flat
  ``vm_pu = 1.0`` profile.
* **Load profiles are placeholders.** The workbook only carries connection
  ratings (``Pmax``/``Sn``), not time series, so each load gets a small synthetic
  daily curve. These are meant to be replaced by the LPG archetype library.
"""
from __future__ import annotations

import io
import json
import math
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

# Sheet names ------------------------------------------------------------- #
S_NODES, S_LINES, S_LTYPES, S_TRAFOS, S_LOADS = (
    "Nodes", "Lines", "Line Types", "Transformers", "Loads",
)


@dataclass
class TrafoDefaults:
    """Fallbacks for transformer fields the workbook leaves as ``tbd``."""

    vk_percent: float = 4.0       # short-circuit voltage (U_sc)
    vkr_percent: float = 1.0      # real part of vk (derived from P_cl when present)
    pfe_kw: float = 0.0           # iron losses (P_il)
    i0_percent: float = 0.0       # no-load current (I_nl)


@dataclass
class GridInputs:
    """The five netzsim input documents as plain dicts."""

    grid_structure: dict[str, Any]
    lines: dict[str, Any]
    load: dict[str, Any]
    generation: dict[str, Any]
    substation: dict[str, Any]
    notes: list[str] = field(default_factory=list)

    def as_files(self) -> dict[str, dict[str, Any]]:
        return {
            "grid_structure.json": self.grid_structure,
            "lines.json": self.lines,
            "load.json": self.load,
            "generation.json": self.generation,
            "substation.json": self.substation,
        }


# --------------------------------------------------------------------------- #
# small parsing helpers
# --------------------------------------------------------------------------- #
def _norm_name(value: Any) -> str:
    """Normalise a node name to a stable string (``1137.0``/``1137`` -> ``"1137"``)."""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _num(value: Any, default: float | None = None) -> float | None:
    """Best-effort float; ``tbd``/blank/NaN -> ``default``."""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return default if (isinstance(value, float) and math.isnan(value)) else float(value)
    s = str(value).strip()
    if not s or s.lower() in {"tbd", "n/a", "na", "-"}:
        return default
    try:
        return float(s)
    except ValueError:
        return default


def _daily(steps: int, base: float, amp: float, peak_hour: float, floor: float = 0.0) -> list[float]:
    """A smooth sinusoidal daily shape peaking at ``peak_hour`` (clock hours)."""
    peak_step = peak_hour / 24.0 * steps
    return [
        round(max(floor, base + amp * math.cos(2 * math.pi * (t - peak_step) / steps)), 6)
        for t in range(steps)
    ]


def _reconnect_islands(
    n_buses: int,
    line_specs: list[dict[str, Any]],
    trafo_specs: list[dict[str, Any]],
    slack_buses: list[int],
    root_lv_bus: int,
    bus_specs: list[dict[str, Any]],
) -> list[str]:
    """Tie any branch-connected component lacking a slack back to the LV busbar.

    Some workbooks omit the line (or node) that links a feeder to the rest of the
    grid, leaving an unsupplied island whose buses solve to ``NaN``. We connect
    each such island to ``root_lv_bus`` with a short synthetic LV tie line so the
    whole grid is energised. Uses union-find (no extra deps). Mutates
    ``line_specs`` in place and returns one note per repair.
    """
    parent = list(range(n_buses))

    def find(x: int) -> int:
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for ln in line_specs:
        union(ln["from_bus"], ln["to_bus"])
    for tr in trafo_specs:
        union(tr["hv_bus"], tr["lv_bus"])

    groups: dict[int, list[int]] = {}
    for b in range(n_buses):
        groups.setdefault(find(b), []).append(b)

    supplied = {find(b) for b in slack_buses}
    root_vn = bus_specs[root_lv_bus]["vn_kv"]
    notes: list[str] = []
    for root in sorted(groups):
        if root in supplied:
            continue
        comp = groups[root]
        # prefer an island bus at the busbar's voltage level for the tie
        candidates = [b for b in comp if abs(bus_specs[b]["vn_kv"] - root_vn) < 1e-6]
        conn = min(candidates or comp)
        line_specs.append({
            "name": f"TIE_{conn}", "from_bus": root_lv_bus, "to_bus": conn,
            "length_km": 0.03, "r_ohm_per_km": 0.2, "x_ohm_per_km": 0.08,
            "c_nf_per_km": 0.0, "max_i_ka": 0.27,
        })
        notes.append(
            f"reconnected isolated island of {len(comp)} bus(es) to the busbar "
            f"(bus {root_lv_bus}) via a synthetic tie line at bus {conn}"
        )
    return notes


# --------------------------------------------------------------------------- #
# conversion
# --------------------------------------------------------------------------- #
def convert_workbook(
    xl: pd.ExcelFile,
    *,
    name: str = "Imported LV grid",
    steps: int = 1440,
    f_hz: float = 50.0,
    placeholder_peak_mw: float = 0.001,
    power_factor: float = 0.95,
    slack_vm_pu: float = 1.0,
    reconnect_islands: bool = True,
    trafo_defaults: TrafoDefaults | None = None,
) -> GridInputs:
    """Translate one opened workbook into :class:`GridInputs`."""
    td = trafo_defaults or TrafoDefaults()
    nodes = xl.parse(S_NODES)
    lines = xl.parse(S_LINES)
    ltypes = xl.parse(S_LTYPES).set_index("Name of line type")
    trafos = xl.parse(S_TRAFOS) if S_TRAFOS in xl.sheet_names else pd.DataFrame()
    loads = xl.parse(S_LOADS) if S_LOADS in xl.sheet_names else pd.DataFrame()

    # --- buses (index == row order) ------------------------------------- #
    bus_index: dict[str, int] = {}
    bus_specs: list[dict[str, Any]] = []
    for i, row in nodes.reset_index(drop=True).iterrows():
        nm = _norm_name(row["Name of the Node"])
        vn = _num(row["Nominal Voltage [kV]"], 0.4) or 0.4
        bus_index[nm] = int(i)
        bus_specs.append({
            "name": nm,
            "vn_kv": vn,
            "type": "b",
            "zone": "MV" if vn > 1.0 else "LV",
            "in_service": True,
        })

    # Some workbooks reference a branch endpoint that was never declared in the
    # Nodes sheet. Rather than fail, auto-create it as a bus, inferring its
    # voltage level from a declared neighbour (default LV 0.4 kV).
    notes: list[str] = []

    def _partner_vn(missing: str) -> float:
        for _, lr in lines.iterrows():
            a, b = _norm_name(lr["From node"]), _norm_name(lr["To node"])
            if missing == a and b in bus_index:
                return bus_specs[bus_index[b]]["vn_kv"]
            if missing == b and a in bus_index:
                return bus_specs[bus_index[a]]["vn_kv"]
        return 0.4

    referenced: set[str] = set()
    for _, lr in lines.iterrows():
        referenced.add(_norm_name(lr["From node"]))
        referenced.add(_norm_name(lr["To node"]))
    for _, tr in trafos.iterrows():
        referenced.add(_norm_name(tr["From node"]))
        referenced.add(_norm_name(tr["To node"]))
    for nm in sorted(n for n in referenced if n not in bus_index):
        vn = _partner_vn(nm)
        bus_index[nm] = len(bus_specs)
        bus_specs.append({
            "name": nm, "vn_kv": vn, "type": "b",
            "zone": "MV" if vn > 1.0 else "LV", "in_service": True,
        })
        notes.append(f"auto-created bus '{nm}' (vn={vn} kV): referenced by a "
                     f"branch but absent from the Nodes sheet")

    def _bus(ref: Any, where: str) -> int:
        key = _norm_name(ref)
        if key not in bus_index:
            raise KeyError(f"{where}: node '{key}' is not in the Nodes sheet")
        return bus_index[key]

    grid_structure = {"name": name, "f_hz": f_hz, "buses": bus_specs}

    # --- slack nodes (needed for both the island repair and the ext_grid) -- #
    slack_mask = nodes["Type"].astype(str).str.strip().str.lower() == "slack"
    slack_nodes = nodes[slack_mask]
    if slack_nodes.empty:
        raise ValueError("no node of Type 'Slack' found - cannot create an ext_grid")
    slack_buses = [_bus(r["Name of the Node"], "slack") for _, r in slack_nodes.iterrows()]

    # --- lines (join with line types -> explicit per-km parameters) ----- #
    cap_factor = 1.0e3 / (2.0 * math.pi * f_hz)  # uS/km -> nF/km
    line_specs: list[dict[str, Any]] = []
    for _, row in lines.iterrows():
        lt_name = str(row["Type"]).strip()
        if lt_name not in ltypes.index:
            raise KeyError(f"line '{row['Name of the line']}': unknown line type '{lt_name}'")
        lt = ltypes.loc[lt_name]
        b_us = _num(lt["B [Mikro S/km]"], 0.0) or 0.0
        line_specs.append({
            "name": _norm_name(row["Name of the line"]),
            "from_bus": _bus(row["From node"], f"line {row['Name of the line']}"),
            "to_bus": _bus(row["To node"], f"line {row['Name of the line']}"),
            "length_km": max(_num(row["Length [km]"], 0.0) or 0.0, 1.0e-4),
            "r_ohm_per_km": _num(lt["R [Ohm/km]"], 0.0),
            "x_ohm_per_km": _num(lt["X [Ohm/km]"], 0.0),
            "c_nf_per_km": round(b_us * cap_factor, 6),
            "max_i_ka": (_num(lt["Nominal Current I [A]"], 1.0) or 1.0) / 1000.0,
        })

    # --- transformers (explicit parameters, tbd -> defaults) ------------ #
    trafo_specs: list[dict[str, Any]] = []
    for _, row in trafos.iterrows():
        hv = _bus(row["From node"], f"trafo {row['Name of the transformer']}")
        lv = _bus(row["To node"], f"trafo {row['Name of the transformer']}")
        vn_hv = _num(row["V_P [kV]"]) or grid_structure["buses"][hv]["vn_kv"]
        vn_lv = _num(row["V_S [kV]"]) or grid_structure["buses"][lv]["vn_kv"]
        # keep HV/LV oriented by actual voltage level
        if vn_hv < vn_lv:
            hv, lv, vn_hv, vn_lv = lv, hv, vn_lv, vn_hv
        sn = _num(row["Smax [MVA]"], 0.1) or 0.1
        p_cl = _num(row["P_cl [kW]"])  # copper losses
        vkr = (p_cl / (sn * 1000.0) * 100.0) if p_cl is not None and sn else td.vkr_percent
        vk = _num(row["U_sc [%]"], td.vk_percent) or td.vk_percent
        parallel = int(_num(row.get("Number of parallel transformers"), 0) or 0) or 1
        trafo_specs.append({
            "name": _norm_name(row["Name of the transformer"]),
            "hv_bus": hv,
            "lv_bus": lv,
            "sn_mva": sn,
            "vn_hv_kv": vn_hv,
            "vn_lv_kv": vn_lv,
            "vk_percent": vk,
            "vkr_percent": min(round(vkr, 6), vk),  # vkr must not exceed vk
            "pfe_kw": _num(row["P_il [kW]"], td.pfe_kw),
            "i0_percent": _num(row["I_nl [%]"], td.i0_percent),
            "parallel": parallel,
        })

    # --- repair islands: connect any slack-less component to the LV busbar -- #
    if reconnect_islands:
        root_lv_bus = trafo_specs[0]["lv_bus"] if trafo_specs else slack_buses[0]
        notes.extend(_reconnect_islands(
            len(bus_specs), line_specs, trafo_specs, slack_buses,
            root_lv_bus, bus_specs,
        ))

    lines_doc = {"lines": line_specs, "transformers": trafo_specs}

    # --- substations (slack nodes -> ext_grid) -------------------------- #
    substations = [{
        "name": _norm_name(r["Name of the Node"]),
        "bus": _bus(r["Name of the Node"], "slack"),
        "vm_pu": [slack_vm_pu] * steps,
        "va_degree": [0.0] * steps,
    } for _, r in slack_nodes.iterrows()]
    substation_doc = {"resolution_minutes": 1440 // steps, "steps": steps,
                      "substations": substations}

    # --- loads (placeholder daily profiles) ----------------------------- #
    tan_phi = math.tan(math.acos(power_factor))
    load_specs: list[dict[str, Any]] = []
    for i, row in loads.reset_index(drop=True).iterrows():
        key = _norm_name(row["Node"])
        if key not in bus_index:
            # Orphan reference: node is in no branch and no Nodes row, so it
            # cannot be connected. Drop the (placeholder) load rather than create
            # an isolated bus that would break the power flow.
            notes.append(f"dropped load '{_norm_name(row['Name of the load'])}': "
                         f"node '{key}' is connected to nothing")
            continue
        bus = bus_index[key]
        # small per-load variety so the feeder isn't perfectly uniform
        peak = placeholder_peak_mw * (0.6 + 0.8 * ((i % 5) / 4.0))
        peak_hour = 18.5 + (i % 4) * 0.5
        p = _daily(steps, base=peak * 0.45, amp=peak * 0.45, peak_hour=peak_hour, floor=0.0)
        q = [round(v * tan_phi, 6) for v in p]
        load_specs.append({
            "name": _norm_name(row["Name of the load"]),
            "bus": bus,
            "p_mw": p,
            "q_mvar": q,
        })
    load_doc = {"resolution_minutes": 1440 // steps, "steps": steps, "loads": load_specs}

    generation_doc = {"resolution_minutes": 1440 // steps, "steps": steps, "generation": []}

    return GridInputs(
        grid_structure=grid_structure,
        lines=lines_doc,
        load=load_doc,
        generation=generation_doc,
        substation=substation_doc,
        notes=notes,
    )


def convert_xlsx_bytes(data: bytes, **kw: Any) -> GridInputs:
    with pd.ExcelFile(io.BytesIO(data)) as xl:
        return convert_workbook(xl, **kw)


def convert_xlsx_file(path: str | Path, **kw: Any) -> GridInputs:
    kw.setdefault("name", Path(path).stem)
    with pd.ExcelFile(path) as xl:
        return convert_workbook(xl, **kw)


def convert_from_zip(zip_path: str | Path, member_substring: str, **kw: Any) -> GridInputs:
    """Convert the first workbook in ``zip_path`` whose path contains the substring."""
    with zipfile.ZipFile(zip_path) as zf:
        matches = [n for n in zf.namelist()
                   if n.endswith(".xlsx") and member_substring in n]
        if not matches:
            raise FileNotFoundError(f"no .xlsx matching '{member_substring}' in {zip_path}")
        member = sorted(matches)[0]
        kw.setdefault("name", Path(member).stem)
        return convert_workbook(pd.ExcelFile(io.BytesIO(zf.read(member))), **kw)


def write_inputs(grid: GridInputs, out_dir: str | Path) -> Path:
    """Write the five JSON files into ``out_dir`` and return the directory."""
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    for fname, obj in grid.as_files().items():
        (d / fname).write_text(json.dumps(obj, indent=2), encoding="utf-8")
    return d
