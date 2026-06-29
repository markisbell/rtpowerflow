"""Build a curated, characterized library of ding0 grids for the Grid page.

ding0 generates *real* MV grid districts by id; size and rural/urban character are
intrinsic to each district (from open_eGo data). This script picks a spread of
districts by their metadata (population density -> rural/suburban/urban; load-area
count -> size), generates them once from the OEP, then derives library entries:

  * one **MV** grid per district (the 10/20 kV graph, LV folded into lumped loads)
  * a few **LV** grids per district (single 0.4 kV grids, by node-count bucket)

Each entry is written to ``data/grid_library.json`` with {voltage, character,
nodes}. The netzsim backend reads that manifest and uses
``ding0_import.convert_ding0_csv(source_dir, scope=..., lv_grid_id=...)`` to load
the chosen grid — no ding0 at runtime.

Run with the Python-3.9 ding0 conda env:
    C:/Users/bell/ding0mamba/python.exe scripts/build_grid_library.py
"""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_ding0_grid as gen  # noqa: E402  (sets up OEP fixes + skip-generators on import)

from egoio.tools import db  # noqa: E402
from egoio.db_tables import grid as gt  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
GRIDS = REPO / "data" / "ding0_grids"
MANIFEST = REPO / "data" / "grid_library.json"

# population density (inh/ha) tertile cuts measured across all 3591 districts
RURAL_MAX, SUBURBAN_MAX = 1.2, 4.0
# per-character target load-area counts -> a small + larger district each
SIZE_TARGETS = {"urban": [6, 22], "suburban": [22, 60], "rural": [60, 130]}
LV_BUCKETS = [(10, 50), (50, 150), (150, 400)]  # node-count buckets for LV picks


def classify(pop_density: float) -> str:
    if pop_density <= RURAL_MAX:
        return "rural"
    if pop_density <= SUBURBAN_MAX:
        return "suburban"
    return "urban"


def select_districts(session) -> list[tuple[int, str]]:
    MVGD = gt.EgoDpMvGriddistrict
    q = session.query(MVGD.subst_id, MVGD.population_density, MVGD.la_count, MVGD.area_ha) \
        .filter(MVGD.version == "v0.4.5")
    df = pd.read_sql_query(q.statement, session.bind).dropna()
    df["character"] = df.population_density.map(classify)
    picks: list[tuple[int, str]] = []
    seen: set[int] = set()
    for char, targets in SIZE_TARGETS.items():
        sub = df[(df.character == char) & (df.la_count <= 160)]
        for t in targets:
            if sub.empty:
                continue
            row = sub.iloc[(sub.la_count - t).abs().argsort().iloc[:1]]
            sid = int(row.subst_id.iloc[0])
            if sid not in seen:
                seen.add(sid)
                picks.append((sid, char))
    return picks


def mv_node_count(csv_dir: Path) -> int:
    b = pd.read_csv(csv_dir / "buses.csv")
    lvg = b["lv_grid_id"].astype(str).str.strip()
    is_mv = (b["lv_grid_id"].isna() | lvg.isin(["", "nan"])) & (b["v_nom"] > 1.0)
    return int(is_mv.sum())


def lv_grid_sizes(csv_dir: Path) -> pd.Series:
    b = pd.read_csv(csv_dir / "buses.csv")
    lv = b.dropna(subset=["lv_grid_id"])
    ids = lv["lv_grid_id"].astype(str).str.split(".").str[0]
    ids = ids[ids != ""]
    return ids.groupby(ids).size().sort_values()


def main() -> int:
    session = sessionmaker(bind=db.connection(readonly=True))()
    picks = select_districts(session)
    print("selected districts:", picks, flush=True)

    entries: list[dict] = []
    for sid, char in picks:
        dest = GRIDS / f"ding0_oep_{sid}"
        try:
            if not (dest / "buses.csv").exists():
                print(f"generating {sid} ({char}) …", flush=True)
                gen.generate(sid)
            else:
                print(f"reusing existing {dest.name}", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"  FAILED to generate {sid}: {e}", flush=True)
            traceback.print_exc()
            continue

        src = dest.name
        # one MV entry
        mvn = mv_node_count(dest)
        entries.append({
            "id": f"mv_{char}_{sid}", "name": f"{char.capitalize()} MV grid · district {sid}",
            "voltage": "MV", "character": char, "nodes": mvn,
            "source_dir": src, "scope": "mv",
        })
        # a few LV entries across node-count buckets
        sizes = lv_grid_sizes(dest)
        for lo, hi in LV_BUCKETS:
            band = sizes[(sizes >= lo) & (sizes <= hi)]
            if band.empty:
                continue
            mid = (lo + hi) / 2
            lvid = (band - mid).abs().idxmin()
            entries.append({
                "id": f"lv_{char}_{sid}_{lvid}", "name": f"{char.capitalize()} LV grid · {lvid}",
                "voltage": "LV", "character": char, "nodes": int(band[lvid]),
                "source_dir": src, "scope": "lv", "lv_grid_id": str(lvid),
            })

    entries.sort(key=lambda e: (e["voltage"], e["character"], e["nodes"]))
    MANIFEST.write_text(json.dumps({"grids": entries}, indent=2))
    print(f"\nwrote {MANIFEST.relative_to(REPO)} with {len(entries)} entries:", flush=True)
    for e in entries:
        print(f"  {e['voltage']:2s} {e['character']:9s} {e['nodes']:4d} nodes  {e['id']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
