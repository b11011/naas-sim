"""Tests for the product-layer features: metrics, seed profiles, persistence."""
import json

import pytest
from fastapi.testclient import TestClient

import simulator.config as config
from simulator.main import app
from simulator.state import Store


@pytest.fixture()
def client():
    with TestClient(app) as c:
        c.post("/_lab/reset")
        c.post("/_lab/metrics/reset")
        yield c
        c.post("/_lab/reset")


def get_token(client):
    return client.post("/oauth/v2/token", data={"grant_type": "client_credentials"},
                       auth=("naas-lab-client", "naas-lab-secret")).json()["access_token"]


def test_metrics_counts_requests_and_errors(client):
    token = get_token(client)
    client.get("/Network/v5/DynamicConnection/unis",
               headers={"Authorization": f"Bearer {token}"})
    client.get("/Network/v5/DynamicConnection/unis")  # 401 — no token

    snap = client.get("/_lab/metrics").json()
    assert snap["totalRequests"] >= 3  # token + 2 unis calls
    assert snap["errored"] >= 1
    routes = {r["route"] for r in snap["byRoute"]}
    assert "GET /Network/v5/DynamicConnection/unis" in routes
    assert any(e["status"] == 401 for e in snap["topErrors"])
    # /_lab traffic must not count itself
    assert not any("/_lab" in r["route"] for r in snap["byRoute"])


def test_metrics_reset(client):
    get_token(client)
    assert client.get("/_lab/metrics").json()["totalRequests"] >= 1
    client.post("/_lab/metrics/reset")
    assert client.get("/_lab/metrics").json()["totalRequests"] == 0


def test_seed_profile_replaces_catalog(client):
    profile = {
        "speeds": [25, 250],
        "locations": [{"masterSiteId": "PL-TEST-1", "name": "Test DC", "address": "x",
                       "partnerId": "1", "partner": "Test", "naasEnabled": True}],
        "services": [{"id": "770000001", "name": "test svc", "status": "active",
                      "customerNumber": "1-TEST", "billingAccountId": "5-TEST",
                      "masterSiteId": "PL-TEST-1", "bandwidth": 25, "bandwidthUnit": "Mbps"}],
    }
    resp = client.post("/_lab/seed", json=profile)
    assert resp.status_code == 200
    assert resp.json()["counts"]["speeds"] == [25, 250]

    token = get_token(client)
    h = {"Authorization": f"Bearer {token}", "x-customer-number": "1-TEST"}
    price = client.get("/Product/v1/price", headers=h,
                       params={"productCode": "718", "masterSiteId": "PL-TEST-1"})
    assert [o["speed"] for o in price.json()["offerings"]] == [25, 250]
    # old catalog speed no longer offered
    quote = client.post("/Product/v1/priceRequest", headers=h, json={
        "productCode": "718", "masterSiteId": "PL-TEST-1", "speed": "200 Mbps"})
    assert quote.status_code == 400


def test_seed_rejects_unknown_sections(client):
    assert client.post("/_lab/seed", json={"bogus": []}).status_code == 400
    assert client.post("/_lab/seed", json={}).status_code == 400


def test_persistence_roundtrip_completes_inflight(tmp_path, monkeypatch):
    state_file = str(tmp_path / "state.json")
    monkeypatch.setattr(config, "STATE_FILE", state_file)

    s1 = Store()
    assert not s1.loaded_from_snapshot
    evc_id = "e27a48de-7ab1-46dc-a0b0-a0abea016b5d"
    # simulate a modification captured mid-flight
    s1.evcs[evc_id]["status"] = "MODIFYING"
    s1.evc_requests["req-1"] = {"requestId": "req-1", "evcId": evc_id, "action": "MODIFY",
                                "status": "IN_PROGRESS", "requestedBandwidth": 500,
                                "createdDateTime": "2026-07-09T00:00:00Z",
                                "completedDateTime": None}
    s1.save()
    assert json.load(open(state_file))["evcs"]

    s2 = Store()  # restores snapshot and reconciles in-flight work
    assert s2.loaded_from_snapshot
    assert s2.evcs[evc_id]["status"] == "ACTIVE"
    assert s2.evcs[evc_id]["bandwidth"] == 500
    assert s2.evc_requests["req-1"]["status"] == "COMPLETED"
    # tokens are ephemeral by design
    assert s2.tokens == {}
