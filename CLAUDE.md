# CLAUDE.md — project context & development state

> This file is the handoff/context document for future development sessions.
> It captures **what exists, how it fits together, what is verified, and what is
> not** so work can resume without re-deriving the design. User-facing docs live
> in [`README.md`](README.md) and [`visualization/README.md`](visualization/README.md).

---

## 1. What this project is

**EchtzeitNetzSimulator** ("realtime grid simulator") is a lightweight realtime
power-flow **time-series simulation** built on
[pandapower](https://github.com/e2nIEE/pandapower), plus a visualization stack.

It is **three applications** that run together via one `docker-compose.yml`
(netzsim API, the InfluxDB/Grafana visualization stack, and a React UI):

1. **`netzsim`** (this repo's `src/`) — a FastAPI service that loads a grid + daily
   profiles and continuously solves a power flow, one 1-minute step per
   *accelerated tick* of wall-clock time. Streams results via REST + WebSocket.
2. **Visualization** (`visualization/`) — a Python **collector** that polls
   netzsim's REST API and writes results to **InfluxDB**, displayed in a
   pre-provisioned **Grafana** dashboard.

### Core behavior (the requirements this was built to)
- Inputs are **5 JSON files**, native to pandapower (see §4).
- A day = **1440 steps** of 1 minute. The engine solves a power flow each step.
- After step 1439 it **wraps to step 0, increments a day counter, and repeats the
  same profiles indefinitely**.
- "Realtime" = **accelerated tick**: one step every `N` real seconds
  (`NETZSIM_STEP_INTERVAL_SECONDS`, default 1.0 → a full day in 1440 s ≈ 24 min).

### Decisions already locked in (from the initial requirements gathering)
- Realtime mode: **accelerated tick** (not true wall-clock, not fast-as-possible).
- Output interface: **REST + WebSocket** (FastAPI).
- Deployment: **Python + Docker**.
- Collector reads netzsim over **REST `/state`** (polling), not the WebSocket.

---

## 2. Directory layout

```
EchtzeitNetzSimulator/
├── CLAUDE.md                 # this file
├── README.md                 # user-facing docs for app 1 (+ pointer to viz)
├── docker-compose.yml        # orchestrates ALL 4 services
├── Dockerfile                # image for netzsim (app 1)
├── pyproject.toml            # netzsim package (src layout, console script)
├── requirements.txt          # netzsim runtime deps
├── .env.example              # documents all NETZSIM_* env vars
├── data/                     # the 5 input JSON files (sample set committed)
│   ├── grid_structure.json
│   ├── lines.json
│   ├── load.json
│   ├── generation.json
│   └── substation.json
├── scripts/
│   └── generate_sample_data.py   # regenerates data/ (5-bus, 1440-step example)
│                                 # (grid GENERATION lives in the separate ../gridgen repo)
├── src/netzsim/              # the simulation package (pure CONSUMER — no generation)
│   ├── config.py             # pydantic-settings (env NETZSIM_*)
│   ├── models.py             # pydantic schemas for the 5 input files
│   ├── data_loader.py        # read + cross-validate inputs -> InputData
│   ├── network_builder.py    # build pandapower net once + numpy profile arrays
│   ├── simulator.py          # apply one step, run_step() -> StepResult (+ observed projection)
│   ├── measurements.py       # OBSERVABILITY layer: MeasurementSet (meter placement + observe(net))
│   ├── engine.py             # async realtime loop (tick, day-wrap, pause/seek)
│   ├── state.py              # latest + history ring buffer + WS broadcast (+ strict-mode truth strip, recorder sink)
│   ├── recorder.py           # session recording: published step stream -> tidy CSVs on disk (+ ZIP)
│   ├── exporter.py           # bulk export: offline replay of whole days into a recorder pack
│   ├── api.py                # FastAPI: REST + WS /ws + grid catalog/swap + measurements + monitor
│   ├── grid_inputs.py        # GridInputs (the 5-doc model) + _daily — what importers produce
│   ├── grid_catalog.py       # list/convert grids for /grids (manifest + ding0/OSM + user)
│   ├── ding0_import.py       # pre-generated ding0 (eDisGo CSV) -> inputs, w/ real lat/lon
│   ├── osm_lv_import.py      # street-routed LV grid JSON (gridformat) -> inputs
│   ├── gridedit_mv_import.py # gridedit MS-layer export (format "gridedit-mv") -> inputs
│   ├── layout.py             # bus coords: length-aware geographic (x,y) + tidy tree (tx,ty)
│   ├── loadgen/              # cached LPG library reader + assignment (runtime, no pylpg)
│   │   ├── library.py        # LoadLibrary: read data/lpg_library/{index,*}.json
│   │   ├── assign.py         # assign_to_loads: archetype/variant -> per-load household profiles
│   │   ├── pv.py             # assign_pv: synthetic clear-sky rooftop PV (sgen)
│   │   └── ev.py             # assign_ev: synthetic additive EV home-charging loads
│   └── main.py               # uvicorn entry point (console script: `netzsim`)
├── data/                     # committed grid dataset (see data/DATASET.md) — from ../gridgen
│   ├── lpg_library/          # committed LPG profiles (index.json + {CHRxx}.json)
│   ├── ding0_grids/          # committed ding0 MV grids (eDisGo CSV, real lat/lon)
│   └── lv_osm/               # committed street-routed LV grids (gridformat JSON)
├── tests/
│   ├── test_simulator.py     # smoke tests (load/build/solve/day-wrap)
│   ├── test_runtime_swap.py  # grid catalog + engine.reconfigure (live grid swap)
│   ├── test_loadgen.py       # LPG library reader + assignment
│   ├── test_measurements.py  # observability: meter placement, projection, strict-mode strip
│   └── test_ding0_import.py  # ding0 CSV import (geo + solve)
├── ui/                       # app 3: React + Vite + TS frontend (served by nginx)
│   ├── src/views/            # NetzStudio (merged grid+loads workflow), LivePowerFlow
│   │                         #   — opened via the desktop-style MENU BAR (MenuBar.tsx,
│   │                         #   Variante B since 2026-07-07: Datei · Ansicht ·
│   │                         #   Messungen · Hilfe + ALWAYS-VISIBLE Sicht segment
│   │                         #   [Lastfluss|Gemessen|Schätzung]; "…"-items open small
│   │                         #   dialogs — Szenario speichern, Tage exportieren,
│   │                         #   Schätz-Richtlinie; Start/Pause only in the transport
│   │                         #   bar; ⏺/⬇ chips + the grid chip are clickable); app
│   │                         #   starts in Live; Live display state (layout/values/
│   │                         #   viewMode) lives in App (LiveView)
│   ├── src/components/       # GridDiagram (SVG), MapDiagram (Leaflet/OSM), Sparkline
│   ├── src/api.ts · types.ts · useWebSocket.ts · scales.ts
│   ├── Dockerfile · nginx.conf   # build static -> nginx, proxies /api + /ws
│   └── vite.config.ts        # dev proxy to netzsim (use 127.0.0.1, not localhost)
└── visualization/            # app 2
    ├── README.md
    ├── collector/
    │   ├── collector.py      # REST /state -> InfluxDB points (dedupe by day,step)
    │   ├── requirements.txt
    │   └── Dockerfile
    └── grafana/
        ├── provisioning/
        │   ├── datasources/influxdb.yml    # auto-wire InfluxDB (Flux, uid=influxdb)
        │   └── dashboards/dashboards.yml   # dashboard provider
        └── dashboards/netzsim.json         # 7-panel dashboard
```

---

## 3. Architecture & data flow

```
data/*.json ─► data_loader (pydantic validate + cross-validate)
                    │  InputData
                    ▼
            network_builder ──► (pandapower net built ONCE,
                    │            profiles packed into numpy arrays
                    │            shape [n_elements, 1440])
                    ▼
   accelerated tick ─► RealtimeEngine (asyncio) ─► Simulator.run_step(step, day)
   (1 step / N sec)        │ wraps 95→0, day++        write step into
                           │                          net.load/sgen/ext_grid,
                           │                          pp.runpp (warm-started)
                           ▼                          ─► StepResult
                       StateStore (latest + history deque + WS pub/sub)
                           │
                  FastAPI: REST + WebSocket /ws + built-in HTML monitor
                           │
        ┌──────────────────┴───────────────────┐
        ▼ (WebSocket)                           ▼ (REST GET /state, polled 0.5s)
   browser / WS clients                     collector ─► InfluxDB ─► Grafana
```

**Key design choices (and why):**
- **Build once, step cheaply.** Topology built a single time in `network_builder`;
  each step only overwrites `p_mw`/`q_mvar`/`vm_pu` columns then calls `runpp`.
- **Profiles double as element definitions.** Every entry in `load.json` /
  `generation.json` / `substation.json` becomes one pandapower `load` / `sgen` /
  `ext_grid`. Element order == pandapower element index == numpy array row.
- **Power flow runs off the event loop** via `asyncio.to_thread` so the API stays
  responsive (`engine.py`).
- **Warm start**: after the first solve, `run_step` uses `init="results"` for
  faster convergence on the next (similar) step. Toggle `NETZSIM_WARM_START`.

---

## 4. Input file formats (native to pandapower)

**Bus references everywhere are integer indices that match the order of
`grid_structure.buses`** — this is exactly how pandapower assigns bus indices.
`data_loader._cross_validate` checks all references and equal step counts.

- **`grid_structure.json`** — `{name, f_hz, buses[{name, vn_kv, type, zone, in_service}]}`
- **`lines.json`** — `{lines[], transformers[]}`. Each line has `from_bus`,
  `to_bus`, `length_km`, and EITHER `std_type` OR all of
  `r_ohm_per_km`/`x_ohm_per_km`/`c_nf_per_km`/`max_i_ka` (validated in `models.py`).
  Transformers: `{hv_bus, lv_bus, std_type}`.
- **`load.json`** — `{resolution_minutes, steps, loads[{name, bus, p_mw[1440], q_mvar?[1440]}]}` → pandapower `load`.
- **`generation.json`** — same shape, key is `"generation"` (alias `gens`),
  `{... generation[{name, bus, p_mw[1440], q_mvar?[1440]}]}` → pandapower `sgen`.
- **`substation.json`** — `{... substations[{name, bus, vm_pu[1440], va_degree?[1440]}]}`
  → pandapower `ext_grid` (the slack / connection to the upper grid layer). At
  least one substation is **required** (the slack).

All profile arrays must have exactly `steps` (=1440) values. `q_mvar`/`va_degree`
default to zeros if omitted.

---

## 5. API surface (netzsim, default :8000)

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Built-in live HTML monitor (uses the WebSocket) |
| GET | `/health` | Liveness |
| GET | `/manual` | The German user manual PDF (docs/Benutzerhandbuch.pdf, Hilfe menu) |
| GET | `/status` | Engine state: running, step, day, steps_per_day, interval |
| GET | `/network` | Static topology (buses+`x,y` layout, lines, trafos, ext_grids, counts) |
| GET | `/state` | Latest solved `StepResult` (404 until first solve) |
| GET | `/history?limit=N` | Recent results from the in-memory ring buffer |
| POST | `/control/start` | Start the loop |
| POST | `/control/pause` | Pause |
| POST | `/control/resume` | Resume |
| POST | `/control/seek?step=N` | Jump to a step |
| GET | `/grids` | List importable grids from the committed dataset (`available`, `grids[]`) |
| GET | `/grids/{id}` | Net-free topology preview of a catalog grid (+ converter `notes`) |
| POST | `/grids/import` | Body `{doc, name?}` → write a gridgen/gridedit JSON to `user_grids/` (validated by converting; 400 removes it) |
| GET | `/loadgen/archetypes` | List cached LPG archetypes (`available`, `ev_available`, metadata) |
| POST | `/loadgen/assign` | Body `{grid_id, policy}` → preview net curve (load + EV − PV) + assignment + `trafo_sn_mva`, `n_households`, `n_mfh` |
| POST | `/config/apply` | Body `{grid_id, loadgen?}` → convert (+ LPG loads, EV, PV) + `engine.reconfigure` |
| GET | `/config/active` | Currently loaded grid metadata (id, name, counts, source, load_source, n_ev, n_pv, notes) |
| GET | `/measurements` | Meter placement (`node_buses`, `trafo_idxs`), `coverage`, `presets`, `expose_ground_truth` |
| POST | `/measurements/node` | Body `{bus}` → install a smart meter at a bus |
| DELETE | `/measurements/node/{bus}` | Remove a node's smart meter |
| POST | `/measurements/trafo` | Body `{trafo}` → install a transformer meter |
| DELETE | `/measurements/trafo/{trafo}` | Remove a transformer meter |
| POST | `/measurements/preset?name=` | Bulk: `all_nodes` \| `all_trafos` \| `substation_trafos` \| `clear` |
| POST | `/measurements/node\|trafo` | Upsert: optional `mode` sets the DEVICE's TAF fidelity (per-device since 2026-07-07: TAF-7 household meters mix with 1-min SMGWs; `/measurements/mode` = bulk switch + default for new devices; `placement()` carries `node_modes`/`trafo_modes`, scenarios persist them, `MeasurementSet.all_standard` gates the estimation raster, `measured_curves` rasters per device; UI: TAF mini-segment in the section's Messwerte block. TAF-7 granularity is STRICT: no intra-window updates — the reading is absent until the first completed 15-min window (also right after switching), and the estimator treats a data-less meter as unmetered so its pseudo-load survives and the WLS stays observable; toggling bumps `meterStamp` so the day graphs refetch in the new raster) |
| GET/POST | `/estimation/config` | Estimation policy (PV/EV pseudo, load basis, std %, zero injection) |
| GET | `/recording` | Session-recorder status (active id, steps, bytes) |
| POST | `/recording/start` | Body `{name?}` → record every published step to `data/recordings/<id>/` |
| POST | `/recording/stop` | Finish the active recording (writes metadata.json) |
| GET | `/recordings` | Stored recordings + active status |
| GET | `/recordings/{id}/download` | The recording as a ZIP of CSVs (409 while being written) |
| DELETE | `/recordings/{id}` | Remove a stored recording |
| POST | `/export/days` | Body `{days: n\|[..], name?, estimate?}` → replay whole days offline into a pack |
| GET | `/export` | Bulk-export progress (steps done/total, ETA, error) |
| POST | `/export/cancel` | Stop the running bulk export (partial pack is finalized) |
| WS | `/ws` | Live stream: one JSON `StepResult` per solved step |

**`StepResult`** (see `simulator.py`): `step, day, time_of_day ("HH:MM"),
converged, solve_ms, timestamp (unix s), buses[], lines[], trafos[], ext_grids[],
summary, error`. Each `trafos[]` entry = `{index, name, hv_bus, lv_bus,
loading_percent, p_hv_mw, q_hv_mvar, i_hv_ka, pl_mw}`. `summary` = `{n_bus,
n_line, n_trafo, vm_pu_min, vm_pu_max, max_line_loading_percent,
max_trafo_loading_percent, total_load_mw, total_gen_mw, total_ext_grid_mw,
total_losses_mw}` (`max_trafo_loading_percent` is `null` when the grid has no
transformer).

**Observability projection** (always present on `StepResult`): `measurements =
{nodes[], trafos[], coverage, phases:3, balanced:true}` — readings ONLY at placed
meters. A node reading = `{bus, name, vm_pu, v_ll_kv, p_mw, q_mvar, s_mva, i_ka}`
(three-phase sums; `i_ka = S/(√3·V_LL)`); a trafo reading =
`{trafo, name, hv_bus, lv_bus, loading_percent, p_hv_mw, q_hv_mvar, i_hv_ka,
pl_mw}`. `observed_summary` aggregates over metered elements only
(`vm_pu_min/max`, `max_trafo_loading_percent`, `measured_node_p_mw`, + coverage).
The truth fields (`buses/lines/trafos/ext_grids/summary`) are **stripped from the
wire** when `NETZSIM_EXPOSE_GROUND_TRUTH=false` — see §12.

---

## 6. Configuration (env vars)

netzsim (prefix `NETZSIM_`, loaded via pydantic-settings, `.env` supported — see
`.env.example`): `DATA_DIR`, `STEP_INTERVAL_SECONDS` (default 1.0),
`STEPS_PER_DAY` (1440), `AUTOSTART` (true), `HISTORY_SIZE` (1440), `WARM_START`
(true), `HOST`, `PORT` (8000), `LOG_LEVEL`, `DING0_DIR` (committed ding0 grids,
default `./data/ding0_grids`), `GRID_LIBRARY` (manifest, default
`./data/grid_library.json`), `CORS_ORIGINS` (default `*`),
`LPG_LIBRARY_DIR` (cached household profiles, default `./data/lpg_library`),
`EXPOSE_GROUND_TRUTH` (default `true` — set `false` to enforce strict
observability, stripping the true power flow from `/state`, `/ws`, `/history`;
see §12), `RECORDINGS_DIR` (default `./data/recordings`, gitignored) and
`RECORD` (default `false`; `true` = continuous recording from startup on,
restarting per grid apply / scenario load).

> Note: `STEPS_PER_DAY` exists in config but the simulator currently derives steps
> from the input files (`InputData.steps_per_day`). Keep the env value and the file
> `steps` consistent (both 1440 in the sample). See §10.

collector (env in `docker-compose.yml`): `NETZSIM_URL`, `INFLUX_URL`,
`INFLUX_TOKEN`, `INFLUX_ORG`, `INFLUX_BUCKET`, `POLL_INTERVAL_SECONDS` (0.5 —
keep ≤ the sim step interval so no step is missed).

---

## 7. InfluxDB schema (written by the collector)

Bucket `powerflow`, org `netzsim`. Point time = `StepResult.timestamp`
(wall-clock solve time → ns), so Grafana "last 5 min" tracks the live sim.
Collector dedupes by `(day, step)` → exactly one write per simulated step.

| Measurement | Tags | Fields |
|-------------|------|--------|
| `summary` | `time_of_day` | `converged`, `solve_ms`, all `summary.*`, `day`, `step` |
| `bus` | `bus_index`, `bus_name`, `time_of_day` | `vm_pu`, `va_degree`, `p_mw`, `q_mvar` |
| `line` | `line_index`, `line_name`, `time_of_day` | `loading_percent`, `i_ka`, `p_from_mw`, `pl_mw` |
| `trafo` | `trafo_index`, `trafo_name`, `time_of_day` | `loading_percent`, `p_hv_mw`, `q_hv_mvar`, `i_hv_ka`, `pl_mw` |
| `ext_grid` | `eg_index`, `eg_name`, `time_of_day` | `p_mw`, `q_mvar` |

`day`/`step` are **fields, not tags**, to avoid unbounded tag cardinality
(`day` grows forever).

Grafana datasource is provisioned with uid `influxdb` (Flux). Dashboard
`netzsim.json` (uid `netzsim-powerflow`) has 7 panels: bus voltages, line loading
(100 % threshold), power balance, max-loading gauge, voltage min/max, solve time,
solver status.

---

## 8. How to run

**Local (app 1 only):**
```bash
python -m venv .venv && source .venv/bin/activate    # Win: .venv\Scripts\activate
pip install -r requirements.txt
python scripts/generate_sample_data.py               # writes data/*.json
PYTHONPATH=src python -m netzsim.main                 # http://localhost:8000/
```

**UI (app 3) — dev:** run the backend (above), then:
```bash
cd ui && npm install && npm run dev          # http://localhost:5173 (proxies to :8000)
```
> Dev proxy targets `http://127.0.0.1:8000` (NOT `localhost`): on Windows
> `localhost` resolves to IPv6 `::1` first, which uvicorn (IPv4-only) refuses.

**Full stack (Docker):**
```bash
docker compose up --build
# ui :8080 · netzsim :8000 · influxdb :8086 (admin/netzsim-admin) · grafana :3000 (admin/admin)
```
The `netzsim` service serves grids from the committed dataset under the mounted
`./data` (ding0 MV grids, street-routed LV grids, the manifest, the LPG library —
see `data/DATASET.md`); no external archive is needed.

**Tests:**
```bash
pip install -e ".[dev]"   # or: pip install pytest httpx
pytest                    # backend suite (19 tests)
cd ui && npm run build    # type-check + build the frontend
```

---

## 9. Current status — what's done & verified

**Implemented & working (app 1):** full pipeline data→net→step→API, async realtime
engine with day-wrap + pause/resume/seek, REST + WebSocket + HTML monitor,
sample data generator, smoke tests.

**Implemented (app 2):** collector (REST→InfluxDB), InfluxDB + Grafana services,
provisioned datasource + dashboard, all in compose.

**Verified in the dev environment (no Docker available there):**
- ✅ `pytest` → 3 passed (load, build 5-bus net, solve at 12:00, day wraps at step 1440).
- ✅ Live server: `/health`, `/status`, `/network`, `/state` OK; engine ticks; physics sane
  (midday PV export, night slack-only).
- ✅ WebSocket `/ws` streams one StepResult per step; pause/resume work.
- ✅ `pandapower 3.4.0` installs and runs; `pp.LoadflowNotConverged` exists.
- ✅ Collector `build_points` produces valid InfluxDB line protocol from a real
  `/state` payload (sample: 11 points/step = 1 summary + 5 bus + 4 line +
  1 ext_grid; **+1 `trafo` point per transformer** on grids that have one).
- ✅ All YAML + dashboard JSON parse.

**NOT yet verified (do this first when resuming):**
- ⚠️ **Full `docker compose up --build` has never been run** (no Docker in the build
  env). Image builds and the InfluxDB↔collector↔Grafana wiring are unproven
  end-to-end. **This is the #1 thing to validate.**
- ⚠️ Grafana dashboard panels not visually confirmed against real InfluxDB data.

---

## 10. Known gaps / TODO / good next steps

- **Run the full stack once** and confirm Grafana shows live data (see §9 ⚠️).
- **Secrets:** InfluxDB token (`netzsim-dev-token`) and Grafana/InfluxDB
  passwords are **dev defaults hard-coded in `docker-compose.yml`** and
  `visualization/grafana/provisioning/datasources/influxdb.yml`. Move to a `.env`
  / secrets before any non-local use.
- **`STEPS_PER_DAY` config is currently unused** by the simulator (steps come from
  the input files). Either wire it through or remove it to avoid confusion.
- **No result persistence in netzsim** beyond the in-memory ring buffer
  (`HISTORY_SIZE`). InfluxDB is the durable store; netzsim itself forgets history
  on restart.
- **No CORS config** on FastAPI — add `CORSMiddleware` if a separate frontend will
  call it from a browser.
- **pandapower version**: `requirements.txt` pins `>=2.14`; dev verified on 3.4.0.
  If pinning for reproducibility, pin to the tested 3.x.
- Possible enhancements: transformer/line outage scenarios, controllable elements,
  per-step CSV/Parquet export, richer frontend, alerting on voltage/loading limits.
- **Vertical MV/LV smart-grid integration — phases 0, 1.1, 1.2 are BUILT
  (branch `feature/vertical`), the rest is planned**: see
  `docs/VERTIKALE_INTEGRATION.md` (2026-07-08).
  - *Phase 0 — cells*: every importer describes its vertical structure as
    **ONS cells** (`GridInputs.cells` / `InputData.cells`, plain dicts
    `{id, name, buses, lv_busbar, mv_bus, station_trafos, lumped}`,
    cross-validated in `data_loader`): `district_import` emits spliced cells
    + inherits lumped ones from ding0 `scope="mv"` (aggregate loads named
    `lv_<gid>` — that name prefix is the intra-module contract),
    `convert_osm_lv` = exactly one cell, `gridedit_mv_import` = degenerate
    cells per drawn station, file-based grids have none. `Simulator.cells` +
    `cell_of_bus` are the runtime handles; `topology()`/`/network` expose
    `cells[]`. Tests: `tests/test_cells.py`.
  - *Phase 1.1 — cell metering presets*: `digital_stations` (one station
    measurement per cell: trafo meter, MV-bus/busbar stand-ins for
    lumped/trafo-less cells) and `cell_full?cell=<id>` (full SMGW rollout of
    one cell); `/measurements` reports per-cell coverage (`cells[]` with
    `station_metered`); Messungen menu shows the preset when cells exist.
  - *Phase 1.2 — hierarchical two-stage WLS*: `HierarchicalEstimator`
    (estimator.py) runs one LOCAL WLS per spliced cell (subnet = members +
    feeding MV bus; slack there, setpoint = previous MV estimate, 1.0 first)
    and feeds each cell's **boundary flow** (measured > cell-estimated >
    profile pseudo, per-cell `boundary_src`) into the reduced-MV-net WLS via
    `Estimator.run(boundary=...)`. Composed result mirrors the monolithic
    shape + `mode` + `cells[]` (per-cell `error` stripped in strict mode,
    state.py). Policy knob `EstConfig.hierarchy` = auto|monolithic|
    hierarchical (API + Schätz-Richtlinie dialog); `wants_hierarchy()`
    resolves it (standalone LV = always monolithic — no MV level).
    `Simulator._make_estimator` dispatches for the live loop AND `daily_est`
    (sweep re-keys via est-config sig). Tests:
    `tests/test_hierarchical_estimation.py` (4); suite 119 passed.
    **Honest finding**: on `mv_rural_3150` hierarchical is equally accurate
    (~3 mpu with digital stations) but NOT faster (~1.16 vs ~1.06 s) — only
    3/157 cells are street-routed, the reduced MV graph keeps 247 buses.
  - *Phase 2 — grid-traffic-light cascade*: controller scopes `"cell"`
    (domain = ONE spliced cell: its station trafo [meter] + cell lines
    [estimate only] + the DERs behind them; direction from the cell's
    boundary flow) and `"mv"` (the COORDINATOR: watches only MV lines +
    HV/MV trafo via meters/MV estimate, throttles nothing itself — its
    ratcheted factors broadcast as SIGNALS to every placed cell controller,
    applied as `min(local law, signal)` = `Controller.effective_ev/pv`,
    which `as_dict()` reports as the factors). One uniform signal factor =
    proportional burden per cell (decision E3). A locally meter-less cell
    controller still EXECUTES signals (command path needs a Steuerbox, not
    a meter); cells without a controller stay uncoordinated. `station`
    keeps its whole-grid semantics (LV back-compat). Scenarios persist
    `cell`; API `POST /controller {scope, bus?, cell?}`. Domain index sets
    (`_cell_lines`/`_mv_lines`/...) precomputed in `Simulator.__init__`.
    Tests: `tests/test_controller_vertical.py` (4; domain-isolation test
    blocks estimation via `sim._est_wall = float("inf")` so meters are the
    only source). Suite 123 passed. NOTE: once ANY meter exists, the
    estimate covers every cell (pseudo cell stages) — "blind" in the old
    sense only exists without any estimate.
  - *Phase 6 (backend + scenario) — reference scenario 4*
    `data/scenarios/4-feierabend-im-bezirk-…json`: district `mv_rural_3150`
    (LPG loads, no household EVs), the Feierabend wave = 42 aggregate
    200-kW wallbox blocks (der_ops `add_ev`, staggered 17:00–18:00, 4 h) at
    the lumped stations of ring L19/L222 (8.4 MW — ding0 MV rings are
    CLOSED; one segment only overloads via many stations), Steuerboxen
    (cell controllers) at those 42 lumped cells — `cell` scope now also
    accepts LUMPED cells (domain = the DERs at their mv_bus; locally blind
    but executes signals), digital stations metering, clock 17:40 @ 0.2 s.
    Numbers (verified live): truth segment L222 ≈ 108 %, hierarchical
    estimate sees 108.0 %, max station meter 61.7 % (no cell sees it);
    placing the MV coordinator (limit 100) → one ratchet, EV signal 0.75,
    42/42 boxes dim, segment settles ~86–87 % (stable in the hysteresis
    band). **Control-law fix that made this stable**: estimate-fed
    controllers act once per NEW telegram (`est["seq"]` counter,
    `Controller.est_stamp`) — per-step ratcheting against a stale estimate
    (the wall-clock-throttled district estimate refreshes ~every 12
    sim-minutes) oscillated hard. Meter-fed controllers keep per-step
    dynamics. The trimmed picker manifest now carries the district + its
    two extra LV grids (E5): 4 LV + 1 MV entries. Tests:
    `tests/test_scenario4.py` (2); suite 125 passed.
  STILL OPEN: rONT (phase 3), UI drill-down (phase 4: cell KPIs/ampel,
  Zellen section, coordinator signal table AND click-placement of cell/mv
  controllers — until then scenario 4's coordinator is placed via API),
  gridedit `lv_ref` (phase 5), the manual chapter for the vertical story.

---

## 11. Conventions & gotchas

- **src layout**: package is under `src/netzsim`; run with `PYTHONPATH=src` or
  `pip install -e .`. Tests set `pythonpath=["src"]` via `pyproject.toml`.
- **Bus index == position in `grid_structure.buses`.** Don't reorder buses without
  updating every reference.
- **Element order matters**: profile arrays are aligned to pandapower element
  indices by insertion order in `network_builder`.
- **Numeric rounding & JSON safety**: results are rounded to 6 digits in
  `simulator._r`, which also maps **non-finite** floats (NaN/±Inf) to `null`.
  Without this, Python's `json` emits the literal `NaN`, which browsers'
  `JSON.parse` reject → the WS client silently drops every frame. Keep all result
  floats going through `_r`.
- **Collector resilience**: it waits for both services' `/health`, retries on
  failure, and skips writes until the first step is solved (`/state` 404).
- **ding0 geo grids & the map**: `data/ding0_grids/` holds pre-generated ding0
  grids (eDisGo CSV) with **real WGS84 lon/lat**. `ding0_import.convert_ding0_csv`
  carries coords onto `BusSpec.geo` (LV buses get their station's coord). When a
  grid has geo, `/network` sets `has_geo: true` and per-bus `geo: [lon, lat]`, and
  the UI defaults to the **Map** view (`MapDiagram.tsx`, Leaflet + CARTO/OSM
  tiles). The map mimics ding0's `plot_mv_topology` aesthetic: **light** CARTO
  basemap by default (Light/Dark toggle), lines on a **green→amber→red** ramp by
  loading (no blue — an idle line reads as healthy, not cold), buses on a
  **Reds** ramp by voltage Δ, amber MV/LV stations (`#f2ae00`), and two
  colorbars — all animating from live results. Colormaps live in `scales.ts`
  (`lineLoadingColor`, `voltageReds`, `LOADING_GRADIENT`, `REDS_GRADIENT`). The default 5-bus
  sample (no geo) falls back to the synthetic Geographic/Schematic SVG views
  (which keep the discrete traffic-light scales).
- **Grids come from the separate `gridgen` project** (`../gridgen`), NOT from
  netzsim. netzsim is a pure consumer: it never runs ding0/OSM/OEP. The committed
  dataset under `data/` (see `data/DATASET.md`) is a snapshot — ding0 MV grids
  (eDisGo CSV, real lon/lat), street-routed LV grids (gridformat JSON), and the
  `grid_library.json` manifest. The OEP work-arounds, the OSM cable routing +
  cable-cabinet logic, and curated-library selection all live in `gridgen` now (see
  `gridgen/docs/`). To refresh the dataset, regenerate with `gridgen` and re-commit
  the snapshot + bump the pin in `data/DATASET.md`. See `docs/GRIDGEN_EXTRACTION.md`.
- **One merged grid workflow view** (`NetzStudio.tsx`, menu "Netz → Netz &
  Lasten…", 2026-07-06): pick a grid from the list (library + user grids) or
  **import a JSON file** (gridgen/gridedit → `POST /grids/import`, written to
  `user_grids/`, validated by converting), configure loads/EV/PV, check the
  **transformer loading** (auto-previewed net curve with the trafo rating as a
  dashed limit line, peak % KPI), then apply & start. The old separate
  GridBrowser (voltage/character/node-count picker) and LoadStudio pages are
  gone. `GridCatalog` is still **manifest-driven** (`library_manifest`, config
  `grid_library`); with no manifest it falls back to raw ding0 dirs.
- **Multi-family buildings (MFH)**: `LoadgenPolicy.mfh` — `"auto"` (UI default;
  applies in **suburban/urban** grids), `"on"`, or `"off"` (**backend default**,
  keeps saved scenario recipes bit-identical). Each load element then sums
  `mfh_min..mfh_max` (3–6) household profiles (`assign.py households_range`);
  the load doc rows carry `households: n` (models.LoadProfile), and the
  estimator's **SLP basis scales with the metering-point count** (the DSO knows
  its meter counts; `Estimator(household_counts=…)`).
- **User-drawn grids** (gridedit) land in `data/user_grids/` (gitignored) and are
  rescanned per `/grids` listing. LV files are gridformat (→ `convert_osm_lv`);
  MV files carry `format: "gridedit-mv"` (→ `gridedit_mv_import.convert_gridedit_mv`:
  appended 110-kV bus + standard HV/MV trafo, stations as lumped loads with
  `household: false`, per-type profiles — mall/HPC `_daily` variants, wind gusty
  deterministic, biogas flat, PV bell). `GenProfile.kind` ("pv"|"wind"|"biogas")
  gates the real-PV day slider: only PV sgens follow the measured day shapes
  (`Simulator._sgen_is_pv`); wind/biogas keep their built-in profiles, and a grid
  without PV doesn't attach the day slider at all (`n_days` stays 1).
- **netzsim's importers** translate the dataset to `GridInputs`:
  `ding0_import.convert_ding0_csv` has a `scope` — `"mv"` keeps the MV graph and
  folds each LV grid into one lumped load at its feeding MV bus; `"lv"` extracts a
  standalone 0.4 kV grid fed at its busbar; `"full"` is the whole district. Manifest
  LV entries carry an `osm_grid` path, which `GridCatalog.get_inputs` dispatches to
  `osm_lv_import.convert_osm_lv` (overrides `scope`). **Line geometry** flows
  `LineSpec.geometry` → `simulator.topology()` (attached by line index) →
  `/network` → `MapDiagram`, which draws each cable as a Leaflet polyline along the
  road (else a straight segment); cable **cabinets** (`BusSpec.kind="cabinet"`) →
  topology `cabinet_buses` → green circles on the map. See `[[lv-grid-geo-next-step]]`.
- **Windows dev env**: this was developed on Windows (`.venv/Scripts/python.exe`).
  Use Bash-tool paths accordingly.
```

---

## 12. Observability layer (reality vs what you can measure)

The simulator computes the **full** power flow every step (reality), but the UI
only *reveals* a quantity where a **measurement device** has been placed. This
separates ground truth from the operator's partial view — the groundwork for
state estimation.

- **Two device kinds** (`measurements.py`, `MeasurementSet`):
  - **Smart meter** at a bus → reveals that node's `vm_pu`, `p_mw`, `q_mvar`, and
    derived current `i_ka = S / (√3 · V_LL)`. The power flow is **balanced
    single-phase-equivalent**, so the three phases are symmetric: reported P/Q are
    the three-phase sums, per-phase = sum/3, current is the per-phase line current.
    True per-phase would need pandapower's `runpp_3ph` (a separate mode — not done).
  - **Transformer meter** → reveals that transformer's `loading_percent` + HV P/Q/I.
  - **Lines carry no meter** in this model → line loading/current is *unknown*
    (drawn dim/grey) unless ground truth is revealed. This is deliberate: it shows
    how sparse real observability is.
- **Placement is grid-specific** and held per-`Simulator` (like batteries), so it
  **resets on grid swap** (`engine.reconfigure` builds a fresh `Simulator`).
  Placed via the UI (click a node/trafo → "place meter") or bulk presets
  (`all_nodes` / `all_trafos` / `substation_trafos` / `clear`).
- **Projection happens in `simulator._collect`**: after the solve, `meters.observe(net)`
  → `StepResult.measurements`, `meters.observed_summary(...)` → `observed_summary`.
- **`NETZSIM_EXPOSE_GROUND_TRUTH`** (default `true`): when `false`, `StateStore`
  strips `buses/lines/trafos/ext_grids/summary` from `/state`, `/ws`, `/history`
  so only the observed projection leaves the server (strict observability). Default
  `true` keeps the InfluxDB collector and the UI's **reveal-ground-truth toggle**
  working. The toggle (`LivePowerFlow`, off by default) overlays the true power
  flow, faded, for comparison.
- **UI** (`LivePowerFlow` + `GridDiagram`/`MapDiagram` + `MeasurementPanel`):
  default view = observed only (unmetered nodes/trafos grey with `?`, blue meter
  badges on metered ones); sidebar shows the observed summary + coverage. The
  reveal toggle is hidden when the server enforces strict mode.
- **Known gap**: the per-element *daily profile* endpoints (`/node/{}/profiles`,
  `/line/…`, `/trafo/…`, used by the click-to-graph panels) still return the full
  simulated curves regardless of meter placement or `expose_ground_truth`. They
  bypass the observability layer; gate them too if strict end-to-end hiding is
  needed.

### Overload controllers (feature/control branch)

Placeable **netzdienliche Steuerung** (`controller.py`, modelled after §14a
EnWG dimming + Einspeisemanagement): a controller placed like a battery/meter
(element menu on a node or the trafo) watches the loading of its domain and
throttles EV charging or PV feed-in stepwise (−25 pp per violating step) when
`limit_pct` (default 100 %) is exceeded, releasing with hysteresis (+5 pp per
step below `release_pct` 80 %). Closed loop: factors from the CURRENT solved
step act on the NEXT one. The lever follows the flow direction: net-exporting
domain → PV, net-importing → EV. Scopes: `station` (whole grid, badge at the
LV busbar) and `bus` (that node's DERs, reacting to its adjacent lines).
Factors are applied in `_apply_step` BEFORE batteries; `StepResult.controllers`
carries live factors; scenarios store `controllers` like batteries. API:
`GET /controllers`, `POST /controller`, `POST /controller/{id}/config`,
`DELETE /controller/{id}`. UI: 🎛️ menu items, section block (limit input +
live EV/PV factors + status), 🎛️ badges. Tests: `tests/test_controller.py`.

**The controller only sees what the operator sees** (customer feedback
2026-07-07): `_controller_update` is fed exclusively from `res.measurements`
(meter readings — a trafo meter's `loading_percent` counts as source
"meter") and `res.estimated` (WLS estimate; line loadings exist ONLY here,
lines carry no meters), never from the truth arrays. Flow direction: station
scope = sign of the (measured, else estimated) HV-side trafo flow; bus scope
= sign of the node's (measured, else estimated) bus injection. In `run_step`
the estimation therefore runs BEFORE `_controller_update`, and
`res.controllers` is refreshed after the update. `Controller.seen_pct` /
`seen_src` ("meter" | "estimate" | null) report what it saw; without meters
the controller is **blind** and holds its factors (UI: ⚠️ "blind — keine
Messdaten", plus a "sieht Auslastung" row). Consequence: control quality =
observability. Scenario #3's mid-feeder overload is only regulated WITH the
plant/wallbox SMGWs (blind estimate → controller idle, overload persists);
scenario #2 works with the station meter alone (the estimate reconstructs
the feeder head from the summed flow).
`test_controller_blind_without_meters_holds_despite_overload` pins the
blindness; the regulation tests meter the grid fully and force a fresh
estimate per step via `sim._est_wall = 0.0` (the estimator's wall-clock
throttle would starve back-to-back test steps).

Known limitation: the daily-sweep curves (day graphs) show the UNCONTROLLED
day — controllers act only in the live loop.

### Scenarios (saved live setups for education/demos)

A scenario is a **recipe, not a snapshot** (`scenarios.py`, files under
`data/scenarios/*.json`, hand-editable): grid_id + the seeded loadgen policy
(remembered in `runtime.active` since apply) + bus-addressed runtime DER ops
(`Simulator.der_log`, coalesced; add+remove cancels) + battery/meter snapshots +
the engine clock. `POST /scenarios` saves the current setup, `POST
/scenarios/{id}/load` replays the chain (apply → `apply_der_op` per entry,
tolerant → batteries → meters → seek day/step → run), `GET/DELETE /scenarios…`
manage. UI: "Szenarien" section in the Live side panel. SOC is not captured
(demos start at 50 %). Robustness note: runtime mutations can race the engine's
solve (no locks by user decision) — `run_step`'s ladder therefore catches ANY
exception (a poisoned step = one non-converged frame, `_solved_once=False`,
self-heals next step) instead of letting the engine task die.

### State estimation (the operator's *calculated* view)

`estimator.py` adds the third layer beside reality and observation: **WLS state
estimation** (`pandapower.estimation`) from only what an operator has — the grid
model (lines/trafos known), the placed meter readings (+ slack setpoint),
structural zero-injection knowledge (junctions/cabinets), and profile-based
pseudo-measurements (per-bus **daily-mean** load, std = 50 % of daily peak;
battery buses get rating-bounded pseudos since setpoints are unknown). It runs
on a lazily deep-copied net whenever ≥ 1 meter is placed, **in the metering
raster** (customer requirement 2026-07-07: the estimate can only be as fine as
the meters deliver — when ALL devices are "standard"/TAF 7 Lastgang
(`meters.all_standard`) → a new estimate only at 15-min window boundaries,
held in between; a single "full"/TAF 9/10/14 device → every 1-min step),
additionally **wall-clock throttled** as a pure compute guard
(spaced 2× its own runtime — every step on LV grids, every ~3 s on the 475-bus
district at ~1–1.6 s; WLS with full metering on the 62-bus suburban grid costs
~0.4 s; numba does NOT speed pandapower's SE path).
`StepResult.estimated = {buses, lines, trafos, solve_ms, step, day, error}`
mirrors the truth arrays; `error` (max/mean |ΔV|, max |ΔI| vs truth) is stripped
by `StateStore` in strict mode, the estimate itself survives. UI: a third
segmented view mode 👁 Wahrheit / Nur beobachtet / 🧮 Schätzung (`LivePowerFlow`
feeds the estimated arrays through the diagrams' truth path); the Übersicht
shows estimate aggregates + the error metric. Tests: `tests/test_estimation.py`.
Quality on the 30-bus LV grid: exact under full metering; < 10 mpu with only the
station trafo meter + pseudo-loads.

**Estimation policy** (customer feedback 2026-07-06, `EstConfig` in
`estimator.py`, UI top tab "🧮 Schätzung", `GET/POST /estimation/config`,
re-applied on grid swap by the engine): what the estimation may use. Defaults
mirror DSO practice — **no PV pseudo** (plants differ in orientation; the
plant size still widens the pseudo std), **no EV pseudo** (stochastic),
`load_basis` "profile" (idealized per-customer daily means) vs **"slp"**
(every household the same `slp_annual_kwh`, applies to household rows only —
RLM customers keep true means; `LoadProfile.household` carries the flag),
`pseudo_std_pct`, `zero_injection` toggleable. **Sweep resolution & view
layers** (customer requirements 2026-07-07): the TRUTH curves of the day
graphs solve the power flow at EVERY step — full input raster, 1 min on the
committed grids — made affordable by pandapower's recycle path (`recycle=
{"bus_pq": True, ...}`, only bus injections rebuilt; validated bit-identical
incl. storage, ~5.6 vs ~11 ms/solve on 62 buses; skipped when the slack
setpoint profile varies). The day-graph data is **layered by view** (profile
endpoints take `?view=truth|measured|est`, the UI passes its perspective;
menu label "Nur beobachtet" → **"Gemessen"**): `daily_curves` = truth only
(bus V/P, line I/loading, trafo P/loading — NO WLS cost, ~10 s first click
on the 62-bus grid), `measured_curves` = derived array math on the truth
sweep, ONLY metered elements and only the quantities their device delivers
in the metering raster (TAF 7 standard: 15-min window means of P, no V/no
loading; TAF 9/10/14 full: 1-min pass-through; lines: never anything),
`daily_est` = lazy WLS mini-sweep re-solving only at the estimate raster,
stored inside the truth cache entry under `_est`, keyed on placement/mode/
est-config (so only the Schätzung view pays the ~15-25 s WLS; policy change
drops just this layer). Est raster: `_est_sweep_min` **pinned per grid** to
clean 15/30/60/120-min tiers (decided once from a robust cost measurement,
never finer than the metering raster — a 1-min WLS sweep would cost ~9 min
on the suburban grid). `battery_profiles` also reports the full raster.
UI: profile components render whatever layers the response carries (measured
series in near-white "device" color, unmetered elements/lines show a hint);
`ProfileGraph.valAt` walks back to the last filled sample so hover readouts
work on the sparse rasters. Honesty tripwire:
`test_estimation_honesty_pv_rise_unknowable` — rural feeder, strong midday PV,
5 % metering, no PV knowledge → the estimator MUST miss > 60 % of the voltage
rise (fails = truth is leaking). Note: the policy is an operator setting —
deliberately NOT part of scenario recipes.

### Session recording (CSV export of live runs, 2026-07-07 — round 1: backend)

`recorder.py` taps `StateStore.publish` via an optional **sink** and receives
exactly the projected wire payload — strict mode therefore strips the truth
from recordings automatically. A dedicated writer thread (queue-fed, never
blocks the engine loop) appends tidy CSVs to `data/recordings/<id>/`:
`summary` / `observed_summary` (one row per step), `buses` / `lines` /
`trafos` / `ext_grids` / `batteries` / `controllers` (long format),
`measurements_nodes` / `measurements_trafos` (the Gemessen layer, TAF
raster), `estimated_*` (one block per NEW estimate — deduped on the
estimate's own (day, step)). Files appear lazily on first row; standard CSV
dialect (comma, dot decimals, UTF-8, None→empty, bool→0/1). Duplicate
(day, step) publishes are dropped; backward seeks legitimately repeat keys
(wall-clock `timestamp` column disambiguates). `metadata.json` holds the
reproducibility recipe (grid + loadgen, meter placement/TAF mode, est
policy, engine interval, netzsim version). One recording = ONE
configuration: `/config/apply` and scenario load auto-finish the active
recording (and start a fresh one when `NETZSIM_RECORD=true`). Download
packs a cached ZIP (`Recorder.pack`, 409 while being written); ids are
validated against path traversal. Tests: `tests/test_recorder.py` (5).
Verified live: 91 steps on scenario 2 → 91×62 bus rows, 91×12 meter rows,
32 estimate blocks, 241-KiB ZIP.

**Bulk export** (`exporter.py`, the primary teaching path — customer
decision: "give me N days as one pack" beats waiting for the accelerated
clock): `POST /export/days {days: n|[indices], name?, estimate?}` REPLAYS
the current setup offline — the endpoint briefly pauses the engine, deep-
copies the live Simulator (runtime DERs, batteries, controllers, meters,
policy included; no locks per project convention, the pause drains the
in-flight step), resumes the clock and drives `run_step` over every step of
the requested days in a worker thread, feeding a private `Recorder`. Output
is byte-compatible with a live recording and appears under `/recordings`.
Replay = LIVE physics: batteries start at 50 % SOC and integrate across
midnight, controllers really regulate, the estimate runs forced-fresh in
the metering raster (`estimate=false` skips WLS — much faster on TAF-9
setups where SE dominates). Day indices wrap modulo the real-PV days.
Progress via `GET /export` (steps, ETA), `POST /export/cancel` finalizes
the partial pack (`export.cancelled` in metadata). One export at a time;
download/delete guard against packs still being written (`_busy_ids`).
Tests: `tests/test_exporter.py` (3). Verified live: 2 days = 2880 steps in
62 s while the engine kept ticking (5-MiB ZIP, 2880×62 bus rows).

**UI** (since the Variante-B menu rework: menu Datei → "Daten-Export" in
`DateiMenu`, `MenuBar.tsx`): "⬇ Tage exportieren…" opens a dialog (day
count + include-estimate checkbox + cost hint); while running a progress
row (percent + ETA + Abbrechen) replaces the item; "⏺ Live-Sitzung
aufzeichnen" toggles the recorder (live step count on the stop button);
finished packs are listed as 💾 download links with ✕ delete. The menu
polls every 2 s while open; the menu BAR itself polls every 3 s and shows
clickable chips ("⏺ REC n" red / "⬇ Export p %" — click opens the Datei
menu) so activity stays visible with all menus closed. Only finished packs
appear in the list (metadata.json is written on stop, and the listing
requires it). Manual chapter exists (ch:export) — its screenshots/text
still show the pre-Variante-B menu ("Simulation") and need a refresh.

### Reference scenarios (the committed teaching set, 2026-07-06; #4 added 2026-07-08)

`data/scenarios/` holds **four reference scenarios** (the earlier
examples were removed on customer request); `data/grid_library.json` is
**trimmed to the grids they use** (`lv_rural_3150_300266`,
`lv_suburban_1864_265991`, and since scenario 4: `mv_rural_3150` + its two
other street-routed LV grids `lv_rural_3150_300668`/`_300575`) so the picker
only offers those. The full 20-entry manifest lives on as
`data/grid_library_full.json` (copy it back to restore;
`test_district_import.py` / `test_runtime_swap.py` point at the full file).
All include a measurement concept and start shortly before the critical time
(interval 0.2 s/step). Scenario 4 ("Feierabend im Bezirk — Engpass unsichtbar
für jede Zelle") is the VERTICAL teaching path — see the vertical-integration
bullet in §10 for its calibrated numbers and cascade walkthrough. 1–3:

1. **`1-bauernhof-pv-75-kw-spannungs-berh-hung`** — rural grid, 75-kWp PV at
   the 500-m feeder end (bus 24). Noon: ~252 V at the farm (EN 50160 limit
   253 V), main line >90 %. Estimate WITH the plant SMGW hits it to <1 V;
   without, the VNB sees ~245 V.
2. **`2-feierabend-laden-strang-berlast-nh-sicherung-l-st-aus`** — suburban
   grid, 12 of 24 households on feeder L43 charge 11-kW EVs staggered
   17:00–18:30 (PV share 50 % but dark by then). From ~18:45 the feeder head
   carries ~108 % / 243 A — the NH fuse *sees* this summed current and would
   trip after a while. Even the blind estimate (station meter only) finds the
   overload — the station measurement is exactly what the fuse sees.
3. **`3-mittags-berlast-unsichtbar-f-r-die-nh-sicherung`** — same suburban
   feeder, 8×27-kWp PV cluster at the far end, 6 EVs near the head charging
   PV surplus at 22 kW (10:30–14:30). Noon: middle segment L12 ~110 % / 248 A
   while the station feeder head carries only ~45 % / 102 A — the NH fuse
   never sees the overload. Blind estimate: ~4 % (clueless); with SMGWs at
   PV + wallboxes: ~110 % (exact). The core SMGW argument.
