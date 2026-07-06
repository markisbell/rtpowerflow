# Grid dataset — provenance

netzsim is a **consumer** of grids, not a generator. The grids under this `data/`
directory are a **committed snapshot** produced by the separate **`gridgen`**
project (a sibling repo at `../gridgen`). netzsim only *reads* them — it has no
ding0/OSM/OEP dependency and never regenerates them at runtime.

| Artifact | What it is | Read by |
|----------|-----------|---------|
| `ding0_grids/<id>/` | MV grid districts, eDisGo CSV (real lon/lat) — 7 districts | `netzsim.ding0_import.convert_ding0_csv` |
| `lv_osm/<id>.json` | Street-routed LV grids, `gridformat` JSON (cables along roads, cable cabinets) — 14 grids | `netzsim.osm_lv_import.convert_osm_lv` |
| `grid_library.json` | Manifest the `/grids` picker is driven by — **trimmed to the 2 grids of the reference scenarios** (`data/scenarios/`) | `netzsim.grid_catalog.GridCatalog` |
| `grid_library_full.json` | The full 20-entry manifest (MV/LV · size · rural/suburban/urban); copy over `grid_library.json` to restore the whole picker. Tests use this file. | tests, on demand |
| `lpg_library/` | Cached LPG household load profiles (index + per-archetype variants) | `netzsim.loadgen.library.LoadLibrary` |
| `grid_structure.json`, `lines.json`, `load.json`, `generation.json`, `substation.json` | The default 5-bus sample the simulator boots with (from `scripts/generate_sample_data.py`) | `netzsim.data_loader` |

## Pinned version

- **Producer:** `gridgen` @ `dc6fada` — see `../gridgen` (`gridgen --lib … {mv|library|lv-osm|lpg}`).
- **Format:** `gridformat` v0.1 (`../gridgen/docs/FORMAT.md`).

The LV grids carry realistic NAYY cable types (sized by cross-section + a feeder
voltage-drop budget). Main cables follow the streets out from the LV busbar,
**branching only at cable cabinets** (junction boxes); consumers connect to the
nearest cabinet by short laterals (≤ ~40 m). Character: **rural** radial,
**suburban** with normally-open ring ties (`closed:false`), **urban** meshed.
`gridgen` **drops** grids whose OSM streets are too coarse to keep laterals short
(3 were dropped here, leaving 14). Every grid passes `gridgen check` (structural
E-Check); `tests/test_echeck.py` re-checks them electrically (solves within
EN 50160-style ±10 % / no overload).

To refresh the dataset, regenerate it with `gridgen` into a library directory and
copy `ding0_grids/`, `lv_osm/`, `grid_library.json` (and `lpg_library/`) here, then
bump the pin above. Do **not** add generation code back into netzsim.
