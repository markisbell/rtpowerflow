"""Result figures for the public report (docs/BENCHMARKS.md §8.2).

Renders the daily overlays and error curves from the arrays under
``benchmarks/out/`` into ``docs/benchmarks/img/`` (150 dpi PNG, light
background, colorblind-safe blue/orange). Selection rule: the electrically
most sensitive spots — the buses/lines the teaching scenarios talk about.

    .venv-bench\\Scripts\\python benchmarks\\plots.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt          # noqa: E402
import numpy as np                       # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "benchmarks" / "out"
IMG = ROOT / "docs" / "benchmarks" / "img"

BLUE, ORANGE, GREY = "#0072B2", "#E69F00", "#7f7f7f"
HOURS = np.arange(1440) / 60.0


def _style(ax, ylabel, title):
    ax.set_xlim(0, 24)
    ax.set_xticks(range(0, 25, 3))
    ax.grid(alpha=0.3)
    ax.set_ylabel(ylabel)
    ax.set_title(title, fontsize=10)


def overlay(fixture: str, kind: str, idx: int, label: str, fname: str,
            scale: float = 1.0, unit: str = "V") -> None:
    ref = np.load(OUT / f"{fixture}_netzsim_aligned.npz")
    dss = np.load(OUT / f"{fixture}_opendss_altdss_aligned.npz")
    if kind == "bus":
        a = ref["vm_pu"][idx] * ref["vn_kv"][idx] * 1000.0 * scale
        b = dss["vll_v"][idx] * scale
    else:
        a = ref["i_ka"][idx] * 1000.0
        b = dss["i_a"][idx]

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(8, 4.6), sharex=True, dpi=150,
        gridspec_kw={"height_ratios": [3, 1], "hspace": 0.12})
    ax1.plot(HOURS, a, color=BLUE, lw=1.1, label="netzsim (pandapower NR)")
    ax1.plot(HOURS[::45], b[::45], "o", color=ORANGE, ms=3.5, mew=0,
             label="OpenDSS (daily mode)")
    _style(ax1, unit, label)
    ax1.legend(loc="best", fontsize=8, framealpha=0.9)
    d = b - a
    dunit = "mV" if unit.startswith("V") else "mA"
    ax2.plot(HOURS, d * 1000.0, color=GREY, lw=0.8)
    _style(ax2, f"Δ [{dunit}]", "")
    ax2.set_xlabel("time of day [h]")
    fig.savefig(IMG / fname, bbox_inches="tight")
    plt.close(fig)
    print(f"  {fname}: max|delta| = {np.abs(d).max()*1000:.3f} {dunit}")


def error_over_day() -> None:
    fig, ax = plt.subplots(figsize=(8, 3.2), dpi=150)
    for fixture, color, label in (
            ("g1_lv_rural", BLUE, "G1 rural LV (30 buses)"),
            ("g2_lv_suburban", ORANGE, "G2 suburban LV (62 buses)"),
            ("g3_mv_district", GREY, "G3 MV district (475 buses)")):
        ref = np.load(OUT / f"{fixture}_netzsim_aligned.npz")
        dss = np.load(OUT / f"{fixture}_opendss_altdss_aligned.npz")
        ref_v = ref["vm_pu"] * ref["vn_kv"][:, None] * 1000.0
        dv_pu = np.abs(dss["vll_v"] - ref_v) / (ref["vn_kv"][:, None] * 1000.0)
        ax.semilogy(HOURS, np.nanmax(dv_pu, axis=0), color=color, lw=0.9,
                    label=label)
    ax.axhline(1e-5, color="red", lw=0.8, ls="--", label="pass gate 1e-5 pu")
    _style(ax, "max |ΔV| over all buses [pu]",
           "netzsim vs OpenDSS: voltage error over the day (trafo-aligned)")
    ax.set_xlabel("time of day [h]")
    ax.legend(loc="upper right", fontsize=8, framealpha=0.9)
    fig.savefig(IMG / "error_over_day.png", bbox_inches="tight")
    plt.close(fig)
    print("  error_over_day.png")


def main() -> int:
    IMG.mkdir(parents=True, exist_ok=True)

    # G1: the farm bus (highest noon voltage = the scenario-1 story) and
    # the head line (highest mean current)
    g1 = np.load(OUT / "g1_lv_rural_netzsim_aligned.npz")
    farm_bus = int(np.argmax(g1["vm_pu"].max(axis=1)))
    g1_head = int(np.argmax(np.nanmean(g1["i_ka"], axis=1)))
    overlay("g1_lv_rural", "bus", farm_bus,
            f"G1 rural LV — farm bus {farm_bus} (75-kWp PV), voltage L-L",
            "g1_farm_voltage.png")
    overlay("g1_lv_rural", "line", g1_head,
            f"G1 rural LV — feeder head line {g1_head}, current",
            "g1_head_current.png", unit="A")

    # G2: the weakest bus (EV evening dip) and the head line (the NH-fuse
    # story: the ~243 A evening plateau)
    g2 = np.load(OUT / "g2_lv_suburban_netzsim_aligned.npz")
    weak_bus = int(np.argmin(g2["vm_pu"].min(axis=1)))
    g2_head = int(np.argmax(np.nanmean(g2["i_ka"], axis=1)))
    overlay("g2_lv_suburban", "bus", weak_bus,
            f"G2 suburban LV — weakest bus {weak_bus} (EV evening), voltage L-L",
            "g2_weak_voltage.png")
    overlay("g2_lv_suburban", "line", g2_head,
            f"G2 suburban LV — feeder head line {g2_head}, current",
            "g2_head_current.png", unit="A")

    # G3: the worst MV bus of the district
    g3 = np.load(OUT / "g3_mv_district_netzsim_aligned.npz")
    mv = g3["vn_kv"] > 1.0
    worst = int(np.argmin(np.where(mv[:, None], g3["vm_pu"], np.inf).min(axis=1)))
    overlay("g3_mv_district", "bus", worst,
            f"G3 MV district — lowest MV bus {worst}, voltage L-L",
            "g3_worst_voltage.png")

    error_over_day()
    return 0


if __name__ == "__main__":
    sys.exit(main())
