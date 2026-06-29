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
│   ├── generate_sample_data.py   # regenerates data/ (5-bus, 1440-step example)
│   ├── import_grid.py            # CLI: European-Archetype xlsx -> 5 input JSON
│   └── build_lpg_library.py      # CLI: run LPG -> data/lpg_library/ (needs pylpg)
├── src/netzsim/              # the simulation package
│   ├── config.py             # pydantic-settings (env NETZSIM_*)
│   ├── models.py             # pydantic schemas for the 5 input files
│   ├── data_loader.py        # read + cross-validate inputs -> InputData
│   ├── network_builder.py    # build pandapower net once + numpy profile arrays
│   ├── simulator.py          # apply one step, run_step() -> StepResult
│   ├── engine.py             # async realtime loop (tick, day-wrap, pause/seek)
│   ├── state.py              # latest + history ring buffer + WS broadcast
│   ├── api.py                # FastAPI: REST + WS /ws + grid catalog/swap + monitor
│   ├── grid_import/          # xlsx grid-model -> netzsim inputs (European Archetype)
│   │   └── xlsx.py           # convert_workbook / convert_from_zip / write_inputs
│   ├── grid_catalog.py       # list/convert grids for /grids (xlsx archetypes + ding0)
│   ├── ding0_import.py       # pre-generated ding0 (eDisGo CSV) -> inputs, w/ real lat/lon
│   ├── layout.py             # bus coords: length-aware geographic (x,y) + tidy tree (tx,ty)
│   ├── loadgen/              # cached LPG library reader + assignment (runtime, no pylpg)
│   │   ├── library.py        # LoadLibrary: read data/lpg_library/{index,*}.json
│   │   ├── assign.py         # assign_to_loads: archetype/variant -> per-load household profiles
│   │   ├── pv.py             # assign_pv: synthetic clear-sky rooftop PV (sgen)
│   │   └── ev.py             # assign_ev: synthetic additive EV home-charging loads
│   └── main.py               # uvicorn entry point (console script: `netzsim`)
├── data/lpg_library/         # committed LPG profiles (index.json + {CHRxx}.json)
├── data/ding0_grids/         # committed pre-generated ding0 grids (eDisGo CSV, real lat/lon)
├── tests/
│   ├── test_simulator.py     # smoke tests (load/build/solve/day-wrap)
│   ├── test_grid_import.py   # converter tests (+ real-archetype end-to-end)
│   ├── test_runtime_swap.py  # grid catalog + engine.reconfigure (live grid swap)
│   ├── test_loadgen.py       # LPG library reader + assignment
│   └── test_ding0_import.py  # ding0 CSV import (geo + solve)
├── ui/                       # app 3: React + Vite + TS frontend (served by nginx)
│   ├── src/views/            # GridBrowser, LoadStudio, LivePowerFlow (3-step flow)
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
| GET | `/status` | Engine state: running, step, day, steps_per_day, interval |
| GET | `/network` | Static topology (buses+`x,y` layout, lines, trafos, ext_grids, counts) |
| GET | `/state` | Latest solved `StepResult` (404 until first solve) |
| GET | `/history?limit=N` | Recent results from the in-memory ring buffer |
| POST | `/control/start` | Start the loop |
| POST | `/control/pause` | Pause |
| POST | `/control/resume` | Resume |
| POST | `/control/seek?step=N` | Jump to a step |
| GET | `/grids` | List importable grids from the archive (`available`, `grids[]`) |
| GET | `/grids/{id}` | Net-free topology preview of a catalog grid (+ converter `notes`) |
| GET | `/grids/{id}/thumbnail` | PNG of the grid (from the archive), if present |
| GET | `/loadgen/archetypes` | List cached LPG archetypes (`available`, `ev_available`, metadata) |
| POST | `/loadgen/assign` | Body `{grid_id, policy}` → preview net curve (load + EV − PV) + assignment |
| POST | `/config/apply` | Body `{grid_id, loadgen?}` → convert (+ LPG loads, EV, PV) + `engine.reconfigure` |
| GET | `/config/active` | Currently loaded grid metadata (id, name, counts, source, load_source, n_ev, n_pv, notes) |
| WS | `/ws` | Live stream: one JSON `StepResult` per solved step |

**`StepResult`** (see `simulator.py`): `step, day, time_of_day ("HH:MM"),
converged, solve_ms, timestamp (unix s), buses[], lines[], trafos[], ext_grids[],
summary, error`. Each `trafos[]` entry = `{index, name, hv_bus, lv_bus,
loading_percent, p_hv_mw, q_hv_mvar, i_hv_ka, pl_mw}`. `summary` = `{n_bus,
n_line, n_trafo, vm_pu_min, vm_pu_max, max_line_loading_percent,
max_trafo_loading_percent, total_load_mw, total_gen_mw, total_ext_grid_mw,
total_losses_mw}` (`max_trafo_loading_percent` is `null` when the grid has no
transformer).

---

## 6. Configuration (env vars)

