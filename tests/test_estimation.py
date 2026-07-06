"""State estimation: the operator's calculated view from meters + grid model.

The estimator (estimator.py) runs WLS on the placed measurements, structural
zero-injection knowledge and profile-based pseudo-loads. It must reproduce the
truth closely under full metering, stay sane under sparse metering, appear
only while meters are placed, and survive strict mode without its truth-based
error metric.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from netzsim.data_loader import input_data_from_dicts, load_inputs
from netzsim.osm_lv_import import convert_osm_lv
from netzsim.simulator import Simulator
from netzsim.state import StateStore

ROOT = Path(__file__).resolve().parents[1]
LV_GRID = ROOT / "data" / "lv_osm" / "lv_rural_3150_300266.json"


@pytest.fixture(scope="module")
def lv_sim() -> Simulator:
    g = convert_osm_lv(LV_GRID, steps=96)
    data = input_data_from_dicts(g.grid_structure, g.lines, g.load,
                                 g.generation, g.substation)
    return Simulator(data)


def test_no_meters_no_estimate():
    sim = Simulator(load_inputs(ROOT / "data"))
    res = sim.run_step(50)
    assert res.converged and res.estimated is None


@pytest.mark.skipif(not LV_GRID.exists(), reason="no committed LV grid")
def test_full_metering_reproduces_truth(lv_sim):
    lv_sim.meters.apply_preset("all_nodes", lv_sim.net)
    res = lv_sim.run_step(76)                      # evening peak
    assert res.converged and res.estimated is not None
    err = res.estimated["error"]
    assert err["max_dv_pu"] < 0.002, f"max dV = {err['max_dv_pu']}"
    assert len(res.estimated["buses"]) == len(res.buses)
    assert len(res.estimated["lines"]) == len(res.lines)


@pytest.mark.skipif(not LV_GRID.exists(), reason="no committed LV grid")
def test_sparse_metering_stays_sane(lv_sim):
    lv_sim.meters.clear()
    lv_sim.meters.apply_preset("substation_trafos", lv_sim.net)  # trafo + busbar only
    res = lv_sim.run_step(76)
    assert res.converged and res.estimated is not None
    err = res.estimated["error"]
    # sparse meters + pseudo-loads: still a usable estimate (well under 1 % V)
    assert err["max_dv_pu"] < 0.01, f"max dV = {err['max_dv_pu']}"
    # the estimate covers the WHOLE grid, not just metered elements
    assert all(b["vm_pu"] is not None for b in res.estimated["buses"])


@pytest.mark.skipif(not LV_GRID.exists(), reason="no committed LV grid")
def test_standard_metering_p_only_degrades_estimate(lv_sim):
    """Standard metering (Lastgang): meters deliver only the 15-min mean active
    power — no voltage, no reactive power. The estimate must still work (from
    P readings + pseudo knowledge), just with degraded quality."""
    lv_sim.meters.clear()
    lv_sim.meters.apply_preset("all_nodes", lv_sim.net)
    lv_sim.meters.set_mode("standard")
    try:
        res = lv_sim.run_step(76)
        assert res.converged
        readings = [n for n in res.measurements["nodes"] if n["p_mw"] is not None]
        assert readings, "standard meters delivered no P at all"
        assert all(n["vm_pu"] is None and n["q_mvar"] is None
                   for n in res.measurements["nodes"])
        assert res.estimated is not None
        err = res.estimated["error"]
        assert err["max_dv_pu"] < 0.05, f"max dV = {err['max_dv_pu']}"

        # full mode at the same placement is strictly more informative
        lv_sim.meters.set_mode("full")
        res_full = lv_sim.run_step(76)
        assert res_full.estimated["error"]["max_dv_pu"] <= err["max_dv_pu"] + 1e-9
    finally:
        lv_sim.meters.set_mode("full")


@pytest.mark.skipif(not LV_GRID.exists(), reason="no committed LV grid")
def test_daily_curves_include_estimate(lv_sim):
    """The daily sweep overlays the operator's estimate: under full metering it
    matches the true current; clearing the meters drops the estimated curve
    (the sweep cache is keyed on the placement)."""
    lv_sim.meters.clear()
    lv_sim.meters.apply_preset("all_nodes", lv_sim.net)
    lp = lv_sim.line_profiles(0)
    assert lp["est_current"] is not None
    pairs = [(a, b) for a, b in zip(lp["current"], lp["est_current"])
             if a is not None and b is not None]
    assert len(pairs) > 10, "estimate present at too few samples"
    assert max(abs(a - b) for a, b in pairs) < 1e-4   # full metering ≈ truth

    lv_sim.meters.clear()
    assert lv_sim.line_profiles(0)["est_current"] is None


@pytest.mark.skipif(not LV_GRID.exists(), reason="no committed LV grid")
def test_strict_mode_keeps_estimate_strips_error(lv_sim):
    lv_sim.meters.clear()
    lv_sim.meters.apply_preset("substation_trafos", lv_sim.net)
    res = lv_sim.run_step(30)
    store = StateStore(history_size=4, expose_ground_truth=False)
    wire = store._project(__import__("dataclasses").asdict(res))
    assert "buses" not in wire and "summary" not in wire   # truth stripped
    assert wire["estimated"] is not None                   # estimate survives
    assert "error" not in wire["estimated"]                # ...without the metric
    assert wire["estimated"]["buses"], "estimated buses missing on the wire"


# --------------------------------------------------------------------------- #
# Estimation policy (config tab): what the operator's estimation may use
# --------------------------------------------------------------------------- #
from netzsim.estimator import EstConfig, Estimator  # noqa: E402


def _fresh_lv_sim() -> Simulator:
    g = convert_osm_lv(LV_GRID, steps=96)
    data = input_data_from_dicts(g.grid_structure, g.lines, g.load,
                                 g.generation, g.substation)
    return Simulator(data)


@pytest.mark.skipif(not LV_GRID.exists(), reason="no committed LV grid")
def test_slp_basis_replaces_household_means():
    """SLP basis: every household enters with the uniform standard assumption
    (annual kWh -> mean MW), not with its true profile mean."""
    sim = _fresh_lv_sim()
    all_rows = set(range(len(sim.prof.load_idx)))
    est = Estimator(sim.net, sim.prof, sim._loads_at, sim._sgens_at,
                    ev_rows=set(), household_rows=all_rows,
                    config=EstConfig(load_basis="slp", slp_annual_kwh=4000.0))
    slp_mw = 4000.0 / 8760.0 / 1000.0
    for b, rows in sim._loads_at.items():
        assert est._p_mean[b] == pytest.approx(slp_mw * len(rows), rel=1e-9)
    # the idealized basis differs (real profiles are not all 4000 kWh)
    est.set_config(EstConfig(load_basis="profile"))
    diffs = [abs(est._p_mean[b] - slp_mw * len(rows))
             for b, rows in sim._loads_at.items()]
    assert max(diffs) > 1e-6


@pytest.mark.skipif(not LV_GRID.exists(), reason="no committed LV grid")
def test_slp_basis_scales_with_metering_points():
    """Multi-family buildings: the DSO knows the meter count per building, so
    the SLP pseudo is n x the per-household assumption."""
    sim = _fresh_lv_sim()
    all_rows = set(range(len(sim.prof.load_idx)))
    bus, rows = next((b, r) for b, r in sim._loads_at.items() if len(r) == 1)
    est = Estimator(sim.net, sim.prof, sim._loads_at, sim._sgens_at,
                    ev_rows=set(), household_rows=all_rows,
                    household_counts={rows[0]: 5},
                    config=EstConfig(load_basis="slp", slp_annual_kwh=4000.0))
    slp_mw = 4000.0 / 8760.0 / 1000.0
    assert est._p_mean[bus] == pytest.approx(5 * slp_mw, rel=1e-9)
    # profile basis is untouched by the count (the true sum already carries it)
    est.set_config(EstConfig(load_basis="profile"))
    assert est._p_mean[bus] == pytest.approx(
        float(sim.prof.load_p[rows[0]].mean()), rel=1e-9)


@pytest.mark.skipif(not LV_GRID.exists(), reason="no committed LV grid")
def test_ev_pseudo_excluded_by_default():
    """EV charging is not part of the pseudo value unless allowed — but it
    always widens the uncertainty (its peak stays in the std base)."""
    sim = _fresh_lv_sim()
    bus = int(next(iter(sim._loads_at)))
    sim.add_ev(bus, kw=11.0)
    sim.meters.apply_preset("substation_trafos", sim.net)
    sim.run_step(76)                                # builds estimator, defaults
    est = sim._estimator
    p_off = est._p_mean[bus]
    peak_off = est._p_peak[bus]
    sim.set_est_config(EstConfig(ev_pseudo=True))
    sim.run_step(76)
    est2 = sim._estimator
    assert est2 is not est                          # config change rebuilds
    assert est2._p_mean[bus] > p_off                # EV mean now included
    assert est2._p_peak[bus] == pytest.approx(peak_off)  # width unchanged


@pytest.mark.skipif(not LV_GRID.exists(), reason="no committed LV grid")
def test_pv_pseudo_flag_controls_generation_value():
    """Default (DSO practice): the PV day-mean is NOT subtracted from the
    pseudo value; with pv_pseudo=True it is."""
    sim = _fresh_lv_sim()
    bus = int(next(iter(sim._loads_at)))
    sim.add_pv(bus, kwp=10.0)
    sim.meters.apply_preset("substation_trafos", sim.net)
    sim.run_step(48)
    m = sim._estimator._net.measurement
    val_off = float(m[(m.element == bus)
                      & (m.measurement_type == "p")].iloc[0]["value"])
    assert val_off == pytest.approx(sim._estimator._p_mean[bus], abs=1e-9)
    sim.set_est_config(EstConfig(pv_pseudo=True))
    sim.run_step(48)
    m2 = sim._estimator._net.measurement
    val_on = float(m2[(m2.element == bus)
                      & (m2.measurement_type == "p")].iloc[0]["value"])
    assert val_on < val_off                         # day-mean gen subtracted


@pytest.mark.skipif(not LV_GRID.exists(), reason="no committed LV grid")
def test_config_change_resweeps_daily_curves():
    """The day graphs must re-run under a new estimation policy — the sweep
    cache is keyed on the config, and the sweep's estimator honors it."""
    sim = _fresh_lv_sim()
    sim.meters.apply_preset("substation_trafos", sim.net)
    d1 = sim.daily_curves()
    assert sim.daily_curves() is d1                 # cache hit under same policy
    sim.set_est_config(EstConfig(load_basis="slp", pseudo_std_pct=200))
    d2 = sim.daily_curves()
    assert d2 is not d1                             # policy change -> re-sweep
    # the re-sweep really used the new policy: some estimated voltage differs
    b = next(iter(d1["est_bus_vm"]))
    pairs = [(a, c) for a, c in zip(d1["est_bus_vm"][b], d2["est_bus_vm"][b])
             if a is not None and c is not None]
    assert pairs and any(abs(a - c) > 1e-7 for a, c in pairs)


