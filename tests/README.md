# Test suite — what each file pins, and why

```bash
pip install -e ".[dev]"   # or: pip install pytest httpx
pytest                    # from the repo root (pythonpath=src via pyproject)
```

The suite (currently 158 tests) follows one project rule: **regression tests
are written BEFORE refactorings and features** — first pin the current
behavior, then change the code, and the pinned test must stay green
*unmodified* through the change. Several files below exist exactly for that
reason (`test_api_surface`, `test_sweeps`), others are the executable form of
a customer requirement (`test_estimation` honesty tripwire, controller
blindness). CI runs the whole suite on every push (`.github/workflows/`,
GitHub-side).

## Core pipeline

| file | purpose |
|---|---|
| `test_simulator.py` | Smoke tests for the base pipeline: the 5 input JSONs load, the pandapower net builds, steps solve, and the day wraps 1439 → 0. If this is red, everything else is noise. |
| `test_api_surface.py` | Regression net for the API: pins the **complete route inventory** (method + path — new endpoints must be added to `EXPECTED_ROUTES`) and smoke-tests one representative endpoint per router area over a real `TestClient`. Written before the `api.py` → `netzsim/api/` router split; also pins fastapi-0.139-proof route walking and the strict-mode gating of the day-graph endpoints. |
| `test_api_validation.py` | Range constraints on API request models: raw calls must not inject physically nonsensical values (negative kWp, out-of-range penetrations) into the profile generators. |
| `test_sweeps.py` | Regression net for the `sweeps.py` extraction from Simulator: battery day curves, the measured layer's TAF rastering, and that a deep-copied Simulator (the bulk exporter's path) sweeps independently of the original's caches. |

## Grid import & catalog

| file | purpose |
|---|---|
| `test_ding0_import.py` | Pre-generated ding0 (eDisGo CSV) importer: real geo-coordinates, MV/LV scopes, the grids solve. |
| `test_osm_lv.py` | Street-routed OSM LV grids: cables carry line geometry, cabinets survive the conversion, the grids solve. |
| `test_district_import.py` | Interconnected districts: MV ring + spliced street-routed LV subgrids compose into ONE solvable 20 kV + 0.4 kV net; unspliced LV grids stay lumped. |
| `test_gridedit_mv.py` | gridedit MV-layer exports (`gridedit-mv`): catalog detection, 110-kV-feed conversion, per-type profiles (mall/HPC/wind/biogas/PV), loadgen exclusion for non-household rows. |
| `test_user_grids.py` | User-drawn gridformat exports: `user_grids/` catalog scan and the importer honoring the file's chosen transformer rating. |
| `test_lv_ref.py` | Vertical phase 5: a drawn MV station referencing a drawn LV export gets the real street grid **spliced** through its own station transformer; a missing reference degrades to a lumped load + warning. |
| `test_echeck.py` | Electrical E-Check over the committed dataset: every committed LV grid solves at evening peak within EN-50160-style limits — a data-quality gate, not a code test. |
| `test_runtime_swap.py` | The `/grids` catalog + `engine.reconfigure`: swapping the grid at runtime builds a fresh Simulator without killing the engine loop. |
| `test_loadgen.py` | LPG library reader + assignment: archetype/variant distribution, seeded determinism, MFH household summing. |

## Observability & state estimation

| file | purpose |
|---|---|
| `test_measurements.py` | The observability layer: meter placement, projection of reality onto the observed subset, strict-mode stripping of ground truth from the wire. |
| `test_estimation.py` | WLS estimation from meters + grid model + pseudo-loads: near-exact under full metering, sane under sparse metering, absent without meters. Contains the **honesty tripwire** `test_estimation_honesty_pv_rise_unknowable`: with 5 % metering and no PV knowledge the estimator MUST miss most of a midday voltage rise — if it doesn't, truth is leaking into the estimate. |
| `test_hierarchical_estimation.py` | Vertical phase 1.2: per-cell local WLS + boundary flows feeding the reduced MV-level WLS; the composed result mirrors the monolithic shape so downstream consumers don't care. |

## Control (horizontal & vertical)

| file | purpose |
|---|---|
| `test_controller.py` | Overload controllers (§14a-style): remove an overload within a few steps using the right lever (EV on import, PV on export), release with hysteresis, bus scope only touches its own node. Pins that a controller is fed ONLY from the operator's view — `test_controller_blind_without_meters_holds_despite_overload` is the blindness contract. |
| `test_controller_vertical.py` | Vertical phase 2: cell scope = one ONS cell's domain; the MV coordinator throttles nothing itself but broadcasts signals applied as min(local law, signal); executing a signal needs a Steuerbox, not a meter. |
| `test_ront.py` | rONT (on-load tap changer): holds the LV busbar in its band using only the operator's view, one mechanical tap per action, blind without data, restores original tap data on removal. |
| `test_cells.py` | Vertical phase 0: every importer emits ONS cells (spliced with members + station trafo, lumped as degenerate cells); cross-validated into `Simulator.cells` and `/network`. |

## Runtime equipment & external nodes

| file | purpose |
|---|---|
| `test_der.py` | Runtime PV/EV mutators: add/resize/move at a node, parameters derivable from the profile rows, the power flow keeps solving after every mutation. |
| `test_ext.py` | External nodes (live P/Q feed): mailbox latest-wins, sample-and-hold staleness (hold \| zero), value bound, full-day history ring, grid-swap reset, the exporter's deterministic replay reset, estimator survival on a driven profile-less bus, scenario persistence of placements. |

## Recording, export & scenarios

| file | purpose |
|---|---|
| `test_recorder.py` | Session recorder: exactly the projected wire layers land as tidy CSVs (strict mode → no truth files), (day, step) dedupe, estimate blocks only on NEW estimates, self-describing pack with metadata.json. |
| `test_exporter.py` | Bulk export: offline replay reproduces LIVE physics (batteries across midnight, meter raster), honors the estimate switch, reports progress, cancels into a finalized partial pack, refuses concurrent runs. |
| `test_scenarios.py` | Scenario recipes: the DER journal captures runtime mutations coalesced, and replaying journal + batteries + meters on a FRESH simulator reproduces the setup. |
| `test_scenario4.py` | Reference scenario 4 recipe sanity: pins the committed artifact's structure (42 wallbox blocks, 42 Steuerboxen, clock) and that the trimmed picker manifest carries its district. |
| `test_benchmark_fixtures.py` | The frozen validation fixtures (`benchmarks/fixtures/`) load, build, solve, and still match their MANIFEST metadata incl. the noon-physics anchor — trips BEFORE a pipeline change silently invalidates the published OpenDSS/MATPOWER benchmark numbers (no OpenDSS/Octave needed here). |

## Conventions worth knowing

- API tests set `settings.autostart = False` **before** importing
  `netzsim.api` so `/state` stays deterministic (404 until a solve).
- Control/estimation tests bypass the estimator's wall-clock throttle with
  `sim._est_wall = 0.0` (a fresh estimate per step) — back-to-back test steps
  would otherwise starve it.
- Tests that write scenarios monkeypatch `settings.scenarios_dir` to
  `tmp_path`; never let a test write into the committed `data/scenarios/`.
- Day-graph/measured-layer assertions depend on the per-device TAF mode;
  placement changes bump the raster, so pin the mode explicitly.
