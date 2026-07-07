"""Session recorder: every published simulation step, appended to CSV on disk.

The recorder consumes exactly the payload that goes out on the WebSocket
(``StateStore``'s projected dict) — so it automatically respects strict
observability: what never reaches the wire never reaches the CSVs either.
Rows are appended incrementally by a dedicated writer thread fed through a
queue, so the engine loop is never blocked and memory stays flat however
long the run gets.

Layout of one recording (``data/recordings/<id>/``):

  metadata.json            recipe + stats (grid, loadgen, meters, policy, ...)
  summary.csv              one row per step: the truth summary
  observed_summary.csv     one row per step: aggregates over metered elements
  buses.csv, lines.csv, trafos.csv, ext_grids.csv
                           tidy long format (day, step, time, element, ...)
  batteries.csv, controllers.csv
  measurements_nodes.csv, measurements_trafos.csv   the Gemessen layer
  estimated_buses.csv, estimated_lines.csv, estimated_trafos.csv
                           one block per NEW estimate (metering raster)

Files appear lazily on their first row: a grid without batteries produces no
``batteries.csv``, and in strict mode no truth file exists at all. CSVs are
standard dialect (comma separator, dot decimals, UTF-8) — made for pandas &
Co.; ``None`` becomes an empty field. Duplicate ``(day, step)`` publishes
(e.g. around a pause) are dropped; a backward seek legitimately repeats
``(day, step)`` keys later in the file — the wall-clock ``timestamp`` column
keeps the recording order unambiguous.
"""
from __future__ import annotations

import csv
import json
import logging
import queue
import re
import shutil
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, TextIO

log = logging.getLogger("netzsim.recorder")

_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# column plans per file: (payload list key, csv name, element columns)
_ELEMENT_FILES = (
    ("buses", "buses.csv",
     ("index", "name", "vm_pu", "va_degree", "p_mw", "q_mvar")),
    ("lines", "lines.csv",
     ("index", "name", "from_bus", "to_bus", "loading_percent", "i_ka",
      "p_from_mw", "pl_mw")),
    ("trafos", "trafos.csv",
     ("index", "name", "hv_bus", "lv_bus", "loading_percent", "p_hv_mw",
      "q_hv_mvar", "i_hv_ka", "pl_mw")),
    ("ext_grids", "ext_grids.csv", ("index", "name", "p_mw", "q_mvar")),
    ("batteries", "batteries.csv",
     ("index", "bus", "name", "mode", "soc_percent", "p_mw",
      "capacity_kwh", "power_kw")),
    ("controllers", "controllers.csv",
     ("id", "scope", "bus", "limit_pct", "release_pct", "ev_factor",
      "pv_factor", "active", "seen_pct", "seen_src")),
)
_MEAS_FILES = (
    ("nodes", "measurements_nodes.csv",
     ("bus", "name", "vm_pu", "v_ll_kv", "p_mw", "q_mvar", "s_mva", "i_ka")),
    ("trafos", "measurements_trafos.csv",
     ("trafo", "name", "hv_bus", "lv_bus", "loading_percent", "p_hv_mw",
      "q_hv_mvar", "i_hv_ka", "pl_mw")),
)
_EST_FILES = (
    ("buses", "estimated_buses.csv",
     ("index", "vm_pu", "va_degree", "p_mw", "q_mvar")),
    ("lines", "estimated_lines.csv",
     ("index", "loading_percent", "i_ka", "p_from_mw", "pl_mw")),
    ("trafos", "estimated_trafos.csv",
     ("index", "loading_percent", "p_hv_mw", "q_hv_mvar", "i_hv_ka", "pl_mw")),
)
_STEP_COLS = ("day", "step", "time_of_day", "timestamp")
_FLUSH_EVERY = 50   # steps between fsync-less flushes (bounded loss on crash)


