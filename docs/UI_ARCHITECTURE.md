# UI & Load-Generation Architecture — netzsim iteration 2

> Design/handoff document for the interactive UI that lets a user **pick a grid**,
> **generate realistic loads** (Load Profile Generator), **run the realtime
> simulation**, and **visualize power flow** (branch current + transformer loading).
> Companion to [`../CLAUDE.md`](../CLAUDE.md). Status: **plan, not yet implemented.**

## 0. Locked decisions

| Topic | Decision |
|-------|----------|
| Frontend stack | **React + Vite + TypeScript**, served as a 4th docker-compose service |
| Load generation | **Pre-built LPG archetype library** (cached 1-min profiles), assigned & scaled to grid loads at request time |
| Grid loading | **New runtime API** — netzsim ingests a grid + profiles and rebuilds the pandapower net without a restart |

## 1. Inputs we are integrating

### 1a. European Archetype LV grid models (`*.zip`)
`.../Low Voltage Network Models/03_LV/network_*.xlsx`. Each workbook has 5 sheets:

- **Nodes** — `Name of the Node` (string), `Type` (`Slack`/`PQ`), `Nominal Voltage [kV]`.
  The `Slack` node is the MV side; PQ nodes are LV (0.4 kV).
- **Lines** — `Name`, `From node`, `To node`, `Type` (→ Line Types), `Length [km]`.
- **Line Types** — `Name`, `R [Ohm/km]`, `X [Ohm/km]`, `B [Mikro S/km]`, `Nominal Current I [A]`.
- **Transformers** — full physical params: `Smax [MVA]`, `V_P/V_S [kV]`, `U_sc [%]`,
  `P_cl/P_il [kW]`, `I_nl [%]`, tap data. (Note: in the sample many are `tbd`/0 → need sane defaults.)
- **Loads** — `Name`, `Node`, `Profile`, `Pmax [MW]`, `Qmax [MVar]`, `Sn [MVA]`.

**Mismatch with netzsim today:** node references are *strings*, netzsim expects
*integer bus indices by order*. Line/trafo types are *explicit parameters*, not
pandapower `std_type`s. → a converter is required (§4).

### 1b. Load Profile Generator (`pylpg`, `LPG10.10.0_core.zip`)
`pylpg` (installed in global Python 3.14) wraps the LPG .NET engine. Key entry
points in `pylpg/lpg_execution.py`: `execute_lpg_single_household(...)`,
`execute_lpg_with_householdata(...)`, configurable `resolution` (e.g. `"00:15:00"`),
`StartDate`/`EndDate`; returns a pandas DataFrame of per-step electricity profiles.
LPG is **slow** (.NET, minutes per household) → we cache, we do not run it inline.

## 2. Service topology (docker-compose)

```
ui (React/nginx) ──REST+WS──► netzsim (FastAPI, extended)
                                  │  ├─ grid_converter   (xlsx → 5 JSON)
                                  │  ├─ lpg_library       (cached profiles)
                                  │  └─ RealtimeEngine ──► pandapower
                                  └─ /state ─► collector ─► InfluxDB ─► Grafana (unchanged)
```

New service: **`ui`**. Everything under `visualization/` stays as-is (Grafana
remains the long-range/durable dashboard; the new UI is the interactive cockpit).

## 3. netzsim backend changes (prerequisite for the UI)

The current API is **start-time-only single-grid**. The UI needs runtime
reconfiguration and transformer data. Required changes:

### 3a. Transformer results — ✅ DONE
The user explicitly wants **transformer loading**, which `simulator._collect` did
not emit. Now implemented:
- `StepResult.trafos: list[dict]` = `{index, name, hv_bus, lv_bus, loading_percent,
  p_hv_mw, q_hv_mvar, i_hv_ka, pl_mw}` from `net.res_trafo`.
- `summary.n_trafo` and `summary.max_trafo_loading_percent` (`null` when no trafo).
- `/network` topology lists transformers (`id, name, hv_bus, lv_bus, sn_mva`) + `n_trafo`.
- Collector writes a `trafo` measurement (tags `trafo_index`/`trafo_name`/`time_of_day`;
  fields `loading_percent, p_hv_mw, q_hv_mvar, i_hv_ka, pl_mw`).
