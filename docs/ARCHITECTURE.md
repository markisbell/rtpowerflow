# netzsim — Architecture

> Developer-facing system documentation, complementing the endpoint
> reference in [`API.md`](API.md) and the German user guide
> (`Benutzerhandbuch.pdf`). The exhaustive, session-by-session development
> log lives in [`CLAUDE.md`](../CLAUDE.md).

## 1. System overview

netzsim is a **realtime power-flow time-series simulator turned teaching
platform** for distribution-grid observability and control. One
`docker-compose.yml` orchestrates three applications:

```
┌────────────────────┐   REST + WebSocket   ┌───────────────────┐
│  netzsim (src/)    │◄────────────────────►│  React UI (ui/)   │
│  FastAPI :8000     │                      │  nginx :8080      │
│  pandapower engine │   REST /state (poll) │  (dev: Vite 5173) │
└─────────┬──────────┘◄──────────┐          └───────────────────┘
          │                      │
          │              ┌───────┴────────┐      ┌───────────────┐
          │              │ collector      │─────►│ InfluxDB :8086│
          │              │ (visualization)│      └───────┬───────┘
          │              └────────────────┘              │
          │                                      ┌───────┴───────┐
          └── data/ (grids, profiles, scenarios) │ Grafana :3000 │
                                                 └───────────────┘
```

The simulation clock is an **accelerated tick**: every `interval` real
seconds (default 1.0, UI-adjustable 0.1–1.0) the engine advances one
simulation step — 1 minute of simulated time on the committed grids. After
step 1439 the day wraps and repeats the same daily profiles indefinitely.

Sibling repositories complete the tool family: **gridgen** *generates* the
committed grid dataset (synthetic MV districts via ding0/OEP, street-routed
LV grids from OSM) and owns the neutral **gridformat** file contract;
**gridedit** lets users *draw* grids on the map and exports into netzsim's
user-grid catalog. netzsim is a pure consumer of both.

## 2. The pedagogical core: three data layers

Every solved step exists in three views, strictly separated end to end:

```
              solve (truth)
                   │
   ┌───────────────┼──────────────────────┐
   ▼               ▼                      ▼
 Lastfluss      Gemessen               Schätzung
 (ground truth) (placed meters only,   (WLS state estimation from
                 per-device TAF raster) meters + grid model + pseudos)
```

1. **Truth** — the full power flow, computed at every bus/line each step.
2. **Measured** — only what a placed device delivers: a smart meter reveals
   its node's V/P/Q/I, a transformer meter its loading and HV-side flow;
   lines carry no meters by design. Per-device fidelity: TAF 9/10/14
   (1-minute telemetry) vs. TAF 7 (15-minute mean active power, strictly
   windowed — no intra-window updates).
3. **Estimated** — `estimator.py` runs pandapower's WLS estimation from the
   meter readings, structural zero-injection knowledge and profile-based
   pseudo-measurements, governed by an operator-configurable policy (PV/EV
   pseudo usage, SLP vs. profile load basis, hierarchy). It reruns in the
   metering raster and is additionally wall-clock throttled on big nets.

`NETZSIM_EXPOSE_GROUND_TRUTH=false` enforces the separation on the wire:
`StateStore` strips the truth arrays from `/state`, `/ws` and `/history`,
and the day-curve endpoints gate their truth layers the same way. This is
the platform's central teaching invariant: **controllers and rONTs are fed
exclusively from layers 2 and 3** — control quality equals observability.

## 3. Backend architecture (`src/netzsim/`)

### 3.1 Data pipeline

```
data/*.json ──► data_loader (pydantic validation + cross-checks) ── InputData
                     │
             network_builder ──► pandapower net built ONCE
                     │           + numpy profile arrays [n_elements, 1440]
                     ▼
        RealtimeEngine (asyncio) ──► Simulator.run_step(step, day)
        1 step / interval, day-wrap    write step columns → runpp → StepResult
                     ▼
                StateStore ── latest + ring-buffer history + WS broadcast
                     │        + optional recorder sink (strict-mode-projected)
        ┌────────────┴────────────┐
        ▼ WebSocket /ws           ▼ REST (FastAPI routers)
```

Design choices, with rationale:

- **Build once, step cheaply.** Topology is built a single time; each step
  only overwrites `p_mw`/`q_mvar`/`vm_pu` columns and calls `runpp`
  (warm-started after the first solve). Day sweeps additionally use
  pandapower's recycle path (only bus injections rebuilt), validated
  bit-identical to full solves.
- **Profiles double as element definitions.** Every row of the load/
  generation/substation inputs becomes one pandapower element; element
  order == pandapower index == numpy row. This invariant underpins all
  runtime mutation code.
- **The solve runs off the event loop** (`asyncio.to_thread`), so the API
  stays responsive during slow district solves.
- **No locks; self-healing instead** (deliberate project decision). Runtime
  mutations may race the engine's solve; `run_step`'s retry ladder catches
  any exception — a poisoned step yields one non-converged frame and the
  next step solves cold.
- **JSON safety.** All result floats pass through `_r` (canonical copy in
  `measurements.py`): 6-digit rounding, NaN/±Inf → `null` — otherwise
  browsers reject whole WebSocket frames.

### 3.2 Module map

The `Simulator` is deliberately the **only stateful core**; three layers
were extracted as function modules that take `sim` as first argument and
keep all state (caches, journals, id counters) on the Simulator — so
scenario recipes, cache invalidation and the bulk exporter's deep copy work
unchanged:

