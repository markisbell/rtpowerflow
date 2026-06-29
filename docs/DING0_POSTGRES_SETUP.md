# Local-PostgreSQL ding0 generation — execution plan

> Goal: run ding0's **generator** locally (the OEP REST API can't execute ding0's
> PostGIS queries — HTTP 400, see CLAUDE.md §11), to produce real geo-referenced
> MV grid districts (20 kV + embedded LV) on demand, instead of the 3 bundled
> eDisGo example grids in `data/ding0_grids/`.
>
> This is a multi-session build. Execute with the **conda ding0 env** that already
> exists: `C:\Users\bell\ding0mamba\python.exe` (Python 3.9 + ding0 0.2.1 + egoio
> 0.4.8 + oedialect 0.0.10 + saio + geopandas/GDAL). Token already in
> `~/.egoio/config.ini` (`[oedb]`, host corrected to `openenergyplatform.org`).

## What ding0 needs from the oedb (version v0.4.5)
From `ding0/config/config_db_tables.cfg` (`[versioned]`, `version = v0.4.5`) — **7 tables**:

| ding0 role | table (schema.table) | size (Germany-wide) |
|---|---|---|
| MV grid districts | `grid.ego_dp_mv_griddistrict` | ~3.6k rows (verified queryable) |
| HV/MV substations | `grid.ego_dp_hvmv_substation` | ~3.6k |
| LV grid districts | `grid.ego_dp_lv_griddistrict` | large (~100k+) |
| MV/LV substations | `grid.ego_dp_mvlv_substation` | large (~100k+) |
| LV load areas | `demand.ego_dp_loadarea` | large |
| renewable generators | `supply.t_ego_dp_res_powerplant_sq_mview` | **~1–2M rows** |
| conventional generators | `supply.t_ego_dp_conv_powerplant_sq_mview` | smaller |

All have PostGIS geometry columns; ding0 runs `ST_Transform` / `ST_Intersects`
spatial joins, so the local DB must be **real PostGIS** with the full tables.
egoio's ORM classes (`egoio.db_tables.{grid,demand,supply,model_draft}`) define
the exact columns/types — use them to create the tables so the schema matches.

## Steps
1. **PostgreSQL + PostGIS** locally. Either an admin install (EDB installer +
   StackBuilder PostGIS) or a no-admin portable PG zip + PostGIS extension.
   Create DB `oedb`, `CREATE EXTENSION postgis;`, and schemas `grid`, `demand`,
   `supply`, `model_draft`.
2. **Create the 7 tables** from egoio ORM metadata: `egoio` Base.metadata for the
   relevant classes → `create_all(local_engine)` (creates them with geometry cols).
3. **Load data** — pull each table from the OEP REST API (works for plain SELECTs)
   and bulk-INSERT into local PG, paginated/resumable. Geometry: request as WKT/
   GeoJSON and insert via `ST_GeomFromText`/`ST_GeomFromGeoJSON`. The generator
   table is the slow one (millions of rows) — page by ~10k, show progress, allow
   resume. Pull only `version = 'v0.4.5'` rows where the column exists.
   *(Alternative if found later: a full oedb `pg_restore` dump would replace this
   whole step — none located so far.)*
4. **Point egoio at local PG**: add a `[local]` section to `~/.egoio/config.ini`
   (dialect `psycopg2`, host localhost, the DB/user/pass), and call
   `db.connection(section='local')` in the generation script (no host/cert
   monkeypatch needed locally).
5. **Generate**: `NetworkDing0(name=...).run_ding0(session, mv_grid_districts_no=[id])`
   then `nd.to_csv(out_dir)`. Pick small districts first (smallest `area_ha`).
6. **Import**: the generated eDisGo-style CSV loads via the existing
   `netzsim.ding0_import.convert_ding0_csv` (already carries WGS84 coords →
   `BusSpec.geo` → Map view). Drop the output dir under `data/ding0_grids/`.

## Risks / open questions
- **Data volume**: pulling ~1–2M generator rows via REST is slow/fragile; make it
  resumable. If impractical, search again for an oedb dump or a regional subset.
- **PostgreSQL install** may need admin (a DB service) — confirm with the user, or
  use a portable no-admin build.
- **egoio version filter**: ding0 applies `version_condition_*`; ensure the loaded
  rows carry the matching `version` value (v0.4.5) or the queries return empty.
- The OEP REST geometry encoding (WKB-hex vs GeoJSON) must round-trip into PostGIS
  intact — validate on `mv_griddistrict` first (small) before the big pulls.

## Status
Not started. Prerequisite: decide PostgreSQL install method (admin vs portable).
Everything upstream (ding0 env, token, importer, Map UI) is in place.
