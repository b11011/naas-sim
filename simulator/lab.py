"""Lab-only control endpoints (not part of the Lumen API surface).

These are unauthenticated on purpose so you can inspect and reset the
simulator without a token while developing.
"""
from fastapi import APIRouter

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
    store.reset()
    return {"status": "reset", "message": "simulator state restored to seed data"}
