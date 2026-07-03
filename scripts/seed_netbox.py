#!/usr/bin/env python3
"""Seed NetBox with the NaaS-sim circuit model. Idempotent — safe to re-run.

Creates: custom fields (naas_status, user_email, customer_number), provider
Lumen, provider account 5-WX3EDQ, circuit types eod-evc / iod-dia, and one
NetBox circuit per simulator seed object. With --from-sim it instead mirrors
whatever EVCs and services currently exist in the simulator.

Usage:
    NETBOX_TOKEN=<token> python scripts/seed_netbox.py
    NETBOX_TOKEN=<token> python scripts/seed_netbox.py --from-sim

Env: NETBOX_URL (default http://localhost:8000), NETBOX_TOKEN (required),
     NAAS_BASE_URL (default http://localhost:8080, only with --from-sim)
"""
import argparse
import os
import sys

import httpx

NETBOX_URL = os.getenv("NETBOX_URL", "http://localhost:8000")
NAAS_URL = os.getenv("NAAS_BASE_URL", "http://localhost:8080")

CUSTOM_FIELDS = [
    {"name": "naas_status", "label": "NaaS status", "type": "text"},
    {"name": "user_email", "label": "NaaS user email", "type": "text"},
    {"name": "customer_number", "label": "NaaS customer number", "type": "text"},
]

# Mirrors simulator/state.py seed data
SEED_CIRCUITS = [
    {"cid": "e27a48de-7ab1-46dc-a0b0-a0abea016b5d", "type": "eod-evc",
     "mbps": 50, "description": "lab-aws-connection (sim seed EVC)",
     "custom_fields": {"naas_status": "ACTIVE", "user_email": "user@email.com"}},
    {"cid": "771234567", "type": "iod-dia",
     "mbps": 100, "description": "My first NaaS order (sim seed service)",
     "custom_fields": {"naas_status": "active", "customer_number": "1-ABCDE"}},
]


class NetBox:
    def __init__(self, url: str, token: str):
        # NetBox 4.5+ v2 tokens (nbt_<key>.<secret>) use the Bearer scheme;
        # legacy v1 tokens use "Token <secret>".
        scheme = "Bearer" if token.startswith("nbt_") else "Token"
        self.client = httpx.Client(
            base_url=url.rstrip("/") + "/api",
            headers={"Authorization": f"{scheme} {token}", "Content-Type": "application/json"},
            timeout=15,
        )

    def get_or_create(self, path: str, lookup: dict, payload: dict) -> tuple[dict, bool]:
        """Return (object, created). Looks up first so re-runs are no-ops."""
        resp = self.client.get(path, params=lookup)
        resp.raise_for_status()
        existing = resp.json()["results"]
        if existing:
            return existing[0], False
        resp = self.client.post(path, json=payload)
        if resp.status_code >= 400:
            sys.exit(f"POST {path} failed [{resp.status_code}]: {resp.text}")
        return resp.json(), True

    def ensure_custom_field(self, field: dict) -> bool:
        payload = {**field, "object_types": ["circuits.circuit"]}
        resp = self.client.get("/extras/custom-fields/", params={"name": field["name"]})
        resp.raise_for_status()
        if resp.json()["results"]:
            return False
        resp = self.client.post("/extras/custom-fields/", json=payload)
        if resp.status_code == 400 and "content_types" in resp.text:
            # NetBox < 4.0 used content_types instead of object_types
            payload = {**field, "content_types": ["circuits.circuit"]}
            resp = self.client.post("/extras/custom-fields/", json=payload)
        if resp.status_code >= 400:
            sys.exit(f"custom field {field['name']} failed [{resp.status_code}]: {resp.text}")
        return True


def log(created: bool, kind: str, name: str):
    print(f"  {'created' if created else 'exists '}  {kind}: {name}")


def circuits_from_sim() -> list[dict]:
    state = httpx.get(f"{NAAS_URL}/_lab/state", timeout=10).json()
    circuits = []
    for evc in state["evcs"]:
        circuits.append({
            "cid": evc["evcId"], "type": "eod-evc", "mbps": evc["bandwidth"],
            "description": evc.get("evcName") or "sim EVC",
            "custom_fields": {"naas_status": evc["status"],
                              "user_email": evc.get("userEmail") or "user@email.com"},
        })
    for svc in state["services"]:
        circuits.append({
            "cid": svc["id"], "type": "iod-dia", "mbps": svc["bandwidth"],
            "description": svc.get("name") or "sim service",
            "custom_fields": {"naas_status": svc["status"],
                              "customer_number": svc.get("customerNumber") or "1-ABCDE"},
        })
    return circuits


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from-sim", action="store_true",
                        help="mirror the simulator's current EVCs/services instead of the static seed list")
    args = parser.parse_args()

    token = os.getenv("NETBOX_TOKEN")
    if not token:
        sys.exit("NETBOX_TOKEN environment variable is required")

    nb = NetBox(NETBOX_URL, token)
    print(f"Seeding {NETBOX_URL} ...")

    for field in CUSTOM_FIELDS:
        log(nb.ensure_custom_field(field), "custom field", field["name"])

    provider, created = nb.get_or_create(
        "/circuits/providers/", {"slug": "lumen"}, {"name": "Lumen", "slug": "lumen"})
    log(created, "provider", "Lumen")

    _, created = nb.get_or_create(
        "/circuits/provider-accounts/", {"account": "5-WX3EDQ"},
        {"provider": provider["id"], "account": "5-WX3EDQ", "name": "EOD billing account"})
    log(created, "provider account", "5-WX3EDQ")

    type_ids = {}
    for name, slug in [("Ethernet On-Demand EVC", "eod-evc"), ("Internet On-Demand DIA", "iod-dia")]:
        ctype, created = nb.get_or_create(
            "/circuits/circuit-types/", {"slug": slug}, {"name": name, "slug": slug})
        type_ids[slug] = ctype["id"]
        log(created, "circuit type", slug)

    circuits = circuits_from_sim() if args.from_sim else SEED_CIRCUITS
    for c in circuits:
        _, created = nb.get_or_create(
            "/circuits/circuits/", {"cid": c["cid"]},
            {"cid": c["cid"], "provider": provider["id"], "type": type_ids[c["type"]],
             "status": "active", "commit_rate": c["mbps"] * 1000,
             "description": c["description"], "custom_fields": c["custom_fields"]})
        log(created, "circuit", f"{c['cid']} ({c['mbps']} Mbps, {c['type']})")

    print("Done.")


if __name__ == "__main__":
    main()
