# netzsim — a realtime distribution-grid teaching platform

netzsim runs a **continuous time-series power flow** on a distribution grid with
[pandapower](https://github.com/e2nIEE/pandapower) and turns it into an
interactive teaching tool for grid operation, observability and control. It
loads a grid plus daily 1-minute profiles, advances one 1-minute step per
*accelerated tick* of wall-clock time (a full day in ≈ 24 min at the default
rate), and streams every solved step live. After step 1439 it wraps to step 0,
increments a day counter and repeats indefinitely.

The pedagogical core is the **three-layer view of a grid**:

1. **Reality** — the true power flow, computed at every bus and line each step.
2. **What you can measure** — only quantities where a smart meter or transformer
   meter is placed; everything else is unknown. Real low-voltage grids are
   almost unobservable, and the UI shows that.
3. **What you can estimate** — a WLS state estimation reconstructs the whole grid
   from the sparse meter readings plus the grid model and load assumptions. How
   close it gets to reality is exactly the lesson.

On top of that sits a **runtime toolkit** — place batteries, PV, EV wallboxes,
overload controllers and regulated transformers live — and a full **vertical
MV/LV integration**: medium-voltage districts with dozens of low-voltage cells,
hierarchical estimation, and a grid-traffic-light (Netzampel) control cascade.

A German **Benutzerhandbuch** (`docs/Benutzerhandbuch.pdf`) is the complete
user guide; `CLAUDE.md` is the exhaustive developer/architecture reference.

---

## The three applications

One `docker-compose.yml` orchestrates the whole stack:

| App | What it is | Port |
|-----|-----------|------|
| **netzsim** (`src/`) | the FastAPI power-flow service (REST + WebSocket) | 8000 |
| **UI** (`ui/`) | a React + Vite + Leaflet frontend (German default, DE/EN) | 8080 (nginx) / 5173 (dev) |
| **Visualization** (`visualization/`) | a collector → InfluxDB → Grafana dashboard | 8086 / 3000 |

---

## What it can do

- **Grid catalog & districts.** Pick from committed synthetic German grids on
  real geography (from the sibling [`gridgen`](../gridgen) project) — rural /
  suburban low-voltage feeders and whole medium-voltage districts — or import a
  JSON drawn in [`gridedit`](../gridedit). The **Netz & Lasten** view assigns
  realistic household load profiles (LPG library, optional multi-family
  buildings, EV and PV penetration) and previews the transformer loading before
  you start.
- **Three parallel views** of every step — **Lastfluss** (truth), **Gemessen**
  (metered only), **Schätzung** (WLS estimate) — each with its own data layer
  and a real Leaflet/OSM map that colours by loading and voltage.
- **Observability by meter placement.** Click a node or transformer to place a
  smart / transformer meter; per-device TAF fidelity (TAF 9/10/14 minute-level
  telemetry vs. TAF 7 15-minute Lastgang). Strict mode
  (`NETZSIM_EXPOSE_GROUND_TRUTH=false`) makes the server withhold the truth
  entirely.
- **WLS state estimation** (`estimator.py`) from the placed meters, the grid
  model, structural zero-injection knowledge and profile pseudo-measurements —
  configurable policy (PV/EV pseudo, SLP vs. profile basis, std, hierarchy).
- **Runtime equipment**, added/edited/removed live: batteries (self-consumption
  / peak-shaving / price strategies), rooftop PV, EV charge windows, and
  **§14a-style overload controllers** that dim EV charging or curtail PV feed-in
  — fed *only* from the operator's view (meters + estimate), so control quality
  equals observability.
- **Vertical MV/LV integration.** A district is a set of **Ortsnetz-Zellen**
  (secondary-substation cells). Estimation runs **hierarchically** (each cell
  estimates locally, its boundary flow feeds the MV-level estimate); a
  **Netzampel cascade** lets an MV coordinator signal every cell's controller to
  throttle proportionally; a **regelbarer Ortsnetztrafo (rONT)** holds a cell's
  busbar voltage in a deadband. A side-panel cell table with map drill-down
  shows all cells at once.
- **Scenarios** (`data/scenarios/*.json`) — hand-editable *recipes* (grid +
  loadgen + runtime DER ops + meters + clock). Four reference scenarios ship,
  including *#4 "Feierabend im Bezirk"*, the vertical teaching path where a
  medium-voltage congestion is invisible to every individual cell.
- **Data export** — record a live session or bulk-replay whole days offline into
  tidy CSV packs (`data/recordings/`) for analysis in Python / MATLAB / Excel.

---

## Run it

### Windows: double-click launcher (recommended here)

`start_netzsim.bat` starts the backend (:8000) and the Vite UI (:5173) in their
own console windows, runs `npm install` if needed, guards against double-starts,
and opens the browser. Close the two server windows to stop. (The sibling
[`gridedit`](../gridedit) has `start_gridedit.bat` the same way.)

### Local (Python ≥ 3.10)

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python scripts/generate_sample_data.py              # writes ./data/*.json (5-bus sample)
PYTHONPATH=src python -m netzsim.main               # http://localhost:8000/
```

The full committed dataset (ding0 MV districts, street-routed LV grids, the LPG
library) lives under `data/` — no external archive needed.

**The React UI** (dev):
```bash
cd ui && npm install && npm run dev                 # http://localhost:5173
```
> The dev proxy targets `http://127.0.0.1:8000` (not `localhost`): on Windows
> `localhost` resolves to IPv6 `::1` first, which uvicorn (IPv4-only) refuses.

### Docker (full stack)

```bash
docker compose up --build
# ui :8080 · netzsim :8000 · influxdb :8086 (admin/netzsim-admin) · grafana :3000 (admin/admin)
```

See [`visualization/README.md`](visualization/README.md) for the Grafana stack.

---

## Architecture (app 1)

```
data/*.json ─► data_loader (validate + cross-validate)
                    │
            network_builder ──► pandapower net built ONCE + numpy profile arrays
                    │
   accelerated tick ─► RealtimeEngine (asyncio) ─► Simulator.run_step(step, day)
   (1 step / N sec)        │ wraps 1439→0, day++    write step → runpp → StepResult
                           ▼
                       StateStore (latest + history + WS pub/sub + recorder sink)
                           │
        ┌──────────────────┴───────────────────┐
        ▼ WebSocket /ws                         ▼ REST GET /state (polled)
   browser / UI                            collector ─► InfluxDB ─► Grafana
```

Key design choices: **build once, step cheaply** (topology built a single time;
each step overwrites injection columns and calls `runpp`, optionally
warm-started); the power flow runs **off the event loop** (`asyncio.to_thread`)
so the API stays responsive; **profiles double as element definitions** (every
row in the load/generation/substation files becomes one pandapower element).

Full module map, the complete ~60-endpoint API, and the observability/estimation
internals are documented in [`CLAUDE.md`](CLAUDE.md).

---

## Input file formats (native to pandapower)

All bus references are **integer indices matching the order of
`grid_structure.buses`** — exactly how pandapower assigns bus indices.

- **`grid_structure.json`** — `{name, f_hz, buses[{name, vn_kv, type, zone, in_service}]}`
- **`lines.json`** — `{lines[], transformers[]}`; each line uses a pandapower
  `std_type` *or* explicit `r/x/c_per_km` + `max_i_ka`.
- **`load.json` / `generation.json` / `substation.json`** — 1440 values per
  element: loads → `load` (`p_mw`/`q_mvar`), generation → `sgen`, substations →
  `ext_grid` slack set-points (`vm_pu`/`va_degree`).

`scripts/generate_sample_data.py` writes a 5-bus example set; real grids come
from the [`gridgen`](../gridgen) dataset committed under `data/`.

---

## Configuration (`.env`, see `.env.example`; prefix `NETZSIM_`)

| Var | Default | Meaning |
|-----|---------|---------|
| `DATA_DIR` | `./data` | Input directory |
| `STEP_INTERVAL_SECONDS` | `1.0` | Real seconds per 1-min step |
| `AUTOSTART` | `true` | Start the loop on boot |
| `WARM_START` | `true` | Warm-start each power flow |
| `EXPOSE_GROUND_TRUTH` | `true` | `false` = stream only observed measurements (strict observability) |
| `GRID_LIBRARY` | `./data/grid_library.json` | Grid catalog manifest |
| `RECORD` / `RECORDINGS_DIR` | `false` / `./data/recordings` | Continuous session recording |

---

## Tests

```bash
pip install -e ".[dev]"   # or: pip install pytest httpx
pytest                    # backend suite
cd ui && npm run build    # type-check + build the frontend
```
