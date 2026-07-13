# Roadmap

## v0.2.0 — Multi-Cloud Gateway + Ethernet Fabric Connect (in progress)

Lumen published two new NaaS products (2026-07-10) that supersede Ethernet On-Demand:

- **Multi-Cloud Gateway** (`/mcgw/v1`) — API-created L3 virtual gateways: interfaces, static routes, BGP sessions, prefix lists
- **Ethernet Fabric Connect** (`/fabric/v1`) — L2 connections onto gateway interfaces: virtual point-to-point plus hosted cloud connections (AWS, GCP, Azure, OCI) with priced bandwidth options

v0.2.0 adds faithful simulation of both, built from the published OpenAPI specs (vendored in [`docs/specs/`](docs/specs/)). Notably, neither new spec lists a mock or sandbox server — making a stateful simulator the only rehearsal environment available for them today.

Existing Ethernet On-Demand and Internet On-Demand simulation remains for teams still on those APIs.

## Later / under consideration

- Multi-tenant sandboxes (isolated state per client credential)
- Conformance harness: same test suite run against the simulator and `api-test.lumen.com`, producing a fidelity scorecard
- Webhook signatures + delivery retries matching platform semantics
- Failure/latency injection ("network weather") for resilience testing
