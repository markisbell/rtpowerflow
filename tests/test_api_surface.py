"""Regression net for the API surface: pins the COMPLETE route inventory
(method + path) and smoke-tests one representative endpoint per router area
over a real TestClient (lifespan runs, engine paused). Written before the
api.py -> netzsim/api/ router split; the split must keep this file green
without modification."""
from __future__ import annotations

import pytest
from fastapi.routing import APIRoute, APIWebSocketRoute
from fastapi.testclient import TestClient

from netzsim.config import settings

# The engine must not tick during API tests (deterministic /state = 404 etc.).
settings.autostart = False

from netzsim.api import app  # noqa: E402  (needs the settings override first)


# --------------------------------------------------------------------------- #
# 1) Route inventory: every endpoint the platform ships, frozen. A router
#    split (or any later change) that loses/renames/duplicates a route fails
#    here with a readable diff.
# --------------------------------------------------------------------------- #
EXPECTED_ROUTES = {
    # core: monitor, health, topology, live results, per-element day curves
    ("GET", "/"),
    ("GET", "/manual"),
    ("GET", "/health"),
    ("GET", "/status"),
    ("GET", "/network"),
    ("GET", "/node/{bus}/profiles"),
    ("GET", "/line/{line}/profiles"),
    ("GET", "/trafo/{trafo}/profiles"),
    ("GET", "/state"),
    ("GET", "/history"),
    ("WS", "/ws"),
    # engine control + real-PV day calendar
    ("POST", "/control/start"),
    ("POST", "/control/pause"),
    ("POST", "/control/resume"),
    ("POST", "/control/seek"),
    ("POST", "/control/interval"),
    ("POST", "/control/seekday"),
    ("GET", "/pv/days"),
    # session recording + bulk export
    ("GET", "/recording"),
    ("POST", "/recording/start"),
    ("POST", "/recording/stop"),
    ("GET", "/recordings"),
    ("GET", "/recordings/{rid}/download"),
    ("DELETE", "/recordings/{rid}"),
    ("POST", "/export/days"),
    ("GET", "/export"),
    ("POST", "/export/cancel"),
    # runtime equipment: batteries, controllers, rONTs, per-node DERs
    ("GET", "/batteries"),
    ("POST", "/battery"),
    ("POST", "/battery/{idx}/mode"),
    ("POST", "/battery/{idx}/size"),
    ("DELETE", "/battery/{idx}"),
    ("GET", "/battery/{idx}/profiles"),
    ("GET", "/controllers"),
    ("POST", "/controller"),
    ("POST", "/controller/{cid}/config"),
    ("DELETE", "/controller/{cid}"),
    ("GET", "/ronts"),
    ("POST", "/ront"),
    ("POST", "/ront/{rid}/config"),
    ("DELETE", "/ront/{rid}"),
    ("GET", "/node/{bus}/der"),
    ("POST", "/pv"),
    ("POST", "/pv/{sgen}"),
    ("DELETE", "/pv/{sgen}"),
    ("POST", "/ev"),
    ("POST", "/ev/{load}"),
    ("DELETE", "/ev/{load}"),
    # observability: meter placement + estimation policy
    ("GET", "/measurements"),
    ("POST", "/measurements/node"),
    ("DELETE", "/measurements/node/{bus}"),
    ("POST", "/measurements/trafo"),
    ("DELETE", "/measurements/trafo/{trafo}"),
    ("POST", "/measurements/mode"),
    ("POST", "/measurements/preset"),
    ("GET", "/estimation/config"),
    ("POST", "/estimation/config"),
    # grid catalog, loadgen, grid swap
    ("GET", "/grids"),
    ("GET", "/grids/{grid_id}"),
    ("POST", "/grids/import"),
    ("GET", "/loadgen/archetypes"),
    ("POST", "/loadgen/assign"),
    ("POST", "/config/apply"),
    ("GET", "/config/active"),
    # scenarios
    ("GET", "/scenarios"),
    ("POST", "/scenarios"),
    ("DELETE", "/scenarios/{sid}"),
    ("POST", "/scenarios/{sid}/load"),
}


