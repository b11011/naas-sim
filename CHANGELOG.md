# Changelog

## v0.2.0 — 2026-07-13

**New-generation API simulation** — Lumen published Multi-Cloud Gateway and
Ethernet Fabric Connect (2026-07-10), superseding Ethernet On-Demand. Both are
now simulated from the published specs (vendored in `docs/specs/`):

- **Multi-Cloud Gateway** (`/mcgw/v1`): gateway lifecycle (`PENDING → PROVISIONING → PROVISIONED`, 201+Location), interfaces, static routes, prefix lists, BGP sessions; tier capacity enforcement (10/50 Gbps/Unlimited aggregate caps → 422)
- **Ethernet Fabric Connect** (`/fabric/v1`): virtual p2p + hosted cloud connections (AWS/GCP/Azure/OCI, 202-async `provisioning → active`), bandwidth-enum PATCH with `updating` state and concurrent-change 409, priced bandwidth options, billing endpoint, endpoint validation against gateways/interfaces
- **RFC 7807 `problem+json` errors** on the new APIs (per spec), legacy `{code, message}` envelope retained for EOD/IOD — path-aware handlers and OpenAPI docs
- **No webhooks on the new APIs** — matching the published specs (polling only); events still visible at `/_lab/events`
- Persistence + startup reconciliation cover the new resources; seed data uses spec-example identifiers

## v0.1.1 — 2026-07-11
## v0.1.1 — 2026-07-11

Found in demo rehearsal:

- `/_lab/reset` now **preserves webhook registrations** — they're integration config, not scenario state; previously a lab reset silently severed consumers' completion events until the consumer restarted (circuits stuck in `MODIFYING`)
- middleware `POST /reconcile` re-registers its completion webhook before sweeping, so one call self-heals both the state and the eventing leg

## v0.1.0 — 2026-07-09

First versioned release.

### Simulator
- Ethernet On-Demand v5: UNIs, EVCs, HAEVCs; bandwidth PATCH with async `MODIFYING → ACTIVE` state machine and request tracking
- Internet On-Demand: qualify → quote (15-min TTL) → order pipeline; 24 orders/day quota; order-contact validation; Flexential site restriction
- OAuth2 client-credentials; Lumen-style `{code, message}` error envelopes (validation failures return 400, and the OpenAPI schema matches the runtime behavior)
- Webhook fan-out for async completions; `/_lab` inspection endpoints (`state`, `events`, `reset`)

### Product layer (new in this release)
- **Container image**: published to `ghcr.io/b11011/naas-sim` on every release (`docker run -p 8080:8080 ghcr.io/b11011/naas-sim`)
- **Usage metrics** (`GET /_lab/metrics`): request counts by route/status plus error-type frequencies — which mistakes integrators actually make
- **Opt-in persistence** (`NAAS_SIM_STATE_FILE`): JSON snapshot; in-flight transitions are completed on restart, so nothing sticks in a transitional state
- **Seedable catalog** (`POST /_lab/seed` or `NAAS_SIM_SEED_FILE`): load your own locations, ports, circuits, services, and speed tiers — example in `examples/seed-profile.json`

### NetBox integration
- Idempotent model/data seeder (`scripts/seed_netbox.py`), custom script for UI-driven changes (`scripts/change_bandwidth.py`)
- Event-driven middleware (`middleware/app.py`): commit_rate edits reconcile against the API with completion webhooks and journal audit trail — now with a **startup reconciliation sweep** that converges drift and unsticks stale statuses after missed webhooks
