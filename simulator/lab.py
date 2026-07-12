"""Lab-only control endpoints (not part of the Lumen API surface).

These are unauthenticated on purpose so you can inspect and reset the
simulator without a token while developing.
"""
from fastapi import APIRouter, Body, HTTPException

from .metrics import metrics
from .state import store

router = APIRouter(prefix="/_lab", tags=["Lab controls"])


@router.get("/state")
async def dump_state():
    return {
        "unis": list(store.unis.values()),
        "evcs": list(store.evcs.values()),
        "haEvcs": list(store.ha_evcs.values()),
        "evcRequests": list(store.evc_requests.values()),
        "haEvcRequests": list(store.ha_evc_requests.values()),
        "locations": store.locations,
        "services": list(store.services.values()),
        "quotes": list(store.quotes.values()),
        "orders": list(store.orders.values()),
        "orderCounts": {f"{cust}@{day}": n for (cust, day), n in store.order_counts.items()},
        "webhooks": store.webhooks,
    }


@router.get("/events")
async def list_events():
    return {"events": store.events}


@router.post("/reset")
async def reset():
    # Webhook registrations are integration config, not scenario state —
    # resetting the catalog shouldn't sever consumers' completion events.
    webhooks = store.webhooks
    store.reset()
    store.webhooks = webhooks
    store.save()
    return {"status": "reset", "message": "simulator state restored to seed data",
            "webhooksPreserved": len(webhooks)}


@router.get("/metrics")
async def get_metrics():
    return metrics.snapshot()


@router.post("/metrics/reset")
async def reset_metrics():
    metrics.reset()
    return {"status": "reset"}


@router.post("/seed")
async def seed(profile: dict = Body(...)):
    """Load a catalog profile: any subset of speeds, locations, unis, evcs,
    services, partnerInterconnects. Replaces those sections; clears
    transactional state (quotes, orders, requests, events)."""
    allowed = {"speeds", "locations", "unis", "evcs", "services", "partnerInterconnects"}
    unknown = set(profile) - allowed
    if unknown:
        raise HTTPException(400, f"unknown seed sections: {sorted(unknown)}; allowed: {sorted(allowed)}")
    if not profile:
        raise HTTPException(400, "empty profile; provide at least one section")
    store.apply_seed(profile)
    return {"status": "seeded", "sections": sorted(profile),
            "counts": {"unis": len(store.unis), "evcs": len(store.evcs),
                       "services": len(store.services), "locations": len(store.locations),
                       "speeds": store.speeds}}
