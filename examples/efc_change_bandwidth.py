"""Change bandwidth on an Ethernet Fabric Connect connection and poll to active.

The EFC counterpart of eod_change_bandwidth.py: PATCH with an enum value,
then poll (the new APIs have no webhooks). Defaults to the seeded connection.

Usage:
    python examples/efc_change_bandwidth.py --bandwidth 2000
    python examples/efc_change_bandwidth.py --connection-id <id> --bandwidth 5000
"""
import argparse
import os
import sys
import time

import httpx

BASE_URL = os.getenv("NAAS_BASE_URL", "http://localhost:8080")
CLIENT_ID = os.getenv("NAAS_CLIENT_ID", "naas-lab-client")
CLIENT_SECRET = os.getenv("NAAS_CLIENT_SECRET", "naas-lab-secret")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection-id", default="d2bdc87d-3f76-4db6-8d2a-1f5f4b3fbbf2",
                        help="fabric connection to modify (default: the seeded hosted-AWS one)")
    parser.add_argument("--bandwidth", type=int, required=True,
                        help="new bandwidth in Mbps (must be a value from the spec enum)")
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()

    with httpx.Client(base_url=BASE_URL, timeout=15) as client:
        resp = client.post("/oauth/v2/token", data={"grant_type": "client_credentials"},
                           auth=(CLIENT_ID, CLIENT_SECRET))
        resp.raise_for_status()
        client.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"

        conn = client.get(f"/fabric/v1/connections/{args.connection_id}")
        if conn.status_code == 404:
            sys.exit(f"connection {args.connection_id} not found")
        conn = conn.json()
        print(f"current: {conn['bandwidth']} Mbps ({conn['state']}, {conn['connection_type']})")

        # the connection-type path segment is required for mutations
        cloud_path = conn["connection_type"].replace("hosted-", "cloud/hosted/") \
            if conn["connection_type"].startswith("hosted-") else "virtual/p2p"
        resp = client.patch(f"/fabric/v1/connections/{cloud_path}/{args.connection_id}",
                            json={"bandwidth": args.bandwidth})
        if resp.status_code >= 400:
            p = resp.json()  # problem+json
            sys.exit(f"rejected [{p['status']}] {p['title']}: {p.get('detail', '')}")
        print(f"accepted: state {resp.json()['state']}")

        deadline = time.time() + args.timeout
        while time.time() < deadline:
            c = client.get(f"/fabric/v1/connections/{args.connection_id}").json()
            if c["state"] == "active" and c["bandwidth"] == args.bandwidth:
                print(f"done: {args.connection_id} active at {c['bandwidth']} Mbps")
                return
            print(f"  waiting... state={c['state']} bandwidth={c['bandwidth']}")
            time.sleep(2)
        sys.exit("timed out waiting for the change to complete")


if __name__ == "__main__":
    main()