- Verified end-to-end on a real archetype grid (trafo ~60 % at evening peak).

*Still TODO for the UI headline:* a Grafana transformer-loading panel and the
React diagram's trafo gauge (consume the new fields).

### 3b. Explicit transformer & line-type parameters (models.py) — ✅ DONE
- `TransformerSpec` now accepts explicit params (`sn_mva, vn_hv_kv, vn_lv_kv,
  vk_percent, vkr_percent, pfe_kw, i0_percent`, `shift_degree`, `parallel`) as an
  alternative to `std_type` → `create_transformer_from_parameters` in
  `network_builder`. (Lines already supported explicit params.)

### 3c. Runtime grid ingestion (new endpoints) — ✅ DONE (grid swap)
`engine.reconfigure(InputData)` halts the loop, rebuilds net+profiles off the
event loop (`asyncio.to_thread`), resets the clock to day 0 / step 0, clears
published history, and resumes if it was running. A `GridCatalog` scans the
archive and converts a chosen grid on demand (cached). **CORS** added.

| Method | Path | Purpose | Status |
|--------|------|---------|--------|
| GET  | `/grids` | list catalog grids (id, name, category, thumbnail; counts once cached) | ✅ |
| GET  | `/grids/{id}` | net-free topology preview (buses, lines, trafos) + converter notes | ✅ |
| GET  | `/grids/{id}/thumbnail` | PNG from the archive | ✅ |
| POST | `/config/apply` | body `{grid_id}` → convert + `engine.reconfigure` → new topology | ✅ |
| GET  | `/config/active` | currently loaded grid metadata | ✅ |
| GET  | `/loadgen/archetypes` | list cached LPG archetypes | ⏳ Phase 4 |
| POST | `/loadgen/assign` | grid id + per-load archetype/scaling → load+gen JSON | ⏳ Phase 4 |

Existing `/status /state /history /control/* /ws /network` are unchanged and now
reflect the active grid (`/network` reads `engine.sim`). `/config/apply` currently
uses the converter's **placeholder** load profiles; the LPG profiles (§5) will plug
into this same path via `/loadgen/assign` before apply.

## 4. Grid converter (`src/netzsim/grid_import/`) — ✅ DONE

`convert_workbook(xl) -> GridInputs` (+ `convert_xlsx_file`, `convert_from_zip`,
`write_inputs`); CLI `scripts/import_grid.py` (`--list`, `--zip/--member`, `--xlsx`).
1. Read **Nodes** in order → integer index = row position; `name→index` map; emit
   `grid_structure.json` (vn_kv, zone MV/LV by voltage).
2. **Slack** node → `substation.json` (ext_grid, flat `vm_pu=1.0` profile).
3. **Lines** joined to **Line Types** → explicit
   `r_ohm_per_km / x_ohm_per_km / c_nf_per_km (= B[µS]·1e3/(2π·f)) / max_i_ka (= I/1000)`;
   zero/missing length clamped to 1e-4 km.
4. **Transformers** → explicit params (§3b); HV/LV oriented by voltage; `tbd`
   fields → `TrafoDefaults` (vk 4 %, vkr from `P_cl` else 1 %, pfe/i0 0).
5. **Loads** → `load` elements with **placeholder daily profiles** (real ones come
   from §5; xlsx `Pmax/Sn` are unrealistic placeholders, so ignored for sizing).

**Robustness (validated on all 25 LV grids in `Low Voltage Network Models/03_LV`,
25/25 converge, 0 NaN buses):** (a) branch endpoints absent from the Nodes sheet
are auto-created as buses (voltage inferred from a declared neighbour); (b) loads
on truly unconnected nodes are dropped; (c) **slack-less islands** — feeders the
workbook left disconnected from the grid (which would solve to `NaN`) — are
reconnected to the LV busbar via a short synthetic tie line (`reconnect_islands`,
default on; union-find, no extra deps). All three are recorded in
`GridInputs.notes`. (Only `network_10` needed a tie, across the 25 grids.)

**Still TODO:** a small **manifest** (`data/grids/index.json`) generated once from
the zip so `/grids` is instant and the 150 MB zip isn't a runtime dependency; and
server-side layout coordinates for the diagram.

## 5. LPG archetype load library (`src/netzsim/loadgen/`) — ✅ DONE

