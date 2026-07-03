"""Internet On-Demand API — TMF-style qualify → quote → order flow.

Bandwidth on demand here is a three-step process, as on the real platform:
  1. GET  /Product/v1/price            — speeds available at a location/service
  2. POST /Product/v1/priceRequest     — returns a quote id valid for 15 minutes
  3. POST /Customer/v3/Ordering/orderRequest — action "modify" + quote id

Real-platform behaviors reproduced: 24 change requests/day per customer
(GMT reset), quote expiry, the Order Contact validation rules from the docs,
no bandwidth updates on Flexential sites, async order completion + webhooks.
"""
import random
import time

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query

from . import config
from .auth import require_token
from .events import emit, schedule
from .state import SPEEDS_MBPS, gmt_date, new_id, now_iso, price_for, store

router = APIRouter(dependencies=[Depends(require_token)], tags=["Internet On-Demand"])

PRODUCT_CODE = "718"


async def require_customer(
        x_customer_number: str = Header(..., description="Lumen customer ID (BusOrg number), e.g. 1-ABCDE")):
    return x_customer_number


def _location(master_site_id: str) -> dict:
    loc = next((loc for loc in store.locations if loc["masterSiteId"] == master_site_id), None)
    if not loc:
        raise HTTPException(404, f"location {master_site_id} not found")
    return loc


def _parse_speed(speed: str) -> int:
    try:
        value = int(str(speed).lower().replace("mbps", "").strip())
    except ValueError:
        raise HTTPException(400, f'invalid speed "{speed}"; expected e.g. "100 Mbps"')
    if value not in SPEEDS_MBPS:
        raise HTTPException(400, f"speed {value} Mbps not offered; available: {SPEEDS_MBPS}")
    return value


# ---------------------------------------------------------------- Qualify

@router.get("/Network/v1/provider")
async def get_providers():
    return {"providers": [{"providerId": "LUMN", "name": "Lumen Technologies", "naasEnabled": True}]}


@router.get("/Network/v1/location")
async def get_locations(searchText: str | None = Query(default=None),
                        masterSiteId: str | None = Query(default=None)):
    locations = store.locations
    if masterSiteId:
        locations = [loc for loc in locations if loc["masterSiteId"] == masterSiteId]
    if searchText:
        needle = searchText.lower()
        locations = [loc for loc in locations
                     if needle in loc["name"].lower() or needle in loc["address"].lower()]
    return {"locations": locations}


@router.get("/Product/v1/price")
async def get_price(productCode: str = Query(...), masterSiteId: str = Query(...),
                    partnerId: str | None = Query(default=None),
                    serviceId: str | None = Query(default=None)):
    if productCode != PRODUCT_CODE:
        raise HTTPException(400, f"unknown productCode {productCode}; Internet On-Demand is {PRODUCT_CODE}")
    location = _location(masterSiteId)
    return {
        "productCode": PRODUCT_CODE,
        "productName": "Internet On-Demand",
        "masterSiteId": masterSiteId,
        "naasEnabled": location["naasEnabled"],
        "offerings": [
            {"speed": s, "displayValue": f"{s} Mbps", "unit": "Mbps", "price": price_for(s)}
            for s in SPEEDS_MBPS
        ],
    }


@router.post("/Product/v1/priceRequest")
async def create_price_request(payload: dict = Body(...), customer: str = Depends(require_customer)):
    if payload.get("productCode") != PRODUCT_CODE:
        raise HTTPException(400, f"unknown productCode; Internet On-Demand is {PRODUCT_CODE}")
    speed = _parse_speed(payload.get("speed", ""))
    master_site_id = payload.get("masterSiteId")
    if master_site_id:
        _location(master_site_id)

    quote = {
        "quoteId": store.next_quote_id(),
        "customerNumber": customer,
        "speedMbps": speed,
        "masterSiteId": master_site_id,
        "serviceId": payload.get("serviceId"),
        "partnerId": payload.get("partnerId"),
        "price": price_for(speed),
        "createdAt": time.time(),
        "expiresAt": time.time() + config.QUOTE_TTL_SECONDS,
    }
    store.quotes[quote["quoteId"]] = quote
    return {
        "quoteId": quote["quoteId"],
        "status": "VALIDATED",
        "speed": f"{speed} Mbps",
        "price": quote["price"],
        "validForSeconds": int(config.QUOTE_TTL_SECONDS),
        "createdDateTime": now_iso(),
    }


