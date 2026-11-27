"""Collect the licence texts of the UI's production dependencies.

The built bundle (ui/dist) holds React, Leaflet, i18next ... in minified form,
and the minifier drops their licence headers. MIT and BSD ask for the notice
to accompany every copy, so the Windows installer ships the texts in
``licenses\\ui\\`` (called by build.ps1).

    python collect_ui_licenses.py <ui-folder> <output-folder>

The production tree comes from ``ui/package-lock.json`` (lockfile v2/v3,
``packages`` map): every entry under ``node_modules/`` that is not marked
``dev``. That is a superset of what ends up in the bundle - harmless, and it
follows the dependencies without a list to maintain here.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

NOTICE_NAMES = ("LICENSE", "LICENCE", "COPYING", "NOTICE")


def _declared(name: str, folder: Path) -> str:
    """Notice for a package that ships no licence file: its own metadata."""
    try:
        meta = json.loads((folder / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    author = meta.get("author")
    if isinstance(author, dict):
        author = author.get("name")
    repo = meta.get("repository")
    if isinstance(repo, dict):
        repo = repo.get("url")
    lines = [
        f"{name} {meta.get('version', '')}".rstrip(),
        "",
        "This package ships no licence file. Its package.json declares:",
        f"  license:    {meta.get('license', 'not stated')}",
        f"  author:     {author or 'not stated'}",
        f"  repository: {repo or meta.get('homepage') or 'not stated'}",
    ]
    return "\n".join(lines) + "\n"


def collect(ui: Path, out: Path) -> tuple[int, list[str]]:
    lock = json.loads((ui / "package-lock.json").read_text(encoding="utf-8"))
    packages = lock.get("packages") or {}
    out.mkdir(parents=True, exist_ok=True)
    done, without = 0, []
    for key, meta in sorted(packages.items()):
        if not key.startswith("node_modules/") or meta.get("dev"):
            continue
        folder = ui / key
        if not folder.is_dir():            # optional / other platform: not installed
            continue
        name = key.rsplit("node_modules/", 1)[-1]          # nested a/node_modules/b -> b
        texts = sorted(p for p in folder.iterdir()
                       if p.is_file() and p.name.upper().startswith(NOTICE_NAMES))
        stem = f"{name.lstrip('@').replace('/', '-')}-{meta.get('version', 'unknown')}"
        if not texts:
            # some packages only DECLARE their licence: record what they say
            without.append(name)
            (out / f"{stem}-NOTICE.txt").write_text(_declared(name, folder), encoding="utf-8")
            continue
        for p in texts:
            suffix = "" if p.suffix.lower() in (".txt", ".md") else ".txt"
            shutil.copyfile(p, out / f"{stem}-{p.name}{suffix}")
        done += 1
    return done, without


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    done, without = collect(Path(sys.argv[1]), Path(sys.argv[2]))
    if not done:
        print("no production packages found in package-lock.json / node_modules",
              file=sys.stderr)
        return 1
    note = (f"; no licence file, declared licence recorded instead: {', '.join(without)}"
            if without else "")
    print(f"licences of {done} UI packages collected{note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