@pytest.mark.skipif(not LV_GRID.exists(), reason="no committed LV grid")
def test_estimation_honesty_pv_rise_unknowable():
    """Truth-leak tripwire (customer feedback): on a rural feeder with long
    lines and strong midday PV, an operator who knows only the slack setpoint,
    ~5 % of the nodes and NO PV pseudo-knowledge CANNOT see the voltage rise
    at the feeder end. If this test ever fails on the 'blind' assertion, truth
    is leaking into the estimator."""
    def build() -> Simulator:
        sim = _fresh_lv_sim()                     # rural: long feeders
        for b in list(sim._loads_at)[::2]:        # every 2nd customer: 15 kWp
            sim.add_pv(b, kwp=15.0)
        return sim

    def midday(sim) -> tuple[dict, dict, int]:
        res = sim.run_step(48)                    # 12:00, PV peak
        assert res.converged and res.estimated is not None
        tru = {b["index"]: b["vm_pu"] for b in res.buses}
        est = {b["index"]: b["vm_pu"] for b in res.estimated["buses"]}
        slack = int(sim.net.ext_grid.bus.iloc[0])
        return tru, est, slack

    n5 = max(1, round(0.05 * len(build().net.bus)))   # ~5 % of the buses
    meters = list(build()._loads_at)[:n5]             # near the feeder head

    # A) blind: 5 % meters, PV pseudo OFF (DSO default)
    sim = build()
    sim.meters.node_buses.update(meters)
    tru, est, slack = midday(sim)
    rise_bus = max(tru, key=lambda i: tru[i] - tru[slack])
    true_rise = tru[rise_bus] - tru[slack]
    assert true_rise > 0.02, "scenario broken: no substantial PV rise"  # > ~4.6 V
    blind_err = abs(est[rise_bus] - tru[rise_bus])
    # the estimate MUST miss most of the rise it cannot know about
    assert blind_err > 0.6 * true_rise, (
        f"estimate suspiciously accurate ({blind_err*1000:.1f} of "
        f"{true_rise*1000:.1f} mpu) — truth leaking into the estimator?")

    # B) PV day-mean pseudo allowed: better, but still short of the midday peak
    sim2 = build()
    sim2.meters.node_buses.update(meters)
    sim2.set_est_config(EstConfig(pv_pseudo=True))
    _, est2, _ = midday(sim2)
    assert abs(est2[rise_bus] - tru[rise_bus]) < blind_err

    # C) full metering: the rise is measured, the estimate is essentially exact
    sim3 = build()
    sim3.meters.apply_preset("all_nodes", sim3.net)
    tru3, est3, _ = midday(sim3)
    assert abs(est3[rise_bus] - tru3[rise_bus]) < 0.002


