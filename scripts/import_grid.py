"""Import a European-Archetype LV grid workbook into netzsim's input format.

Examples
--------
List the LV workbooks inside the archive::

    python scripts/import_grid.py --zip "European Archetpye Distribution Grid Models.zip" --list

Convert one workbook (matched by a substring of its path) into ./data::

    python scripts/import_grid.py \
        --zip "European Archetpye Distribution Grid Models.zip" \
        --member network_10_1_1137 --out data

Convert a standalone .xlsx file::

    python scripts/import_grid.py --xlsx path/to/network.xlsx --out data
"""
from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

# Allow running straight from a checkout without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from netzsim.grid_import import (  # noqa: E402
    convert_from_zip,
    convert_xlsx_file,
    write_inputs,
)


def _list_zip(zip_path: str) -> None:
    with zipfile.ZipFile(zip_path) as zf:
        xlsx = sorted(n for n in zf.namelist() if n.endswith(".xlsx") and "03_LV" in n)
    for n in xlsx:
        print(n)
    print(f"\n{len(xlsx)} LV workbook(s).")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--zip", help="archive containing the .xlsx grid workbooks")
    src.add_argument("--xlsx", help="a single .xlsx grid workbook")
    ap.add_argument("--member", help="substring selecting a workbook inside --zip")
    ap.add_argument("--list", action="store_true", help="list LV workbooks in --zip and exit")
    ap.add_argument("--out", default="data", help="output directory (default: data)")
    ap.add_argument("--steps", type=int, default=1440, help="steps per day (default: 1440)")
    ap.add_argument("--peak-mw", type=float, default=0.001,
                    help="placeholder per-load peak in MW (default: 0.001)")
    args = ap.parse_args(argv)

    if args.list:
        if not args.zip:
            ap.error("--list requires --zip")
        _list_zip(args.zip)
        return 0

    if args.zip:
        if not args.member:
            ap.error("--zip requires --member (substring of the workbook path)")
        grid = convert_from_zip(args.zip, args.member,
                                steps=args.steps, placeholder_peak_mw=args.peak_mw)
    else:
        grid = convert_xlsx_file(args.xlsx,
                                 steps=args.steps, placeholder_peak_mw=args.peak_mw)

    out = write_inputs(grid, args.out)
    n_bus = len(grid.grid_structure["buses"])
    n_line = len(grid.lines["lines"])
    n_tr = len(grid.lines["transformers"])
    n_load = len(grid.load["loads"])
    print(f"wrote {out}/  -  {n_bus} buses, {n_line} lines, {n_tr} trafo(s), {n_load} loads")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