# ---------------------------------------------------------------- Order

def _valid_quote(quote_id: str) -> dict:
    quote = store.quotes.get(quote_id)
    if not quote:
        raise HTTPException(400, f"quote {quote_id} not found; create one with POST /Product/v1/priceRequest")
    if quote["expiresAt"] < time.time():
        raise HTTPException(400, f"quote {quote_id} has expired; quotes are valid for "
                                 f"{int(config.QUOTE_TTL_SECONDS // 60)} minutes")
    return quote


def _validate_contacts(payload: dict):
    contacts = payload.get("relatedContactInformation") or []
    order_contact = next((c for c in contacts if c.get("role") == "Order Contact"), None)
    if not order_contact:
        raise HTTPException(400, "relatedContactInformation must include an entry with role 'Order Contact'")
    name = order_contact.get("name", "")
    if " " not in name.strip():
        raise HTTPException(400, "Order Contact name must include a first and last name separated by a space")


def _characteristic(item: dict, name: str):
    for c in (item.get("product") or {}).get("productCharacteristic") or []:
        if c.get("name") == name:
            return c.get("value")
    return None


def _enforce_quota(customer: str):
    key = (customer, gmt_date())
    count = store.order_counts.get(key, 0)
    if count >= config.DAILY_ORDER_LIMIT:
        raise HTTPException(429, f"you cannot send more than {config.DAILY_ORDER_LIMIT} change requests "
                                 f"in a day; the quota resets at the start of the day (GMT)")
    store.order_counts[key] = count + 1


@router.post("/Customer/v3/Ordering/orderRequest", status_code=201)
async def create_order(payload: dict = Body(...), customer: str = Depends(require_customer)):
    items = payload.get("productOrderItem") or []
    if not items:
        raise HTTPException(400, "productOrderItem is required")
    _validate_contacts(payload)
    _enforce_quota(customer)

    item = items[0]
    action = item.get("action")
    quote_ids = [q.get("id") for q in payload.get("quote") or [] if q.get("id")]

    order = {
        "id": new_id(),
        "externalId": payload.get("externalId"),
        "state": "acknowledged",
        "orderDate": now_iso(),
        "completionDate": None,
        "customerNumber": customer,
        "action": action,
        "serviceId": None,
        "requestedBandwidth": None,
    }

    if action == "add":
        if not quote_ids:
            raise HTTPException(
                400, "an 'add' order requires a quote; create one with POST /Product/v1/priceRequest")
        quote = _valid_quote(quote_ids[0])
        service_id = f"77{random.randint(1_000_000, 9_999_999)}"
        service = {
            "id": service_id,
            "name": _characteristic(item, "Customer Service Name") or f"NaaS service {service_id}",
            "status": "in progress",
            "customerNumber": customer,
            "billingAccountId": (payload.get("billingAccount") or {}).get("id"),
            "productOffering": {"id": PRODUCT_CODE, "name": "Internet On-Demand"},
            "productSpecification": {"id": "5001", "name": "NaaS Internet"},
            "masterSiteId": quote["masterSiteId"],
            "mUniServiceId": quote["serviceId"],
            "bandwidth": quote["speedMbps"],
            "bandwidthUnit": "Mbps",
            "ipv4Prefix": "203.0.113.0/29",
            "createdDateTime": now_iso(),
            "modifiedDateTime": None,
        }
        store.services[service_id] = service
        order["serviceId"] = service_id
        order["requestedBandwidth"] = quote["speedMbps"]

        def complete():
            service["status"] = "active"
            order["state"], order["completionDate"] = "completed", now_iso()
            emit("productOrderStateChangeEvent", {"orderId": order["id"], "state": "completed",
                                                  "serviceId": service_id, "action": "add"})

    elif action == "modify":
        service_id = (item.get("product") or {}).get("id")
        service = store.services.get(service_id)
        if not service:
            raise HTTPException(404, f"service {service_id} not found in inventory")
        order["serviceId"] = service_id
        new_name = _characteristic(item, "Customer Service Name")

        if quote_ids:  # bandwidth change
            location = next((loc for loc in store.locations
                             if loc["masterSiteId"] == service.get("masterSiteId")), None)
            if location and location["partner"] == "Flexential":
                raise HTTPException(400, "bandwidth update is not available on Flexential sites")
            quote = _valid_quote(quote_ids[0])
            order["requestedBandwidth"] = quote["speedMbps"]

            def complete():
                service["bandwidth"] = quote["speedMbps"]
                service["modifiedDateTime"] = now_iso()
                order["state"], order["completionDate"] = "completed", now_iso()
                emit("productOrderStateChangeEvent", {"orderId": order["id"], "state": "completed",
                                                      "serviceId": service_id, "action": "modify",
                                                      "bandwidth": quote["speedMbps"]})
        elif new_name:  # rename only
            def complete():
                service["name"] = new_name
                service["modifiedDateTime"] = now_iso()
                order["state"], order["completionDate"] = "completed", now_iso()
                emit("productOrderStateChangeEvent", {"orderId": order["id"], "state": "completed",
                                                      "serviceId": service_id, "action": "modify"})
        else:
            raise HTTPException(400, "nothing to modify: provide a quote (bandwidth change) or a "
                                     "'Customer Service Name' product characteristic (rename)")

    elif action == "delete":
        service_id = (item.get("product") or {}).get("id")
        service = store.services.get(service_id)
        if not service:
            raise HTTPException(404, f"service {service_id} not found in inventory")
        order["serviceId"] = service_id
        service["status"] = "disconnecting"

        def complete():
            store.services.pop(service_id, None)
            order["state"], order["completionDate"] = "completed", now_iso()
            emit("productOrderStateChangeEvent", {"orderId": order["id"], "state": "completed",
                                                  "serviceId": service_id, "action": "delete"})

    else:
        raise HTTPException(400, f"unsupported action '{action}'; use add, modify, or delete")

    store.orders[order["id"]] = order
    schedule(config.TRANSITION_DELAY_SECONDS, complete)
    return order


