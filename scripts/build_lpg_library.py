"""Build a cached library of realistic household load profiles with the LPG.

Runs the Load Profile Generator (via ``pylpg``) once per household archetype for
a full year, then samples a handful of representative days and stores them as
normalised 1-minute daily profiles. The result is committed under
``data/lpg_library/`` so the netzsim runtime never needs the .NET engine.

Run with the interpreter that has ``pylpg`` installed (here: the *global*
Python 3.14, not the project venv)::

    python scripts/build_lpg_library.py                 # curated 8 archetypes
    python scripts/build_lpg_library.py --archetypes CHR01 CHR07 --variants 12

Notes
-----
* We use the per-household electricity series ``Electricity_HH1`` (appliance +
  lighting demand), not the house-level series (which nets out the HT01 battery).
* LPG values are kWh consumed per 1-minute step; we store **kW** = value * 60.
* Date strings are parsed MM.DD.YYYY by the engine; the post-processing only
  relies on whole 1440-step days actually returned, so it is robust to that.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

import numpy as np

OUT = Path(__file__).resolve().parents[1] / "data" / "lpg_library"

# Curated archetypes spanning the household-type spectrum.
CURATED = [
    "CHR01",  # couple, both at work        -> strong evening peak
    "CHR03",  # family, 1 child, both work
    "CHR05",  # family, 3 children, both work
    "CHR07",  # single with work
    "CHR16",  # couple over 65 (home all day) -> flatter daytime load
    "CHR30",  # single retired man
    "CHR22",  # single woman, 1 child, with work
    "CHR52",  # student flatsharing          -> late-night load
]


def _label(attr_name: str) -> str:
    # CHR01_Couple_both_at_Work -> "Couple both at Work"
    return attr_name.split("_", 1)[1].replace("_", " ")


def _household_refs():
    from pylpg.lpgdata import Households  # global-python only
    refs = {}
    for attr in dir(Households):
        if attr.startswith("CHR"):
            refs[attr.split("_")[0]] = (attr, getattr(Households, attr))
    return refs


def _sample_days(n_days: int, n_variants: int) -> list[int]:
    if n_days <= n_variants:
        return list(range(n_days))
    # evenly spaced across the year -> seasonal + weekday spread
    return [int(round(i)) for i in np.linspace(0, n_days - 1, n_variants)]


def build_archetype(code: str, refs: dict, *, year: int, start: str, end: str,
                    resolution: str, n_variants: int, round_kw: int) -> dict:
    from pylpg import lpg_execution as lpe
    from pylpg.lpgdata import HouseTypes

    attr, ref = refs[code]
    print(f"[{code}] {attr}: running LPG ({start}..{end}) ...", flush=True)
    t0 = time.time()
    df = lpe.execute_lpg_single_household(
        year=year, householdref=ref,
        housetype=HouseTypes.HT01_House_with_a_10kWh_Battery_and_a_fuel_cell_battery_charger_5_MWh_yearly_space_heating_gas_heating,
        startdate=start, enddate=end, resolution=resolution,
    )
    if df is None or "Electricity_HH1" not in df.columns:
        raise RuntimeError(f"{code}: LPG returned no Electricity_HH1 series")

    kwh_per_min = np.asarray(df["Electricity_HH1"], dtype=float)
    n_days = len(kwh_per_min) // 1440
    if n_days < 1:
        raise RuntimeError(f"{code}: only {len(kwh_per_min)} samples, <1 full day")
    daily = kwh_per_min[: n_days * 1440].reshape(n_days, 1440)
    kw = daily * 60.0  # kWh per minute -> kW

    picks = _sample_days(n_days, n_variants)
    variants = [[round(v, round_kw) for v in kw[d]] for d in picks]
    annual_kwh = float(daily.sum()) * (365.0 / n_days)  # extrapolate if <365 days

    print(f"[{code}] {n_days} days in {time.time()-t0:.0f}s; "
          f"{len(variants)} variants; ~{annual_kwh:.0f} kWh/yr", flush=True)
    return {
        "id": code,
        "name": attr,
        "label": _label(attr),
        "source": "LoadProfileGenerator 10.10.0",
        "load_type": "Electricity",
        "resolution_minutes": 1,
        "steps": 1440,
        "annual_kwh": round(annual_kwh, 1),
        "mean_kw": round(float(kw.mean()), round_kw),
        "peak_kw": round(float(kw.max()), round_kw),
        "n_variants": len(variants),
        "variant_day_of_year": [int(p) + 1 for p in picks],
        "variants_kw": variants,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--archetypes", nargs="+", default=CURATED,
                    help="household codes (e.g. CHR01 CHR07); default: curated set")
    ap.add_argument("--year", type=int, default=2019)
    ap.add_argument("--start", default="01.01.2019", help="MM.DD.YYYY")
    ap.add_argument("--end", default="12.31.2019", help="MM.DD.YYYY")
    ap.add_argument("--resolution", default="00:01:00")
    ap.add_argument("--variants", type=int, default=18, help="days sampled per archetype")
    ap.add_argument("--round", type=int, default=5, dest="round_kw")
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--force", action="store_true", help="rebuild archetypes already present")
    args = ap.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    refs = _household_refs()

    index_entries: list[dict] = []
    for code in args.archetypes:
        if code not in refs:
            print(f"!! unknown archetype {code}; skipping", flush=True)
            continue
        target = out / f"{code}.json"
        if target.exists() and not args.force:
            print(f"[{code}] already built; skipping (use --force to rebuild)", flush=True)
            doc = json.loads(target.read_text(encoding="utf-8"))
        else:
            doc = build_archetype(
                code, refs, year=args.year, start=args.start, end=args.end,
                resolution=args.resolution, n_variants=args.variants,
                round_kw=args.round_kw,
            )
            target.write_text(json.dumps(doc), encoding="utf-8")
        index_entries.append({
            k: doc[k] for k in
            ("id", "name", "label", "annual_kwh", "mean_kw", "peak_kw", "n_variants")
        } | {"file": f"{code}.json"})

    index = {
        "source": "LoadProfileGenerator 10.10.0",
        "generated": date.today().isoformat(),
        "load_type": "Electricity",
        "steps": 1440,
        "resolution_minutes": 1,
        "archetypes": index_entries,
    }
    (out / "index.json").write_text(json.dumps(index, indent=2), encoding="utf-8")
    print(f"\nwrote {out}/index.json with {len(index_entries)} archetype(s)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
