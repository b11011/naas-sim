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

    def reset(self):
        self.tokens = {}            # access token -> expiry (epoch seconds)

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
