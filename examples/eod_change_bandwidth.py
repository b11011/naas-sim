"""Change bandwidth on an Ethernet On-Demand EVC and poll until the change lands.

Usage:
    python examples/eod_change_bandwidth.py --bandwidth 200
    python examples/eod_change_bandwidth.py --evc-id <id> --bandwidth 500
"""
import argparse
import os
import sys
import time

import httpx

BASE_URL = os.getenv("NAAS_BASE_URL", "http://localhost:8080")
CLIENT_ID = os.getenv("NAAS_CLIENT_ID", "naas-lab-client")
CLIENT_SECRET = os.getenv("NAAS_CLIENT_SECRET", "naas-lab-secret")
BILLING_ACCOUNT = os.getenv("NAAS_BILLING_ACCOUNT", "5-WX3EDQ")


def get_token(client: httpx.Client) -> str:
    resp = client.post("/oauth/v2/token", data={"grant_type": "client_credentials"},
                       auth=(CLIENT_ID, CLIENT_SECRET))
    resp.raise_for_status()
    return resp.json()["access_token"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evc-id", default="e27a48de-7ab1-46dc-a0b0-a0abea016b5d",
                        help="EVC to modify (default: the seeded lab EVC)")
    parser.add_argument("--bandwidth", type=int, required=True, help="new bandwidth in Mbps")
    parser.add_argument("--email", default="user@email.com")
    parser.add_argument("--timeout", type=float, default=120, help="seconds to wait for completion")
    args = parser.parse_args()

    with httpx.Client(base_url=BASE_URL, timeout=10) as client:
        headers = {
            "Authorization": f"Bearer {get_token(client)}",
            "x-billing-account-number": BILLING_ACCOUNT,
        }
        evc_url = f"/Network/v5/DynamicConnection/evcs/{args.evc_id}"

        before = client.get(evc_url, headers=headers)
        before.raise_for_status()
        print(f"current: {before.json()['bandwidth']} Mbps ({before.json()['status']})")

        resp = client.patch(evc_url, headers=headers,
                            json={"bandwidth": args.bandwidth, "userEmail": args.email})
        if resp.status_code != 202:
            print(f"PATCH failed [{resp.status_code}]: {resp.text}", file=sys.stderr)
            sys.exit(1)
        body = resp.json()
        print(f"accepted: request {body['evcRequestId']} status {body['status']}")

        deadline = time.time() + args.timeout
        while time.time() < deadline:
            evc = client.get(evc_url, headers=headers).json()
            if evc["status"] == "ACTIVE" and evc["bandwidth"] == args.bandwidth:
                print(f"done: EVC {args.evc_id} is ACTIVE at {evc['bandwidth']} Mbps")
                return
            print(f"  waiting... status={evc['status']} bandwidth={evc['bandwidth']}")
            time.sleep(2)

        print("timed out waiting for the modification to complete", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
