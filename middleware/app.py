"""NetBox → NaaS reconciler (Phase 2 of the naas-sim lab).

Listens for NetBox event-rule webhooks; when a circuit's commit_rate changes,
pushes the bandwidth change to the NaaS simulator (EOD PATCH or IOD
quote→order by circuit type slug), then closes the loop on the simulator's
completion webhook by updating naas_status and journaling in NetBox.

Config (env): NAAS_BASE_URL, NETBOX_URL, NETBOX_TOKEN (or token file at
~/.config/naas-sim-netbox-token), SELF_URL.
"""
import logging
import os
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, Request

NAAS = os.getenv("NAAS_BASE_URL", "http://127.0.0.1:8080")
NETBOX = os.getenv("NETBOX_URL", "http://localhost:8000")
SELF_URL = os.getenv("SELF_URL", "http://localhost:8090")  # how the sim reaches us
CLIENT_ID = os.getenv("NAAS_CLIENT_ID", "naas-lab-client")
CLIENT_SECRET = os.getenv("NAAS_CLIENT_SECRET", "naas-lab-secret")

TOKEN_FILE = Path.home() / ".config" / "naas-sim-netbox-token"
NETBOX_TOKEN = os.getenv("NETBOX_TOKEN") or (
    TOKEN_FILE.read_text().strip() if TOKEN_FILE.exists() else "")
if not NETBOX_TOKEN:
    raise SystemExit("set NETBOX_TOKEN or create ~/.config/naas-sim-netbox-token")

ORDER_CONTACT = [{"number": "5555550100", "emailAddress": "lab@example.com",
                  "role": "Order Contact", "organization": "Lab", "name": "FirstName LastName"}]

log = logging.getLogger("uvicorn.error")
app = FastAPI(title="naas-middleware")
pending = {}  # sim request/order id -> {"circuit_id", "cid", "mbps"}


def naas_token() -> dict:
    r = httpx.post(f"{NAAS}/oauth/v2/token", auth=(CLIENT_ID, CLIENT_SECRET),
                   data={"grant_type": "client_credentials"}, timeout=10)
    r.raise_for_status()
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def nb_headers() -> dict:
    # NetBox >= 4.5 v2 tokens (nbt_...) use Bearer; legacy v1 used "Token <secret>"
    scheme = "Bearer" if NETBOX_TOKEN.startswith("nbt_") else "Token"
    return {"Authorization": f"{scheme} {NETBOX_TOKEN}", "Content-Type": "application/json"}


@app.on_event("startup")
def register_sim_webhook():
    """Subscribe to the sim's completion events (idempotent across restarts)."""
    auth = naas_token()
    hooks = httpx.get(f"{NAAS}/Customer/v3/Ordering/notifications", headers=auth).json()["webhooks"]
    if not any(h["callback"] == f"{SELF_URL}/naas-events" for h in hooks):
        httpx.post(f"{NAAS}/Customer/v3/Ordering/notifications", headers=auth,
                   json={"callback": f"{SELF_URL}/naas-events"})
        log.info("registered sim webhook -> %s/naas-events", SELF_URL)


# ---------- inbound from NetBox -----------------------------------------
@app.post("/netbox-hook")
async def netbox_hook(request: Request):
    payload = await request.json()
    snap = payload.get("snapshots") or {}
    pre, post = snap.get("prechange") or {}, snap.get("postchange") or {}
    data = payload.get("data", {})

    # Only act when commit_rate actually changed (also breaks the loop:
    # our own naas_status writeback fires this hook again, but rate is equal then)
    if pre.get("commit_rate") == post.get("commit_rate"):
        return {"action": "ignored", "reason": "commit_rate unchanged"}

    cid = data["cid"]
    mbps = int(post["commit_rate"]) // 1000
    slug = (data.get("type") or {}).get("slug", "")
    cf = data.get("custom_fields") or {}
    log.info("circuit %s: commit_rate -> %s Mbps (%s)", cid, mbps, slug)

    auth = naas_token()
    try:
        if slug == "eod-evc":
            h = {**auth, "x-billing-account-number": "5-WX3EDQ"}
            r = httpx.patch(f"{NAAS}/Network/v5/DynamicConnection/evcs/{cid}", headers=h,
                            json={"bandwidth": mbps,
                                  "userEmail": cf.get("user_email") or "user@email.com"}, timeout=10)
            r.raise_for_status()
            ref = r.json()["evcRequestId"]
        elif slug == "iod-dia":
            h = {**auth, "x-customer-number": cf.get("customer_number") or "1-ABCDE"}
            svc = httpx.get(f"{NAAS}/ProductInventory/v1/inventory", params={"id": cid},
                            headers=h, timeout=10).json()["services"][0]
            q = httpx.post(f"{NAAS}/Product/v1/priceRequest", headers=h, timeout=10,
                           json={"productCode": "718", "masterSiteId": svc["masterSiteId"],
                                 "serviceId": cid, "speed": f"{mbps} Mbps"})
            q.raise_for_status()
            o = httpx.post(f"{NAAS}/Customer/v3/Ordering/orderRequest", headers=h, timeout=10, json={
                "externalId": f"NB-{int(time.time())}",
                "productOrderItem": [{"id": "Order1", "quantity": 1, "action": "modify",
                                      "product": {"id": cid, "productCharacteristic": []},
                                      "productOffering": {"id": "718", "name": "Internet On-Demand"}}],
                "quote": [{"id": q.json()["quoteId"], "name": "quoteId"}],
                "relatedContactInformation": ORDER_CONTACT})
            o.raise_for_status()
            ref = o.json()["id"]
        else:
            return {"action": "ignored", "reason": f"type {slug} not NaaS-managed"}
    except httpx.HTTPStatusError as exc:
        journal(data["id"], f"NaaS change to {mbps} Mbps REJECTED: {exc.response.text}", kind="danger")
        set_status(data["id"], "FAILED")
        return {"action": "failed", "detail": exc.response.text}

    pending[ref] = {"circuit_id": data["id"], "cid": cid, "mbps": mbps}
    set_status(data["id"], "MODIFYING")
    return {"action": "submitted", "ref": ref}


# ---------- inbound from the simulator -----------------------------------
@app.post("/naas-events")
async def naas_events(request: Request):
    event = await request.json()
    body = event.get("event", {})
    ref = body.get("evcRequestId") or body.get("haEvcRequestId") or body.get("orderId")
    job = pending.pop(ref, None)
    if not job:
        return {"action": "ignored"}
    log.info("completion for %s (%s Mbps)", job["cid"], job["mbps"])
    set_status(job["circuit_id"], "ACTIVE")
    journal(job["circuit_id"],
            f"NaaS confirmed {job['mbps']} Mbps on `{job['cid']}` "
            f"(event `{event['eventType']}`, ref `{ref}`).")
    return {"action": "closed"}


# ---------- NetBox writeback ---------------------------------------------
def set_status(circuit_id: int, status: str):
    httpx.patch(f"{NETBOX}/api/circuits/circuits/{circuit_id}/", headers=nb_headers(),
                json={"custom_fields": {"naas_status": status}}, timeout=10)


def journal(circuit_id: int, text: str, kind: str = "success"):
    httpx.post(f"{NETBOX}/api/extras/journal-entries/", headers=nb_headers(), timeout=10,
               json={"assigned_object_type": "circuits.circuit",
                     "assigned_object_id": circuit_id, "kind": kind, "comments": text})