netzsim (prefix `NETZSIM_`, loaded via pydantic-settings, `.env` supported — see
`.env.example`): `DATA_DIR`, `STEP_INTERVAL_SECONDS` (default 1.0),
`STEPS_PER_DAY` (1440), `AUTOSTART` (true), `HISTORY_SIZE` (1440), `WARM_START`
(true), `HOST`, `PORT` (8000), `LOG_LEVEL`, `GRID_ARCHIVE` (the importable-grid
zip, default the European-Archetype archive), `GRID_FILTER` (path substring,
default `Low Voltage Network Models/03_LV`), `CORS_ORIGINS` (default `*`),
`LPG_LIBRARY_DIR` (cached household profiles, default `./data/lpg_library`).

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
The `netzsim` service mounts the grid archive zip read-only at `/app/grids.zip`
so `/grids` is populated; the LPG library is under the mounted `./data`.

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
  floats going through `_r`. (The grid converter also reconnects isolated feeders
  so buses don't go NaN in the first place — see below — but `_r` is the net.)
- **Island repair**: `grid_import` reconnects slack-less feeder islands (a
  workbook data gap) to the LV busbar via a synthetic tie line
  (`reconnect_islands`, default on). Recorded in `GridInputs.notes`; only
  `network_10` needed it across the 25 LV grids.
- **Collector resilience**: it waits for both services' `/health`, retries on
  failure, and skips writes until the first step is solved (`/state` 404).
- **ding0 geo grids & the map**: `data/ding0_grids/` holds pre-generated ding0
  grids (eDisGo CSV) with **real WGS84 lon/lat**. `ding0_import.convert_ding0_csv`
  carries coords onto `BusSpec.geo` (LV buses get their station's coord). When a
  grid has geo, `/network` sets `has_geo: true` and per-bus `geo: [lon, lat]`, and
  the UI defaults to the **Map** view (`MapDiagram.tsx`, Leaflet + CARTO/OSM
  tiles). The map mimics ding0's `plot_mv_topology` aesthetic: **light** CARTO
  basemap by default (Light/Dark toggle), lines on a **jet** colormap by loading,
  buses on a **Reds** ramp by voltage Δ, amber MV/LV stations (`#f2ae00`), and two
  colorbars — all animating from live results. Colormaps live in `scales.ts`
  (`jetColor`, `voltageReds`, `JET_GRADIENT`, `REDS_GRADIENT`). Non-geo (xlsx)
  grids fall back to the synthetic Geographic/Schematic SVG views (which keep the
  discrete traffic-light scales).
- **ding0 live generation WORKS over the OEP** (no local Postgres needed). Use
  `scripts/generate_ding0_grid.py <district_id ...>` with the Python-3.9 ding0
  conda env (`C:\Users\bell\ding0mamba\python.exe`) + a valid `~/.egoio`
  `[oedb]` token. It writes `data/ding0_grids/ding0_oep_<id>/` (auto-discovered by
  the catalog). The earlier "HTTP 400" was **not** a PostGIS-over-REST limit — it
  was two ding0/OEP bugs the script works around without editing site-packages:
  (1) ding0 passes `ST_Transform`'s SRID as a *string* (`'4326'`) which the OEP
  parser rejects — coerce it to int in the request body; (2) the generator
  materialized views `supply.ego_dp_*_powerplant_sq_mview` were dropped from the
  OEP (404) — skip `import_generators` (netzsim layers its own PV/EV). Verified:
  districts 1605 (14 buses) and 1003 (4395 buses) generate in seconds and solve.
  See `docs/DING0_GENERATION.md`. (Other Py-3.9 envs `C:\Users\bell\{python39,
  ding0env}` are leftover and unused.)
- **Curated grid library & the generator UI**: the Grid page is a *generator
  picker* — choose **voltage** (MV / LV), **area character** (rural / suburban /
  urban) and **approximate node count** (10–500) — not a list of the old LV
  archetypes (those are **no longer scanned**). It is backed by a committed
  library: `scripts/build_grid_library.py` (ding0 conda env) selects districts by
  OEP metadata (population density → character; load-area count → size), generates
  them, and writes `data/grid_library.json` — a manifest of entries
  `{id, name, voltage, character, nodes, source_dir, scope, lv_grid_id?}`.
  `GridCatalog` is **manifest-driven** (`library_manifest`, config `grid_library`);
  with no manifest it falls back to listing raw ding0 dirs. `convert_ding0_csv`
  gained a `scope`: `"mv"` keeps the MV graph and folds each LV grid into one
  lumped load at its feeding MV bus; `"lv"` extracts one standalone 0.4 kV grid fed
  at its busbar; `"full"` is the whole district (default). One generated district
  thus yields several library entries (1 MV + a few LV by size bucket).
- **Street-routed LV grids (OSM)**: ding0 0.2.1 does **not** geo-reference LV grids
  (its LV builder is a statistical cable-string model with no coordinates; `osmnx`
  is never called, and there is no published street-routed dataset). So LV library
  grids are **rebuilt geographically from OpenStreetMap** by
  `scripts/build_lv_osm_grids.py` (ding0 conda env, needs internet): it takes the
  LV station + load count from the committed ding0 grid, places loads at OSM
  **building footprints**, routes a cable **backbone along the streets**
  (shortest-path tree from the station, sized for downstream load via parallel
  cables) and taps each building onto the nearest road point. Output is a small
  JSON per grid under `data/lv_osm/<entry_id>.json`; each line carries a
  `geometry` polyline. Manifest LV entries gain `osm_grid` (path) which
  `GridCatalog.get_inputs` dispatches to `netzsim.osm_lv_import.convert_osm_lv`
  (overrides `scope`). **Line geometry** flows `LineSpec.geometry` →
  `simulator.topology()` (attached by line index) → `/network` → `MapDiagram`,
  which draws each cable as a Leaflet polyline along the road (else a straight
  segment). MV grids are unaffected (ding0 already geo-references them). See
  `[[lv-grid-geo-next-step]]` for the decision history.
- **Windows dev env**: this was developed on Windows (`.venv/Scripts/python.exe`).
  Use Bash-tool paths accordingly.
```