@pytest.mark.skipif(not LV_GRID.exists(), reason="no committed LV grid")
def test_estimate_sweep_resolution_is_pinned():
    """The estimated day curve keeps ONE consistent resolution per grid: the
    decimation tier is pinned after the first decision and survives policy
    changes — no flipping between 15-min and hourly sampling."""
    sim = _fresh_lv_sim()
    sim.meters.apply_preset("substation_trafos", sim.net)
    sim._est_sweep_every = 4                        # pretend: a slow (big) net
    d = sim.daily_curves()
    b = next(iter(d["est_bus_vm"]))
    idx = [i for i, v in enumerate(d["est_bus_vm"][b]) if v is not None]
    assert idx, "no estimated samples at all"
    assert all(i % 4 == 0 for i in idx)             # hourly raster, aligned
    sim.set_est_config(EstConfig(load_basis="slp"))
    assert sim._est_sweep_every == 4                # tier survives the policy swap
    d2 = sim.daily_curves()
    idx2 = [i for i, v in enumerate(d2["est_bus_vm"][b]) if v is not None]
    assert idx2 == idx                              # same raster after re-sweep


@pytest.mark.skipif(not LV_GRID.exists(), reason="no committed LV grid")
def test_zero_injection_can_be_disabled():
    sim = _fresh_lv_sim()
    sim.meters.apply_preset("substation_trafos", sim.net)
    junction = next(int(b) for b in sim.net.bus.index
                    if int(b) not in sim._loads_at
                    and int(b) not in sim._sgens_at
                    and int(b) not in {int(x) for x in sim.net.ext_grid.bus}
                    and int(b) not in set(sim.meters.node_buses))
    sim.run_step(30)
    m = sim._estimator._net.measurement
    sel = (m.element == junction) & (m.measurement_type == "p") & (m.element_type == "bus")
    assert len(m[sel]) == 1
    sim.set_est_config(EstConfig(zero_injection=False))
    sim.run_step(30)
    m2 = sim._estimator._net.measurement
    sel2 = (m2.element == junction) & (m2.measurement_type == "p") & (m2.element_type == "bus")
    assert len(m2[sel2]) == 0
