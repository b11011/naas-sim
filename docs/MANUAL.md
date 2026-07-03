# Lumen NaaS Simulator — User Manual

> Independent project for development and education, based on Lumen's publicly
> published OpenAPI specs. Not affiliated with or endorsed by Lumen Technologies.

A stateful lab replica of Lumen's NaaS APIs for developing and testing solutions
without touching the real platform. It simulates the two bandwidth-on-demand
mechanisms exposed on the [Lumen Connect developer portal](https://developer.lumen.com/):

- **Ethernet On-Demand v5** — Layer 2 circuits (EVCs) with a direct bandwidth `PATCH`
- **Internet On-Demand** — TMF-style qualify → quote → order pipeline

Everything here was designed against Lumen's published OpenAPI specs
([Ethernet On-Demand](https://d26yp52fi26crs.cloudfront.net/docs/ethernet-on-demand/openapi/Ethernet_On-Demand_API_8_1_2025.yaml),
[Internet On-Demand](https://d26yp52fi26crs.cloudfront.net/docs/internet-on-demand/openapi/Internet_On-Demand_2_19_2026.yaml)),
so client code you write against the lab carries over to the real API with minimal change.

---

## 1. Installation and startup

Requirements: Python 3.11+.

```bash
cd ~/Documents/code/naas
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python -m simulator
```

The simulator listens on **http://localhost:8080**. Useful pages:

| URL | What it is |
|---|---|
| `http://localhost:8080/` | Service card — endpoints at a glance |
| `http://localhost:8080/docs` | Interactive Swagger UI (try every endpoint from the browser) |
| `http://localhost:8080/redoc` | Read-only API reference |

Run the test suite any time with `pytest` (from the repo root, venv active).

### Configuration

All settings are environment variables read at startup:

| Variable | Default | Purpose |
|---|---|---|
| `NAAS_SIM_HOST` / `NAAS_SIM_PORT` | `127.0.0.1` / `8080` | Listen address |
| `NAAS_SIM_CLIENT_ID` / `NAAS_SIM_CLIENT_SECRET` | `naas-lab-client` / `naas-lab-secret` | OAuth client credentials |
| `NAAS_SIM_DELAY_SECONDS` | `10` | How long async operations stay transitional (`MODIFYING`, `CREATING`, orders in flight). Real platform: ~minutes. Set low (e.g. `2`) for rapid dev loops. |
| `NAAS_SIM_QUOTE_TTL_SECONDS` | `900` | Internet On-Demand quote validity (real: 15 min) |
| `NAAS_SIM_DAILY_ORDER_LIMIT` | `24` | Order quota per customer per GMT day (real: 24) |
| `NAAS_SIM_TOKEN_TTL_SECONDS` | `3600` | Access-token lifetime |

Example: `NAAS_SIM_DELAY_SECONDS=2 python -m simulator`

State is **in-memory**: restarting the process (or calling `POST /_lab/reset`)
returns everything to the seed data below.

### Seed data

The lab boots with identifiers copied from Lumen's own documentation examples, so
requests pasted from the vendor docs work unchanged:

| Object | ID | Details |
|---|---|---|
| EVC | `e27a48de-7ab1-46dc-a0b0-a0abea016b5d` | ACTIVE, 50 Mbps, hourly, on UNI `CA/KXFN/111111/LVLC` |
| UNI 1 | `CA/KXFN/111111/LVLC` | San Jose, 1 Gbps port (max bandwidth 1000 Mbps) |
| UNI 2 | `CO/DVXX/222222/LVLC` | Denver, 10 Gbps port |
| Billing account (EOD) | `5-WX3EDQ` | Goes in the `x-billing-account-number` header |
| Internet service | `771234567` | active, 100 Mbps, at site `PL0000000001` |
| Customer number (IOD) | `1-ABCDE` | Goes in the `x-customer-number` header |
| Billing account (IOD) | `5-RHQBGCGK` | |
| Locations | `PL0000000001` (Equinix SV1), `PL0000000002` (Lumen Denver), `PL0000000003` (Flexential — rejects bandwidth changes) | |

Offered speeds at every location: **10, 20, 50, 100, 200, 500, 1000 Mbps**.

---

## 2. Authentication

Every API call (except `/_lab/*` and `/`) needs a bearer token from the OAuth2
client-credentials flow, exactly like Lumen Connect:

```bash
TOKEN=$(curl -s -X POST http://localhost:8080/oauth/v2/token \
  -u naas-lab-client:naas-lab-secret \
  -d grant_type=client_credentials | python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
```

The client id/secret can go in HTTP Basic auth (shown above) or as
`client_id`/`client_secret` form fields. Response:

```json
{"access_token": "…", "token_type": "Bearer", "expires_in": 3600}
```

Then send `Authorization: Bearer $TOKEN` on every request. Expired or missing
tokens get `401 {"code": 401, "message": "invalid or expired token"}`.

---

## 3. Ethernet On-Demand (Layer 2 circuits)

Base path: `/Network/v5/DynamicConnection`. All EVC/HAEVC routes additionally
require the header **`x-billing-account-number`** (seeded value: `5-WX3EDQ`).

### Concepts

- **UNI** — your physical port; its `maxAvailableBandwidth` caps every EVC riding on it.
- **EVC** — an Ethernet Virtual Connection between a UNI and a cloud/data-center partner.
- **HAEVC** — a high-availability pair of EVCs managed as one unit.
- **Request records** — every async operation (create/modify/delete) produces a
  request you can poll at `/evcRequests` / `/haEvcRequests`.

### Endpoints

| Method & path | Purpose |
|---|---|
| `GET /unis`, `GET /unis/{id}` | List/inspect your ports |
| `GET /partnerInterconnects/{productType}` | Where you can land a circuit (`aws-hosted-connection`, `google-interconnect`) |
| `GET /evcs`, `GET /evcs/{evcId}` | List/inspect circuits |
| `POST /evcs` | Create a circuit (202 → `CREATING` → `ACTIVE`) |
| **`PATCH /evcs/{evcId}`** | **Change bandwidth** (202 → `MODIFYING` → `ACTIVE`) |
| `DELETE /evcs/{evcId}?userEmail=…` | Tear down (202 → `DELETING` → gone) |
| `GET/POST/PATCH/DELETE /haEvcs…` | Same lifecycle for HA pairs |
| `GET /evcRequests?evcId=…` | Track async operations |

### Walkthrough: change circuit bandwidth

```bash
EVC=e27a48de-7ab1-46dc-a0b0-a0abea016b5d
AUTH=(-H "Authorization: Bearer $TOKEN" -H "x-billing-account-number: 5-WX3EDQ")

# 1. Check the circuit
curl "${AUTH[@]}" http://localhost:8080/Network/v5/DynamicConnection/evcs/$EVC
# -> "status": "ACTIVE", "bandwidth": 50

# 2. Request the change
curl "${AUTH[@]}" -X PATCH -H "Content-Type: application/json" \
  http://localhost:8080/Network/v5/DynamicConnection/evcs/$EVC \
  -d '{"bandwidth": 200, "userEmail": "user@email.com"}'
# -> 202 {"evcId": "…", "evcRequestId": "…", "status": "MODIFYING"}

# 3. Poll until it lands (or listen on a webhook, §5)
curl "${AUTH[@]}" http://localhost:8080/Network/v5/DynamicConnection/evcs/$EVC
# after NAAS_SIM_DELAY_SECONDS -> "status": "ACTIVE", "bandwidth": 200, "modifyDateTime" set
```

Or use the ready-made client: `python examples/eod_change_bandwidth.py --bandwidth 200`

### Rules the simulator enforces

| Condition | Response |
|---|---|
| EVC doesn't exist | `404` |
| EVC not `ACTIVE` (e.g. already `MODIFYING`) | `400 "…bandwidth can only be modified while ACTIVE"` |
| Bandwidth exceeds the UNI's `maxAvailableBandwidth` | `400 "…exceeds max available 1000 Mbps on UNI …"` |
| Missing `x-billing-account-number` header | `400` |
| Missing/expired token | `401` |

### Creating a circuit

```bash
curl "${AUTH[@]}" -X POST -H "Content-Type: application/json" \
  http://localhost:8080/Network/v5/DynamicConnection/evcs \
  -d '{
    "evcName": "my-test-circuit",
    "bandwidth": 100,
    "billingType": "hourly",
    "cos": "basic",
    "userEmail": "user@email.com",
    "uniServiceId": "CO/DVXX/222222/LVLC",
    "ceVlan": 200,
    "cloudProvider": "aws-hosted-connection",
    "cloudProperties": {"region": "us-west-1"}
  }'
```

> The creation body is a *simplified* version of the real spec (flat
> `uniServiceId`/`cloudProvider` instead of the full nested endpoint schema).
> GET/PATCH/DELETE shapes follow the spec.

---

## 4. Internet On-Demand (DIA services)

Headers: **`x-customer-number`** (seeded: `1-ABCDE`) on ordering, pricing, and
inventory calls. Product code for Internet On-Demand is always **`718`**.

### The three-step bandwidth change

Bandwidth changes are *orders*, not direct PATCHes — same as the real platform:

```bash
H=(-H "Authorization: Bearer $TOKEN" -H "x-customer-number: 1-ABCDE" -H "Content-Type: application/json")

# Step 0 — find your service in inventory
curl "${H[@]}" "http://localhost:8080/ProductInventory/v1/inventory"
# -> service 771234567, bandwidth 100, masterSiteId PL0000000001

# Step 1 — QUALIFY: what speeds does this location offer?
curl "${H[@]}" "http://localhost:8080/Product/v1/price?productCode=718&masterSiteId=PL0000000001&serviceId=771234567"
# -> offerings: 10…1000 Mbps with monthly/hourly prices

# Step 2 — PRICE: get a quote (valid 15 minutes!)
curl "${H[@]}" -X POST http://localhost:8080/Product/v1/priceRequest \
  -d '{"productCode":"718","masterSiteId":"PL0000000001","serviceId":"771234567","speed":"200 Mbps"}'
# -> {"quoteId": "NaaS-1001", "status": "VALIDATED", …}

# Step 3 — ORDER: promote the quote to a modify order
curl "${H[@]}" -X POST http://localhost:8080/Customer/v3/Ordering/orderRequest \
  -d '{
    "externalId": "EXT-001",
    "productOrderItem": [{
      "id": "Order1", "quantity": 1, "action": "modify",
      "product": {"id": "771234567", "productCharacteristic": []},
      "productOffering": {"id": "718", "name": "Internet On-Demand"}
    }],
    "quote": [{"id": "NaaS-1001", "name": "quoteId"}],
    "relatedContactInformation": [{
      "number": "5555550100", "emailAddress": "you@example.com",
      "role": "Order Contact", "organization": "Lab", "name": "FirstName LastName"
    }]
  }'
# -> 201 {"id": "<orderId>", "state": "acknowledged", …}

# Step 4 — wait for completion, then confirm
curl "${H[@]}" http://localhost:8080/Customer/v3/Ordering/orderRequest/<orderId>   # lab extension
curl "${H[@]}" "http://localhost:8080/ProductInventory/v1/inventory?id=771234567"  # bandwidth: 200
```

Or in one shot: `python examples/iod_change_bandwidth.py --speed 200`

### Other order actions

- **`action: "add"`** — new service. Requires a valid quote; creates a new inventory
  entry (`in progress` → `active`) and returns its generated service id.
- **`action: "modify"` without a quote** — rename. Put a
  `{"name": "Customer Service Name", "value": "new name"}` product characteristic on the item.
- **`action: "delete"`** — disconnect. Service goes `disconnecting`, then leaves inventory.

### Rules the simulator enforces (all mirror the real platform)

| Condition | Response |
|---|---|
| More than 24 orders per customer per GMT day | `429` (resets at GMT midnight) |
| Quote expired (>15 min) or unknown | `400` |
| No contact with `role: "Order Contact"` | `400` |
| Order Contact `name` lacks first + last name | `400 "…first and last name separated by a space"` |
| Bandwidth change on a Flexential site (`PL0000000003`) | `400 "bandwidth update is not available on Flexential sites"` |
| Speed not in the offered list | `400` (lists valid speeds) |
| `modify`/`delete` for a service not in inventory | `404` |

---

## 5. Webhooks and events

Async completions (bandwidth applied, circuit created/deleted, order completed)
generate events. Two ways to consume them:

**Pull (zero setup):**

```bash
curl -s http://localhost:8080/_lab/events   # every event, newest last — no auth needed
```

**Push (like the real platform):** register a callback URL; the simulator POSTs
each event to it as JSON.

```bash
# Terminal 1: a receiver that prints whatever arrives
uvicorn examples.webhook_receiver:app --port 9000

# Terminal 2: register it
curl "${H[@]}" -X POST http://localhost:8080/Customer/v3/Ordering/notifications \
  -d '{"callback": "http://localhost:9000/events"}'
```

Event shape (simulator-defined, not Lumen's exact schema):

```json
{
  "eventId": "…",
  "eventType": "evc.bandwidthModified",
  "eventTime": "2026-07-02T16:37:45Z",
  "event": {"evcId": "…", "status": "ACTIVE", "bandwidth": 200, "evcRequestId": "…"}
}
```

Event types: `evc.created` / `evc.bandwidthModified` / `evc.deleted`, the same
three for `haEvc.*`, and `productOrderStateChangeEvent` for Internet On-Demand orders.
Failed deliveries are logged and skipped (no retries).

---

## 6. Lab controls

Unauthenticated helpers under `/_lab` — **not** part of the Lumen surface:

| Endpoint | Purpose |
|---|---|
| `GET /_lab/state` | Full dump: UNIs, EVCs, services, quotes, orders, quota counters, webhooks |
| `GET /_lab/events` | Every emitted event |
| `POST /_lab/reset` | Restore seed data (wipes tokens too — re-authenticate after) |

---

## 7. Developing and testing your solutions against the lab

- **Point your client at the lab** via a base-URL setting. The example scripts read
  `NAAS_BASE_URL` (default `http://localhost:8080`) — copy that pattern, and swapping
  to `https://api-test.lumen.com` later is a one-line change.
- **Fast feedback:** run the simulator with `NAAS_SIM_DELAY_SECONDS=2` so state
  transitions don't slow your loop.
- **Deterministic tests:** see `tests/test_flows.py` for the pattern — the repo's
  `conftest.py` sets a 0.2 s delay and a small quota before the app is imported, and
  tests poll with a `wait_for(predicate)` helper instead of fixed sleeps. In-process
  testing via FastAPI's `TestClient` needs no running server.
- **Isolation:** call `POST /_lab/reset` between test scenarios, and use a distinct
  `x-customer-number` per scenario so quota counters don't interfere.
- **Exercise failure paths:** the enforced rules in §3–§4 (quota, quote expiry,
  capacity, Flexential) exist precisely so your solution can handle real-platform
  errors before it meets them.

### Reference clients

| Script | What it demonstrates |
|---|---|
| `examples/eod_change_bandwidth.py` | Token → PATCH → poll until ACTIVE |
| `examples/iod_change_bandwidth.py` | Inventory → qualify → quote → order → poll |
| `examples/webhook_receiver.py` | Receiving push notifications |

---

## 8. Error format

All errors use the Lumen-style envelope:

```json
{"code": 400, "message": "quote NaaS-1001 has expired; quotes are valid for 15 minutes"}
```

Request-body validation failures return **400** (not FastAPI's usual 422) with an
`errors` array of `{field, message}`. Status codes used: `400` invalid input/state,
`401` auth, `404` unknown resource, `429` daily quota, `5xx` never expected — report those as bugs.

## 9. Known deviations from the real APIs

1. EVC/HAEVC **creation** request bodies are simplified (§3).
2. `GET /Customer/v3/Ordering/orderRequest/{id}` is a lab-only polling convenience;
   the real platform reports order state via the separate Order Status API and webhooks.
3. `POST /Customer/v3/Ordering/notifications` is modeled as webhook *registration*.
4. Webhook payload schema is simulator-defined.
5. Prices are a deterministic formula (`$55 + $1.10/Mbps` monthly), not real rate cards.
6. No pagination filters beyond `pageIndex`/`pageSize` on EVC/UNI lists.

## 10. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `401 invalid or expired token` | Tokens live 1 h and die on restart/reset — fetch a fresh one |
| `address already in use` at startup | Something else owns the port: `NAAS_SIM_PORT=8081 python -m simulator` |
| PATCH returns `400 …only be modified while ACTIVE` | Previous change still in flight — wait out the transition delay |
| `429` on orders | Daily quota hit — different `x-customer-number`, `POST /_lab/reset`, or raise `NAAS_SIM_DAILY_ORDER_LIMIT` |
| Quote rejected | Older than 15 min — request a new one (step 2) |
| Changes vanished | Process restarted; state is in-memory by design |
| Webhook not arriving | Receiver must be reachable from the simulator host; check the simulator log for `webhook delivery … failed` |