| Module | Responsibility |
|---|---|
| `simulator.py` | pandapower net + profiles, `run_step` ladder, result collection, topology, equipment/meter CRUD |
| `sweeps.py` | day sweeps: truth/measured/estimated layers of the day graphs + per-element profile curves (caches on `sim`) |
| `der.py` | runtime PV/EV mutators, `node_der`, the bus-addressed DER journal scenarios replay |
| `control_runtime.py` | per-step controller/rONT passes: domain views, traffic-light signals, tap decisions |
| `estimator.py` | WLS estimation (monolithic + hierarchical two-stage), estimation policy (`EstConfig`) |
| `measurements.py` | `MeasurementSet`: meter placement, per-device TAF modes, the observed projection, canonical `_r` |
| `engine.py` | async realtime loop: tick, day-wrap, pause/seek, live grid swap (`reconfigure`) |
| `state.py` | latest + history ring buffer, WS pub/sub, strict-mode strip, recorder sink |
| `recorder.py` / `exporter.py` | session recording (published stream → tidy CSVs) / offline bulk replay of whole days on a deep-copied Simulator |
| `grid_catalog.py` + importers | manifest-driven catalog; `ding0_import` (MV/LV/full scopes), `osm_lv_import`, `gridedit_mv_import` (incl. `lv_ref` splicing), `district_import` |
| `loadgen/` | cached LPG household library, EV/PV assignment policies |
| `scenarios.py` | recipe files: grid + loadgen + DER ops + equipment + meters + clock |
| `api/` | FastAPI routers by area (core, control, equipment, measurements, grids, recordings, scenarios) around a `runtime` container assembled in the lifespan |

### 3.3 Vertical MV/LV integration

Districts are modelled as **ONS cells** (secondary-substation cells),
emitted by every importer (`GridInputs.cells`): spliced cells carry their
member buses, LV busbar and station transformers; lumped stations are
degenerate cells at their MV bus.

- **Hierarchical estimation**: each spliced cell runs a local WLS on its
  subnet (slack = feeding MV bus, setpoint from the previous MV estimate);
  each cell's boundary flow (measured > cell-estimated > profile pseudo)
  feeds the reduced-MV WLS. Policy `hierarchy: auto|monolithic|hierarchical`.
- **Netzampel cascade**: cell controllers (scope `cell`) throttle their
  cell's DERs by §14a-style dimming; an MV coordinator (scope `mv`) watches
  only the MV level and broadcasts one traffic-light factor to all placed
  cell controllers, applied as `min(local law, signal)`. Estimate-fed
  actors act once per NEW estimation telegram (`est.seq`) — per-step
  ratcheting against a throttled estimate oscillates.
- **rONT**: an on-load tap changer per station transformer (±4 × 1.5 %),
  holding the LV busbar in a deadband around its setpoint, fed only from
  the operator view.

## 4. Frontend architecture (`ui/`)

React + TypeScript + Vite; Leaflet for the geographic map. German-first
with a DE/EN toggle (`i18n.ts`, key parity enforced at build time).

- **Views** (`src/views/`): `LivePowerFlow` (the live operation view) and
  `NetzStudio` (grid + load configuration workflow), opened through a
  desktop-style menu bar (`MenuBar.tsx`) with the always-visible view
  segment *Lastfluss | Gemessen | Schätzung*.
- **State slices as hooks** (`src/hooks.ts`): `useEquipment` (batteries/
  controllers/rONTs + CRUD round-trips), `useMeterPlacement` (placement,
  TAF modes, refetch stamps), `useDerState` (PV/EV per node). The view
  composes them and adds presentation concerns (section pinning, menus).
- **Components**: `MapDiagram` (Leaflet, loading/voltage color ramps, badge
  markers, signal rings, cell drill-down) and `GridDiagram` (schematic SVG
  fallback for grids without geo), the side-panel sections
  (`OverviewSection`, `AmpelSection`, `CellsSection`), per-element profile
  graphs, and the equipment input controls.
- **Live data** arrives over one WebSocket (`useWebSocket.ts`); day curves
  are fetched per element with the active view's `?view=` parameter, so
  each perspective only ever receives its own data layer.

## 5. Persistence & artifacts

| Artifact | Where | Notes |
|---|---|---|
| Grid dataset | `data/` (committed) | ding0 MV districts (real WGS84), street-routed LV grids, LPG library, catalog manifest — snapshot from gridgen |
| User grids | `data/user_grids/` (gitignored) | gridedit exports (gridformat LV + `gridedit-mv`), rescanned per listing |
| Scenarios | `data/scenarios/` (committed) | hand-editable recipes; four reference teaching scenarios |
| Recordings | `data/recordings/` (gitignored) | tidy CSV packs from live recording or bulk export, ZIP download |
| Live history | in-memory ring buffer | InfluxDB (app 2) is the durable store |

## 6. Testing & CI

- **Backend**: 148 pytest tests (`tests/`), including pinned regression
  nets: the full API route inventory + per-router smokes
  (`test_api_surface.py`), TAF rastering and Simulator-deepcopy
  independence (`test_sweeps.py`), estimation honesty tripwires (a truth
  leak into the estimate fails the suite), and the vertical-integration
  suites (cells, hierarchical WLS, cascade, rONT, `lv_ref` splicing).
- **Frontend**: `npm run build` (strict tsc + Vite) is the type gate.
- **CI** (GitHub Actions): pytest job gates three Docker image builds
  (netzsim, ui, collector). Dependencies float within pinned majors —
  `pandapower >=3.0,<4` after a top-level re-export removal broke CI once;
  the route-inventory test walks FastAPI's routing structures duck-typed
  because 0.139 changed how included routers appear in `app.routes`.

## 7. Running it

`start_netzsim.bat` (Windows double-click: backend :8000 + Vite :5173) ·
local venv (`PYTHONPATH=src python -m netzsim.main`) · full stack via
`docker compose up --build`. See the [README](../README.md) for details and
the dev-proxy gotcha (use `127.0.0.1`, not `localhost`, on Windows).
