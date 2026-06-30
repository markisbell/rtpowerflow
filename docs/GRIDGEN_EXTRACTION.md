# Extracting the grid generator into a standalone component

> **Goal.** Pull the synthetic-grid *generator* out of netzsim (the realtime
> simulator) into a self-contained, reusable component, so it can serve future
> applications (other simulators, GIS, planning studies, a generation service)
> independent of the realtime UI.
>
> **Status:** planning. Nothing has been moved yet. This document is the roadmap;
> execute it phase by phase. Start with **Phase 0** (freeze the format), which is
> safe and reversible.

---

## 1. Why this is mostly a packaging job, not a rewrite

The generator is already loosely coupled from the simulator:

- The generation **scripts are standalone** — they run in the **Python-3.9 ding0
  conda env** (`C:\Users\bell\ding0mamba`, ding0 0.2.1 + osmnx + egoio + OEP
  token), totally separate from netzsim's `.venv` (modern Python + pandapower).
  The two *cannot* share an environment, which makes the seam natural.
- The only generator → simulator import today is the **legacy**
  `scripts/import_grid.py` (the old European-Archetype xlsx converter; the
  archetypes are no longer used). Everything else is clean.
- netzsim's **importers** (`src/netzsim/ding0_import.py`,
  `src/netzsim/osm_lv_import.py`) depend only on the `GridInputs` dataclass —
  never on `simulator`, `engine`, `network_builder`, or `api`. They are a thin
  *translation* layer, not part of generation.

So the real interface between the two sides is **the on-disk grid format**. Cut
there.

---

## 2. The seam: the grid file format (today, informally)

Three artifacts already act as the contract. Phase 0 formalizes them.

**(a) eDisGo-style CSV** — full / MV ding0 districts, `data/ding0_grids/<id>/`:
`buses.csv` (name, x=lon, y=lat, mv_grid_id, lv_grid_id, v_nom, in_building),
`lines.csv` (bus0, bus1, length, r, x, s_nom, num_parallel, type_info),
`transformers.csv`, `transformers_hvmv.csv`, `loads.csv`, `generators.csv`,
`switches.csv`, `network.csv`. Produced by ding0; read by
`ding0_import.convert_ding0_csv(dir, scope="full|mv|lv", lv_grid_id=...)`.

**(b) OSM-LV JSON** — street-routed LV grids, `data/lv_osm/<id>.json`:
```jsonc
{
  "name": "...", "station": [lon, lat], "slack_bus": <int>,
  "buses":  [{"name", "vn_kv", "geo": [lon, lat], "role": "slack|backbone|cabinet|load"}],
  "lines":  [{"from", "to", "length_km", "r_ohm_per_km", "x_ohm_per_km",
              "c_nf_per_km", "max_i_ka", "parallel", "geometry": [[lon,lat], ...]}],
  "loads":  [{"bus": <int>, "peak_mw": <float>}]
}
```
Read by `osm_lv_import.convert_osm_lv(path)`.

**(c) `grid_library.json`** — the manifest the UI picker is driven by:
```jsonc
{ "grids": [
  {"id", "name", "voltage": "MV|LV", "character": "rural|suburban|urban",
   "nodes": <int>, "source_dir": "<ding0 dir>", "scope": "mv|lv|full",
   "lv_grid_id"?: "<id>", "osm_grid"?: "lv_osm/<id>.json"} ]}
```
Read by `grid_catalog.GridCatalog` (manifest-driven).

---

## 3. Target architecture (three pieces)

```
  gridgen  (Py3.9 env)          grid library (versioned data)        netzsim (simulator+UI)
  ─────────────────────         ────────────────────────────        ──────────────────────
  ding0 + OEP + osmnx     ──►   eDisGo CSV / OSM-LV JSON / manifest  ──►  thin loader → InputData
  CLI + Python API             (committed snapshot OR data release)       (pure consumer)
        │
        └────────────  gridformat  (tiny pure-python lib: schema + IO + converters) ──────────►
                       to pandapower / pypsa / eDisGo / GeoJSON, for OTHER apps
```

