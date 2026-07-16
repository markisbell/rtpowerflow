# Benchmark plan: validating netzsim against OpenDSS and MATPOWER

> **Status: Phase 0 ✅ + Phase 1 ✅ BUILT (2026-07-16) — the T-series is
> GREEN.** Fixtures frozen (`benchmarks/fixtures/` g1/g2/g3 + MANIFEST,
> effective profiles of the published teaching scenarios, seeds baked in),
> the netzsim reference runner produces full-day arrays
> (`netzsim_runner.py`; G1 vm 0.9845–1.0931 pu = the scenario-1 voltage
> rise, G2 0.9065–1.0472 pu = the EV evening), and **pandapower vs real
> MATPOWER 8.1 on byte-identical IEEE cases passes all gates**
> (`run_matpower.py`): case9 4.4e-16, case14 5.5e-12, case_ieee30
> 2.6e-10, case118 2.6e-12 pu max |ΔVm| — orders of magnitude inside the
> 1e-6 gate.
> **Phase 2 ✅ (G-series vs OpenDSS): G1 and G2 PASS over the full
> 1440-step day** against OpenDSS daily mode (AltDSS engine,
> `to_dss.py` + `run_opendss.py`): with the §5.2 trafo alignment
> (`--align-trafo` on both sides) **max |ΔV| = 5.1e-7 pu (0.2 mV), max
> |ΔI| = 0.0005 A** on both grids — 20× inside the 1e-5 gate. The
> full-model supplementary runs land at 2.4e-5 (G1) / 3.2e-5 pu (G2):
> that is the measured effect of the **magnetizing-branch POSITION**
> (pandapower's T model centers it, OpenDSS hangs it at winding 1) —
> diagnosed via a constant LV-side offset with an exact slack. Mapping
> notes baked into `to_dss.py`: pandapower `parallel` folds into
> effective per-km values (netzsim's i_ka is the bundle current);
> normally-open lines map to `enabled=no`; loads/sgens use ABSOLUTE
> LoadShapes (`UseActual=yes`, Pmult/Qmult in kW/kvar, sgens as negative
> loads) which sidesteps P/Q-proportionality assumptions. The official
> EPRI engine leg (`--engine epri`) stays an open item: py-dss-interface
> `text()` also hangs on `compile` in non-interactive shells — run it
> from a real terminal later; the AltDSS engine is dss-extensions'
> regularly cross-validated implementation and `check_env` proves the
> EPRI 11.0.0.1 engine loads here. Next: G3 snapshots, MATPOWER G-leg,
> plots + public report (§8). Two Phase-1 findings, both patched identically into the ONE
> struct both solvers consume (no physics change): classic IEEE CDF cases
> carry **BASE_KV=0** (MATPOWER's solver never uses it; pandapower's
> from_mpc divides by it → set 100 kV), and **RATE_A=0** (= unlimited)
> trips an indexing bug in pandapower's from_ppc → set MATPOWER's
> conventional 9900-MVA placeholder. Next: §9 step 4, `to_dss.py` +
> OpenDSS snapshot/daily (G-series). The
> environment stands and `benchmarks/check_env.py` is green on all six
> checks (Python 3.12.10 in `.venv-bench`, pandapower 3.4.0,
> OpenDSSDirect.py 0.9.4 [AltDSS engine, OpenDSS SVN 3723 base],
> py-dss-interface 2.3.0 [**official EPRI engine 11.0.0.1
> "Charlottesville"** — the same version as the current SourceForge
> installer], Octave 11.3.0 portable at `C:\Users\bell\octave\`,
> MATPOWER 8.1). Two Phase-0 findings baked into this plan:
> (a) **oct2py 6.x is incompatible with pandapower 3.4.0** (scipy>=1.17.1
> vs scipy<1.17) — pin `oct2py==5.8.0`; (b) **py-dss-interface's
> `text()` hangs in non-interactive shells** — `check_env` reads the
> engine version via the property API, and Phase 1 must clarify the
> command-driving path for that engine (candidates: run it under a real
> terminal, or drive `.dss` files via OpenDSSDirect.py and use
> py-dss-interface only for the cross-check solve). The plan was
> researched against the July-2026 state of the toolchains; the
> implementing agent updates this header as further phases land.

## 1. Goal and trust argument

netzsim is a *teaching* platform, but its physics must be beyond doubt: every
lesson it teaches (voltage rise, feeder overloads, estimation error) is only
as credible as its power flow. netzsim solves with
[pandapower](https://github.com/e2nIEE/pandapower)'s Newton-Raphson — which is
itself validated — but **this repository adds its own pipeline on top**
(input JSONs → network construction → std-type resolution → 1440-step
time series → derived quantities like `i_ka = S/(√3·V_LL)`). The benchmark
validates that **whole pipeline end-to-end** against two independent,
widely trusted solvers:

- **OpenDSS** (EPRI, [sourceforge.net/projects/electricdss](https://sourceforge.net/projects/electricdss/)) —
  the reference distribution-system simulator, with native daily time-series
  mode.
- **MATPOWER** ([github.com/MATPOWER/matpower](https://github.com/MATPOWER/matpower)) —
  the reference steady-state power-flow package, run as *real* MATPOWER 8.1
  on GNU Octave (no MATLAB license required).

Deliverables, all committed to this repo:

1. `benchmarks/` — a self-contained, reproducible harness
   (`python benchmarks/run_all.py` regenerates everything).
2. `docs/benchmarks/README.md` — the public results report: per-grid error
   tables (absolute node-voltage errors, branch-current errors) and daily
   profile overlays (voltage + current) for selected buses/lines.
3. A **Validation** section + badge in the main README linking to the report.

## 2. What is being compared (scope and honesty)

netzsim runs a **balanced positive-sequence** AC power flow (three-phase
symmetric; documented in `README.md` §observability). The comparison is
therefore defined on balanced models in all three tools:

- OpenDSS is driven as a balanced solver: 3-phase symmetric elements defined
  from sequence data (R1/X1/C1), balanced wye PQ loads. This is exact — the
  phases decouple and equal the positive-sequence solution.
- The famous *unbalanced* IEEE feeders (13/34/123-node) are **out of scope**
  and the report must say so: netzsim does not model per-phase unbalance
  (pandapower's `runpp_3ph` is not used). Benchmarking against them would
  compare different problems.
- Quantities compared: bus voltage magnitude (absolute error in **pu** and
  **volts**), branch current magnitude (**A**), losses (kW), convergence,
  solve time. Voltage *angles* are compared where vector groups allow
  (transformer `shift_degree` is zeroed in the mapped models, see §5.3).

## 3. Phase 0 — environment installation (reproducible)

### 3.1 A dedicated benchmark venv (NOT the dev venv)

The dev venv runs **Python 3.14**; the OpenDSS Python stack is only tested
up to 3.12 (see below). Create a pinned 3.12 venv:

```powershell
# from the repo root; py -3.12 must be installed (python.org installer)
py -3.12 -m venv .venv-bench
.venv-bench\Scripts\pip install -r benchmarks\requirements.txt
```

`benchmarks/requirements.txt` (pin exactly; re-verify latest at execution):

```
pandapower==3.4.0          # same version as the dev venv — the thing under test
numpy>=1.24
scipy>=1.11                # .mat bridge + savemat fallback
matplotlib>=3.8            # result plots
# --- OpenDSS (two engines, see §3.3) ---
OpenDSSDirect.py==0.9.4    # dss-extensions AltDSS engine (pip, cross-platform)
py-dss-interface==2.3.0    # bundles the OFFICIAL "OpenDSS Powered by EPRI" C++ engine (Windows)
# --- MATPOWER via Octave ---
matpower==8.1.0.2.3.0      # bundles real MATPOWER 8.1 (yasirroni/matpower-pip)
oct2py==5.8.0              # Octave bridge — NOT 6.x: oct2py 6.x requires
                           # scipy>=1.17.1, pandapower 3.4.0 pins scipy<1.17
                           # (found the hard way during Phase 0)
```

### 3.2 GNU Octave (for real MATPOWER)

```powershell
winget install -e --id GNU.Octave        # 11.3.0 at plan time
# oct2py discovery is NOT automatic on Windows — set explicitly:
setx OCTAVE_EXECUTABLE "C:\Program Files\GNU Octave\Octave-11.3.0\mingw64\bin\octave-cli.exe"
```

(Chocolatey lags Octave releases; winget is the reproducible path. The
harness must fail fast with a clear message if `OCTAVE_EXECUTABLE` is unset
and Octave is not on PATH.)

### 3.3 Two OpenDSS engines — deliberate redundancy

| engine | package | role |
|---|---|---|
| **Official EPRI engine** (the SourceForge OpenDSS, bundled as C++ build) | `py-dss-interface` 2.3.0 (Python 3.9–3.14, Windows) | **Primary reference** — this is what "validated against OpenDSS" must mean |
| AltDSS / DSS C-API (dss-extensions; *"cross-validated with the official OpenDSS engine on a regular basis"*, but explicitly not EPRI-supported) | `OpenDSSDirect.py` 0.9.4 → `dss-python` 0.15.7 | Portability leg (Linux/CI) + an extra parity check for free |

The harness runs the **same generated `.dss` files through both** and the
report states both results. Agreement between the two engines is itself a
sanity gate (they must agree to ≤ 1e-6 pu; if they don't, the `.dss` model
is ambiguous — fix the model, don't average).

`benchmarks/check_env.py` verifies the whole stack and prints exact versions
(Python, pandapower, both DSS engines + their engine version strings, Octave
version via `oct2py`, MATPOWER version via `m.mpver()`) — this output is
committed into the results manifest (§7.2).

## 4. Phase 1 — harness architecture

```
benchmarks/
├── requirements.txt
├── check_env.py            # §3.3 — env verification, prints the manifest header
├── run_all.py              # single entry point: runs T-, G- (and L-) series, writes docs/benchmarks/
├── fixtures/               # FROZEN inputs — the reproducibility anchor (§6.4)
│   ├── g1_lv_rural/        #   the 5 netzsim input JSONs (data_dir format), seeded loads baked in
│   ├── g2_lv_suburban/
│   └── g3_mv_district/
├── netzsim_runner.py       # netzsim side: GridInputs → Simulator → 1440 offline steps → arrays
├── to_dss.py               # exporter: pandapower net (+ profiles) → circuit.dss + loadshape CSVs
├── to_mpc.py               # exporter: pandapower net → MATPOWER mpc dict (+ per-step scaling)
├── run_opendss.py          # drives BOTH engines over the same .dss (snapshot + daily loop)
├── run_matpower.py         # oct2py session: runpf per case / per step
├── compare.py              # error metrics, tables, pass/fail gates
└── plots.py                # daily overlays V/I, error-over-day curves
```

Design rules:

- **netzsim side = the real pipeline, no shortcuts.** `netzsim_runner.py`
  loads the fixture via `data_loader.load_inputs` and steps a `Simulator`
  offline over 1440 steps (the bulk exporter's replay path) — so std-type
  resolution, profile packing and the derived quantities are all under test.
  No REST server needed; runs deterministic.
- **Exporters read the EFFECTIVE pandapower tables** (`net.line`,
  `net.trafo`, `net.load` after construction), never the input JSONs —
  netzsim lines/trafos defined via `std_type` get their r/x/c from
  pandapower's library, and that resolved data is what must be mapped.
- Every exporter emits a `mapping_report.txt` per grid (element counts,
  parameter ranges, what was zeroed) so an external reviewer can audit the
  translation without reading code.

## 5. Phase 1b — the model mapping (where benchmarks live or die)

These conversions were source-verified against the pandapower 3.x element
docs, the OpenDSS reference (opendss.epri.com + dss-extensions/dss-format)
and the MATPOWER case-format docs. The implementing agent must copy them
into code comments verbatim.

### 5.1 Lines

pandapower: `Z = (r_ohm_per_km + j·x_ohm_per_km)·length_km/parallel`;
shunt `Y = j·2π·f·c_nf_per_km·1e-9·length_km·parallel` (total, half per end),
`f = net.f_hz = 50`.

- **→ OpenDSS** (1:1 with `units=km` on BOTH LineCode and Line — the default
  `units=none` silently assumes agreement):

  ```
  New LineCode.lc_<i> nphases=3 r1=<r_ohm_per_km> x1=<x_ohm_per_km>
      c1=<c_nf_per_km> r0=<r> x0=<x> c0=<c> units=km normamps=<max_i_ka*1000>
  New Line.<name> bus1=<from> bus2=<to> linecode=lc_<i> length=<length_km>
      units=km phases=3
  ```

  `c1` is natively **nF per unit length** — no conversion. Setting r0/x0/c0
  = r1/x1/c1 avoids OpenDSS's unbalanced defaults (never excited in a
  balanced case, but keeps the model unambiguous).

- **→ MATPOWER** (per-unit on `Zbase = BASE_KV²/baseMVA`, baseMVA = 100):

  ```
  r = r_ohm_per_km·L / Zbase        x = x_ohm_per_km·L / Zbase
  b = 2π·50 · (c_nf_per_km·1e-9·L) · Zbase      # TOTAL charging (BR_B is total)
  ```

  MATPOWER has no branch shunt conductance — netzsim grids have
  `g_us_per_km = 0`, assert that in the exporter.

### 5.2 Transformers

pandapower (`vk_percent`, `vkr_percent`, `pfe_kw`, `i0_percent`, `sn_mva`,
tap): for the benchmark **zero the magnetizing branch on the pandapower
copy** (`pfe_kw=0`, `i0_percent=0`) — then pandapower's default T-model is
identical to a pi/series branch and all three tools agree by construction.
(The committed netzsim grids use std types whose pfe/i0 are small but
nonzero; the benchmark documents this zeroing as a *model alignment*, and
one supplementary run keeps them nonzero to show the — tiny — effect.)

- **→ OpenDSS**:

  ```
  XHL        = sqrt(vk_percent² − vkr_percent²)     # percent on winding-1 kVA base
  %loadloss  = vkr_percent
  %noloadloss = 0        %imag = 0                  # zeroed as above
  New Transformer.<name> phases=3 windings=2 buses=[<hv> <lv>]
      conns=[wye wye] kVs=[<vn_hv_kv> <vn_lv_kv>] kVAs=[<sn_mva*1000> <sn_mva*1000>]
      XHL=<XHL> %loadloss=<vkr_percent> %noloadloss=0 %imag=0 ppm_antifloat=0
  ```

  **Traps (verified against Transformer.pas):** an "empty" OpenDSS
  transformer is NOT lossless — defaults are `XHL=7 %`, `%loadloss=0.4 %`,
  `ppm_antifloat=1e-6` (a tiny grounding reactance per winding). All three
  must be set explicitly. `conns=[wye wye]` avoids the ±30° Dyn vector-group
  shift; correspondingly set `shift_degree=0` on the pandapower copy (std
  types carry 150°) — or compare magnitudes only.

- **→ MATPOWER** (branch with trafo→system rebase; impedance sits at the
  *to* side, tap at the *from* side, HV as from-bus):

  ```
  r = (vkr_percent/100)·(baseMVA/sn_mva)
  x = (sqrt(vk²−vkr²)/100)·(baseMVA/sn_mva)
  TAP = (vn_hv_kv/BASE_KV_hv) / (vn_lv_kv/BASE_KV_lv)   # = 1 when BASE_KV = vn
  ```

### 5.3 Loads, slack, solver settings

- **Loads → OpenDSS**: `New Load.<name> bus1=<bus> phases=3 conn=wye model=1
  kv=<vn_kv LL> kW=<p_mw*1000> kvar=<q_mvar*1000> Vminpu=0.0 Vmaxpu=2.0
  daily=<shape>`. Two verified killers:
  - **`Vminpu` defaults to 0.95** — below it OpenDSS silently converts the
    load to constant impedance (convergence aid). A loaded LV feeder dips
    below 0.95 pu; without `Vminpu=0` OpenDSS solves a *different problem*
    than pandapower's strict constant-PQ.
  - Loads without an explicit `daily=` follow OpenDSS's built-in `default`
    LoadShape in daily mode — attach a shape to every load.
- **Slack → OpenDSS**: the Vsource is a Thevenin equivalent by default
  (`MVAsc3=2000` → the source bus sags under load). Use an ideal slack:
  `New Circuit.<name> bus1=slack basekv=<vn_kv LL> pu=<vm_pu> phases=3
  Model=Ideal puZideal=[1e-8 1e-8]`.
- **Frequency**: `Set DefaultBaseFrequency=50` **immediately after `Clear`,
  before `New Circuit`** — OpenDSS defaults to 60 Hz and the C1→B conversion
  inherits it (silent +20 % line charging otherwise).
- **Solver**: `Set VoltageBases=[<all vn_kv LL>]` + `CalcVoltageBases`, then
  `Set tolerance=1e-8 maxiterations=100 ControlMode=Static`. Daily mode:
  `Set Mode=daily StepSize=1m Number=1`, one `dss.Solution.Solve()` per step,
  LoadShapes as `New LoadShape.<n> npts=1440 minterval=1 mult=(file=…)` —
  netzsim's 1440×1-min arrays map 1:1.
- **MATPOWER**: `mpoption('verbose',0,'out.all',0)`, `pf.tol` default 1e-8
  matches pandapower's `tolerance_mva=1e-8`. Time series = per-step rewrite
  of `bus[:, PD/QD]` (columns 3/4, 1-based) from netzsim's profile arrays,
  one `runpf` per step, reusing ONE Octave instance (`start_instance()` has
  seconds of startup; a 1440-step loop is fine once warm).
- **Readout conventions**: OpenDSS reports volts **L-N** — compare in
  absolute volts via `vm_pu·vn_kv·1000/√3` (per-unit comparison via the
  API's `puVmagAngle` needs `CalcVoltageBases` to reproduce netzsim's per-bus
  `vn_kv` exactly, which off-nominal trafo LV sides break; raw volts are
  unambiguous). MATPOWER `VM` (bus col 8) is already pu on `BASE_KV`; branch
  flow results are appended columns `PF/QF/PT/QT` (14–17) — derive current
  as `i_ka = sqrt(PF²+QF²)/(√3·VM·BASE_KV)`, the same convention as
  netzsim's `i_ka`.

## 6. Phase 2 — benchmark selection

Two families, per the requirement "generated grids + IEEE reference grids",
plus an optional stretch case.

### 6.1 T-series: IEEE reference cases, pandapower vs MATPOWER (snapshot)

Identical case data on both sides — `pandapower.networks` ships the cases
(verified in the dev venv, pandapower 3.4.0) and MATPOWER 8.1 ships the same
`.m` files; the harness additionally round-trips netzsim's side through
`pandapower.converter.matpower.from_mpc` on MATPOWER's own file so both
solvers consume **byte-identical case data**.

| id | case | note |
|---|---|---|
| T1 | `case14` | THE IEEE 14-bus case (UW archive lineage via cdf2matp) |
| T2 | `case_ieee30` | the true IEEE 30-bus case — **not** `case30`, which is the Alsac & Stott variant (documented trap) |
| T3 | `case118` | IEEE 118-bus |
| T4 | `case9` | Chow/WSCC 9-bus — labeled as textbook reference, *not* IEEE |

Compared: `vm_pu` per bus, `va_degree`, branch P/Q flows, losses.
**Pass gate: max |ΔVm| ≤ 1e-6 pu** (same NR family — pandapower's solver
descends from PYPOWER/MATPOWER — at matching tolerance 1e-8; pandapower's
own element validation against PowerFactory uses the same 1e-6 gate).

### 6.2 G-series: netzsim-generated grids, 1440-step day vs OpenDSS + MATPOWER

The heart of the benchmark — the committed teaching grids with their real
daily profiles, i.e. exactly what netzsim users simulate:

| id | grid (fixture) | buses | profiles | compared against |
|---|---|---|---|---|
| G1 | `lv_rural_3150_300266` — the scenario-1 grid | ~30 | LPG households (seed-frozen) + the 75-kWp farm PV at bus 24, a clear-sky day | OpenDSS daily (both engines) + MATPOWER 1440-step loop |
| G2 | `lv_suburban_1864_265991` — the scenario-2/3 grid | ~62 | LPG + 12 staggered 11-kW EV wallboxes (the evening feeder-overload story) | OpenDSS daily (both engines) |
| G3 | `mv_rural_3150` district | 475 | committed placeholder/LPG profiles | snapshot at 3 characteristic steps (03:00 night valley, 12:00 PV noon, 18:45 evening peak) vs OpenDSS + MATPOWER; full day optional |

Selection rationale: G1/G2 tie the validation to the *published teaching
scenarios* — the same feeder-end voltage rise and feeder-head current the
manual discusses are the ones independently recomputed. G3 proves the
mapping scales to the MV district incl. HV/MV transformers and 154 lumped
stations.

**Pass gates (mapped models): max |ΔVm| ≤ 1e-5 pu per step (fail > 1e-4),
max |ΔI| ≤ 0.1 A on LV / ≤ 0.5 A on MV.** Rationale: OpenDSS's own default
convergence tolerance is 1e-4 — only after tightening to 1e-8, ideal-slack
Vsource, `Vminpu=0` and the §5.2 transformer zero-outs is 1e-5…1e-6
achievable; 1e-5 is the defensible public gate, and the report prints the
achieved numbers (typically better).

### 6.3 L-series (stretch, phase B): IEEE European LV Test Feeder

OpenDSS ships the IEEE European LV Test Feeder as a reference model
(`electricdss-tst: Version8/Distrib/IEEETestCases/LVTestCase/`, incl.
`Daily_1min_100profiles/` — 1-min × 1440 household profiles, exactly
netzsim's raster). Direction is *reversed* here: translate the feeder INTO
netzsim's gridformat (balanced aggregation of the single-phase loads,
documented) and compare netzsim against OpenDSS running the *original*
reference files. This is the strongest external anchor (IEEE model + IEEE
profiles + untouched OpenDSS input) but the unbalanced→balanced aggregation
means magnitudes-only comparison with a wider documented tolerance. Keep it
out of the pass/fail gates; report it as a separate, honestly-labeled
section.

### 6.4 Fixtures — freezing the inputs

`benchmarks/fixtures/g*/` holds each grid as the **5 netzsim input JSONs**
(data_dir format: grid_structure/lines/load/generation/substation), produced
once via the catalog + seeded loadgen and then committed. This removes every
source of nondeterminism (LPG assignment seeds, dataset drift) — an external
person clones the repo and reruns bit-identical inputs. A
`fixtures/MANIFEST.md` records how each fixture was generated (grid id,
loadgen policy JSON, seed, netzsim commit).

## 7. Phase 3 — verification protocol (for external reproduction)

### 7.1 One command

```powershell
py -3.12 -m venv .venv-bench
.venv-bench\Scripts\pip install -r benchmarks\requirements.txt
winget install -e --id GNU.Octave          # + OCTAVE_EXECUTABLE, see §3.2
.venv-bench\Scripts\python benchmarks\run_all.py --all
```

`run_all.py` options: `--series T|G|L`, `--grid g1`, `--skip-matpower`
(no Octave present), `--steps N` (quick smoke). Everything it writes lands
under `docs/benchmarks/` — rerunning MUST be idempotent.

### 7.2 The results manifest

`docs/benchmarks/manifest.json`, regenerated on every run: timestamp, OS,
exact versions of Python/pandapower/OpenDSSDirect.py/dss-python engine
string/py-dss-interface engine string/Octave/MATPOWER, fixture SHA-256
hashes, netzsim git commit, per-benchmark pass/fail with achieved maxima.
The report README embeds this table — an external reviewer sees *which*
engine builds produced the numbers.

### 7.3 Method transparency

`docs/benchmarks/README.md` opens with the method section (short form of
§2/§5 of this plan): what is compared, the model-alignment choices
(magnetizing branch zeroed, wye-wye, ideal slack, 50 Hz, Vminpu=0), and the
known limitations (balanced-only; loading_percent compared as ampere current
against ratings, since OpenDSS `normamps` semantics differ). Every
alignment choice must be listed — a benchmark that hides its alignment is
advertising, not validation.

### 7.4 Regression protection

- `tests/test_benchmark_fixtures.py` (runs in the NORMAL dev suite, no
  OpenDSS/Octave needed): fixtures load, build, solve; exporter mapping
  invariants (element counts match, no silently dropped shunts, mapping
  formulas' round-trip on a 3-bus toy net vs hand-computed values).
- The full benchmark is NOT part of default CI (heavy native deps). Optional:
  a `workflow_dispatch` GitHub Action (`benchmarks.yml`) on ubuntu using
  OpenDSSDirect.py + `apt install octave` + matpower-pip, uploading
  `docs/benchmarks/` as artifact — run manually before releases.

## 8. Phase 4 — results presentation

### 8.1 Error tables (the headline numbers)

Per benchmark, in `docs/benchmarks/README.md` (numbers below are the table
*shape*, to be filled by the run):

| id | grid | steps | vs | max \|ΔV\| [pu] | mean \|ΔV\| [pu] | max \|ΔV\| [V] | max \|ΔI\| [A] | gate | result |
|---|---|---|---|---|---|---|---|---|---|
| G1 | lv_rural (30 buses) | 1440 | OpenDSS (EPRI) | … | … | … | … | 1e-5 | ✅/❌ |
| G1 | lv_rural | 1440 | OpenDSS (AltDSS) | … | … | … | … | 1e-5 | |
| G1 | lv_rural | 1440 | MATPOWER 8.1 | … | … | … | … | 1e-5 | |
| T1 | case14 | 1 | MATPOWER 8.1 | … | … | — | — | 1e-6 | |

Plus per-benchmark CSVs under `docs/benchmarks/data/` (per-bus max errors,
per-step max errors) so reviewers can drill down without rerunning.

### 8.2 Daily profile overlays (the visual proof)

`plots.py` renders PNG (150 dpi, light background, colorblind-safe) into
`docs/benchmarks/img/`, embedded in the report:

1. **G1 / bus 24 (the farm, feeder end): voltage over the day** — netzsim
   line vs OpenDSS markers (both engines), plus a ΔV(t) subplot in mV. This
   is the scenario-1 voltage-rise story independently recomputed: the noon
   PV peak pushing ~252 V must appear identically in both tools.
2. **G1 / main feeder line: current over the day** — same overlay + ΔI(t).
3. **G2 / feeder head L43: current over the day** — the scenario-2 NH-fuse
   story (~243 A evening plateau) vs OpenDSS.
4. **G2 / weakest bus: voltage over the day.**
5. **Error-over-day curve per grid**: max |ΔV| across all buses per step —
   shows the agreement holds through PV noon and EV evening, not just at
   light load.

Selection rule: one feeder-end bus + one feeder-head line per grid — the
electrically most sensitive spots, and the ones the manual talks about.

### 8.3 Visibility

- Main `README.md`: new **Validation** section (3–4 sentences + the headline
  table row per tool + link to `docs/benchmarks/README.md`), badge
  `![validated](https://img.shields.io/badge/validated-OpenDSS%20%7C%20MATPOWER-brightgreen)`
  next to the existing license/AI badges.
- `CLAUDE.md` §9/§10 updated (verified-status + the benchmark as a done item).
- Benutzerhandbuch: one paragraph in the Einführung ("Die Physik ist gegen
  OpenDSS und MATPOWER validiert, siehe docs/benchmarks/") — no full chapter.

## 9. Execution order for the implementing agent

1. **Env** (§3): venv, requirements, Octave; `check_env.py` green. ~30 min.
2. **Fixtures** (§6.4): freeze G1–G3 inputs + MANIFEST. Verify each loads
   and solves via `netzsim_runner.py` (this also produces the netzsim-side
   reference arrays).
3. **T-series first** (§6.1): smallest surface (no exporter needed beyond
   `from_mpc`), proves the MATPOWER/Octave leg + metric code. Gate 1e-6.
4. **`to_dss.py` + snapshot G1** vs both OpenDSS engines: iterate on the
   mapping until the snapshot meets 1e-5/1e-6; the §5 traps checklist is the
   debugging guide (work through it item by item when errors are ~1e-3 —
   that magnitude almost always means Vminpu, 60 Hz charging, or trafo
   defaults).
5. **G1 full day** (OpenDSS daily + MATPOWER loop), then G2, then G3
   snapshots. Runtime budget: G-series ≈ minutes (OpenDSS daily is fast;
   1440 Octave runpf ≈ a few minutes warm).
6. **compare.py / plots.py / report** (§8), manifest, README section, badge.
7. **`tests/test_benchmark_fixtures.py`** into the dev suite; update
   `tests/README.md`, `CLAUDE.md`, this file's status header → BUILT.
8. (Phase B, separate session) L-series §6.3.

Acceptance checklist: `run_all.py --all` exits 0 with all gates green on
this machine; `docs/benchmarks/README.md` renders on GitHub with tables +
6 figures; manifest committed; dev suite still green (158 + new fixture
tests); an external reader can follow §7.1 verbatim.

## 10. Risks & fallbacks

| risk | mitigation |
|---|---|
| OpenDSS wheels on Python 3.13/3.14 untested (dss-extensions last release 2024-03, wheels are cp37-abi3) | dedicated 3.12 venv (§3.1); py-dss-interface claims 3.9–3.14 as fallback |
| py-dss-interface is Windows-bundled only (Linux = build from source) | CI/portable leg uses OpenDSSDirect.py; the EPRI-engine run is documented as Windows-local |
| Octave not found by oct2py | explicit `OCTAVE_EXECUTABLE`; `check_env.py` fails fast with the fix command |
| 1440 × runpf Octave round-trips slow | reuse one warm `start_instance()`; fall back to `--steps 288` (5-min raster) for smoke runs — full raster for the committed report |
| G3 (475 buses × both tools) runtime | snapshots at 3 steps are the committed gate; full day optional flag |
| Mapped-model disagreement > gate | work the §5 trap checklist in order (Vminpu → 50 Hz → trafo defaults → ppm_antifloat → voltage bases); compare raw volts not pu; the two-engine OpenDSS cross-check separates "our .dss is wrong" from "engines differ" |
| `case_ieee30` naming in pandapower.networks differs | fall back to `from_mpc` on MATPOWER's own `case_ieee30.m` (byte-identical data is the requirement, not the loader) |
| pandapower `to_mpc` top-level export missing in 3.x | verified: import from `pandapower.converter.matpower` (works in 3.4.0) |

## 11. Verified toolchain facts (research snapshot, 2026-07)

So the implementing agent doesn't re-research: OpenDSSDirect.py 0.9.4 /
dss-python 0.15.7 (AltDSS engine ≈ official v9.6.1.3, released 2024-03-29,
wheels tested to Python 3.12); py-dss-interface 2.3.0 (2026-02, official
EPRI C++ engine bundled, Python 3.9–3.14, Windows prebuilt); official
Windows installer 11.0.0.1 "Charlottesville" (2026-01-30, COM); IEEE test
feeders live in `github.com/dss-extensions/electricdss-tst` under
`Version8/Distrib/IEEETestCases/` (not in the pip wheels). matpower-pip
8.1.0.2.3.0 (2026-02-25) = MATPOWER 8.1 (2025-07-12, latest upstream) via
oct2py 6.0.3 (needs Python ≥ 3.11); GNU Octave 11.3.0 (winget `GNU.Octave`);
PYPOWER 5.1.19 is a MATPOWER **4.x** port — CI sanity check at most, never
the headline comparator. MATPOWER result columns: bus VM=8/VA=9, branch
PF/QF/PT/QT=14–17 (1-based). pandapower 3.4.0: MATPOWER converter lives at
`pandapower.converter.matpower` (no top-level re-export);
`pandapower.networks` ships case9/14/30/118, CIGRE nets and
`ieee_european_lv_asymmetric`.
