"""Scenario store: the fully configured live setup as a JSON file.

A scenario is a *recipe*, not a snapshot: grid id + the (seeded, deterministic)
load-generation policy + the runtime mutations layered on top (bus-addressed
DER ops, batteries, meter placement) + the engine clock. Replaying the recipe
reproduces the setup exactly, and the files are small, human-readable JSON —
deliberately hand-editable for education and demo preparation.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

SCENARIO_VERSION = 1


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "scenario"


class ScenarioStore:
    """Reads/writes scenario JSON files in a directory (created on demand)."""

    def __init__(self, directory: str | Path):
        self.dir = Path(directory)

    def list(self) -> list[dict[str, Any]]:
        if not self.dir.exists():
            return []
        out: list[dict[str, Any]] = []
        for f in sorted(self.dir.glob("*.json")):
            try:
                doc = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            out.append({"id": f.stem, "name": doc.get("name", f.stem),
                        "description": doc.get("description", ""),
                        "grid_id": doc.get("grid_id"),
                        "created": doc.get("created")})
        return out

    def read(self, sid: str) -> dict[str, Any] | None:
        f = self.dir / f"{_slug(sid)}.json"
        if not f.exists():
            return None
        return json.loads(f.read_text(encoding="utf-8"))

    def write(self, doc: dict[str, Any]) -> str:
        """Persist a scenario; the name's slug is the id (same name overwrites,
        so a scenario can be iterated on)."""
        sid = _slug(str(doc.get("name", "scenario")))
        doc = {"version": SCENARIO_VERSION,
               "created": time.strftime("%Y-%m-%dT%H:%M:%S"), **doc}
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / f"{sid}.json").write_text(
            json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
        return sid

    def delete(self, sid: str) -> bool:
        f = self.dir / f"{_slug(sid)}.json"
        if not f.exists():
            return False
        f.unlink()
        return True
