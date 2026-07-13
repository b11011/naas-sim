"""Ethernet Fabric Connect API — /fabric/v1 (new-generation, spec v1.0.0).

L2 connections onto Multi-Cloud Gateway interfaces: virtual point-to-point
plus hosted cloud connections (AWS, GCP, Azure, OCI). Bandwidth is an enum
with priced options (GET /connections/{id}/bandwidths); changes are a PATCH
on the connection. State machine: provisioning → active → updating/deleting.
Errors are RFC 7807 problem+json. No webhooks (matching the published spec)
— poll the connection state.
"""
from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field

from . import config
from .auth import require_token
from .events import emit, schedule
from .multicloud_gateway import _refresh_aggregate, check_gateway_capacity
from .problems import ProblemException, paginate
from .state import new_id, now_iso, store

router = APIRouter(prefix="/fabric/v1", dependencies=[Depends(require_token)],
                   tags=["Ethernet Fabric Connect"])

# ConnectionBandwidth enum from the published spec (Mbps)
FABRIC_BANDWIDTHS = [10, 20, 50, 100, 200, 300, 400, 500,
                     1000, 2000, 3000, 5000, 10000, 15000, 20000, 25000]
CLOUDS = ("aws", "gcp", "azure", "oci")


# ------------------------------------------------------------------ models

class CreateP2PConnection(BaseModel):
    name: str = Field(..., max_length=255)
    description: str | None = None
    class_of_service: str = "basic"
    source_endpoint: dict
    dest_endpoint: dict
    bandwidth: int
    term: str = "Monthly"
    customer_number: str
    billing_account_number: str


class CreateHostedConnection(BaseModel):
    name: str = Field(..., max_length=255)
    description: str | None = None
    class_of_service: str = "basic"
    source_endpoint: dict
    dest_endpoint: dict
    bandwidth: int
    term: str = "Monthly"
    customer_number: str
    billing_account_number: str


class UpdateConnection(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    description: str | None = None
    bandwidth: int | None = None


# ------------------------------------------------------------------ helpers

def _connection(connection_id: str, connection_type: str | None = None) -> dict:
    c = store.fabric_connections.get(connection_id)
    if not c or (connection_type and c["connection_type"] != connection_type):
        raise ProblemException(404, "Connection not found",
                               f"No connection with id {connection_id}"
                               + (f" of type {connection_type}" if connection_type else ""))
    return c


def _validate_bandwidth(mbps: int):
    if mbps not in FABRIC_BANDWIDTHS:
        raise ProblemException(400, "Invalid bandwidth",
                               f"bandwidth must be one of {FABRIC_BANDWIDTHS}; got {mbps}")


def _gateway_ids(conn_body: dict) -> list[str]:
    ids = []
    for end in (conn_body.get("source_endpoint"), conn_body.get("dest_endpoint")):
        if isinstance(end, dict):
            gw = (end.get("gateway") or {}).get("gateway_id")
            if gw:
                ids.append(gw)
    return ids


def _validate_endpoints(body) -> None:
    """Gateway endpoints must reference a PROVISIONED gateway + interface."""
    for end in (body.source_endpoint, body.dest_endpoint):
        gw_ref = (end.get("gateway") or {}) if isinstance(end, dict) else {}
        if gw_ref:
            gw = store.gateways.get(gw_ref.get("gateway_id", ""))
            if not gw or gw["state"] == "DELETED":
                raise ProblemException(404, "Gateway not found",
                                       f"Endpoint references unknown gateway {gw_ref.get('gateway_id')}")
            if gw["state"] != "PROVISIONED":
                raise ProblemException(409, "Gateway not ready",
                                       f"Gateway {gw['gateway_id']} is {gw['state']}")
            if_ref = (end.get("interface") or {})
            if if_ref:
                iface = store.mcg_interfaces.get(if_ref.get("interface_id", ""))
                if not iface or iface["state"] == "DELETED" \
                        or iface["gateway"]["gateway_id"] != gw["gateway_id"]:
                    raise ProblemException(404, "Interface not found",
                                           f"Endpoint references unknown interface "
                                           f"{if_ref.get('interface_id')} on gateway {gw['gateway_id']}")


def price_option(mbps: int, category: str, cos: str) -> dict:
    base = {"datacenter": 0.75, "metro": 1.0, "longhaul": 1.25}[category] * mbps * 0.625
    if cos == "dedicated":
        base *= 1.127
    return {"category": category, "class_of_service": cos,
            "price_hourly": f"{base / 600:.6f}", "price_monthly": f"{base:.6f}"}


def _create_connection(body, connection_type: str) -> dict:
    _validate_bandwidth(body.bandwidth)
    _validate_endpoints(body)
    for gw_id in _gateway_ids(body.model_dump()):
        check_gateway_capacity(gw_id, body.bandwidth)
    conn = {
        "connection_id": new_id(),
        "connection_type": connection_type,
        "state": "provisioning",
        "name": body.name,
        "description": body.description,
        "class_of_service": body.class_of_service,
        "bandwidth": body.bandwidth,
        "term": body.term,
        "source_endpoint": body.source_endpoint,
        "dest_endpoint": body.dest_endpoint,
        "customer_number": body.customer_number,
        "billing_account_number": body.billing_account_number,
        "created_at": now_iso(), "created_by": "user@email.com",
        "updated_at": None, "updated_by": None,
    }
    store.fabric_connections[conn["connection_id"]] = conn

    def complete():
        if conn["state"] == "provisioning":
            conn["state"] = "active"
            for gw_id in _gateway_ids(conn):
                _refresh_aggregate(gw_id)
            emit("fabric.connection.active",
                 {"connection_id": conn["connection_id"], "type": connection_type}, deliver=False)

    schedule(config.TRANSITION_DELAY_SECONDS, complete)
    return conn


def _patch_connection(connection_id: str, body: UpdateConnection,
                      connection_type: str | None = None) -> dict:
    conn = _connection(connection_id, connection_type)
    if conn["state"] != "active":
        raise ProblemException(409, "Connection not active",
                               f"Connection {connection_id} is {conn['state']}; "
                               "modifications require an active connection")
    if body.name is not None:
        conn["name"] = body.name
    if body.description is not None:
        conn["description"] = body.description
    if body.bandwidth is not None and body.bandwidth != conn["bandwidth"]:
        _validate_bandwidth(body.bandwidth)
        delta = body.bandwidth - conn["bandwidth"]
        if delta > 0:
            for gw_id in _gateway_ids(conn):
                check_gateway_capacity(gw_id, delta)
        new_bw = body.bandwidth
        conn["state"] = "updating"

        def complete():
            conn["bandwidth"] = new_bw
            conn["state"] = "active"
            conn["updated_at"], conn["updated_by"] = now_iso(), "user@email.com"
            for gw_id in _gateway_ids(conn):
                _refresh_aggregate(gw_id)
            emit("fabric.connection.updated",
                 {"connection_id": connection_id, "bandwidth": new_bw}, deliver=False)

        schedule(config.TRANSITION_DELAY_SECONDS, complete)
    else:
        conn["updated_at"], conn["updated_by"] = now_iso(), "user@email.com"
    return conn


def _delete_connection(connection_id: str, connection_type: str | None = None) -> Response:
    conn = _connection(connection_id, connection_type)
    conn["state"] = "deleting"

    def complete():
        store.fabric_connections.pop(connection_id, None)
        for gw_id in _gateway_ids(conn):
            _refresh_aggregate(gw_id)
        emit("fabric.connection.deleted", {"connection_id": connection_id}, deliver=False)

    schedule(config.TRANSITION_DELAY_SECONDS, complete)
    return Response(status_code=204)


# ------------------------------------------------------------------ generic connections

@router.get("/connections")
async def list_connections(limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0)):
    return paginate(list(store.fabric_connections.values()), limit, offset)


