# Generating geo-referenced grids with ding0 (live, from the OEP)

netzsim ships a few example ding0 grids under `data/ding0_grids/`. You can
generate **any** German MV grid district (20 kV with embedded LV) on demand,
live from the [OpenEnergy Platform](https://openenergyplatform.org) (OEP), with
real WGS84 geography for the Map view.

> **Local PostgreSQL is NOT required.** An earlier attempt concluded the OEP REST
> API "couldn't run ding0's PostGIS queries (HTTP 400)" and a local-Postgres build
> was planned. That was wrong — the 400 came from two small, fixable bugs (below),
> not a fundamental limitation. `scripts/generate_ding0_grid.py` works around both
> and generates grids in seconds.

## How to run

```bash
# Use the Python-3.9 ding0 conda env (NOT the netzsim venv):
C:/Users/bell/ding0mamba/python.exe scripts/generate_ding0_grid.py 1605 1003
```

- Output: `data/ding0_grids/ding0_oep_<district_id>/` (eDisGo CSV). The netzsim
  grid catalog auto-discovers any `data/ding0_grids/*/buses.csv`, so the new grid
  appears in the Grid Browser immediately (id = the directory name).
- Requires `~/.egoio/config.ini` with a valid `[oedb]` section (OEP API token).
- Districts **1605** (~14 buses) and **1003** (~4395 buses) are small/medium and
  finish in seconds. Large districts can take much longer.

## The two fixes (handled in the script, no site-packages edits)

1. **String SRID → int.** ding0 builds `srid = str(int(...))` and emits
   `ST_Transform(geom, '4326')`. The OEP query parser rejects a *string* SRID
   (HTTP 400 "Invalid request") but accepts the *integer*. The script wraps
   `oedialect.engine.requests` and coerces the SRID operand to int in the POSTed
   query JSON — this covers every ding0 call site at once.
2. **Dropped generator materialized views.** ding0's `import_generators` queries
   `supply.ego_dp_res_powerplant_sq_mview` / `..._conv_...`, which have been
   removed from the OEP (HTTP 404 "Table does not exist"). The base tables
   (`supply.ego_dp_res_powerplant`, `supply.ego_dp_conv_powerplant`) still exist
   but lack the precomputed grid-district assignment columns (`subst_id`, `la_id`,
   `mvlv_subst_id`, `rea_geom_new`, `w_id`) ding0 filters on. So the script skips
   `import_generators`. **DERs are not lost** — netzsim synthesizes PV/EV in the
   Load Studio (`loadgen/pv.py`, `loadgen/ev.py`).

The script also rewrites the OEP host (`openenergy-platform.org` →
`openenergyplatform.org`, since oedialect 0.0.10 hard-codes the dead domain) and
sets `OEDIALECT_VERIFY_CERTIFICATE=FALSE`.

## Restoring real generators later (optional)

To use ding0's real DERs instead of skipping them, rewrite the generator query to
spatially self-join the base `*_powerplant` table to the district geometry
(`ST_Within(generator.geom, district.geom)`) and derive `voltage_level`/`subst_id`
on the fly, replacing the dependency on the removed `_sq_mview`. Not needed for
netzsim's purpose; documented here so the option is clear.

## Why ding0 grids are MV (not LV)

ding0 generates **MV grid districts** (20 kV) that *contain* their LV grids — so a
generated grid is a full MV+LV network, not a standalone LV feeder. That is the
correct, complete unit; netzsim's pandapower power flow is voltage-agnostic and
solves the whole thing.
