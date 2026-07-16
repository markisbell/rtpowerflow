"""Benchmark environment check (docs/BENCHMARKS.md §3.3).

Verifies the complete validation stack and prints the exact versions that
go into the results manifest: Python, pandapower, BOTH OpenDSS engines
(with their engine version strings), Octave and MATPOWER. Exits non-zero
with an actionable message on the first missing piece — an external person
reproducing the benchmark should never see a bare stack trace.

Run inside the benchmark venv:
    .venv-bench\\Scripts\\python benchmarks\\check_env.py
"""
from __future__ import annotations

import json
import os
import shutil
import sys

FAILURES: list[str] = []
INFO: dict[str, str] = {}


def check(name: str, fn, fix: str) -> None:
    try:
        INFO[name] = str(fn())
        print(f"  ok  {name}: {INFO[name]}")
    except Exception as exc:  # noqa: BLE001 — report, don't crash
        FAILURES.append(f"{name}: {exc}\n      fix: {fix}")
        print(f"FAIL  {name}: {exc}")


def _python() -> str:
    v = sys.version_info
    if not (3, 11) <= (v.major, v.minor) <= (3, 12):
        raise RuntimeError(f"Python {v.major}.{v.minor} — the dss-extensions "
                           f"wheels are only tested up to 3.12 (plan §3.1)")
    return sys.version.split()[0]


def _pandapower() -> str:
    import pandapower
    return pandapower.__version__


def _opendssdirect() -> str:
    from opendssdirect import dss
    dss.Text.Command("Clear")
    return f"{dss.__version__} (engine: {dss.Basic.Version().split(';')[0].strip()})"


def _py_dss_interface() -> str:
    # NOTE: d.text(...) hangs in non-interactive shells (observed during
    # Phase 0 on this machine) — use the property API here; the harness
    # (Phase 1) must clarify how to drive .dss commands with this package.
    import py_dss_interface
    d = py_dss_interface.DSS()
    ver = d.dssinterface.version
    return f"{py_dss_interface.__version__} (engine: {str(ver).strip()[:80]})"


def _octave() -> str:
    exe = os.environ.get("OCTAVE_EXECUTABLE") or shutil.which("octave-cli") \
        or shutil.which("octave")
    if not exe or not os.path.exists(exe):
        raise RuntimeError("Octave not found (OCTAVE_EXECUTABLE unset, "
                           "octave-cli not on PATH)")
    from oct2py import Oct2Py
    with Oct2Py() as oc:
        return f"{oc.feval('OCTAVE_VERSION')} ({exe})"


def _matpower() -> str:
    from matpower import start_instance
    m = start_instance()
    try:
        ver = m.mpver("all")
        name = ver["Name"] if isinstance(ver, dict) else "MATPOWER"
        vers = ver["Version"] if isinstance(ver, dict) else str(ver)
        return f"{name} {vers}"
    finally:
        m.exit()


def main() -> int:
    print("benchmark environment check (docs/BENCHMARKS.md)")
    check("python", _python,
          "py -3.12 -m venv .venv-bench && pip install -r benchmarks/requirements.txt")
    check("pandapower", _pandapower, "pip install pandapower==3.4.0")
    check("OpenDSSDirect.py", _opendssdirect, "pip install OpenDSSDirect.py==0.9.4")
    check("py-dss-interface", _py_dss_interface, "pip install py-dss-interface==2.3.0")
    check("octave", _octave,
          "unzip octave-11.3.0-w64.zip and set OCTAVE_EXECUTABLE to "
          r"...\octave-11.3.0-w64\mingw64\bin\octave-cli.exe")
    check("matpower", _matpower, 'pip install "matpower==8.1.0.2.3.0" oct2py==6.0.3')

    print()
    if FAILURES:
        print(f"{len(FAILURES)} problem(s):")
        for f in FAILURES:
            print(" -", f)
        return 1
    print("environment complete — manifest header:")
    print(json.dumps(INFO, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
