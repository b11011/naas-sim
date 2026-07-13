"""Multi-Cloud Gateway API — /mcgw/v1 (new-generation, spec v1.0.0).

L3 virtual gateways created entirely via API: interfaces, static routes,
prefix lists, BGP sessions. Lifecycle: PENDING → PROVISIONING → PROVISIONED
(async, TRANSITION_DELAY_SECONDS), DELETING → DELETED. Errors are RFC 7807
problem+json (LpdpProblem), per the published spec — not the legacy envelope.
No webhooks: the real product offers none; poll the resource state.
"""
from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field

from . import config
from .auth import require_token
from .events import emit, schedule
from .problems import ProblemException, paginate
from .state import new_id, now_iso, store

router = APIRouter(prefix="/mcgw/v1", dependencies=[Depends(require_token)],
                   tags=["Multi-Cloud Gateway"])

TIERS = {"10 Gbps": 10_000, "50 Gbps": 50_000, "Unlimited": None}


# ------------------------------------------------------------------ models

class CreateGateway(BaseModel):
    name: str = Field(..., max_length=255)
    description: str | None = Field(default=None, max_length=500)
    tier: str
    asn: int | None = None
    customer_number: str
    billing_account_number: str
    term: str = "Monthly"


class UpdateGateway(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=500)
    tier: str | None = None


class CreateInterface(BaseModel):
    name: str = Field(..., max_length=255)
    description: str | None = None
    address_family: str = "IPV4"


class CreateStaticRoute(BaseModel):
    name: str = Field(..., max_length=255)
    prefix: str
    next_hop: str
    interface_id: str | None = None


class CreatePrefixList(BaseModel):
    name: str = Field(..., max_length=255)
    address_family: str = "IPV4"
    prefixes: list[str] = Field(default_factory=list)


class CreateBgpSession(BaseModel):
    name: str = Field(..., max_length=255)
    remote_asn: int
    remote_peer_ip: str
    med: int | None = None
    prefix_list_id: str | None = None


# ------------------------------------------------------------------ helpers

def _gateway(gateway_id: str) -> dict:
    gw = store.gateways.get(gateway_id)
    if not gw or gw["state"] == "DELETED":
        raise ProblemException(404, "Gateway not found",
                               f"No Multi-Cloud Gateway with id {gateway_id}")
    return gw


def _interface(gateway_id: str, interface_id: str) -> dict:
    iface = store.mcg_interfaces.get(interface_id)
    if not iface or iface["state"] == "DELETED" or iface["gateway"]["gateway_id"] != gateway_id:
        raise ProblemException(404, "Interface not found",
                               f"No interface {interface_id} on gateway {gateway_id}")
    return iface


def gateway_aggregate_mbps(gateway_id: str) -> int:
    """Sum of bandwidth on non-deleted fabric connections anchored to this gateway."""
    total = 0
    for c in store.fabric_connections.values():
        if c["state"] in ("deleting",):
            continue
        for end in (c.get("source_endpoint") or {}, c.get("dest_endpoint") or {}):
            if isinstance(end, dict) and (end.get("gateway") or {}).get("gateway_id") == gateway_id:
                total += c["bandwidth"]
                break
    return total


def check_gateway_capacity(gateway_id: str, additional_mbps: int):
    gw = _gateway(gateway_id)
    cap = TIERS.get(gw["tier"])
    if cap is None:
        return
    used = gateway_aggregate_mbps(gateway_id)
    if used + additional_mbps > cap:
        raise ProblemException(
            422, "Gateway tier capacity exceeded",
            f"Gateway {gateway_id} ({gw['tier']} tier) has {used} Mbps in use; "
            f"adding {additional_mbps} Mbps exceeds the {cap} Mbps tier limit.")


def _refresh_aggregate(gateway_id: str):
    gw = store.gateways.get(gateway_id)
    if gw:
        gw["total_aggregate_bw"] = f"{gateway_aggregate_mbps(gateway_id)} Mbps"


# ------------------------------------------------------------------ gateways

@router.get("/gateways")
async def list_gateways(limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0)):
    visible = [g for g in store.gateways.values() if g["state"] != "DELETED"]
    return paginate(visible, limit, offset)