**Build step (offline, `scripts/build_lpg_library.py`, needs `pylpg` + the .NET
engine):** runs LPG for 8 curated archetypes (CHR01/03/05 couple+families,
CHR07/22 working single + single parent, CHR16/30 retired, CHR52 student) for a
**full year** (~2 min each), takes the per-household `Electricity_HH1` series
(kWh/min → kW), samples ~18 days spread across the year for seasonal/weekday
diversity, and writes `data/lpg_library/{CHRxx}.json` + `index.json`. Committed,
so the runtime never needs the engine. LPG binaries extracted to `pylpg/LPG_win/`.

**Request step (instant, runtime, no pylpg):** `LoadLibrary` reads the cached
JSON; `assign_to_loads` cycles distinct `(archetype, variant)` pairs across a
grid's loads (round-robin/​random+seed, interleaved for diversity), resamples to
the active `steps`, applies a `scale`, derives `q_mvar` from a power factor, and
optional per-load time jitter. `POST /loadgen/assign` previews the aggregate daily
curve + per-load assignment; `POST /config/apply {grid_id, loadgen}` applies LPG
loads in the same swap call. Deterministic given the policy seed.

**EV chargers (✅ synthetic — `loadgen/ev.assign_ev`):** LPG was tried first but
**can't produce a usable home-charging load** for these archetypes — its agents
prefer the (always-present) bus, so the car barely charges (~40 kWh/yr), and
folding charging into `Electricity_HH1` makes EV-homes *lower* the evening peak
(transportation pulls occupants out). So EV is modelled directly: a
`ev_penetration` fraction of homes get an extra load element that plugs in on a
diversified evening arrival (~N(18 h, 2 h)) and draws its wallbox power
(`ev_charger_kw` 3.7/11/22 kW) until the day's energy (`ev_daily_kwh`, ±50 %) is
met — "uncontrolled" charging, the grid-relevant worst case. It strictly **adds**.
Verified on `network_10`: 0 → 50 → 100 % EV @ 11 kW drives the transformer
**84 → 143 → 177 %** at 19:00.

**Rooftop PV (✅ synthetic — `loadgen/pv.assign_pv`):** clear-sky bell-curve
`sgen` on a `pv_penetration` fraction of buses, sized to `pv_kwp` (small scatter).
High penetration drives **midday reverse flow** (negative slack) and over-voltage —
verified: 40 % @ 5 kWp on `network_10` exports 86 kW at noon, Vmax 1.05.

EV and PV are layered onto the LPG household base in `/loadgen/assign` &
`/config/apply`; the preview returns gross load, PV and **net** curves.

*Limitations:* LPG base uses one gas-heated house type (electricity excludes space
heat); EV/PV are parametric (not per-household behavioural). Optional future:
smart/overnight EV charging, behavioural EV from a different mobility model.

## 6. Frontend (React + Vite + TS) — `ui/`

### Pages / flow
1. **Grid Browser** — gallery of archetype grids (PNG thumbnail + stats from
   `/grids`); select → topology preview.
2. **Load Studio** — assign LPG archetypes to loads (bulk policy + per-load
   override), choose PV penetration & power factor, preview the aggregate daily
   curve; **Apply** → `POST /loadgen/assign` then `POST /config/apply`.
3. **Live Power-Flow** — start/pause/seek; the main visualization.

### Live visualization (the core deliverable)
- **Single-line grid diagram** (SVG, D3-force or precomputed layout via
  pandapower `create_generic_coordinates` exposed in `/network`):
  - **Branches colored by current/loading** — width ∝ `i_ka`, color ramp by
    `loading_percent` (green→amber→red, 100 % threshold), animated flow direction
    from sign of `p_from_mw`.
  - **Transformer node** with a **loading gauge** (`res_trafo.loading_percent`).
  - **Bus markers** colored by `vm_pu` (under/over-voltage).
- **Live data via `/ws`** (one `StepResult`/step); REST `/state` for initial paint.
- Side panel: clock (`time_of_day`/`day`), Vmin/Vmax, max line & **max trafo
  loading**, losses, solver status — driven by `summary`.
- Time controls: start/pause/resume + a 1440-step scrubber → `/control/seek`.

