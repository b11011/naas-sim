"""Full lifecycle walkthrough of the new-generation APIs (v0.2.0+):

  Multi-Cloud Gateway:   create gateway -> wait PROVISIONED -> add interface
  Ethernet Fabric Connect: hosted AWS connection -> wait active -> price menu
                           -> bandwidth change -> billing -> teardown

Demonstrates the two things every MCG/EFC integration must handle:
polling (no webhooks exist on these APIs) and RFC 7807 problem+json errors.

Usage:
    python examples/mcg_efc_walkthrough.py
    NAAS_BASE_URL=http://192.168.86.113:8080 python examples/mcg_efc_walkthrough.py
"""
import os
import sys
import time

import httpx

BASE_URL = os.getenv("NAAS_BASE_URL", "http://localhost:8080")
CLIENT_ID = os.getenv("NAAS_CLIENT_ID", "naas-lab-client")
CLIENT_SECRET = os.getenv("NAAS_CLIENT_SECRET", "naas-lab-secret")
BILLING = {"customer_number": "1-ABCD", "billing_account_number": "1-1ABCDE-F"}


def die_on_problem(resp: httpx.Response, context: str):
    """New-generation APIs return RFC 7807 problem+json on errors."""
    if resp.status_code < 400:
        return
    if resp.headers.get("content-type", "").startswith("application/problem+json"):
        p = resp.json()
        sys.exit(f"{context}: {p['status']} {p['title']} — {p.get('detail', '')}")
    sys.exit(f"{context}: HTTP {resp.status_code}: {resp.text}")


def wait_for_state(client, url, field, target, timeout=120, context=""):
    """No webhooks on MCG/EFC — poll the resource until it reaches the target state."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get(url)
        die_on_problem(resp, f"polling {context}")
        state = resp.json()[field]
        if state == target:
            return resp.json()
        print(f"  ... {context}: {state}")
        time.sleep(2)
    sys.exit(f"{context} did not reach {target} within {timeout}s")


def main():
    with httpx.Client(base_url=BASE_URL, timeout=15) as anon:
        tok = anon.post("/oauth/v2/token", data={"grant_type": "client_credentials"},
                        auth=(CLIENT_ID, CLIENT_SECRET))
        die_on_problem(tok, "authentication")
        token = tok.json()["access_token"]

    with httpx.Client(base_url=BASE_URL, timeout=15,
                      headers={"Authorization": f"Bearer {token}"}) as api:
        # -- 1. Create a gateway (201 + Location; PENDING/PROVISIONING -> PROVISIONED)
        r = api.post("/mcgw/v1/gateways", json={
            "name": "walkthrough-gw", "tier": "10 Gbps", "term": "Monthly", **BILLING})
        die_on_problem(r, "create gateway")
        gw = r.json()
        print(f"gateway {gw['gateway_id']} created: {gw['state']} "
              f"(Location: {r.headers.get('location')})")
        gw = wait_for_state(api, f"/mcgw/v1/gateways/{gw['gateway_id']}",
                            "state", "PROVISIONED", context="gateway")
        print(f"gateway PROVISIONED (tier {gw['tier']}, aggregate {gw['total_aggregate_bw']})")

        # -- 2. Add an interface (202; PROVISIONING -> PROVISIONED)
        r = api.post(f"/mcgw/v1/gateways/{gw['gateway_id']}/interfaces",
                     json={"name": "walkthrough-if"})
        die_on_problem(r, "create interface")
        iface = wait_for_state(
            api, f"/mcgw/v1/gateways/{gw['gateway_id']}/interfaces/{r.json()['interface_id']}",
            "state", "PROVISIONED", context="interface")
        print(f"interface {iface['interface_id']} PROVISIONED")

        # -- 3. Hosted AWS connection onto that interface (202; provisioning -> active)
        r = api.post("/fabric/v1/connections/cloud/hosted/aws", json={
            "name": "walkthrough-aws", "bandwidth": 1000, "term": "Hourly",
            "class_of_service": "basic", **BILLING,
            "source_endpoint": {"gateway": {"gateway_id": gw["gateway_id"]},
                                "interface": {"interface_id": iface["interface_id"]}},
            "dest_endpoint": {"aws_account_id": "123456789012", "region": "us-east-1"},
        })
        die_on_problem(r, "create connection")
        cid = r.json()["connection_id"]
        wait_for_state(api, f"/fabric/v1/connections/{cid}", "state", "active",
                       context="connection")
        print(f"connection {cid} active at 1000 Mbps")

        # -- 4. Shop the priced bandwidth menu, then change bandwidth (PATCH -> updating)
        options = api.get(f"/fabric/v1/connections/{cid}/bandwidths").json()
        pick = next(o for o in options if o["bandwidth"] == 2000)
        print(f"2000 Mbps options: {pick['options'][0]['category']} basic = "
              f"${pick['options'][0]['price_monthly']}/mo")
        r = api.patch(f"/fabric/v1/connections/cloud/hosted/aws/{cid}",
                      json={"bandwidth": 2000})
        die_on_problem(r, "bandwidth change")
        wait_for_state(api, f"/fabric/v1/connections/{cid}", "state", "active",
                       context="bandwidth change")
        print("bandwidth now:",
              api.get(f"/fabric/v1/connections/{cid}").json()["bandwidth"], "Mbps")

        # -- 5. Billing reflects the change; gateway aggregate updated
        print("billing:", api.get(f"/fabric/v1/billing/{cid}").json())
        print("gateway aggregate:",
              api.get(f"/mcgw/v1/gateways/{gw['gateway_id']}").json()["total_aggregate_bw"])

        # -- 6. Demonstrate a guardrail on purpose: bandwidth not in the enum
        r = api.patch(f"/fabric/v1/connections/cloud/hosted/aws/{cid}",
                      json={"bandwidth": 123})
        print(f"guardrail demo -> {r.status_code} {r.json()['title']}: {r.json()['detail'][:60]}...")

        # -- 7. Teardown in dependency order (connection -> interface -> gateway)
        api.delete(f"/fabric/v1/connections/cloud/hosted/aws/{cid}")
        wait_for = time.time() + 60
        while api.get(f"/fabric/v1/connections/{cid}").status_code != 404:
            if time.time() > wait_for:
                sys.exit("connection deletion timed out")
            time.sleep(2)
        api.delete(f"/mcgw/v1/gateways/{gw['gateway_id']}/interfaces/{iface['interface_id']}")
        time.sleep(3)
        r = api.delete(f"/mcgw/v1/gateways/{gw['gateway_id']}")
        die_on_problem(r, "delete gateway")
        print("teardown complete — walkthrough finished")


if __name__ == "__main__":
    main()
