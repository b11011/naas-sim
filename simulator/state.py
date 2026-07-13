"""In-memory state for the NaaS simulator.

Seed data reuses the identifiers from Lumen's own documentation examples
(billing account 5-WX3EDQ, customer number 1-ABCDE, service 771234567, the
e27a48de... EVC, masterSiteId PL0000000001, partnerId 43558) so requests
copied from the vendor docs work against the lab unchanged.
"""
import uuid
from datetime import datetime, timezone

# Speeds offered at every simulated location, in Mbps.
SPEEDS_MBPS = [10, 20, 50, 100, 200, 500, 1000]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def gmt_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def new_id() -> str:
    return str(uuid.uuid4())


def price_for(speed_mbps: int) -> dict:
    monthly = round(55 + 1.10 * speed_mbps, 2)
    return {
        "currencyCode": "USD",
        "monthlyRecurring": monthly,
        "hourly": round(monthly / 730, 4),
    }


class Store:
    def __init__(self):
        self.reset()
        self.loaded_from_snapshot = self.load()
        if self.loaded_from_snapshot:
            self.finish_pending()

    def reset(self):
        self.tokens = {}            # access token -> expiry (epoch seconds)
        self.speeds = list(SPEEDS_MBPS)  # offered speeds; seedable per catalog

        # Ethernet On-Demand
        self.unis = {}              # uniId -> UNI
        self.evcs = {}              # evcId -> EVC
        self.ha_evcs = {}           # haEvcId -> HAEVC
        self.evc_requests = {}      # requestId -> async request record
        self.ha_evc_requests = {}
        self.partner_interconnects = {}

        # Internet On-Demand
        self.locations = []
        self.services = {}          # service (product) id -> inventory record
        self.quotes = {}            # quoteId -> quote
        self.orders = {}            # orderId -> order
        self.order_counts = {}      # (customerNumber, gmt date) -> count

        # Multi-Cloud Gateway (new-generation, /mcgw/v1)
        self.gateways = {}          # gateway_id -> Gateway
        self.mcg_interfaces = {}    # interface_id -> Interface
        self.mcg_static_routes = {} # static_route_id -> route
        self.prefix_lists = {}      # prefix_list_id -> prefix list
        self.bgp_sessions = {}      # session_id -> BGP session

        # Ethernet Fabric Connect (new-generation, /fabric/v1)
        self.fabric_connections = {}  # connection_id -> connection

        # Webhooks / eventing
        self.webhooks = []          # [{"id": ..., "callback": url}]
        self.events = []            # every emitted event, newest last

        self._quote_seq = 1000
        self._seed()

    def next_quote_id(self) -> str:
        self._quote_seq += 1
        return f"NaaS-{self._quote_seq}"

    def uni_by_service_id(self, uni_service_id):
        return next((u for u in self.unis.values() if u["uniServiceId"] == uni_service_id), None)

    # ---------------------------------------------------------- persistence

    _PERSISTED = ["speeds", "unis", "evcs", "ha_evcs", "evc_requests", "ha_evc_requests",
                  "partner_interconnects", "locations", "services", "quotes", "orders",
                  "webhooks", "events", "gateways", "mcg_interfaces", "mcg_static_routes",
                  "prefix_lists", "bgp_sessions", "fabric_connections"]

    def save(self):
        """Snapshot state to config.STATE_FILE (no-op when unset). Tokens are
        deliberately ephemeral — clients re-authenticate after a restart."""
        from . import config
        if not config.STATE_FILE:
            return
        import json
        import os
        data = {key: getattr(self, key) for key in self._PERSISTED}
        data["order_counts"] = [[cust, day, n] for (cust, day), n in self.order_counts.items()]
        data["_quote_seq"] = self._quote_seq
        tmp = config.STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, config.STATE_FILE)

    def load(self) -> bool:
        """Restore a snapshot if one exists. Returns True when state was loaded."""
        from . import config
        if not config.STATE_FILE:
            return False
        import json
        import os
        if not os.path.exists(config.STATE_FILE):
            return False
        with open(config.STATE_FILE) as f:
            data = json.load(f)
        for key in self._PERSISTED:
            if key in data:
                setattr(self, key, data[key])
        self.order_counts = {(cust, day): n for cust, day, n in data.get("order_counts", [])}
        self._quote_seq = data.get("_quote_seq", self._quote_seq)
        self.tokens = {}
        return True

    def finish_pending(self):
        """Reconcile-on-startup: complete transitions that were in flight when
        the process stopped, so no circuit/order is stuck transitional forever."""
        for req in self.evc_requests.values():
            if req["status"] != "IN_PROGRESS":
                continue
            evc = self.evcs.get(req["evcId"])
            if req["action"] == "DELETE":
                self.evcs.pop(req["evcId"], None)
            elif evc is not None:
                if req["action"] == "MODIFY" and req.get("requestedBandwidth"):
                    evc["bandwidth"] = req["requestedBandwidth"]
                    evc["modifyDateTime"] = now_iso()
                evc["status"] = "ACTIVE"
            req["status"], req["completedDateTime"] = "COMPLETED", now_iso()
        for req in self.ha_evc_requests.values():
            if req["status"] != "IN_PROGRESS":
                continue
            ha = self.ha_evcs.get(req["haEvcId"])
            if req["action"] == "DELETE" and ha is not None:
                for evc_id in ha.get("evcIds", []):
                    self.evcs.pop(evc_id, None)
                self.ha_evcs.pop(req["haEvcId"], None)
            elif ha is not None:
                if req["action"] == "MODIFY" and req.get("requestedBandwidth"):
                    ha["bandwidth"] = req["requestedBandwidth"]
                ha["status"] = "ACTIVE"
                for evc_id in ha.get("evcIds", []):
                    if evc_id in self.evcs:
                        if req["action"] == "MODIFY" and req.get("requestedBandwidth"):
                            self.evcs[evc_id]["bandwidth"] = req["requestedBandwidth"]
                        self.evcs[evc_id]["status"] = "ACTIVE"
            req["status"], req["completedDateTime"] = "COMPLETED", now_iso()
        for order in self.orders.values():
            if order["state"] != "acknowledged":
                continue
            service = self.services.get(order.get("serviceId") or "")
            if order["action"] == "delete":
                self.services.pop(order.get("serviceId"), None)
            elif service is not None:
                if order["action"] == "modify" and order.get("requestedBandwidth"):
                    service["bandwidth"] = order["requestedBandwidth"]
                    service["modifiedDateTime"] = now_iso()
                service["status"] = "active"
            order["state"], order["completionDate"] = "completed", now_iso()
        # New-generation resources: complete in-flight lifecycle transitions
        for gw in self.gateways.values():
            if gw["state"] in ("PENDING", "PROVISIONING"):
                gw["state"] = "PROVISIONED"
            elif gw["state"] == "DELETING":
                gw["state"] = "DELETED"
        for iface in self.mcg_interfaces.values():
            if iface["state"] in ("PENDING", "PROVISIONING"):
                iface["state"] = "PROVISIONED"
            elif iface["state"] == "DELETING":
                iface["state"] = "DELETED"
        for b in self.bgp_sessions.values():
            if b["state"] in ("PENDING", "PROVISIONING"):
                b["state"], b["session_status"] = "PROVISIONED", "ESTABLISHED"
        for cid in list(self.fabric_connections):
            c = self.fabric_connections[cid]
            if c["state"] in ("provisioning", "updating", "resetting"):
                c["state"] = "active"
            elif c["state"] == "deleting":
                self.fabric_connections.pop(cid, None)
        self.save()

    # ---------------------------------------------------------- seeding

    def apply_seed(self, profile: dict):
        """Load a catalog profile (any subset of: speeds, locations, unis, evcs,
        services, partnerInterconnects). Replaces the provided sections and
        clears transactional state (quotes, orders, requests, events)."""
        if "speeds" in profile:
            self.speeds = sorted(int(s) for s in profile["speeds"])
        if "locations" in profile:
            self.locations = profile["locations"]
        if "partnerInterconnects" in profile:
            self.partner_interconnects = profile["partnerInterconnects"]
        if "unis" in profile:
            self.unis = {u["uniId"]: u for u in profile["unis"]}
        if "evcs" in profile:
            self.evcs = {e["evcId"]: e for e in profile["evcs"]}
        if "services" in profile:
            self.services = {s["id"]: s for s in profile["services"]}
        self.ha_evcs = {}
        self.evc_requests, self.ha_evc_requests = {}, {}
        self.quotes, self.orders, self.order_counts = {}, {}, {}
        self.events = []
        self.save()

    def _seed(self):
        for uni in [
            {
                "uniId": "11111111-aaaa-4bbb-8ccc-000000000001",
                "uniServiceId": "CA/KXFN/111111/LVLC",
                "uniName": "SJC-lab-port-1",
                "status": "ACTIVE",
                "location": "San Jose, CA - Equinix SV1",
                "portSpeed": 1000,
                "maxAvailableBandwidth": 1000,
            },
            {
                "uniId": "11111111-aaaa-4bbb-8ccc-000000000002",
                "uniServiceId": "CO/DVXX/222222/LVLC",
                "uniName": "DEN-lab-port-1",
                "status": "ACTIVE",
                "location": "Denver, CO - Lumen DC2",
                "portSpeed": 10000,
                "maxAvailableBandwidth": 10000,
            },
        ]:
            self.unis[uni["uniId"]] = uni

        self.evcs["e27a48de-7ab1-46dc-a0b0-a0abea016b5d"] = {
            "evcId": "e27a48de-7ab1-46dc-a0b0-a0abea016b5d",
            "evcName": "lab-aws-connection",
            "evcServiceAlias": "VLXX/D00123/LVLC",
            "status": "ACTIVE",
            "haEvcId": None,
            "bandwidth": 50,
            "billingType": "hourly",
            "cos": "basic",
            "userEmail": "user@email.com",
            "startDateTime": now_iso(),
            "modifyDateTime": None,
            "endDateTime": None,
            "endPoint1": {"uniServiceId": "CA/KXFN/111111/LVLC", "ceVlan": 99},
            "endPoint2": {
                "cloudProvider": "aws-hosted-connection",
                "region": "us-west-1",
                "interconnectId": "dxcon-fh3g3dun",
            },
            "billingAccountNumber": "5-WX3EDQ",
        }

        self.partner_interconnects = {
            "aws-hosted-connection": [
                {"location": "San Jose, CA", "interconnectId": "dxcon-fh3g3dun",
                 "region": "us-west-1", "availableSpeeds": SPEEDS_MBPS},
                {"location": "Ashburn, VA", "interconnectId": "dxcon-a91b2c3d",
                 "region": "us-east-1", "availableSpeeds": SPEEDS_MBPS},
            ],
            "google-interconnect": [
                {"location": "Denver, CO", "interconnectId": "gcp-den-001",
                 "region": "us-central1", "availableSpeeds": SPEEDS_MBPS},
            ],
        }

        self.locations = [
            {"masterSiteId": "PL0000000001", "name": "Equinix SV1",
             "address": "11 Great Oaks Blvd, San Jose, CA", "partnerId": "43558",
             "partner": "Equinix", "naasEnabled": True},
            {"masterSiteId": "PL0000000002", "name": "Lumen Denver DC2",
             "address": "1500 Champa St, Denver, CO", "partnerId": "43559",
             "partner": "Lumen", "naasEnabled": True},
            {"masterSiteId": "PL0000000003", "name": "Flexential Charlotte",
             "address": "8910 Lenox Pointe Dr, Charlotte, NC", "partnerId": "43560",
             "partner": "Flexential", "naasEnabled": False},
        ]

        gw_id = "4c85553e-91ce-4eab-9551-2014985f8c84"   # MCG spec example id
        self.gateways[gw_id] = {
            "gateway_id": gw_id,
            "state": "PROVISIONED",
            "name": "Primary-Prod-MCGW",
            "description": "Primary production gateway for east region cloud connectivity",
            "tier": "50 Gbps",
            "asn": 65010,
            "total_aggregate_bw": "1000 Mbps",
            "term": "Monthly",
            "customer_number": "1-ABCD",
            "billing_account_number": "1-1ABCDE-F",
            "created_at": now_iso(), "created_by": "user@email.com",
            "updated_at": None, "updated_by": None,
        }
        if_id = "f0b9cf7b-18ca-4d04-82d5-76198ca6d34f"    # EFC spec example id
        self.mcg_interfaces[if_id] = {
            "interface_id": if_id,
            "state": "PROVISIONED",
            "name": "Primary Interface",
            "description": "Primary interface for hosted cloud connections",
            "gateway": {"gateway_id": gw_id, "name": "Primary-Prod-MCGW"},
            "connection": None,
            "address_family": "IPV4",
            "created_at": now_iso(), "created_by": "user@email.com",
            "updated_at": None, "updated_by": None,
        }
        conn_id = "d2bdc87d-3f76-4db6-8d2a-1f5f4b3fbbf2"  # EFC spec example id
        self.fabric_connections[conn_id] = {
            "connection_id": conn_id,
            "connection_type": "hosted-aws",
            "state": "active",
            "name": "Hosted AWS Connection",
            "description": "AWS to MCGW primary connection",
            "class_of_service": "basic",
            "bandwidth": 1000,
            "term": "Hourly",
            "source_endpoint": {"gateway": {"gateway_id": gw_id, "name": "Primary-Prod-MCGW"},
                                "interface": {"interface_id": if_id, "name": "Primary Interface"},
                                "ipv4_address": "10.10.0.1/30"},
            "dest_endpoint": {"aws_account_id": "123456789012", "region": "us-east-1"},
            "customer_number": "1-ABCD",
            "billing_account_number": "1-1ABCDE-F",
            "created_at": now_iso(), "created_by": "user@email.com",
            "updated_at": None, "updated_by": None,
        }

        self.services["771234567"] = {
            "id": "771234567",
            "name": "My first NaaS order",
            "status": "active",
            "customerNumber": "1-ABCDE",
            "billingAccountId": "5-RHQBGCGK",
            "productOffering": {"id": "718", "name": "Internet On-Demand"},
            "productSpecification": {"id": "5001", "name": "NaaS Internet"},
            "masterSiteId": "PL0000000001",
            "mUniServiceId": "XX/XXXX/164523/LUMN",
            "bandwidth": 100,
            "bandwidthUnit": "Mbps",
            "ipv4Prefix": "203.0.113.0/29",
            "createdDateTime": now_iso(),
            "modifiedDateTime": None,
        }


store = Store()