@router.post("/gateways", status_code=201)
async def create_gateway(body: CreateGateway, response: Response):
    if body.tier not in TIERS:
        raise ProblemException(400, "Invalid tier",
                               f"tier must be one of {sorted(TIERS)}; got '{body.tier}'")
    gw = {
        "gateway_id": new_id(),
        "state": "PROVISIONING",
        "name": body.name,
        "description": body.description,
        "tier": body.tier,
        "asn": body.asn or 65000,
        "total_aggregate_bw": "0 Mbps",
        "term": body.term,
        "customer_number": body.customer_number,
        "billing_account_number": body.billing_account_number,
        "created_at": now_iso(), "created_by": "user@email.com",
        "updated_at": None, "updated_by": None,
    }
    store.gateways[gw["gateway_id"]] = gw

    def complete():
        if gw["state"] == "PROVISIONING":
            gw["state"] = "PROVISIONED"
            emit("mcgw.gateway.provisioned", {"gateway_id": gw["gateway_id"]}, deliver=False)

    schedule(config.TRANSITION_DELAY_SECONDS, complete)
    response.headers["Location"] = f"https://api.lumen.com/mcgw/v1/gateways/{gw['gateway_id']}"
    return gw


@router.get("/gateways/{gateway_id}")
async def get_gateway(gateway_id: str):
    return _gateway(gateway_id)


@router.patch("/gateways/{gateway_id}")
async def update_gateway(gateway_id: str, body: UpdateGateway):
    gw = _gateway(gateway_id)
    if body.tier is not None:
        if body.tier not in TIERS:
            raise ProblemException(400, "Invalid tier",
                                   f"tier must be one of {sorted(TIERS)}; got '{body.tier}'")
        cap = TIERS[body.tier]
        used = gateway_aggregate_mbps(gateway_id)
        if cap is not None and used > cap:
            raise ProblemException(422, "Tier below current usage",
                                   f"{used} Mbps in use exceeds the {body.tier} limit of {cap} Mbps")
        gw["tier"] = body.tier
    if body.name is not None:
        gw["name"] = body.name
    if body.description is not None:
        gw["description"] = body.description
    gw["updated_at"], gw["updated_by"] = now_iso(), "user@email.com"
    return gw


@router.delete("/gateways/{gateway_id}", status_code=204)
async def delete_gateway(gateway_id: str):
    gw = _gateway(gateway_id)
    active = [i for i in store.mcg_interfaces.values()
              if i["gateway"]["gateway_id"] == gateway_id and i["state"] not in ("DELETED",)]
    if active:
        raise ProblemException(409, "Gateway has interfaces",
                               f"Delete the {len(active)} interface(s) on gateway {gateway_id} first")
    gw["state"] = "DELETING"

    def complete():
        gw["state"] = "DELETED"
        emit("mcgw.gateway.deleted", {"gateway_id": gateway_id}, deliver=False)

    schedule(config.TRANSITION_DELAY_SECONDS, complete)
    return Response(status_code=204)


# ------------------------------------------------------------------ interfaces

@router.get("/gateways/{gateway_id}/interfaces")
async def list_interfaces(gateway_id: str, limit: int = Query(25, ge=1, le=100),
                          offset: int = Query(0, ge=0)):
    _gateway(gateway_id)
    items = [i for i in store.mcg_interfaces.values()
             if i["gateway"]["gateway_id"] == gateway_id and i["state"] != "DELETED"]
    return paginate(items, limit, offset)


@router.post("/gateways/{gateway_id}/interfaces", status_code=202)
async def create_interface(gateway_id: str, body: CreateInterface):
    gw = _gateway(gateway_id)
    if gw["state"] != "PROVISIONED":
        raise ProblemException(409, "Gateway not ready",
                               f"Gateway {gateway_id} is {gw['state']}; wait for PROVISIONED")
    iface = {
        "interface_id": new_id(),
        "state": "PROVISIONING",
        "name": body.name,
        "description": body.description,
        "gateway": {"gateway_id": gateway_id, "name": gw["name"]},
        "connection": None,
        "address_family": body.address_family,
        "created_at": now_iso(), "created_by": "user@email.com",
        "updated_at": None, "updated_by": None,
    }
    store.mcg_interfaces[iface["interface_id"]] = iface

    def complete():
        if iface["state"] == "PROVISIONING":
            iface["state"] = "PROVISIONED"
            emit("mcgw.interface.provisioned",
                 {"interface_id": iface["interface_id"], "gateway_id": gateway_id}, deliver=False)

    schedule(config.TRANSITION_DELAY_SECONDS, complete)
    return iface


