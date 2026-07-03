"""Ethernet On-Demand API v5 — /Network/v5/DynamicConnection/*

Implements the EVC/HAEVC lifecycle from Lumen's Ethernet_On-Demand_API spec,
including the bandwidth-on-demand PATCH: changes are accepted with 202,
the circuit sits in MODIFYING for TRANSITION_DELAY_SECONDS, then returns to
ACTIVE with the new bandwidth and a webhook event is emitted.
"""
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from . import config
from .auth import require_token
from .events import emit, schedule
from .state import new_id, now_iso, store

router = APIRouter(
    prefix="/Network/v5/DynamicConnection",
    dependencies=[Depends(require_token)],
    tags=["Ethernet On-Demand"],
)


async def require_ban(
        x_billing_account_number: str = Header(..., description="Billing account number, e.g. 5-WX3EDQ")):
    return x_billing_account_number


class EvcModificationRequest(BaseModel):
    bandwidth: int = Field(..., gt=0, description="EVC bandwidth in Mbps")
    userEmail: str


class EvcActivationRequest(BaseModel):
    evcName: str
    bandwidth: int = Field(..., gt=0)
    billingType: str = "hourly"
    cos: str = "basic"
    userEmail: str
    uniServiceId: str
    ceVlan: int = 100
    cloudProvider: str = "aws-hosted-connection"
    cloudProperties: dict = Field(default_factory=dict)


class HaEvcActivationRequest(BaseModel):
    haEvcName: str
    bandwidth: int = Field(..., gt=0)
    billingType: str = "hourly"
    cos: str = "basic"
    userEmail: str
    evcs: list[EvcActivationRequest] = Field(default_factory=list)


