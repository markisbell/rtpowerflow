"""Read the cached LPG archetype library (index + per-archetype variant files)."""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path


@dataclass
class Archetype:
    id: str
    name: str
    label: str
    annual_kwh: float
    mean_kw: float
    peak_kw: float
    n_variants: int
    variants_kw: list[list[float]]  # each list is one representative day (kW)

    def meta(self) -> dict:
        return {
            "id": self.id, "name": self.name, "label": self.label,
            "annual_kwh": self.annual_kwh, "mean_kw": self.mean_kw,
            "peak_kw": self.peak_kw, "n_variants": self.n_variants,
        }


class LoadLibrary:
    """Lazily loads archetype variant arrays from a directory of JSON files."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self._archetypes: dict[str, Archetype] = {}

    @cached_property
    def _index(self) -> dict:
        path = self.directory / "index.json"
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    @property
    def available(self) -> bool:
        return bool(self._index.get("archetypes"))

    @property
    def steps(self) -> int:
        return int(self._index.get("steps", 1440))

    def ids(self) -> list[str]:
        return [a["id"] for a in self._index.get("archetypes", [])]

    def list(self) -> list[dict]:
        """Lightweight metadata (no variant arrays)."""
        return [dict(a) for a in self._index.get("archetypes", [])]

    def has(self, archetype_id: str) -> bool:
        return archetype_id in self.ids()

    def get(self, archetype_id: str) -> Archetype:
        if archetype_id not in self._archetypes:
            entry = next(
                (a for a in self._index.get("archetypes", []) if a["id"] == archetype_id),
                None,
            )
            if entry is None:
                raise KeyError(archetype_id)
            doc = json.loads(
                (self.directory / entry["file"]).read_text(encoding="utf-8")
            )
            self._archetypes[archetype_id] = Archetype(
                id=doc["id"], name=doc["name"], label=doc["label"],
                annual_kwh=doc["annual_kwh"], mean_kw=doc["mean_kw"],
                peak_kw=doc["peak_kw"], n_variants=doc["n_variants"],
                variants_kw=doc["variants_kw"],
            )
        return self._archetypes[archetype_id]
