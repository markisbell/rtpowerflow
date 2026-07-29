"""Puppet mode (gamebridge): the external clock must be a drop-in replacement
for the internal accelerated tick.

The core guarantee (simgames ROADMAP Phase 0, Spike C): N externally clocked
steps produce the IDENTICAL result sequence as N internally clocked steps on
the same inputs — the game can own time without changing the physics.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from netzsim.config import settings

# The engine must not tick on its own during these tests.
settings.autostart = False

from netzsim.api import app  # noqa: E402  (needs the settings override first)

ROOT = Path(__file__).resolve().parents[1]

N_STEPS = 100

# Non-deterministic per-run fields (wall-clock) excluded from the comparison;
# everything physical must match exactly.
_COMPARE_KEYS = ("step", "day", "time_of_day", "converged", "buses", "lines",
                 "trafos", "ext_grids", "summary")


@pytest.fixture(scope="session")
def data_dir(tmp_path_factory):
    sys.path.insert(0, str(ROOT / "scripts"))
    import generate_sample_data as g  # type: ignore

    out = tmp_path_factory.mktemp("data")
    g.DATA = out
    g.main()
    return out


def _normalize(results: list[dict]) -> list[dict]:
    return [{k: r[k] for k in _COMPARE_KEYS} for r in results]


def _run_internal_clock(data_dir: Path, n: int) -> list[dict]:
    """Drive the REAL internal loop (start_loop + accelerated tick)."""
    from netzsim.data_loader import load_inputs
    from netzsim.engine import RealtimeEngine
    from netzsim.simulator import Simulator
    from netzsim.state import StateStore

    async def go() -> list[dict]:
        store = StateStore(history_size=n + 10)
        engine = RealtimeEngine(
            Simulator(load_inputs(data_dir)), store, interval_seconds=0.01
        )
        engine.start_loop()
        while len(store.history()) < n:
            await asyncio.sleep(0.01)
        await engine.stop()
        return store.history(limit=None)[:n]

    return asyncio.run(go())


def _run_external_clock(data_dir: Path, n: int) -> list[dict]:
    """Drive the SAME engine via external_step only (puppet mode)."""
    from netzsim.data_loader import load_inputs
    from netzsim.engine import RealtimeEngine
    from netzsim.simulator import Simulator
    from netzsim.state import StateStore

    async def go() -> list[dict]:
        store = StateStore(history_size=n + 10)
        engine = RealtimeEngine(
            Simulator(load_inputs(data_dir)), store, interval_seconds=0.01
        )
        for _ in range(n):
            await engine.external_step()
        return store.history(limit=None)[:n]

    return asyncio.run(go())


def test_external_clock_equivalence(data_dir):
    internal = _normalize(_run_internal_clock(data_dir, N_STEPS))
    external = _normalize(_run_external_clock(data_dir, N_STEPS))
    assert len(internal) == len(external) == N_STEPS
    for i, (a, b) in enumerate(zip(internal, external)):
        assert a == b, f"step {i}: internal and external results differ"


def test_external_step_refused_while_internal_clock_runs(data_dir):
    from netzsim.data_loader import load_inputs
    from netzsim.engine import RealtimeEngine
    from netzsim.simulator import Simulator
    from netzsim.state import StateStore

    async def go() -> None:
        engine = RealtimeEngine(
            Simulator(load_inputs(data_dir)), StateStore(), interval_seconds=0.01
        )
        engine.start_loop()
        with pytest.raises(RuntimeError):
            await engine.external_step()
        engine.pause()  # paused internal clock => external stepping is allowed
        await engine.external_step()
        await engine.stop()

    asyncio.run(go())


def test_gb_endpoints(monkeypatch):
    """API smoke: version handshake + /gb/step with an EMPTY body keeps the
    pre-contract debug behavior (advance one step, return the /state
    projection)."""
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        version = client.get("/gb/version").json()
        assert version["backend"] == "netzsim"
        assert version["contract"] == "1.1"
        assert "pandapower" in version["solver"]

        step0 = client.get("/status").json()["step"]
        result = client.post("/gb/step")
        assert result.status_code == 200
        body = result.json()
        assert body["converged"] is True
        assert body["step"] == step0
        assert client.get("/status").json()["step"] == step0 + 1
        assert client.get("/state").json() == body


# --------------------------------------------------------------------------- #
# Contract v1 (simgames docs/contract/v1.md): /gb/net/reset + /gb/net/patch +
# the contract step path over HTTP and WS. The shared golden-file suite lives
# in simgames tests/contract/; these tests pin the netzsim-side mechanics.
# --------------------------------------------------------------------------- #

STEPS = 96


def _topology() -> dict:
    """A 3-bus 0.4-kV chain with 2 zones and slack/generator/coupling devices;
    native carries one fixed background load at b1 (contract §3.1: native rows
    stay verbatim)."""
    native = {
        "grid_structure": {
            "name": "gb_test", "f_hz": 50.0,
            "buses": [{"name": f"b{i}", "vn_kv": 0.4} for i in range(3)],
        },
        "lines": {
            "lines": [
                {"name": "l01", "from_bus": 0, "to_bus": 1, "length_km": 0.3,
                 "std_type": "NAYY 4x150 SE"},
                {"name": "l12", "from_bus": 1, "to_bus": 2, "length_km": 0.3,
                 "std_type": "NAYY 4x150 SE"},
            ],
            "transformers": [],
        },
        "load": {"steps": STEPS, "loads": [
            {"name": "bg1", "bus": 1, "p_mw": [0.005] * STEPS}]},
        "generation": {"steps": STEPS, "generation": []},
        "substation": {"steps": STEPS, "substations": []},
    }
    return {
        "contract": "1.0",
        "network_kind": "power",
        "name": "gb_test",
        "steps_per_day": STEPS,
        "native": native,
        "zones": [{"id": "z0", "node": "b1"}, {"id": "z1", "node": "b2"}],
        "devices": [
            {"id": "slack", "kind": "slack", "node": "b0", "params": {"vm_pu": 1.0}},
            {"id": "gen1", "kind": "generator", "node": "b1",
             "params": {"p_max_kw": 100}},
            {"id": "cpl", "kind": "coupling_load", "node": "b2"},
        ],
    }


def _step_req(t: int, **extra) -> dict:
    return {"t": t, "dt_s": 900, **extra}


def test_gb_reset_builds_the_extended_grid():
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        r = client.post("/gb/net/reset", json=_topology())
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] is True and body["network_kind"] == "power"
        assert body["n_zones"] == 2 and body["n_devices"] == 3
        assert isinstance(body["warmup_solve_ms"], (int, float))
        assert body["warnings"] == []
        # the swap went through the engine: 96-step clock, still paused
        st = client.get("/status").json()
        assert st["steps_per_day"] == STEPS and st["running"] is False
        # native background load + 2 zone rows + 1 coupling row
        topo = client.get("/network").json()
        assert topo["n_load"] == 4
        # a reset clears the step cache
        assert client.get("/gb/result/latest").status_code == 404


def test_gb_step_contract_path():
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        assert client.post("/gb/net/reset", json=_topology()).status_code == 200

        # any first t is accepted (the game clock continues across resets)
        r = client.post("/gb/step", json=_step_req(
            4711,
            zone_demand={"z0": {"value": 50.0}, "z1": {"value": 30.0}},
            device_setpoints={"gen1": {"p_kw": 150.0}},   # above p_max_kw 100
            weather={"wind_ms": 6.2, "ghi_wm2": 410.0, "temp_c": 12.5},
        ))
        assert r.status_code == 200, r.text
        res = r.json()
        assert res["t"] == 4711 and res["status"] == "converged"
        assert res["coupling_out"] == {}
        assert set(res["zones"]) == {"z0", "z1"}
        for z in res["zones"].values():
            assert z["supplied"] == 1.0
            assert 0.9 <= z["detail"]["v_pu"] <= 1.1
        # generator clamped to its rating, reported as a 'clamped' violation
        assert res["devices"]["gen1"]["output_kw"] == pytest.approx(100.0)
        clamps = [v for v in res["violations"] if v["kind"] == "clamped"]
        assert clamps and clamps[0]["element"] == "device:gen1"
        assert clamps[0]["severity"] == "info" and clamps[0]["value"] == 150.0
        # slack balances: zones 80 + background 5 - gen 100 => ~-15 kW + losses
        assert res["devices"]["slack"]["output_kw"] == pytest.approx(-15.0, abs=3.0)
        assert res["devices"]["cpl"]["output_kw"] == pytest.approx(0.0)

        # sample-and-hold: no zone_demand in the next request => held values
        r = client.post("/gb/step", json=_step_req(
            4712, device_setpoints={"gen1": {"p_kw": 60.0}}))
        res2 = r.json()
        assert res2["status"] == "converged"
        assert res2["devices"]["gen1"]["output_kw"] == pytest.approx(60.0)
        assert res2["devices"]["slack"]["output_kw"] == pytest.approx(25.0, abs=3.0)
        assert not [v for v in res2["violations"] if v["kind"] == "clamped"]

        # idempotent re-send: byte-equal cached result, no re-solve
        again = client.post("/gb/step", json=_step_req(
            4712, device_setpoints={"gen1": {"p_kw": 60.0}}))
        assert again.status_code == 200 and again.json() == res2

        # out-of-order t: the one 4xx in the step path
        bad = client.post("/gb/step", json=_step_req(4720))
        assert bad.status_code == 409
        frame = bad.json()
        assert frame["status"] == "error" and frame["error"] == "out_of_order"
        assert frame["expected"] == [4712, 4713]

        # coupling_in routes onto the coupling_load device (draw positive)
        r = client.post("/gb/step", json=_step_req(
            4713, coupling_in={"cpl": {"p_kw": 40.0}}))
        res3 = r.json()
        assert res3["devices"]["cpl"]["output_kw"] == pytest.approx(40.0)
        # 50+30+5+40-60 = 65 kW plus ~4 kW real line losses at this loading
        assert res3["devices"]["slack"]["output_kw"] == pytest.approx(65.0, abs=6.0)

        # /gb/result/latest is the crash-recovery read
        assert client.get("/gb/result/latest").json() == res3

        # the native StepResult stream keeps working unchanged
        state = client.get("/state").json()
        assert state["converged"] is True and len(state["buses"]) == 3
        assert client.get("/status").json()["step"] == 3


def test_gb_zone_demand_is_signed_net_load():
    """Contract §4 power note: zone demand is the SIGNED net load — negative
    means the zone exports (rooftop PV backfeed through the district trafo)."""
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        assert client.post("/gb/net/reset", json=_topology()).status_code == 200
        res = client.post("/gb/step", json=_step_req(
            0, zone_demand={"z0": {"value": -50.0}, "z1": {"value": 0.0}})).json()
        assert res["status"] == "converged"
        # slack sees the export: background 5 - 50 => ~-45 kW (import-positive)
        assert res["devices"]["slack"]["output_kw"] == pytest.approx(-45.0, abs=3.0)


def test_gb_ws_step_channel():
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        assert client.post("/gb/net/reset", json=_topology()).status_code == 200
        with client.websocket_connect("/gb/ws") as ws:
            ws.send_text(json.dumps(_step_req(
                0, zone_demand={"z0": {"value": 20.0}})))
            res = ws.receive_json()
            assert res["t"] == 0 and res["status"] == "converged"
            assert res["zones"]["z0"]["supplied"] == 1.0

            # HTTP fallback must behave identically: idempotent re-send
            http = client.post("/gb/step", json=_step_req(
                0, zone_demand={"z0": {"value": 20.0}}))
            assert http.status_code == 200 and http.json() == res

            # out-of-order over WS: an error frame, not a closed socket
            ws.send_text(json.dumps(_step_req(5)))
            frame = ws.receive_json()
            assert frame["status"] == "error" and frame["error"] == "out_of_order"
            assert frame["expected"] == [0, 1]

            ws.send_text("not json")
            frame = ws.receive_json()
            assert frame["status"] == "error" and frame["error"] == "bad_request"


def test_gb_patch_device_ops():
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        assert client.post("/gb/net/reset", json=_topology()).status_code == 200

        # add a generator, dispatch it, resize it, remove it
        probe = {"id": "gen_probe", "kind": "generator", "node": "b2",
                 "params": {"p_max_kw": 50}}
        r = client.post("/gb/net/patch", json=[{"op": "add_device", "device": probe}])
        assert r.status_code == 200
        assert r.json() == {"applied": ["gen_probe"], "errors": []}

        res = client.post("/gb/step", json=_step_req(
            0, zone_demand={"z0": {"value": 40.0}},
            device_setpoints={"gen_probe": {"p_kw": 30.0}})).json()
        assert res["devices"]["gen_probe"]["output_kw"] == pytest.approx(30.0)

        r = client.post("/gb/net/patch",
                        json=[{"op": "set_device", "id": "gen_probe",
                               "params": {"p_max_kw": 20}}])
        assert r.json()["applied"] == ["gen_probe"]
        res = client.post("/gb/step", json=_step_req(1)).json()  # held 30 > new cap
        assert res["devices"]["gen_probe"]["output_kw"] == pytest.approx(20.0)
        assert [v for v in res["violations"] if v["kind"] == "clamped"]

        r = client.post("/gb/net/patch", json=[{"op": "remove_device", "id": "gen_probe"}])
        assert r.json() == {"applied": ["gen_probe"], "errors": []}

        # tolerant per entry: unknown id is an error entry, not an HTTP error;
        # ops before it stay applied
        r = client.post("/gb/net/patch", json=[
            {"op": "add_device", "device": probe},
            {"op": "remove_device", "id": "__no_such_device__"},
        ])
        assert r.status_code == 200
        body = r.json()
        assert body["applied"] == ["gen_probe"]
        assert len(body["errors"]) == 1 and body["errors"][0]["index"] == 1
        assert "__no_such_device__" in body["errors"][0]["error"]

        # the slack is topology, not a patchable device
        r = client.post("/gb/net/patch", json=[{"op": "remove_device", "id": "slack"}])
        assert r.json()["applied"] == [] and len(r.json()["errors"]) == 1

        # zone rows stayed aligned across add/remove: demands still apply
        res = client.post("/gb/step", json=_step_req(
            2, zone_demand={"z0": {"value": 10.0}, "z1": {"value": 10.0}})).json()
        assert res["status"] == "converged"
        assert res["devices"]["slack"]["output_kw"] == pytest.approx(25.0, abs=3.0)


def test_gb_reset_rejects_bad_documents():
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        good = _topology()

        bad = dict(good, network_kind="heat")
        assert client.post("/gb/net/reset", json=bad).status_code == 400

        bad = dict(good, contract="2.0")
        assert client.post("/gb/net/reset", json=bad).status_code == 400

        bad = dict(good, zones=[{"id": "z0", "node": "nowhere"}])
        assert client.post("/gb/net/reset", json=bad).status_code == 400

        bad = dict(good, devices=good["devices"] + [
            {"id": "chp1", "kind": "chp", "node": "b1"}])
        assert client.post("/gb/net/reset", json=bad).status_code == 400

        # profile arrays must match steps_per_day (contract §3.1)
        bad = json.loads(json.dumps(good))
        bad["native"]["load"]["loads"][0]["p_mw"] = [0.005] * (STEPS - 1)
        assert client.post("/gb/net/reset", json=bad).status_code == 400

        # a failed reset must not have replaced a previously loaded topology
        assert client.post("/gb/net/reset", json=good).status_code == 200
        assert client.post("/gb/step", json=_step_req(0)).status_code == 200


# --------------------------------------------------------------------------- #
# Contract 1.1 (spec §3.1 battery row + §4 'edges'): game-dispatched battery
# devices and the per-edge overlay quantities. dt_s is 900 s (0.25 h)
# throughout, so the SoC arithmetic below stays exact: energy per step =
# p_kw / 4 kWh; charging stores with efficiency 0.95, discharging is lossless.
# --------------------------------------------------------------------------- #


def _topology5(devices: "list[dict] | None" = None) -> dict:
    """A 5-bus net WITH a transformer: b0 (20 kV) -T0- b1 -L0- b2 -L1- b3
    -L2- b4 (0.4-kV NAYY chain), 2 zones, slack@b0 + battery bat1@b2
    (e_kwh 10, p_max_kw 50 → SoC starts at 5 kWh)."""
    buses = [{"name": "b0", "vn_kv": 20.0}] + \
            [{"name": f"b{i}", "vn_kv": 0.4} for i in range(1, 5)]
    native = {
        "grid_structure": {"name": "gb_test5", "f_hz": 50.0, "buses": buses},
        "lines": {
            "lines": [
                {"name": f"l{i}{i + 1}", "from_bus": i, "to_bus": i + 1,
                 "length_km": 0.2, "std_type": "NAYY 4x150 SE"}
                for i in range(1, 4)
            ],
            "transformers": [
                {"name": "t0", "hv_bus": 0, "lv_bus": 1,
                 "std_type": "0.25 MVA 20/0.4 kV"}],
        },
        "load": {"steps": STEPS, "loads": []},
        "generation": {"steps": STEPS, "generation": []},
        "substation": {"steps": STEPS, "substations": []},
    }
    return {
        "contract": "1.1",
        "network_kind": "power",
        "name": "gb_test5",
        "steps_per_day": STEPS,
        "native": native,
        "zones": [{"id": "z0", "node": "b2"}, {"id": "z1", "node": "b4"}],
        "devices": devices if devices is not None else [
            {"id": "slack", "kind": "slack", "node": "b0",
             "params": {"vm_pu": 1.0}},
            {"id": "bat1", "kind": "battery", "node": "b2",
             "params": {"e_kwh": 10, "p_max_kw": 50}},
        ],
    }


def _clamps(res: dict) -> "list[dict]":
    return [v for v in res["violations"] if v["kind"] == "clamped"]


def test_gb_battery_dispatch_clamps_and_soc_trajectory():
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        r = client.post("/gb/net/reset", json=_topology5())
        assert r.status_code == 200, r.text
        assert r.json()["warnings"] == []      # battery is implemented, not idle

        # t=0: discharge request 100 kW -> ±p_max_kw 50 -> SoC bound
        # 5 kWh * 3600/900 = 20 kW; SoC after: 5 - 20*0.25 = 0
        res = client.post("/gb/step", json=_step_req(
            0, zone_demand={"z0": {"value": 30.0}},
            device_setpoints={"bat1": {"p_kw": 100.0}})).json()
        bat = res["devices"]["bat1"]
        assert bat["output_kw"] == pytest.approx(20.0)
        assert bat["soc"] == pytest.approx(0.0)
        clamp = _clamps(res)
        assert clamp and clamp[0]["element"] == "device:bat1"
        assert clamp[0]["severity"] == "info" and clamp[0]["value"] == 100.0
        # the realized discharge feeds the solved net: slack = 30 - 20 + losses
        assert res["devices"]["slack"]["output_kw"] == pytest.approx(10.0, abs=3.0)

        # t=1: charge request -100 -> -p_max 50 -> headroom bound
        # (10 - 0) * 3600/900 = 40 kW; SoC after: 0.95 * 40 * 0.25 = 9.5 kWh
        res = client.post("/gb/step", json=_step_req(
            1, device_setpoints={"bat1": {"p_kw": -100.0}})).json()
        bat = res["devices"]["bat1"]
        assert bat["output_kw"] == pytest.approx(-40.0)
        assert bat["soc"] == pytest.approx(0.95)
        assert _clamps(res)
        # charging draws from the grid: slack = 30 (held) + 40 + losses
        assert res["devices"]["slack"]["output_kw"] == pytest.approx(70.0, abs=5.0)

        # t=2: charge -4 kW, but headroom only (10 - 9.5) * 4 = 2 kW;
        # SoC after: 9.5 + 0.95 * 2 * 0.25 = 9.975 kWh
        res = client.post("/gb/step", json=_step_req(
            2, device_setpoints={"bat1": {"p_kw": -4.0}})).json()
        assert res["devices"]["bat1"]["output_kw"] == pytest.approx(-2.0)
        assert res["devices"]["bat1"]["soc"] == pytest.approx(0.9975)
        assert _clamps(res)

        # t=3: in-bounds discharge -> no clamp; SoC: 9.975 - 10*0.25 = 7.475
        res = client.post("/gb/step", json=_step_req(
            3, device_setpoints={"bat1": {"p_kw": 10.0}})).json()
        assert res["devices"]["bat1"]["output_kw"] == pytest.approx(10.0)
        assert res["devices"]["bat1"]["soc"] == pytest.approx(0.7475)
        assert not _clamps(res)


def test_gb_battery_idempotent_resend_does_not_double_integrate():
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        assert client.post("/gb/net/reset", json=_topology5()).status_code == 200
        # t=0: discharge 10 kW -> SoC 5 - 2.5 = 2.5 kWh
        res = client.post("/gb/step", json=_step_req(
            0, device_setpoints={"bat1": {"p_kw": 10.0}})).json()
        assert res["devices"]["bat1"]["soc"] == pytest.approx(0.25)
        # idempotent re-send: byte-equal cached result, NO second integration
        again = client.post("/gb/step", json=_step_req(
            0, device_setpoints={"bat1": {"p_kw": 10.0}}))
        assert again.status_code == 200 and again.json() == res
        # t=1 continues from 2.5 kWh (held 10-kW discharge drains it exactly);
        # had the re-send double-integrated, SoC would already be 0 and the
        # discharge would clamp to 0 kW here.
        res1 = client.post("/gb/step", json=_step_req(1)).json()
        assert res1["devices"]["bat1"]["output_kw"] == pytest.approx(10.0)
        assert res1["devices"]["bat1"]["soc"] == pytest.approx(0.0)
        assert not _clamps(res1)


def test_gb_battery_set_device_resizes_and_clamps_soc():
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        assert client.post("/gb/net/reset", json=_topology5()).status_code == 200
        # shrink e_kwh 10 -> 4: the stored 5 kWh is clamped to 4 -> soc 1.0
        r = client.post("/gb/net/patch", json=[
            {"op": "set_device", "id": "bat1", "params": {"e_kwh": 4}}])
        assert r.json() == {"applied": ["bat1"], "errors": []}
        res = client.post("/gb/step", json=_step_req(0)).json()
        assert res["devices"]["bat1"]["soc"] == pytest.approx(1.0)
        # shrink p_max_kw -> 8: a 20-kW discharge clamps to 8 kW
        r = client.post("/gb/net/patch", json=[
            {"op": "set_device", "id": "bat1", "params": {"p_max_kw": 8}}])
        assert r.json()["applied"] == ["bat1"]
        res = client.post("/gb/step", json=_step_req(
            1, device_setpoints={"bat1": {"p_kw": 20.0}})).json()
        assert res["devices"]["bat1"]["output_kw"] == pytest.approx(8.0)
        assert res["devices"]["bat1"]["soc"] == pytest.approx((4.0 - 2.0) / 4.0)
        assert _clamps(res)
        # invalid resize is a tolerant per-entry error; the state stays sane
        r = client.post("/gb/net/patch", json=[
            {"op": "set_device", "id": "bat1", "params": {"e_kwh": -1}}])
        assert r.status_code == 200
        assert r.json()["applied"] == [] and len(r.json()["errors"]) == 1
        res = client.post("/gb/step", json=_step_req(
            2, device_setpoints={"bat1": {"p_kw": 0.0}})).json()
        assert res["devices"]["bat1"]["soc"] == pytest.approx(0.5)


def test_gb_battery_patch_add_remove():
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        assert client.post("/gb/net/reset", json=_topology()).status_code == 200
        probe = {"id": "bat_probe", "kind": "battery", "node": "b2",
                 "params": {"e_kwh": 8, "p_max_kw": 20}}
        r = client.post("/gb/net/patch", json=[{"op": "add_device", "device": probe}])
        assert r.json() == {"applied": ["bat_probe"], "errors": []}
        # charge -12 kW: draws 3 kWh, stores 2.85 -> soc (4 + 2.85) / 8
        res = client.post("/gb/step", json=_step_req(
            0, device_setpoints={"bat_probe": {"p_kw": -12.0}})).json()
        assert res["devices"]["bat_probe"]["output_kw"] == pytest.approx(-12.0)
        assert res["devices"]["bat_probe"]["soc"] == pytest.approx(6.85 / 8.0)
        r = client.post("/gb/net/patch", json=[
            {"op": "remove_device", "id": "bat_probe"}])
        assert r.json()["applied"] == ["bat_probe"]
        res = client.post("/gb/step", json=_step_req(1)).json()
        assert "bat_probe" not in res["devices"]
        assert res["status"] == "converged"


def test_gb_battery_requires_positive_params():
    from fastapi.testclient import TestClient

    slack = {"id": "slack", "kind": "slack", "node": "b0",
             "params": {"vm_pu": 1.0}}
    with TestClient(app) as client:
        # no params at all
        bad = _topology5(devices=[slack, {"id": "bat1", "kind": "battery",
                                          "node": "b2"}])
        assert client.post("/gb/net/reset", json=bad).status_code == 400
        # e_kwh must be > 0
        bad = _topology5(devices=[slack, {"id": "bat1", "kind": "battery",
                                          "node": "b2",
                                          "params": {"e_kwh": 0, "p_max_kw": 10}}])
        assert client.post("/gb/net/reset", json=bad).status_code == 400
        # add_device via patch enforces the same rule (tolerant per entry)
        assert client.post("/gb/net/reset", json=_topology5()).status_code == 200
        r = client.post("/gb/net/patch", json=[{"op": "add_device", "device": {
            "id": "bat2", "kind": "battery", "node": "b3",
            "params": {"e_kwh": 5}}}])
        assert r.status_code == 200
        assert r.json()["applied"] == [] and len(r.json()["errors"]) == 1


def test_gb_edges_field():
    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        assert client.get("/gb/version").json()["contract"] == "1.1"
        assert client.post("/gb/net/reset", json=_topology5()).status_code == 200
        res = client.post("/gb/step", json=_step_req(
            0, zone_demand={"z0": {"value": 40.0}, "z1": {"value": 20.0}})).json()
        # ALL native lines and trafos, ids in native element order (spec §4)
        assert set(res["edges"]) == {"L0", "L1", "L2", "T0"}
        for entry in res["edges"].values():
            lp = entry["loading_percent"]
            assert isinstance(lp, (int, float)) and not isinstance(lp, bool)
            assert lp >= 0.0
            assert isinstance(entry["p_from_kw"], (int, float))
        # plausible physics: the trafo carries the whole 60-kW demand
        # (+ losses) -> roughly a quarter of its 250-kVA rating
        assert res["edges"]["T0"]["p_from_kw"] == pytest.approx(60.0, abs=6.0)
        assert 5.0 < res["edges"]["T0"]["loading_percent"] < 60.0
        # the tail segment b3-b4 carries only z1's 20 kW
        assert res["edges"]["L2"]["p_from_kw"] == pytest.approx(20.0, abs=3.0)
        assert res["edges"]["L0"]["p_from_kw"] > res["edges"]["L2"]["p_from_kw"]
        # edges mirror the solved native tables exactly
        state = client.get("/state").json()
        assert res["edges"]["L0"]["loading_percent"] == \
            state["lines"][0]["loading_percent"]
        assert res["edges"]["T0"]["loading_percent"] == \
            state["trafos"][0]["loading_percent"]
        # idempotent re-send carries the identical edges
        again = client.post("/gb/step", json=_step_req(
            0, zone_demand={"z0": {"value": 40.0}, "z1": {"value": 20.0}})).json()
        assert again["edges"] == res["edges"]