@router.get("/gateways/{gateway_id}/interfaces/{interface_id}")
async def get_interface(gateway_id: str, interface_id: str):
    return _interface(gateway_id, interface_id)


@router.patch("/gateways/{gateway_id}/interfaces/{interface_id}")
async def update_interface(gateway_id: str, interface_id: str, body: CreateInterface):
    iface = _interface(gateway_id, interface_id)
    iface["name"] = body.name
    if body.description is not None:
        iface["description"] = body.description
    iface["updated_at"], iface["updated_by"] = now_iso(), "user@email.com"
    return iface


@router.delete("/gateways/{gateway_id}/interfaces/{interface_id}", status_code=204)
async def delete_interface(gateway_id: str, interface_id: str):
    iface = _interface(gateway_id, interface_id)
    attached = [c for c in store.fabric_connections.values()
                if c["state"] != "deleting" and any(
                    isinstance(e, dict) and (e.get("interface") or {}).get("interface_id") == interface_id
                    for e in (c.get("source_endpoint"), c.get("dest_endpoint")) if e)]
    if attached:
        raise ProblemException(409, "Interface in use",
                               f"{len(attached)} fabric connection(s) terminate on interface {interface_id}")
    iface["state"] = "DELETING"

    def complete():
        iface["state"] = "DELETED"

    schedule(config.TRANSITION_DELAY_SECONDS, complete)
    return Response(status_code=204)


# ------------------------------------------------------------------ routes (read-only) + static routes

@router.get("/gateways/{gateway_id}/routes")
async def list_routes(gateway_id: str, limit: int = Query(25, ge=1, le=100),
                      offset: int = Query(0, ge=0)):
    _gateway(gateway_id)
    learned = [{"prefix": "0.0.0.0/0", "next_hop": "lumen-fabric", "protocol": "connected"}]
    statics = [{"prefix": r["prefix"], "next_hop": r["next_hop"], "protocol": "static"}
               for r in store.mcg_static_routes.values() if r["gateway_id"] == gateway_id]
    return paginate(learned + statics, limit, offset)


@router.get("/gateways/{gateway_id}/static-routes")
async def list_static_routes(gateway_id: str, limit: int = Query(25, ge=1, le=100),
                             offset: int = Query(0, ge=0)):
    _gateway(gateway_id)
    return paginate([r for r in store.mcg_static_routes.values()
                     if r["gateway_id"] == gateway_id], limit, offset)


@router.post("/gateways/{gateway_id}/static-routes", status_code=201)
async def create_static_route(gateway_id: str, body: CreateStaticRoute):
    _gateway(gateway_id)
    route = {"static_route_id": new_id(), "gateway_id": gateway_id, "state": "PROVISIONED",
             "name": body.name, "prefix": body.prefix, "next_hop": body.next_hop,
             "interface_id": body.interface_id,
             "created_at": now_iso(), "updated_at": None}
    store.mcg_static_routes[route["static_route_id"]] = route
    return route


@router.get("/gateways/{gateway_id}/static-routes/{static_route_id}")
async def get_static_route(gateway_id: str, static_route_id: str):
    route = store.mcg_static_routes.get(static_route_id)
    if not route or route["gateway_id"] != gateway_id:
        raise ProblemException(404, "Static route not found",
                               f"No static route {static_route_id} on gateway {gateway_id}")
    return route


@router.patch("/gateways/{gateway_id}/static-routes/{static_route_id}")
async def update_static_route(gateway_id: str, static_route_id: str, body: CreateStaticRoute):
    route = await get_static_route(gateway_id, static_route_id)
    route.update({"name": body.name, "prefix": body.prefix, "next_hop": body.next_hop,
                  "interface_id": body.interface_id, "updated_at": now_iso()})
    return route


@router.delete("/gateways/{gateway_id}/static-routes/{static_route_id}", status_code=204)
async def delete_static_route(gateway_id: str, static_route_id: str):
    await get_static_route(gateway_id, static_route_id)
    store.mcg_static_routes.pop(static_route_id, None)
    return Response(status_code=204)


# ------------------------------------------------------------------ prefix lists

@router.get("/prefix-lists")
async def list_prefix_lists(limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0)):
    return paginate(list(store.prefix_lists.values()), limit, offset)