def _actual_routes() -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for r in app.routes:
        if isinstance(r, APIWebSocketRoute):
            out.add(("WS", r.path))
        elif isinstance(r, APIRoute):
            for m in r.methods - {"HEAD", "OPTIONS"}:
                out.add((m, r.path))
    return out


def test_route_inventory_complete():
    actual = _actual_routes()
    missing = EXPECTED_ROUTES - actual
    extra = actual - EXPECTED_ROUTES
    assert not missing, f"routes lost: {sorted(missing)}"
    assert not extra, f"unexpected new routes (extend EXPECTED_ROUTES): {sorted(extra)}"


def test_no_duplicate_routes():
    seen: list[tuple[str, str]] = []
    for r in app.routes:
        if isinstance(r, APIRoute):
            seen += [(m, r.path) for m in r.methods - {"HEAD", "OPTIONS"}]
    dupes = {x for x in seen if seen.count(x) > 1}
    assert not dupes, f"route registered twice: {sorted(dupes)}"


# --------------------------------------------------------------------------- #
# 2) The import surface other code relies on (main.py, tests) must survive
#    turning api.py into a package.
# --------------------------------------------------------------------------- #
def test_public_import_surface():
    from netzsim import api
    for name in ("app", "runtime", "BatteryRequest", "ControllerRequest",
                 "RontRequest", "PvRequest", "EvRequest", "LoadgenPolicy",
                 "EstimationConfigModel", "_households_range"):
        assert hasattr(api, name), f"netzsim.api.{name} vanished"


# --------------------------------------------------------------------------- #
# 3) Smoke over a real client: lifespan boots the default 5-bus sample from
#    ./data, autostart is off, so responses are deterministic. One request
#    per router area proves the wiring end-to-end.
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_openapi_schema_builds(client):
    assert client.get("/openapi.json").status_code == 200


def test_core_endpoints(client):
    assert client.get("/health").json() == {"status": "ok"}
    st = client.get("/status").json()
    assert st["running"] is False and st["steps_per_day"] == 1440
    topo = client.get("/network").json()
    assert len(topo["buses"]) >= 2
    assert client.get("/state").status_code == 404          # no solve yet
    assert client.get("/history").json() == []


def test_profiles_endpoints(client):
    prof = client.get("/node/1/profiles", params={"view": "truth"}).json()
    assert prof["bus"] == 1 and len(prof["voltage"]) == 1440
    assert client.get("/node/9999/profiles").status_code == 404
    line = client.get("/line/0/profiles", params={"view": "truth"}).json()
    assert len(line["loading"]) == 1440


def test_control_endpoints(client):
    st = client.post("/control/pause").json()
    assert st["running"] is False
    assert client.get("/pv/days").status_code == 200


def test_equipment_roundtrips(client):
    b = client.post("/battery", json={"bus": 1}).json()
    assert b["bus"] == 1
    assert client.delete(f"/battery/{b['index']}").json() == {"removed": b["index"]}
    c = client.post("/controller", json={"scope": "station"}).json()
    assert c["scope"] == "station"
    assert client.delete(f"/controller/{c['id']}").status_code == 200
    assert client.get("/ronts").json() == {"ronts": []}
    assert client.get("/node/1/der").status_code == 200


def test_measurement_roundtrip(client):
    m = client.post("/measurements/node", json={"bus": 1}).json()
    assert 1 in m["node_buses"]
    m = client.delete("/measurements/node/1").json()
    assert 1 not in m["node_buses"]
    cfg = client.get("/estimation/config").json()
    assert cfg["pv_pseudo"] is False                        # DSO default


def test_grid_and_recording_listings(client):
    g = client.get("/grids").json()
    assert "grids" in g and "available" in g
    assert client.get("/loadgen/archetypes").status_code == 200
    assert client.get("/config/active").status_code == 200
    r = client.get("/recordings").json()
    assert "recordings" in r and "active" in r
    assert client.get("/export").status_code == 200
    assert client.get("/scenarios").status_code == 200
