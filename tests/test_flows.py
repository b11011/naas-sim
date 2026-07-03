"""End-to-end smoke tests for both bandwidth-on-demand flows.

conftest.py sets NAAS_SIM_DELAY_SECONDS=0.2 and NAAS_SIM_DAILY_ORDER_LIMIT=5
before the app is imported.
"""
import time

import pytest
from fastapi.testclient import TestClient

from simulator.main import app
from simulator.state import store

SEEDED_EVC = "e27a48de-7ab1-46dc-a0b0-a0abea016b5d"
SEEDED_SERVICE = "771234567"

ORDER_CONTACT = [{
    "number": "5555550100", "emailAddress": "first.lastname@domain.com",
    "role": "Order Contact", "organization": "Lab Org", "name": "FirstName LastName",
}]


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def token(client):
    resp = client.post("/oauth/v2/token", data={"grant_type": "client_credentials"},
                       auth=("naas-lab-client", "naas-lab-secret"))
    assert resp.status_code == 200
    return resp.json()["access_token"]


@pytest.fixture(scope="module")
def eod_headers(token):
    return {"Authorization": f"Bearer {token}", "x-billing-account-number": "5-WX3EDQ"}


@pytest.fixture(scope="module")
def iod_headers(token):
    return {"Authorization": f"Bearer {token}", "x-customer-number": "1-ABCDE"}


def wait_for(predicate, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_requests_require_token(client):
    resp = client.get("/Network/v5/DynamicConnection/unis")
    assert resp.status_code == 401
    assert resp.json()["code"] == 401


def test_bad_credentials_rejected(client):
    resp = client.post("/oauth/v2/token", data={"grant_type": "client_credentials"},
                       auth=("naas-lab-client", "wrong"))
    assert resp.status_code == 401


def test_eod_bandwidth_change(client, eod_headers):
    url = f"/Network/v5/DynamicConnection/evcs/{SEEDED_EVC}"
    resp = client.patch(url, headers=eod_headers,
                        json={"bandwidth": 200, "userEmail": "user@email.com"})
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "MODIFYING"

    assert client.get(url, headers=eod_headers).json()["status"] == "MODIFYING"
    assert wait_for(lambda: client.get(url, headers=eod_headers).json()["status"] == "ACTIVE")
    evc = client.get(url, headers=eod_headers).json()
    assert evc["bandwidth"] == 200
    assert evc["modifyDateTime"] is not None

    reqs = client.get("/Network/v5/DynamicConnection/evcRequests",
                      params={"evcId": SEEDED_EVC}, headers=eod_headers).json()["evcRequests"]
    assert any(r["requestId"] == body["evcRequestId"] and r["status"] == "COMPLETED" for r in reqs)


def test_eod_patch_validation(client, eod_headers):
    base = "/Network/v5/DynamicConnection/evcs"
    assert client.patch(f"{base}/nonexistent", headers=eod_headers,
                        json={"bandwidth": 100, "userEmail": "u@e.com"}).status_code == 404
    # exceeds the 1000 Mbps UNI capacity
    assert client.patch(f"{base}/{SEEDED_EVC}", headers=eod_headers,
                        json={"bandwidth": 5000, "userEmail": "u@e.com"}).status_code == 400
    # missing billing account header -> Lumen-style 400
    resp = client.patch(f"{base}/{SEEDED_EVC}", headers={"Authorization": eod_headers["Authorization"]},
                        json={"bandwidth": 100, "userEmail": "u@e.com"})
    assert resp.status_code == 400
    assert resp.json()["code"] == 400


def test_eod_create_and_delete_evc(client, eod_headers):
    resp = client.post("/Network/v5/DynamicConnection/evcs", headers=eod_headers, json={
        "evcName": "test-evc", "bandwidth": 100, "userEmail": "u@e.com",
        "uniServiceId": "CO/DVXX/222222/LVLC", "ceVlan": 200,
    })
    assert resp.status_code == 202
    evc_id = resp.json()["evcId"]
    url = f"/Network/v5/DynamicConnection/evcs/{evc_id}"
    assert wait_for(lambda: client.get(url, headers=eod_headers).json()["status"] == "ACTIVE")

    resp = client.delete(url, params={"userEmail": "u@e.com"}, headers=eod_headers)
    assert resp.status_code == 202
    assert wait_for(lambda: client.get(url, headers=eod_headers).status_code == 404)


def test_iod_full_bandwidth_flow(client, iod_headers):
    price = client.get("/Product/v1/price", headers=iod_headers,
                       params={"productCode": "718", "masterSiteId": "PL0000000001",
                               "serviceId": SEEDED_SERVICE})
    assert price.status_code == 200
    assert 200 in [o["speed"] for o in price.json()["offerings"]]

    quote = client.post("/Product/v1/priceRequest", headers=iod_headers, json={
        "productCode": "718", "masterSiteId": "PL0000000001",
        "serviceId": SEEDED_SERVICE, "speed": "200 Mbps",
    })
    assert quote.status_code == 200
    quote_id = quote.json()["quoteId"]

    order = client.post("/Customer/v3/Ordering/orderRequest", headers=iod_headers, json={
        "externalId": "EXT-TEST-1",
        "productOrderItem": [{"id": "Order1", "quantity": 1, "action": "modify",
                              "product": {"id": SEEDED_SERVICE, "productCharacteristic": []}}],
        "quote": [{"id": quote_id, "name": "quoteId"}],
        "relatedContactInformation": ORDER_CONTACT,
    })
    assert order.status_code == 201, order.text
    order_id = order.json()["id"]

    def bandwidth_updated():
        inv = client.get("/ProductInventory/v1/inventory",
                         params={"id": SEEDED_SERVICE}, headers=iod_headers).json()
        return inv["services"][0]["bandwidth"] == 200

    assert wait_for(bandwidth_updated)
    final = client.get(f"/Customer/v3/Ordering/orderRequest/{order_id}", headers=iod_headers).json()
    assert final["state"] == "completed"
    assert any(e["eventType"] == "productOrderStateChangeEvent" for e in store.events)


def test_iod_order_contact_enforced(client, iod_headers):
    resp = client.post("/Customer/v3/Ordering/orderRequest", headers=iod_headers, json={
        "productOrderItem": [{"id": "Order1", "action": "modify",
                              "product": {"id": SEEDED_SERVICE}}],
        "relatedContactInformation": [{"role": "Order Contact", "name": "Cher"}],
    })
    assert resp.status_code == 400
    assert "first and last name" in resp.json()["message"]


def test_iod_unknown_speed_rejected(client, iod_headers):
    resp = client.post("/Product/v1/priceRequest", headers=iod_headers, json={
        "productCode": "718", "masterSiteId": "PL0000000001", "speed": "123 Mbps",
    })
    assert resp.status_code == 400


def test_iod_daily_quota(client, token):
    headers = {"Authorization": f"Bearer {token}", "x-customer-number": "1-QUOTA"}
    rename_order = {
        "productOrderItem": [{"id": "Order1", "action": "modify",
                              "product": {"id": SEEDED_SERVICE,
                                          "productCharacteristic": [{"name": "Customer Service Name",
                                                                     "valueType": "String",
                                                                     "value": "renamed"}]}}],
        "relatedContactInformation": ORDER_CONTACT,
    }
    for _ in range(5):  # NAAS_SIM_DAILY_ORDER_LIMIT=5 in tests
        assert client.post("/Customer/v3/Ordering/orderRequest",
                           headers=headers, json=rename_order).status_code == 201
    resp = client.post("/Customer/v3/Ordering/orderRequest", headers=headers, json=rename_order)
    assert resp.status_code == 429