@router.post("/prefix-lists", status_code=201)
async def create_prefix_list(body: CreatePrefixList):
    pl = {"prefix_list_id": new_id(), "name": body.name,
          "address_family": body.address_family, "prefixes": body.prefixes,
          "created_at": now_iso(), "updated_at": None}
    store.prefix_lists[pl["prefix_list_id"]] = pl
    return pl


@router.get("/prefix-lists/{prefix_list_id}")
async def get_prefix_list(prefix_list_id: str):
    pl = store.prefix_lists.get(prefix_list_id)
    if not pl:
        raise ProblemException(404, "Prefix list not found",
                               f"No prefix list with id {prefix_list_id}")
    return pl


@router.put("/prefix-lists/{prefix_list_id}")
@router.patch("/prefix-lists/{prefix_list_id}")
async def update_prefix_list(prefix_list_id: str, body: CreatePrefixList):
    pl = await get_prefix_list(prefix_list_id)
    pl.update({"name": body.name, "address_family": body.address_family,
               "prefixes": body.prefixes, "updated_at": now_iso()})
    return pl


@router.delete("/prefix-lists/{prefix_list_id}", status_code=204)
async def delete_prefix_list(prefix_list_id: str):
    await get_prefix_list(prefix_list_id)
    in_use = [b for b in store.bgp_sessions.values() if b.get("prefix_list_id") == prefix_list_id]
    if in_use:
        raise ProblemException(409, "Prefix list in use",
                               f"{len(in_use)} BGP session(s) reference prefix list {prefix_list_id}")
    store.prefix_lists.pop(prefix_list_id, None)
    return Response(status_code=204)


# ------------------------------------------------------------------ BGP sessions

@router.get("/gateways/{gateway_id}/interfaces/{interface_id}/bgp-sessions")
async def list_bgp_sessions(gateway_id: str, interface_id: str,
                            limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0)):
    _interface(gateway_id, interface_id)
    return paginate([b for b in store.bgp_sessions.values()
                     if b["interface_id"] == interface_id], limit, offset)


@router.post("/gateways/{gateway_id}/interfaces/{interface_id}/bgp-sessions", status_code=202)
async def create_bgp_session(gateway_id: str, interface_id: str, body: CreateBgpSession):
    _interface(gateway_id, interface_id)
    if body.prefix_list_id and body.prefix_list_id not in store.prefix_lists:
        raise ProblemException(404, "Prefix list not found",
                               f"No prefix list with id {body.prefix_list_id}")
    session = {"session_id": new_id(), "gateway_id": gateway_id, "interface_id": interface_id,
               "state": "PROVISIONING", "name": body.name, "remote_asn": body.remote_asn,
               "remote_peer_ip": body.remote_peer_ip, "med": body.med,
               "prefix_list_id": body.prefix_list_id, "session_status": "IDLE",
               "created_at": now_iso(), "updated_at": None}
    store.bgp_sessions[session["session_id"]] = session

    def complete():
        if session["state"] == "PROVISIONING":
            session["state"], session["session_status"] = "PROVISIONED", "ESTABLISHED"

    schedule(config.TRANSITION_DELAY_SECONDS, complete)
    return session


@router.get("/gateways/{gateway_id}/interfaces/{interface_id}/bgp-sessions/{session_id}")
async def get_bgp_session(gateway_id: str, interface_id: str, session_id: str):
    b = store.bgp_sessions.get(session_id)
    if not b or b["interface_id"] != interface_id:
        raise ProblemException(404, "BGP session not found",
                               f"No BGP session {session_id} on interface {interface_id}")
    return b


@router.patch("/gateways/{gateway_id}/interfaces/{interface_id}/bgp-sessions/{session_id}")
async def update_bgp_session(gateway_id: str, interface_id: str, session_id: str,
                             body: CreateBgpSession):
    b = await get_bgp_session(gateway_id, interface_id, session_id)
    b.update({"name": body.name, "remote_asn": body.remote_asn,
              "remote_peer_ip": body.remote_peer_ip, "med": body.med,
              "prefix_list_id": body.prefix_list_id, "updated_at": now_iso()})
    return b


@router.delete("/gateways/{gateway_id}/interfaces/{interface_id}/bgp-sessions/{session_id}",
               status_code=204)
async def delete_bgp_session(gateway_id: str, interface_id: str, session_id: str):
    await get_bgp_session(gateway_id, interface_id, session_id)
    store.bgp_sessions.pop(session_id, None)
    return Response(status_code=204)
