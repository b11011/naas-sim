"""Tests for the new-generation APIs: Multi-Cloud Gateway + Ethernet Fabric Connect.

conftest.py sets NAAS_SIM_DELAY_SECONDS=0.2 so lifecycle transitions complete fast.
"""
import time

import pytest
from fastapi.testclient import TestClient

from simulator.main import app

SEEDED_GW = "4c85553e-91ce-4eab-9551-2014985f8c84"
SEEDED_IF = "f0b9cf7b-18ca-4d04-82d5-76198ca6d34f"
SEEDED_CONN = "d2bdc87d-3f76-4db6-8d2a-1f5f4b3fbbf2"

BILLING = {"customer_number": "1-ABCD", "billing_account_number": "1-1ABCDE-F"}


@pytest.fixture()
def client():
    with TestClient(app) as c:
        c.post("/_lab/reset")
        yield c
        c.post("/_lab/reset")


@pytest.fixture()
def h(client):
    tok = client.post("/oauth/v2/token", data={"grant_type": "client_credentials"},
                      auth=("naas-lab-client", "naas-lab-secret")).json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}


def wait_for(predicate, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


# ------------------------------------------------------------------ error model

def test_new_apis_speak_problem_json(client, h):
    r = client.get("/mcgw/v1/gateways/00000000-0000-0000-0000-000000000000", headers=h)
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["title"] == "Gateway not found" and body["status"] == 404 and "type" in body


def test_legacy_apis_keep_legacy_envelope(client, h):
    r = client.get("/Network/v5/DynamicConnection/evcs/nope",
                   headers={**h, "x-billing-account-number": "5-WX3EDQ"})
    assert r.status_code == 404
    assert "code" in r.json() and "title" not in r.json()


def test_validation_error_is_problem_json_on_new_paths(client, h):
    r = client.post("/mcgw/v1/gateways", headers=h, json={"name": "x"})  # missing required
    assert r.status_code == 400
    assert r.headers["content-type"].startswith("application/problem+json")


# ------------------------------------------------------------------ MCG lifecycle

def test_gateway_lifecycle(client, h):
    r = client.post("/mcgw/v1/gateways", headers=h,
                    json={"name": "t-gw", "tier": "10 Gbps", **BILLING})
    assert r.status_code == 201
    assert "location" in r.headers
    gw = r.json()
    assert gw["state"] == "PROVISIONING"
    gid = gw["gateway_id"]
    assert wait_for(lambda: client.get(f"/mcgw/v1/gateways/{gid}",
                                       headers=h).json()["state"] == "PROVISIONED")
    assert client.delete(f"/mcgw/v1/gateways/{gid}", headers=h).status_code == 204
    assert wait_for(lambda: client.get(f"/mcgw/v1/gateways/{gid}", headers=h).status_code == 404)


def test_invalid_tier_rejected(client, h):
    r = client.post("/mcgw/v1/gateways", headers=h,
                    json={"name": "t", "tier": "99 Gbps", **BILLING})
    assert r.status_code == 400
    assert "tier" in r.json()["detail"]


def test_interface_requires_provisioned_gateway(client, h):
    gid = client.post("/mcgw/v1/gateways", headers=h,
                      json={"name": "t2", "tier": "10 Gbps", **BILLING}).json()["gateway_id"]
    # still PROVISIONING -> 409
    r = client.post(f"/mcgw/v1/gateways/{gid}/interfaces", headers=h, json={"name": "if-1"})
    assert r.status_code == 409


def test_gateway_delete_blocked_by_interfaces(client, h):
    r = client.delete(f"/mcgw/v1/gateways/{SEEDED_GW}", headers=h)
    assert r.status_code == 409
    assert "interface" in r.json()["detail"].lower()


def test_list_envelope_shape(client, h):
    r = client.get("/mcgw/v1/gateways", headers=h).json()
    assert set(r) == {"pagination", "data"}
    assert r["pagination"]["total"] >= 1


# ------------------------------------------------------------------ EFC connections

def test_hosted_connection_lifecycle_and_aggregate(client, h):
    r = client.post("/fabric/v1/connections/cloud/hosted/gcp", headers=h, json={
        "name": "gcp-conn", "bandwidth": 1000, "term": "Hourly", **BILLING,
        "source_endpoint": {"gateway": {"gateway_id": SEEDED_GW},
                            "interface": {"interface_id": SEEDED_IF}},
        "dest_endpoint": {"pairing_key": "abc/region/1"},
    })
    assert r.status_code == 202
    conn = r.json()
    assert conn["state"] == "provisioning"
    cid = conn["connection_id"]
    assert wait_for(lambda: client.get(f"/fabric/v1/connections/cloud/hosted/gcp/{cid}",
                                       headers=h).json()["state"] == "active")
    gw = client.get(f"/mcgw/v1/gateways/{SEEDED_GW}", headers=h).json()
    assert gw["total_aggregate_bw"] == "2000 Mbps"  # 1000 seeded + 1000 new


def test_bandwidth_patch_flow(client, h):
    r = client.patch(f"/fabric/v1/connections/cloud/hosted/aws/{SEEDED_CONN}",
                     headers=h, json={"bandwidth": 2000})
    assert r.status_code == 200
    assert r.json()["state"] == "updating"
    # concurrent modification rejected while updating
    r2 = client.patch(f"/fabric/v1/connections/cloud/hosted/aws/{SEEDED_CONN}",
                      headers=h, json={"bandwidth": 3000})
    assert r2.status_code == 409
    assert wait_for(lambda: client.get(f"/fabric/v1/connections/{SEEDED_CONN}",
                                       headers=h).json()["bandwidth"] == 2000)
    assert client.get(f"/fabric/v1/connections/{SEEDED_CONN}",
                      headers=h).json()["state"] == "active"


def test_bandwidth_enum_enforced(client, h):
    r = client.patch(f"/fabric/v1/connections/cloud/hosted/aws/{SEEDED_CONN}",
                     headers=h, json={"bandwidth": 123})
    assert r.status_code == 400
    assert "bandwidth" in r.json()["detail"]


def test_tier_capacity_enforced(client, h):
    # 50 Gbps seeded gateway with 1000 Mbps used: adding 2x 25000 must fail on the second
    payload = {
        "name": "big", "bandwidth": 25000, "term": "Monthly", **BILLING,
        "source_endpoint": {"gateway": {"gateway_id": SEEDED_GW},
                            "interface": {"interface_id": SEEDED_IF}},
        "dest_endpoint": {"aws_account_id": "123456789012"},
    }
    assert client.post("/fabric/v1/connections/cloud/hosted/aws",
                       headers=h, json=payload).status_code == 202
    r = client.post("/fabric/v1/connections/cloud/hosted/aws", headers=h,
                    json={**payload, "name": "big2"})
    assert r.status_code == 422
    assert "tier" in r.json()["detail"].lower() or "capacity" in r.json()["title"].lower()


def test_bandwidth_options_priced(client, h):
    r = client.get(f"/fabric/v1/connections/{SEEDED_CONN}/bandwidths", headers=h)
    assert r.status_code == 200
    options = r.json()
    assert [o["bandwidth"] for o in options][:3] == [10, 20, 50]
    first = options[0]["options"][0]
    assert {"category", "class_of_service", "price_hourly", "price_monthly"} <= set(first)


def test_endpoint_validation(client, h):
    r = client.post("/fabric/v1/connections/virtual/p2p", headers=h, json={
        "name": "bad", "bandwidth": 100, **BILLING,
        "source_endpoint": {"gateway": {"gateway_id": "00000000-0000-0000-0000-000000000000"}},
        "dest_endpoint": {"service_id": "TN/KXFN/000002/LVLC", "ce_vlan": 240},
    })
    assert r.status_code == 404
    assert "gateway" in r.json()["detail"].lower()


def test_billing_endpoint(client, h):
    r = client.get(f"/fabric/v1/billing/{SEEDED_CONN}", headers=h)
    assert r.status_code == 200
    b = r.json()
    assert b["connection_id"] == SEEDED_CONN and "price_monthly" in b