### Stack details
React 18 + Vite + TS; `@tanstack/react-query` for REST, native WebSocket hook for
`/ws`; D3 (or visx) for the SVG single-line diagram + gauges; Zustand for UI state.
Built to static assets, served by nginx in the `ui` container; dev proxy to
netzsim `:8000`.

## 7. Implementation phases

1. **Backend foundation** — explicit trafo params (§3b) ✅ done; transformer
   **results** (§3a) ✅ done (StepResult/topology/collector all emit trafo data).
2. **Grid converter** (§4) ✅ done (module + CLI + tests, 25/25 LV grids).
3. **Runtime reconfig API** (§3c) ✅ done — `GridCatalog` + `/grids*` + `/config/*`
   + CORS + `engine.reconfigure` (live swap verified over HTTP).
4. **LPG library build script** + cached profiles + `/loadgen` (§5) ✅ done
   (build script + committed library + reader/assign + endpoints + tests).
5. **UI scaffold** (Vite/TS, Dockerfile, nginx, compose `ui` service) + Grid
   Browser ✅ done (React 18 + TS, lean: hand-rolled hooks/SVG, no heavy deps).
6. **Load Studio** + apply flow ✅ done (archetype picker, scale/pf, aggregate
   curve preview via `/loadgen/assign`, apply via `/config/apply`).
7. **Live Power-Flow diagram** ✅ done — SVG single-line (server-side tree
   layout), branch color/width by loading & current, transformer loading badge,
   bus-voltage coloring, pan/zoom, WS stream, controls. **Verified end-to-end in
   a browser** (grid → LPG loads → live power flow).
8. Polish: **PV/rooftop solar ✅**, **EV chargers ✅** (synthetic additive — see §5),
   and **geographic layout ✅** — `layout.py` builds a **length-aware** radial
   layout (feeders fan from the substation, edge lengths ∝ real cable length;
   the source grids have **no GIS data** — coords exist only in the PNG pixels,
   untangleable for dense grids). Geographic/Schematic + Map toggle; the diagram
   has a **street/house map underlay** (streets trace feeders, houses at
   `load_buses`, panels at `sgen_buses`, a substation building) for a
   navigation-map feel.
   Remaining: voltage/loading alerts, per-load archetype override, CSV export,
   multi-grid compare. (Isolated-feeder islands ✅ fixed — §4c.)
9. **Real-map view (ding0 grids) ✅** — the archetype xlsx grids have no GIS data,
   so we added **pre-generated ding0 grids** (`data/ding0_grids/`, eDisGo CSV from
   openego/eDisGo) which carry **real WGS84 lon/lat**. `ding0_import.py` converts
   them (coords → `BusSpec.geo`; LV buses inherit their station's coord); the
   catalog exposes them (`source:"ding0"`, `geo:true`); `/network` emits `has_geo`
   + per-bus `geo`. The Live view defaults to a **Map** layout — `MapDiagram.tsx`
   (Leaflet + CARTO/OSM dark tiles) draws the grid at its real coordinates with the
   live power-flow overlay (loading colors, current width, transformer loading,
   voltages) restyled each WS tick. Verified end-to-end: ding0_1 renders near
   Waldkirch (Black Forest). *Live ding0 generation was abandoned — the OEP REST
   API can't execute ding0's PostGIS queries (HTTP 400); local-Postgres generation
   is the only route and was deferred.*

## 8. Open questions / risks
- **Layout coordinates:** xlsx has no geo data → generate a layout server-side
  (pandapower `create_generic_coordinates`) and expose in `/network`.
- **`tbd` transformer params** in the xlsx → handled via `TrafoDefaults`
  (vk 4 %, vkr 1 %/derived, pfe/i0 0). These are assumptions; revisit if accuracy
  matters. Note the 0.4 kV buses with a 0.416 kV trafo secondary push LV `vm_pu`
  to ~1.03–1.04 (faithful to the source data, not a bug).
- **LPG runtime availability:** library is pre-cached & committed; live regen is
  optional and gated on the .NET engine being unzipped/configured.
- **Grid size:** some LV grids have ~148 buses → diagram must stay readable
  (collapse feeders / zoom).
- **Single active grid** per netzsim instance (reconfig swaps it). Multi-grid
  concurrency is out of scope for v2.
