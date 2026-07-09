"""Async transitions and webhook delivery.

`schedule()` must be called from inside an async request handler (the event
loop must be running), which is why every simulator endpoint is `async def`.
"""
import asyncio
import logging

import httpx

from .state import new_id, now_iso, store

log = logging.getLogger("naas-sim")


def emit(event_type: str, payload: dict):
    """Record an event and fan it out to every registered webhook."""
    event = {"eventId": new_id(), "eventType": event_type, "eventTime": now_iso(), "event": payload}
    store.events.append(event)
    for hook in store.webhooks:
        asyncio.create_task(_deliver(hook["callback"], event))


async def _deliver(url: str, event: dict):
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            await client.post(url, json=event)
    except Exception as exc:
        log.warning("webhook delivery to %s failed: %s", url, exc)


def schedule(delay_seconds: float, fn):
    """Run sync callable `fn` after `delay_seconds` on the event loop."""
    async def runner():
        try:
            await asyncio.sleep(delay_seconds)
            fn()
            store.save()
        except Exception:
            log.exception("scheduled transition failed")

    asyncio.create_task(runner())
