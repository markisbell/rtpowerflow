"""Generate a geo-referenced MV grid district *live* from the OpenEnergy Platform
(OEP) using ding0, and write it as eDisGo CSV into ``data/ding0_grids/`` where
the netzsim grid catalog auto-discovers it.

This replaces the abandoned local-PostgreSQL route: live OEP generation works
once two OEP-REST incompatibilities are worked around (both handled here, no
edits to the installed ding0/oedialect packages):

1. **String SRID** — ding0 builds ``ST_Transform(geom, '4326')`` with the SRID as
   a *string*; the OEP query parser rejects a string SRID (HTTP 400) but accepts
   an integer. We coerce it to int in the request body at the ``requests`` layer.
2. **Dropped materialized views** — ding0's generator import queries
   ``supply.ego_dp_res_powerplant_sq_mview`` / ``..._conv_...``, which have been
   removed from the OEP (HTTP 404). The base tables exist but lack the precomputed
   grid-district assignment columns ding0 needs, so we skip the generator import.
   netzsim layers its own PV/EV in the Load Studio, so DERs are not lost.

Run it with the **ding0 conda env** (Python 3.9), NOT the netzsim venv:

    C:/Users/bell/ding0mamba/python.exe scripts/generate_ding0_grid.py 1605 [1003 ...]

Requires ``~/.egoio/config.ini`` with a valid ``[oedb]`` section (OEP token).
Tip: districts 1605 and 1003 are among the smallest (~50 ha) and generate in
seconds; large districts can take much longer.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
os.environ.setdefault("OEDIALECT_VERIFY_CERTIFICATE", "FALSE")

import urllib3  # noqa: E402

urllib3.disable_warnings()

import oedialect.engine as _oe  # noqa: E402
import requests as _rq  # noqa: E402

# oedialect 0.0.10 rewrites the API host to the dead hyphenated domain; the OEP
# moved to openenergyplatform.org. Rewrite it back at the request layer.
_OLD_HOST, _NEW_HOST = "openenergy-platform.org", "openenergyplatform.org"


def _coerce_srid(node) -> None:
    """Recursively turn ST_Transform's string SRID operand into an int."""
    if isinstance(node, dict):
        if node.get("function") == "ST_Transform":
            ops = node.get("operands")
            grouping = ops.get("grouping") if isinstance(ops, dict) else None
            if isinstance(grouping, list):
                for i, el in enumerate(grouping):
                    if isinstance(el, str) and el.lstrip("-").isdigit():
                        grouping[i] = int(el)
        for v in node.values():
            _coerce_srid(v)
    elif isinstance(node, list):
        for v in node:
            _coerce_srid(v)


class _RequestsShim:
    """Drop-in for oedialect.engine.requests: fixes host + string SRID."""

    def __getattr__(self, name):
        fn = getattr(_rq, name)

        def wrapped(url, *args, **kw):
            if isinstance(url, str):
                url = url.replace(_OLD_HOST, _NEW_HOST)
            if name == "post":
                if isinstance(kw.get("json"), dict):
                    _coerce_srid(kw["json"])
                elif isinstance(kw.get("data"), (str, bytes)):
                    try:
                        body = json.loads(kw["data"])
                        _coerce_srid(body)
                        kw["data"] = json.dumps(body)
                    except Exception:
                        pass
            return fn(url, *args, **kw)

        return wrapped


_oe.requests = _RequestsShim()

from egoio.tools import db  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from ding0.core import NetworkDing0  # noqa: E402


def _skip_generators(self, session, debug=False):  # noqa: ANN001
    print("  [import_generators skipped — OEP generator mviews are gone (404)]", flush=True)


NetworkDing0.import_generators = _skip_generators

REPO = Path(__file__).resolve().parents[1]
OUT_ROOT = REPO / "data" / "ding0_grids"


def generate(district_id: int) -> Path:
    """Generate one MV grid district and write flattened eDisGo CSV to OUT_ROOT."""
    session = sessionmaker(bind=db.connection(readonly=True))()
    nd = NetworkDing0(name=f"ding0_oep_{district_id}")
    t0 = time.time()
    nd.run_ding0(session=session, mv_grid_districts_no=[district_id])
    print(f"  run_ding0 OK in {time.time() - t0:.0f}s", flush=True)

    # ding0.to_csv writes to <tmp>/<grid_id>/; flatten into ding0_oep_<id>/.
    tmp = OUT_ROOT / f".tmp_{district_id}"
    if tmp.exists():
        shutil.rmtree(tmp)
    nd.to_csv(str(tmp))
    inner = next(p for p in tmp.iterdir() if p.is_dir() and (p / "buses.csv").exists())
    dest = OUT_ROOT / f"ding0_oep_{district_id}"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.move(str(inner), str(dest))
    shutil.rmtree(tmp, ignore_errors=True)
    n_bus = sum(1 for _ in (dest / "buses.csv").open()) - 1
    print(f"  wrote {dest.relative_to(REPO)}  ({n_bus} buses)", flush=True)
    return dest


def main() -> int:
    ids = [int(a) for a in sys.argv[1:]] or [1605]
    OUT_ROOT.mkdir(parents=True, exist_ok=True)
    ok = []
    for did in ids:
        print(f"=== generating MV grid district {did} from OEP ===", flush=True)
        try:
            generate(did)
            ok.append(did)
        except Exception as e:  # noqa: BLE001
            import traceback

            print(f"  FAILED district {did}: {type(e).__name__}: {e}", flush=True)
            traceback.print_exc()
    print(f"\nDone. Generated {len(ok)}/{len(ids)}: {ok}", flush=True)
    return 0 if len(ok) == len(ids) else 1


if __name__ == "__main__":
    raise SystemExit(main())