- **`gridgen`** — owns all generation: `generate_ding0_grid`, `build_grid_library`,
  `build_lv_osm_grids`, `build_lpg_library`, the OEP work-arounds (SRID-as-int,
  skip dropped mviews — see `docs/DING0_GENERATION.md`) and the OSM routing +
  cable-cabinet logic. Ships its own conda env spec + OEP-token instructions.
  **No netzsim dependency.** Exposes a CLI **and** a Python API
  (`gridgen.generate_mv(id)`, `gridgen.build_lv_grid(district, lv_id)` → an
  in-memory grid object).
- **`gridformat`** *(optional, but the real reuse enabler)* — pure-Python,
  no heavy deps: defines + validates the schema, reads/writes it, and provides
  **converters** to pandapower / pypsa / eDisGo / GeoJSON so any downstream tool
  can consume a grid without ding0 or netzsim.
- **`netzsim`** — keeps only the *loader* (today's importers + manifest catalog,
  trimmed) and pins a **dataset version**; it no longer knows how grids are made.

---

## 4. The phased path

### Phase 0 — Freeze the contract  *(do this first; ~½ day, no code moves)*
- Write the schema for the three artifacts (§2) as a versioned spec + a JSON
  Schema for the OSM-LV JSON and the manifest.
- Add a **round-trip test**: `write → read → identical` for the OSM-LV JSON, and
  an `import → solve` smoke test for one of each artifact (already partly covered
  by `tests/test_osm_lv.py`, `tests/test_ding0_import.py`).
- Outcome: producer and consumer can now evolve independently against a stable
  interface. Reversible — nothing has been restructured.

### Phase 1 — Carve out `gridgen`  *(~1–2 days)*
- New package/repo with `pyproject.toml` + the `ding0_env.yml` conda spec.
- Move the four generation scripts in as a package with a CLI:
  `gridgen mv <id...>`, `gridgen library`, `gridgen lv-osm`, `gridgen lpg`.
- Cut the one real coupling: drop or rewrite `scripts/import_grid.py` (legacy
  archetype converter that imports netzsim).
- Carry over `docs/DING0_GENERATION.md` (OEP work-arounds) and the
  `[[lv-grid-geo-next-step]]` knowledge (LV is rebuilt from OSM).

### Phase 2 — netzsim becomes a pure consumer  *(~½ day)*
- Keep `ding0_import`, `osm_lv_import`, and the manifest-driven `grid_catalog` in
  netzsim as the **loader** (they already only touch `GridInputs`).
- Remove the generation scripts from netzsim's `scripts/`.
- netzsim reads a **dataset** pinned by version (committed snapshot, or fetched —
  Phase 3), with no knowledge of how it was produced.

### Phase 3 — Distribute for reuse  *(ongoing)*
- Publish `gridgen` (pip-installable in its env) and release the grid library as a
  **versioned data artifact** (GitHub Release / Zenodo / OEP) so any app can fetch
  a pinned version.
- Add the `gridformat` converters (pandapower / pypsa / eDisGo / GeoJSON).

---

## 5. Decisions to settle (the forks)

1. **Separate repo vs. monorepo subpackage.** Separate repo gives the generator
   its own release cadence (recommended given the "future applications" goal); a
   subpackage is lower overhead now.
2. **One canonical format vs. keep two.** Today MV is eDisGo-CSV, LV is custom
   JSON. Converging on one (e.g. extend eDisGo-CSV with a geometry/`kind` sidecar
   for line polylines + cabinets) simplifies every consumer; keeping two is less
   work but more loader code.
3. **Where the neutral grid model lives.** Promote `GridInputs` into `gridformat`,
   or keep files as the only contract and let each consumer define its own model
   (recommended: files are the contract).
4. **How netzsim gets the data.** Commit a snapshot (offline, simple) vs. fetch a
   pinned release (smaller repo, cleaner provenance).

---

## 6. What stays in netzsim (for clarity)

The simulator, async engine, REST/WS API, the React UI, the **line-geometry +
cable-cabinet rendering** (`LineSpec.geometry`, topology `cabinet_buses`,
`MapDiagram` polylines/green circles), and the *loader* importers. None of that
moves — netzsim keeps reading the format and simulating.
