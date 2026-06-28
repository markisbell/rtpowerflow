# netzsim — lightweight realtime power-flow simulation

A small service that runs a **continuous time-series power flow** on a grid using
[pandapower](https://github.com/e2nIEE/pandapower). It loads a grid and its daily
profiles (1-minute resolution), then advances one 1-minute step per *accelerated
tick* of wall-clock time, solving a power flow each step. After a full day (1440
steps) it wraps back to step 0 and repeats the same profiles indefinitely.

Results stream live over **WebSocket** and are queryable over **REST**.

---

## Architecture

```
                        ┌──────────────────────────────────────────────┐
   data/*.json          │                  netzsim                      │
 ┌─────────────────┐    │                                              │
 │ grid_structure  │    │   data_loader ──► models (pydantic validate) │
 │ lines           │──► │        │                                     │
 │ load            │    │        ▼                                     │
 │ generation      │    │  network_builder ──► pandapower net + dense  │
 │ substation      │    │        │             profile arrays         │
 └─────────────────┘    │        ▼                                     │
                        │     Simulator.run_step(t):                   │
                        │        write step t into net.load/sgen/      │
                        │        ext_grid → pp.runpp → StepResult       │
                        │        ▲                                     │
   accelerated tick     │        │ one step / interval                 │
   (1 step / N sec) ───►│   RealtimeEngine (asyncio loop)              │
                        │        │ publish                             │
                        │        ▼                                     │
                        │     StateStore (latest + history + pub/sub)  │
                        │        │                                     │
                        │   FastAPI: REST  +  WebSocket /ws            │
                        └────────┼──────────────────────────────────────┘
                                 ▼
                        REST clients · WebSocket clients · built-in monitor (/)
```

**Modules** (`src/netzsim/`):

| File                 | Responsibility |
|----------------------|----------------|
| `config.py`          | Env/.env settings (`NETZSIM_*`) |
| `models.py`          | Pydantic schemas for the 5 input files |
| `data_loader.py`     | Read + cross-validate inputs |
| `network_builder.py` | Build the pandapower net once; pack profiles into numpy arrays |
| `simulator.py`       | Apply one step's values and solve the power flow → `StepResult` |
| `engine.py`          | Async realtime loop; day-wrap; pause/resume/seek |
| `state.py`           | Latest result, bounded history, WebSocket broadcast |
| `api.py`             | FastAPI REST + WebSocket + tiny monitor page |
| `main.py`            | uvicorn entry point |

Design choices:
- **Build once, step cheaply.** Topology is built a single time; each step only
  overwrites `p_mw`/`q_mvar`/`vm_pu` columns and calls `runpp` — optionally
  *warm-started* from the previous solution for fast convergence.
- **The power flow runs off the event loop** (`asyncio.to_thread`) so the API
  stays responsive while pandapower solves.
- **Profiles double as element definitions** — every entry in `load.json` /
  `generation.json` / `substation.json` becomes one pandapower load / sgen /
  ext_grid, keeping the format native and minimal.

---

## Input file formats

All bus references are **integer indices matching the order of `grid_structure.buses`**
(this is exactly how pandapower assigns bus indices).

**`grid_structure.json`**
```json
{ "name": "Example LV feeder", "f_hz": 50.0,
  "buses": [ {"name": "Substation", "vn_kv": 0.4, "zone": "LV"}, ... ] }
```

**`lines.json`** — each line uses a pandapower `std_type` *or* explicit per-km params:
```json
{ "lines": [
    {"name":"L0-1","from_bus":0,"to_bus":1,"length_km":0.12,"std_type":"NAYY 4x150 SE"},
    {"name":"L1-2","from_bus":1,"to_bus":2,"length_km":0.1,
     "r_ohm_per_km":0.642,"x_ohm_per_km":0.083,"c_nf_per_km":210,"max_i_ka":0.142}
  ],
  "transformers": [ {"hv_bus":0,"lv_bus":1,"std_type":"0.4 MVA 20/0.4 kV"} ] }
```

**`load.json` / `generation.json` / `substation.json`** — 1440 values per element:
```json
{ "resolution_minutes":1, "steps":1440,
  "loads":[ {"name":"Load B1","bus":1,"p_mw":[...1440...],"q_mvar":[...1440...]} ] }
```
- `generation.json` uses key `"generation"` (sgen, `p_mw`/`q_mvar`).
- `substation.json` uses key `"substations"` with `vm_pu` (and optional
  `va_degree`) — modelled as ext_grid slack set-points representing the upper grid.

---

## Run it

### Local (Python ≥ 3.10)
```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python scripts/generate_sample_data.py              # creates ./data/*.json
PYTHONPATH=src python -m netzsim.main
```
Open <http://localhost:8000/> for the live monitor.

### Docker
```bash
docker compose up --build
```
This starts **four** services: the power-flow API (`netzsim`, :8000), plus the
visualization stack — `collector`, `influxdb` (:8086) and `grafana` (:3000).
Open Grafana at <http://localhost:3000> (`admin`/`admin`) for the live dashboard.
See [`visualization/README.md`](visualization/README.md) for details.

---

## API

| Method | Path              | Description |
|--------|-------------------|-------------|
| GET    | `/`               | Built-in live monitor (HTML) |
| GET    | `/health`         | Liveness |
| GET    | `/status`         | Engine state (running, step, day, interval) |
| GET    | `/network`        | Static topology (buses, lines, ext_grids) |
| GET    | `/state`          | Latest solved `StepResult` |
| GET    | `/history?limit=` | Recent results from the in-memory ring buffer |
| POST   | `/control/start`  | Start the loop |
| POST   | `/control/pause`  | Pause |
| POST   | `/control/resume` | Resume |
| POST   | `/control/seek?step=` | Jump to a step |
| WS     | `/ws`             | Live stream: one JSON `StepResult` per solved step |

Each `StepResult` contains: `step`, `day`, `time_of_day`, `converged`, `solve_ms`,
`buses[]`, `lines[]`, `ext_grids[]`, and a `summary` (Vmin/Vmax, max line loading,
total load/gen/slack/losses).

---

## Configuration (`.env`, see `.env.example`)

| Var | Default | Meaning |
|-----|---------|---------|
| `NETZSIM_DATA_DIR` | `./data` | Input directory |
| `NETZSIM_STEP_INTERVAL_SECONDS` | `1.0` | Real seconds per 1-min step |
| `NETZSIM_STEPS_PER_DAY` | `1440` | Steps in one day |
| `NETZSIM_AUTOSTART` | `true` | Start loop on boot |
| `NETZSIM_HISTORY_SIZE` | `1440` | In-memory history length |
| `NETZSIM_WARM_START` | `true` | Warm-start each power flow |

---

## Tests
```bash
pip install -e ".[dev]"
pytest
```
