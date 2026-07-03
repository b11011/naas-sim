"""Modify Internet On-Demand bandwidth via the full qualify → quote → order flow.

Usage:
    python examples/iod_change_bandwidth.py --speed 200
    python examples/iod_change_bandwidth.py --service-id 771234567 --speed 500
"""
import argparse
import os
import sys
import time

import httpx

BASE_URL = os.getenv("NAAS_BASE_URL", "http://localhost:8080")
CLIENT_ID = os.getenv("NAAS_CLIENT_ID", "naas-lab-client")
CLIENT_SECRET = os.getenv("NAAS_CLIENT_SECRET", "naas-lab-secret")
CUSTOMER_NUMBER = os.getenv("NAAS_CUSTOMER_NUMBER", "1-ABCDE")


def get_token(client: httpx.Client) -> str:
    resp = client.post("/oauth/v2/token", data={"grant_type": "client_credentials"},
                       auth=(CLIENT_ID, CLIENT_SECRET))
    resp.raise_for_status()
    return resp.json()["access_token"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service-id", default="771234567",
                        help="inventory service to modify (default: the seeded lab service)")
    parser.add_argument("--speed", type=int, required=True, help="new speed in Mbps")
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()

    with httpx.Client(base_url=BASE_URL, timeout=10) as client:
        headers = {
            "Authorization": f"Bearer {get_token(client)}",
            "x-customer-number": CUSTOMER_NUMBER,
        }

        # Locate the service in inventory
        inv = client.get("/ProductInventory/v1/inventory",
                         params={"id": args.service_id}, headers=headers)
        inv.raise_for_status()
        services = inv.json()["services"]
        if not services:
            print(f"service {args.service_id} not found in inventory", file=sys.stderr)
            sys.exit(1)
        service = services[0]
        print(f"service {service['id']} ({service['name']}): {service['bandwidth']} Mbps")

        # Step 1: qualify — confirm the speed is offered at this location
        price = client.get("/Product/v1/price", headers=headers,
                           params={"productCode": "718", "masterSiteId": service["masterSiteId"],
                                   "serviceId": service["id"]})
        price.raise_for_status()
        speeds = [o["speed"] for o in price.json()["offerings"]]
        if args.speed not in speeds:
            print(f"speed {args.speed} not offered; available: {speeds}", file=sys.stderr)
            sys.exit(1)

        # Step 2: price — get a quote (valid 15 minutes)
        quote_resp = client.post("/Product/v1/priceRequest", headers=headers, json={
            "sourceSystem": "NaaS ExternalApi",
            "customerNumber": CUSTOMER_NUMBER,
            "currencyCode": "USD",
            "masterSiteId": service["masterSiteId"],
            "productCode": "718",
            "serviceId": service["id"],
            "productName": "Internet On-Demand",
            "speed": f"{args.speed} Mbps",
        })
        quote_resp.raise_for_status()
        quote = quote_resp.json()
        print(f"quote {quote['quoteId']} for {quote['speed']} "
              f"(${quote['price']['monthlyRecurring']}/mo)")

        # Step 3: order — promote the quote to a modify order
        order_resp = client.post("/Customer/v3/Ordering/orderRequest", headers=headers, json={
            "externalId": f"EXT-{int(time.time())}",
            "billingAccount": {"id": service.get("billingAccountId"), "name": "NaaS Lab Account"},
            "channel": [{"id": 99, "name": "NaaS ExternalApi"}],
            "note": [{"text": "lab bandwidth change"}],
            "productOrderItem": [{
                "id": "Order1",
                "quantity": 1,
                "action": "modify",
                "product": {
                    "id": service["id"],
                    "productCharacteristic": [],
                    "productSpecification": {"id": "5001", "name": "NaaS Internet"},
                },
                "productOffering": {"id": "718", "name": "Internet On-Demand"},
            }],
            "quote": [{"id": quote["quoteId"], "name": "quoteId"}],
            "relatedContactInformation": [{
                "number": "5555550100",
                "emailAddress": "first.lastname@domain.com",
                "role": "Order Contact",
                "organization": "Lab Org",
                "name": "FirstName LastName",
            }],
        })
        if order_resp.status_code != 201:
            print(f"order failed [{order_resp.status_code}]: {order_resp.text}", file=sys.stderr)
            sys.exit(1)
        order = order_resp.json()
        print(f"order {order['id']} {order['state']}")

        # Step 4: poll inventory until the new bandwidth is live
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            svc = client.get("/ProductInventory/v1/inventory",
                             params={"id": args.service_id}, headers=headers).json()["services"][0]
            if svc["bandwidth"] == args.speed:
                print(f"done: service {svc['id']} now at {svc['bandwidth']} Mbps")
                return
            print(f"  waiting... bandwidth={svc['bandwidth']}")
            time.sleep(2)

        print("timed out waiting for the order to complete", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