@router.get("/Customer/v3/Ordering/orderRequest/{order_id}")
async def get_order(order_id: str, customer: str = Depends(require_customer)):
    """Lab extension: the real platform reports order state via the separate
    Order Status API and webhooks; this endpoint exists for polling convenience."""
    order = store.orders.get(order_id)
    if not order:
        raise HTTPException(404, f"order {order_id} not found")
    return order


@router.post("/Customer/v3/Ordering/notifications", status_code=201)
async def register_webhook(payload: dict = Body(...)):
    callback = payload.get("callback")
    if not callback:
        raise HTTPException(400, "callback URL is required")
    hook = {"id": new_id(), "callback": callback}
    store.webhooks.append(hook)
    return hook


@router.get("/Customer/v3/Ordering/notifications")
async def list_webhooks():
    return {"webhooks": store.webhooks}


# ---------------------------------------------------------------- Inventory

@router.get("/ProductInventory/v1/inventory")
async def get_inventory(id: str | None = Query(default=None), customer: str = Depends(require_customer)):
    services = list(store.services.values())
    if id:
        services = [s for s in services if s["id"] == id]
    else:
        services = [s for s in services if s["customerNumber"] == customer]
    return {"services": services}


@router.get("/Account/v1/billingAccount")
async def get_billing_accounts(customer: str = Depends(require_customer)):
    return {"billingAccounts": [{"id": "5-RHQBGCGK", "name": "NaaS Lab Account", "customerNumber": customer}]}


@router.get("/serviceProvisioning/v3/internet")
async def get_internet_provisioning(serviceId: str = Query(...)):
    service = store.services.get(serviceId)
    if not service:
        raise HTTPException(404, f"service {serviceId} not found")
    return {
        "serviceId": service["id"],
        "status": service["status"],
        "bandwidth": service["bandwidth"],
        "bandwidthUnit": "Mbps",
        "ipv4Prefix": service["ipv4Prefix"],
        "masterSiteId": service["masterSiteId"],
    }