@router.get("/connections/{connection_id}")
async def get_connection(connection_id: str):
    return _connection(connection_id)


@router.get("/connections/{connection_id}/bandwidths")
async def list_bandwidth_options(connection_id: str):
    """Priced bandwidth options for an existing connection, per the spec example."""
    _connection(connection_id)
    return [
        {"bandwidth": mbps,
         "options": [price_option(mbps, cat, cos)
                     for cat in ("datacenter", "metro", "longhaul")
                     for cos in ("basic", "dedicated")]}
        for mbps in FABRIC_BANDWIDTHS
    ]


@router.get("/billing/{connection_id}")
async def get_billing(connection_id: str):
    conn = _connection(connection_id)
    category = "datacenter" if conn["connection_type"].startswith("hosted") else "metro"
    opt = price_option(conn["bandwidth"], category, conn["class_of_service"])
    return {
        "connection_id": connection_id,
        "term": conn["term"],
        "bandwidth": conn["bandwidth"],
        "class_of_service": conn["class_of_service"],
        "price_hourly": opt["price_hourly"],
        "price_monthly": opt["price_monthly"],
        "currency": "USD",
    }


# ------------------------------------------------------------------ virtual p2p

@router.get("/connections/virtual/p2p")
async def list_p2p(limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0)):
    return paginate([c for c in store.fabric_connections.values()
                     if c["connection_type"] == "virtual-p2p"], limit, offset)


@router.post("/connections/virtual/p2p", status_code=202)
async def create_p2p(body: CreateP2PConnection):
    return _create_connection(body, "virtual-p2p")


@router.get("/connections/virtual/p2p/{connection_id}")
async def get_p2p(connection_id: str):
    return _connection(connection_id, "virtual-p2p")


@router.patch("/connections/virtual/p2p/{connection_id}")
async def patch_p2p(connection_id: str, body: UpdateConnection):
    return _patch_connection(connection_id, body, "virtual-p2p")


@router.delete("/connections/virtual/p2p/{connection_id}", status_code=204)
async def delete_p2p(connection_id: str):
    return _delete_connection(connection_id, "virtual-p2p")


# ------------------------------------------------------------------ hosted cloud connections

def _register_cloud_routes(cloud: str):
    ctype = f"hosted-{cloud}"

    @router.get(f"/connections/cloud/hosted/{cloud}")
    async def list_hosted(limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0)):
        return paginate([c for c in store.fabric_connections.values()
                         if c["connection_type"] == ctype], limit, offset)

    @router.post(f"/connections/cloud/hosted/{cloud}", status_code=202)
    async def create_hosted(body: CreateHostedConnection):
        return _create_connection(body, ctype)

    @router.get(f"/connections/cloud/hosted/{cloud}/{{connection_id}}")
    async def get_hosted(connection_id: str):
        return _connection(connection_id, ctype)

    @router.patch(f"/connections/cloud/hosted/{cloud}/{{connection_id}}")
    async def patch_hosted(connection_id: str, body: UpdateConnection):
        return _patch_connection(connection_id, body, ctype)

    @router.delete(f"/connections/cloud/hosted/{cloud}/{{connection_id}}", status_code=204)
    async def delete_hosted(connection_id: str):
        return _delete_connection(connection_id, ctype)


for _cloud in CLOUDS:
    _register_cloud_routes(_cloud)