class Recorder:
    """Records the published step stream of ONE configuration to disk."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._active = False
        self._id: str | None = None
        self._dir: Path | None = None
        self._meta: dict[str, Any] = {}
        self._started: float = 0.0
        self._steps = 0
        self._last_key: tuple | None = None
        self._last_est_key: tuple | None = None
        self._files: dict[str, tuple[TextIO, Any]] = {}   # name -> (fh, writer)

    # -- lifecycle --------------------------------------------------------- #
    def start(self, meta: dict[str, Any], name: str | None = None) -> dict:
        if self._active:
            raise RuntimeError("a recording is already active")
        rid = time.strftime("%Y%m%d-%H%M%S")
        if name:
            slug = re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-")[:40]
            if slug:
                rid = f"{rid}_{slug}"
        d = self.root / rid
        n = 1
        while d.exists():                       # same-second restart
            n += 1
            d = self.root / f"{rid}-{n}"
        d.mkdir(parents=True)
        self._id = d.name
        self._dir = d
        self._meta = meta
        self._started = time.time()
        self._steps = 0
        self._last_key = None
        self._last_est_key = None
        self._files = {}
        self._q = queue.Queue()
        self._active = True
        self._thread = threading.Thread(target=self._run, name="netzsim-recorder",
                                        daemon=True)
        self._thread.start()
        log.info("Recording started: %s", self._id)
        return self.status()

    def record(self, payload: dict[str, Any]) -> None:
        """Enqueue one published step (called on the event loop — never blocks)."""
        if self._active:
            self._q.put(payload)

    def stop(self) -> dict | None:
        """Finish the active recording: drain the queue, close the files and
        write metadata.json. Returns the final status (None if idle)."""
        if not self._active:
            return None
        self._active = False
        self._q.put(None)                       # sentinel
        if self._thread is not None:
            self._thread.join(timeout=30)
        final = {
            **self._meta,
            "id": self._id,
            "started": _iso(self._started),
            "ended": _iso(time.time()),
            "steps_recorded": self._steps,
            "files": sorted(p.name for p in self._dir.iterdir()),
        }
        (self._dir / "metadata.json").write_text(
            json.dumps(final, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info("Recording stopped: %s (%d steps)", self._id, self._steps)
        out = self.status()
        self._id = None
        self._dir = None
        return out

    def status(self) -> dict:
        return {
            "active": self._active,
            "id": self._id,
            "steps": self._steps,
            "started": _iso(self._started) if self._id else None,
            "bytes": _dir_bytes(self._dir) if self._dir else 0,
        }

    # -- stored recordings --------------------------------------------------- #
    def list(self) -> list[dict]:
        out = []
        if not self.root.is_dir():
            return out
        for d in sorted(self.root.iterdir()):
            mf = d / "metadata.json"
            if not d.is_dir() or not mf.is_file():
                continue
            try:
                meta = json.loads(mf.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 — half-written/corrupt: still list it
                meta = {"id": d.name}
            out.append({"id": d.name,
                        "grid": (meta.get("grid") or {}).get("name"),
                        "started": meta.get("started"),
                        "ended": meta.get("ended"),
                        "steps": meta.get("steps_recorded"),
                        "bytes": _dir_bytes(d)})
        return out

    def dir_of(self, rid: str) -> Path:
        """Validated path of a stored recording (guards path traversal)."""
        if not _ID_RE.match(rid):
            raise KeyError(rid)
        d = self.root / rid
        if not d.is_dir() or not (d / "metadata.json").is_file():
            raise KeyError(rid)
        return d

    def pack(self, rid: str) -> Path:
        """ZIP a finished recording (cached — recordings are immutable)."""
        d = self.dir_of(rid)
        zp = self.root / f"{rid}.zip"
        if zp.is_file() and zp.stat().st_mtime >= (d / "metadata.json").stat().st_mtime:
            return zp
        tmp = zp.with_suffix(".zip.tmp")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for p in sorted(d.iterdir()):
                z.write(p, arcname=f"{rid}/{p.name}")
        tmp.replace(zp)
        return zp

    def delete(self, rid: str) -> None:
        d = self.dir_of(rid)
        shutil.rmtree(d)
        (self.root / f"{rid}.zip").unlink(missing_ok=True)

    # -- writer thread ------------------------------------------------------- #
    def _run(self) -> None:
        try:
            while True:
                item = self._q.get()
                if item is None:
                    break
                try:
                    self._write(item)
                except Exception:  # noqa: BLE001 — a bad step must not kill the run
                    log.exception("Recorder failed to write a step; skipping it.")
        finally:
            for fh, _ in self._files.values():
                try:
                    fh.close()
                except Exception:  # noqa: BLE001
                    pass
            self._files = {}

    def _w(self, name: str, header: tuple[str, ...]):
        got = self._files.get(name)
        if got is None:
            fh = (self._dir / name).open("w", newline="", encoding="utf-8")
            w = csv.writer(fh)
            w.writerow(header)
            got = self._files[name] = (fh, w)
        return got[1]

    def _write(self, p: dict[str, Any]) -> None:
        key = (p.get("day"), p.get("step"))
        if key == self._last_key:               # double publish around a pause
            return
        self._last_key = key
        stamp = [p.get("day"), p.get("step"), p.get("time_of_day"), p.get("timestamp")]

        for pkey, fname, cols in _ELEMENT_FILES:
            rows = p.get(pkey)
            if rows:
                w = self._w(fname, _STEP_COLS + cols)
                for r in rows:
                    w.writerow(stamp + [_c(r.get(c)) for c in cols])

        for dkey, extra in (("summary", ("converged", "solve_ms")),
                            ("observed_summary", ())):
            d = p.get(dkey)
            if d:
                cols = tuple(extra) + tuple(sorted(d))
                w = self._w(f"{dkey}.csv", _STEP_COLS + cols)
                w.writerow(stamp + [_c(p.get(c) if c in extra else d.get(c))
                                    for c in cols])

        meas = p.get("measurements") or {}
        for mkey, fname, cols in _MEAS_FILES:
            rows = meas.get(mkey)
            if rows:
                w = self._w(fname, _STEP_COLS + cols)
                for r in rows:
                    w.writerow(stamp + [_c(r.get(c)) for c in cols])

        est = p.get("estimated")
        if isinstance(est, dict) and est.get("buses"):
            ekey = (est.get("day"), est.get("step"))
            if ekey != self._last_est_key:       # only NEW estimates (raster)
                self._last_est_key = ekey
                estamp = [est.get("day"), est.get("step"), None, p.get("timestamp")]
                for ekey_, fname, cols in _EST_FILES:
                    rows = est.get(ekey_)
                    if rows:
                        w = self._w(fname, _STEP_COLS + cols)
                        for r in rows:
                            w.writerow(estamp + [_c(r.get(c)) for c in cols])

        self._steps += 1
        if self._steps % _FLUSH_EVERY == 0:
            for fh, _ in self._files.values():
                fh.flush()


def _c(v):
    """CSV cell: None → empty, bools → 0/1 (spreadsheet-friendly)."""
    if v is None:
        return ""
    if isinstance(v, bool):
        return int(v)
    return v


def _iso(t: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t))


def _dir_bytes(d: Path | None) -> int:
    if d is None or not d.is_dir():
        return 0
    return sum(p.stat().st_size for p in d.iterdir() if p.is_file())