def _page(items: list, key: str, page_index: int, page_size: int) -> dict:
    total = len(items)
    pages = max(1, -(-total // page_size))
    return {
        key: items[page_index * page_size:(page_index + 1) * page_size],
        "firstPage": page_index == 0,
        "lastPage": page_index >= pages - 1,
        "pageIndex": page_index,
        "pageSize": page_size,
        "totalElements": total,
        "totalPages": pages,
    }


def _record_request(registry: dict, subject_key: str, subject_id: str, action: str, bandwidth=None) -> dict:
    req = {
        "requestId": new_id(),
        subject_key: subject_id,
        "action": action,
        "status": "IN_PROGRESS",
        "requestedBandwidth": bandwidth,
        "createdDateTime": now_iso(),
        "completedDateTime": None,
    }
    registry[req["requestId"]] = req
    return req


def _check_uni_capacity(uni_service_id: str, bandwidth: int):
    uni = store.uni_by_service_id(uni_service_id)
    if uni is None:
        raise HTTPException(404, f"UNI with service id {uni_service_id} not found")
    if bandwidth > uni["maxAvailableBandwidth"]:
        raise HTTPException(
            400,
            f"requested bandwidth {bandwidth} Mbps exceeds max available "
            f"{uni['maxAvailableBandwidth']} Mbps on UNI {uni_service_id}",
        )


# ---------------------------------------------------------------- UNIs

@router.get("/unis")
async def get_unis(pageIndex: int = Query(0, ge=0), pageSize: int = Query(100, ge=1)):
    return _page(list(store.unis.values()), "unis", pageIndex, pageSize)


@router.get("/unis/{uni_id}")
async def get_uni(uni_id: str):
    uni = store.unis.get(uni_id)
    if not uni:
        raise HTTPException(404, f"UNI {uni_id} not found")
    return uni


@router.get("/partnerInterconnects/{product_type}")
async def get_partner_interconnects(product_type: str):
    interconnects = store.partner_interconnects.get(product_type)
    if interconnects is None:
        raise HTTPException(404, f"no partner interconnects for product type {product_type}")
    return {"productType": product_type, "interconnects": interconnects}


# ---------------------------------------------------------------- EVCs

@router.get("/evcs")
async def get_evcs(pageIndex: int = Query(0, ge=0), pageSize: int = Query(100, ge=1),
                   ban: str = Depends(require_ban)):
    return _page(list(store.evcs.values()), "evcs", pageIndex, pageSize)


@router.post("/evcs", status_code=202)
async def create_evc(body: EvcActivationRequest, ban: str = Depends(require_ban)):
    _check_uni_capacity(body.uniServiceId, body.bandwidth)
    evc = {
        "evcId": new_id(),
        "evcName": body.evcName,
        "evcServiceAlias": f"VLXX/D{len(store.evcs) + 124:05d}/LVLC",
        "status": "CREATING",
        "haEvcId": None,
        "bandwidth": body.bandwidth,
        "billingType": body.billingType,
        "cos": body.cos,
        "userEmail": body.userEmail,
        "startDateTime": now_iso(),
        "modifyDateTime": None,
        "endDateTime": None,
        "endPoint1": {"uniServiceId": body.uniServiceId, "ceVlan": body.ceVlan},
        "endPoint2": {"cloudProvider": body.cloudProvider, **body.cloudProperties},
        "billingAccountNumber": ban,
    }
    store.evcs[evc["evcId"]] = evc
    req = _record_request(store.evc_requests, "evcId", evc["evcId"], "CREATE", body.bandwidth)

    def complete():
        evc["status"] = "ACTIVE"
        req["status"], req["completedDateTime"] = "COMPLETED", now_iso()
        emit("evc.created", {"evcId": evc["evcId"], "status": "ACTIVE",
                             "bandwidth": evc["bandwidth"], "evcRequestId": req["requestId"]})

    schedule(config.TRANSITION_DELAY_SECONDS, complete)
    return {"evcId": evc["evcId"], "evcRequestId": req["requestId"], "status": "CREATING"}


@router.get("/evcs/{evc_id}")
async def get_evc(evc_id: str, ban: str = Depends(require_ban)):
    evc = store.evcs.get(evc_id)
    if not evc:
        raise HTTPException(404, f"EVC {evc_id} not found")
    return evc


@router.patch("/evcs/{evc_id}", status_code=202)
async def update_evc(evc_id: str, body: EvcModificationRequest, ban: str = Depends(require_ban)):
    evc = store.evcs.get(evc_id)
    if not evc:
        raise HTTPException(404, f"EVC {evc_id} not found")
    if evc["status"] != "ACTIVE":
        raise HTTPException(
            400, f"EVC {evc_id} is {evc['status']}; bandwidth can only be modified while ACTIVE")
    _check_uni_capacity(evc["endPoint1"]["uniServiceId"], body.bandwidth)

    evc["status"] = "MODIFYING"
    req = _record_request(store.evc_requests, "evcId", evc_id, "MODIFY", body.bandwidth)

    def complete():
        evc["bandwidth"] = body.bandwidth
        evc["status"] = "ACTIVE"
        evc["modifyDateTime"] = now_iso()
        req["status"], req["completedDateTime"] = "COMPLETED", now_iso()
        emit("evc.bandwidthModified", {"evcId": evc_id, "status": "ACTIVE",
                                       "bandwidth": body.bandwidth, "evcRequestId": req["requestId"]})

    schedule(config.TRANSITION_DELAY_SECONDS, complete)
    return {"evcId": evc_id, "evcRequestId": req["requestId"], "status": "MODIFYING"}


@router.delete("/evcs/{evc_id}", status_code=202)
async def delete_evc(evc_id: str, userEmail: str = Query(...), ban: str = Depends(require_ban)):
    evc = store.evcs.get(evc_id)
    if not evc:
        raise HTTPException(404, f"EVC {evc_id} not found")
    if evc["status"] not in ("ACTIVE", "CREATING"):
        raise HTTPException(400, f"EVC {evc_id} is {evc['status']} and cannot be deleted")

    evc["status"] = "DELETING"
    req = _record_request(store.evc_requests, "evcId", evc_id, "DELETE")

    def complete():
        store.evcs.pop(evc_id, None)
        req["status"], req["completedDateTime"] = "COMPLETED", now_iso()
        emit("evc.deleted", {"evcId": evc_id, "status": "DELETED", "evcRequestId": req["requestId"]})

    schedule(config.TRANSITION_DELAY_SECONDS, complete)
    return {"evcId": evc_id, "evcRequestId": req["requestId"], "status": "DELETING"}


# ---------------------------------------------------------------- HAEVCs

@router.get("/haEvcs")
async def get_ha_evcs(pageIndex: int = Query(0, ge=0), pageSize: int = Query(100, ge=1),
                      ban: str = Depends(require_ban)):
    return _page(list(store.ha_evcs.values()), "haEvcs", pageIndex, pageSize)


@router.post("/haEvcs", status_code=202)
async def create_ha_evc(body: HaEvcActivationRequest, ban: str = Depends(require_ban)):
    for leg in body.evcs:
        _check_uni_capacity(leg.uniServiceId, body.bandwidth)
    ha = {
        "haEvcId": new_id(),
        "haEvcName": body.haEvcName,
        "status": "CREATING",
        "bandwidth": body.bandwidth,
        "billingType": body.billingType,
        "cos": body.cos,
        "userEmail": body.userEmail,
        "startDateTime": now_iso(),
        "modifyDateTime": None,
        "evcIds": [],
        "billingAccountNumber": ban,
    }
    for leg in body.evcs:
        evc_id = new_id()
        store.evcs[evc_id] = {
            "evcId": evc_id, "evcName": f"{body.haEvcName}-{leg.evcName}",
            "evcServiceAlias": f"VLXX/D{len(store.evcs) + 124:05d}/LVLC",
            "status": "CREATING", "haEvcId": ha["haEvcId"],
            "bandwidth": body.bandwidth, "billingType": body.billingType,
            "cos": body.cos, "userEmail": body.userEmail,
            "startDateTime": now_iso(), "modifyDateTime": None, "endDateTime": None,
            "endPoint1": {"uniServiceId": leg.uniServiceId, "ceVlan": leg.ceVlan},
            "endPoint2": {"cloudProvider": leg.cloudProvider, **leg.cloudProperties},
            "billingAccountNumber": ban,
        }
        ha["evcIds"].append(evc_id)
    store.ha_evcs[ha["haEvcId"]] = ha
    req = _record_request(store.ha_evc_requests, "haEvcId", ha["haEvcId"], "CREATE", body.bandwidth)

    def complete():
        ha["status"] = "ACTIVE"
        for evc_id in ha["evcIds"]:
            if evc_id in store.evcs:
                store.evcs[evc_id]["status"] = "ACTIVE"
        req["status"], req["completedDateTime"] = "COMPLETED", now_iso()
        emit("haEvc.created", {"haEvcId": ha["haEvcId"], "status": "ACTIVE",
                               "bandwidth": ha["bandwidth"], "haEvcRequestId": req["requestId"]})

    schedule(config.TRANSITION_DELAY_SECONDS, complete)
    return {"haEvcId": ha["haEvcId"], "haEvcRequestId": req["requestId"], "status": "CREATING"}


@router.get("/haEvcs/{ha_evc_id}")
async def get_ha_evc(ha_evc_id: str, ban: str = Depends(require_ban)):
    ha = store.ha_evcs.get(ha_evc_id)
    if not ha:
        raise HTTPException(404, f"HAEVC {ha_evc_id} not found")
    return ha


@router.patch("/haEvcs/{ha_evc_id}", status_code=202)
async def update_ha_evc(ha_evc_id: str, body: EvcModificationRequest, ban: str = Depends(require_ban)):
    ha = store.ha_evcs.get(ha_evc_id)
    if not ha:
        raise HTTPException(404, f"HAEVC {ha_evc_id} not found")
    if ha["status"] != "ACTIVE":
        raise HTTPException(
            400, f"HAEVC {ha_evc_id} is {ha['status']}; bandwidth can only be modified while ACTIVE")
    for evc_id in ha["evcIds"]:
        evc = store.evcs.get(evc_id)
        if evc:
            _check_uni_capacity(evc["endPoint1"]["uniServiceId"], body.bandwidth)

    ha["status"] = "MODIFYING"
    req = _record_request(store.ha_evc_requests, "haEvcId", ha_evc_id, "MODIFY", body.bandwidth)

    def complete():
        ha["bandwidth"] = body.bandwidth
        ha["status"] = "ACTIVE"
        ha["modifyDateTime"] = now_iso()
        for evc_id in ha["evcIds"]:
            if evc_id in store.evcs:
                store.evcs[evc_id]["bandwidth"] = body.bandwidth
                store.evcs[evc_id]["modifyDateTime"] = now_iso()
        req["status"], req["completedDateTime"] = "COMPLETED", now_iso()
        emit("haEvc.bandwidthModified", {"haEvcId": ha_evc_id, "status": "ACTIVE",
                                         "bandwidth": body.bandwidth, "haEvcRequestId": req["requestId"]})

    schedule(config.TRANSITION_DELAY_SECONDS, complete)
    return {"haEvcId": ha_evc_id, "haEvcRequestId": req["requestId"], "status": "MODIFYING"}


@router.delete("/haEvcs/{ha_evc_id}", status_code=202)
async def delete_ha_evc(ha_evc_id: str, userEmail: str = Query(...), ban: str = Depends(require_ban)):
    ha = store.ha_evcs.get(ha_evc_id)
    if not ha:
        raise HTTPException(404, f"HAEVC {ha_evc_id} not found")
    ha["status"] = "DELETING"
    req = _record_request(store.ha_evc_requests, "haEvcId", ha_evc_id, "DELETE")

    def complete():
        for evc_id in ha["evcIds"]:
            store.evcs.pop(evc_id, None)
        store.ha_evcs.pop(ha_evc_id, None)
        req["status"], req["completedDateTime"] = "COMPLETED", now_iso()
        emit("haEvc.deleted", {"haEvcId": ha_evc_id, "status": "DELETED", "haEvcRequestId": req["requestId"]})

    schedule(config.TRANSITION_DELAY_SECONDS, complete)
    return {"haEvcId": ha_evc_id, "haEvcRequestId": req["requestId"], "status": "DELETING"}


# ---------------------------------------------------------------- Requests

@router.get("/evcRequests")
async def get_evc_requests(evcId: str | None = Query(default=None)):
    requests = list(store.evc_requests.values())
    if evcId:
        requests = [r for r in requests if r["evcId"] == evcId]
    return {"evcRequests": requests}


@router.get("/haEvcRequests")
async def get_ha_evc_requests(haEvcId: str | None = Query(default=None)):
    requests = list(store.ha_evc_requests.values())
    if haEvcId:
        requests = [r for r in requests if r["haEvcId"] == haEvcId]
    return {"haEvcRequests": requests}
