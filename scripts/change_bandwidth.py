"""Change a NaaS circuit's bandwidth from NetBox (Phase 1 of the naas-sim lab)."""
import time

import requests
from circuits.models import Circuit
from extras.models import JournalEntry
from extras.scripts import ChoiceVar, ObjectVar, Script

NAAS = "http://localhost:8080"
CLIENT_ID = "naas-lab-client"
CLIENT_SECRET = "naas-lab-secret"
POLL_TIMEOUT = 90  # sim default transition delay is 10s; real platform: minutes

SPEED_CHOICES = [(str(s), f"{s} Mbps") for s in (10, 20, 50, 100, 200, 500, 1000)]

ORDER_CONTACT = [{
    "number": "5555550100", "emailAddress": "lab@example.com",
    "role": "Order Contact", "organization": "Lab", "name": "FirstName LastName",
}]


class ChangeCircuitBandwidth(Script):
    class Meta:
        name = "Change circuit bandwidth"
        description = "Push a bandwidth change to the NaaS simulator and sync NetBox"
        commit_default = True

    circuit = ObjectVar(model=Circuit, description="Circuit to modify")
    speed = ChoiceVar(choices=SPEED_CHOICES, label="New bandwidth")

    def run(self, data, commit):
        circuit, mbps = data["circuit"], int(data["speed"])

        tok = requests.post(f"{NAAS}/oauth/v2/token", auth=(CLIENT_ID, CLIENT_SECRET),
                            data={"grant_type": "client_credentials"}, timeout=10)
        tok.raise_for_status()
        auth = {"Authorization": f"Bearer {tok.json()['access_token']}"}

        slug = circuit.type.slug
        if slug == "eod-evc":
            request_id = self.modify_evc(circuit, mbps, auth)
        elif slug == "iod-dia":
            request_id = self.modify_dia(circuit, mbps, auth)
        else:
            self.log_failure(f"Circuit type '{slug}' is not NaaS-managed (expected eod-evc or iod-dia)")
            return

        circuit.commit_rate = mbps * 1000  # NetBox stores kbps
        circuit.custom_field_data["naas_status"] = "ACTIVE"
        circuit.save()
        JournalEntry.objects.create(
            assigned_object=circuit, kind="info",
            comments=f"Bandwidth changed to {mbps} Mbps via NaaS API (request `{request_id}`).",
        )
        self.log_success(f"{circuit.cid} now at {mbps} Mbps; commit_rate and journal updated")

    # --- Ethernet On-Demand: direct PATCH -------------------------------
    def modify_evc(self, circuit, mbps, auth):
        ban = circuit.provider_account.account if circuit.provider_account else "5-WX3EDQ"
        email = circuit.custom_field_data.get("user_email") or "user@email.com"
        h = {**auth, "x-billing-account-number": ban}
        url = f"{NAAS}/Network/v5/DynamicConnection/evcs/{circuit.cid}"

        r = requests.patch(url, headers=h, json={"bandwidth": mbps, "userEmail": email}, timeout=10)
        if r.status_code != 202:
            raise Exception(f"PATCH rejected [{r.status_code}]: {r.text}")
        request_id = r.json()["evcRequestId"]
        self.log_info(f"Accepted: request {request_id}, EVC MODIFYING")

        deadline = time.time() + POLL_TIMEOUT
        while time.time() < deadline:
            evc = requests.get(url, headers=h, timeout=10).json()
            if evc["status"] == "ACTIVE" and evc["bandwidth"] == mbps:
                return request_id
            time.sleep(3)
        raise Exception(f"EVC did not converge within {POLL_TIMEOUT}s (request {request_id})")

    # --- Internet On-Demand: quote -> order ------------------------------
    def modify_dia(self, circuit, mbps, auth):
        cust = circuit.custom_field_data.get("customer_number") or "1-ABCDE"
        h = {**auth, "x-customer-number": cust}

        inv = requests.get(f"{NAAS}/ProductInventory/v1/inventory",
                           params={"id": circuit.cid}, headers=h, timeout=10).json()["services"]
        if not inv:
            raise Exception(f"service {circuit.cid} not in simulator inventory")
        service = inv[0]

        q = requests.post(f"{NAAS}/Product/v1/priceRequest", headers=h, timeout=10, json={
            "productCode": "718", "masterSiteId": service["masterSiteId"],
            "serviceId": circuit.cid, "speed": f"{mbps} Mbps",
        })
        if q.status_code != 200:
            raise Exception(f"quote rejected [{q.status_code}]: {q.text}")
        quote_id = q.json()["quoteId"]
        self.log_info(f"Quote {quote_id} for {mbps} Mbps")

        o = requests.post(f"{NAAS}/Customer/v3/Ordering/orderRequest", headers=h, timeout=10, json={
            "externalId": f"NETBOX-{int(time.time())}",
            "productOrderItem": [{"id": "Order1", "quantity": 1, "action": "modify",
                                  "product": {"id": circuit.cid, "productCharacteristic": []},
                                  "productOffering": {"id": "718", "name": "Internet On-Demand"}}],
            "quote": [{"id": quote_id, "name": "quoteId"}],
            "relatedContactInformation": ORDER_CONTACT,
        })
        if o.status_code != 201:
            raise Exception(f"order rejected [{o.status_code}]: {o.text}")
        order_id = o.json()["id"]
        self.log_info(f"Order {order_id} acknowledged")

        deadline = time.time() + POLL_TIMEOUT
        while time.time() < deadline:
            svc = requests.get(f"{NAAS}/ProductInventory/v1/inventory",
                               params={"id": circuit.cid}, headers=h, timeout=10).json()["services"][0]
            if svc["bandwidth"] == mbps:
                return order_id
            time.sleep(3)
        raise Exception(f"order {order_id} did not complete within {POLL_TIMEOUT}s")
