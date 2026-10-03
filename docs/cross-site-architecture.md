# Cross-site serving behind one endpoint: what we built and why

Several models run on four sites, each its own air-gapped OpenShift cluster:

| Model | Hardware | Sites |
|---|---|---|
| GLM-5.3 FP4 | B200 | 2 |
| Kimi K2.7 | H200 | 3 |
| Qwen3.8 27B | | 2 |

This document explains:
- how one URL in front of them makes the sites behave as **one pool of GPUs per model**;
- how we got to this design;
- what was deliberately left out.

References:
- The design audit, with source citations, is in [`FINDINGS.md`](../FINDINGS.md).
- The operator references are [`fleet/README.md`](../fleet/README.md) (tier 0) and the "Cross-site
  serving" section of [`chart/README.md`](../chart/README.md) (model releases).

---

## 1. Goal and constraints

**Goal:** one URL; every model reachable from any site; each model's requests spread over every
site that serves it. **Which sites serve which model, and how loaded they are, must be discovered,
not listed.** The platform owner's priorities, in order:
1. Use the hardware across all sites (GPU saturation).
2. Keep KV-cache locality where it is real.
3. WAN latency, last.

**Constraints that shaped every choice:**

| Constraint | Consequence |
|---|---|
| Air-gapped clusters, internal registry, internal S3 | Nothing new may need internet. A new component costs an internal image build and mirror. |
| Avi/AKO load balancing, Avi GSLB via AMKO, no MetalLB | No anycast. The global name is DNS GSLB. |
| Inter-site RTT < 10 ms | Distance is cheap. A cross-site hop adds about one RTT to time-to-first-token. |
| Agent Router (Envoy AI Gateway) v1.1 on Envoy Gateway v1.8 | Routing must be expressible in `AIGatewayRoute` / Envoy Gateway policy, or be a new ext-proc. |
| Several models, unevenly placed, mixed hardware across models | Tier 0 can't belong to one model's release, and must not assume a model runs everywhere. |
| One hardware type per model | A model behaves identically on every site that serves it (same recipe, same `max-model-len`). |
| Argo CD delivers the charts; charts stay generic | Model settings in `values/<model>-<hw>.yaml`, cluster settings in `values/sites/<cluster>.yaml`, fleet catalogue in `values/fleet.yaml`. |

---

## 2. Where we started

```
client -> GSLB (Avi/AMKO) -> site VIP (L4) -> Agent Router -> InferencePool -> EPP -> vLLM
          tier 1                                              tier 3
```

- **Tier 1** (DNS) picked a site per resolver, with consistent hashing.
- **Tier 3** (the llm-d Endpoint Picker, EPP) picked a node inside the site.
- **Nothing chose between sites using live signals.**

As a result:
- A saturated site had nowhere to send overflow.
- The GSLB health check said "up" whether a site was idle or at 100%.
- Consistent hashing on a handful of internal resolver addresses can pin most traffic to one or
  two sites.

That was failover, not pooling.

---

## 3. How we got to this design

### 3.1 The original brief, and the audit

The original brief (AGENT.md) proposed a tier 0 on every site:
- a custom ext-proc "picker" choosing a site per request;
- a score of prefix-cache warmth × headroom, with locality dropped;
- a shared cross-site KV index as the ambitious end.

Before building anything we audited every assumption against upstream **source** at the deployed
versions. The results changed the plan.

**What held:**
- **An ext-proc can re-route a request (A1).** Envoy clears the route cache when an ext-proc returns
  a header mutation with `clear_route_cache`. Agent Router's own `x-ai-eg-model` routing works
  exactly this way.
- **A user ext-proc runs after Agent Router's (A2).** Agent Router inserts its ext-proc first, so a
  later one sees the model name.
- **An `AIGatewayRoute` rule can't mix an `InferencePool` with `AIServiceBackend`s (A3).** So the
  local site has to be reached as an `AIServiceBackend`, like every other site.

**What failed, and why it matters:**

1. **Prefix affinity doesn't survive inside a site today (A9, the central finding).** Each B200 pod
   runs 8 data-parallel ranks behind one port. The EPP picks a *pod*. vLLM's internal DP load
   balancer then picks one of 8 independent KV caches **by load only** (vLLM v0.30.0
   `core_client.py:1548`). So sending a conversation back to the "warm" site turns into a cache hit
   only about 1 time in 8. **Cross-site prefix routing would be close to decorative until
   per-rank routing exists.** The fix is Track A, below.
2. **The shared cross-site KV index (A8) would collide.**
   - llm-d keys pods by IP:port, and OpenShift's default pod network is the same on every cluster.
   - The Redis backend has no TTL, so a cut-off site's entries never expire.
   - Every lookup would pay a WAN round trip.
3. **Several mechanisms in the brief were wrong:**
   - A circuit breaker sheds with 503s; it doesn't queue (A10).
   - Polling load faster doesn't stop several pickers herding onto the same site.
   - The load metric named (`vllm:gpu_cache_usage_perc`) doesn't exist in vLLM 0.30; it is
     `vllm:kv_cache_usage_perc`.
4. **Prior art exists.** NVIDIA Dynamo shipped an experimental multi-datacenter KV relay (v1.4,
   2026-08). GKE's multi-cluster gateway does cross-site *saturation* routing, though not *prefix*
   routing.

### 3.2 The key decision: the score has two terms, and only one works today

A9 kills the **prefix** term. It says nothing about the **headroom** term, and headroom is the
stated goal: use every site's GPUs. So the work split into two tracks that run in parallel:

- **Track B: pooling across sites.** Gateway configuration only; it delivers the goal now. **This
  is what was built.**
- **Track A: affinity inside a site.** Per-rank endpoints, KV events, precise prefix scoring and
  CPU KV offloading. It changes the serving stack on production clusters, so it is the riskier
  track and it doesn't block B.

### 3.3 Why no custom picker in v1

Track B could have started with the picker ext-proc. We chose Envoy-native first:

- **Without A9 fixed, the picker's main advantage (affinity) is worth little.** Pooling and
  overflow alone don't need it.
- **A picker is a new air-gapped workload**: a Go service, an internal image, and its own
  availability problem in the request path of every site.
- **The source showed Envoy can already do the important half:**
  - shedding at the destination plus retrying at the sender moves overflow to another site with no
    load reporting at all (a 503 carrying `x-envoy-overloaded` is retried; retries skip hosts
    already tried);
  - health checks give membership;
  - least-request gives a local load signal.

### 3.4 From one model to a fleet, and the correction it forced

The first version (`crossSite` in the model chart, PR #2) assumed one model on a homogeneous B200
fleet. It had three properties:
- each model release carried its own tier-0 route;
- each release had a hand-written list of sites;
- sites were weighted by node count.

The actual fleet broke all three:

- **A client can enter at a site without the model.** Only releases of that model rendered its
  tier-0 route, so the other sites returned 404.
- **GSLB monitored one model.** A site without GLM looked dead to everyone, Kimi clients included.
- **Placement was hand-maintained.** Adding a model to a site meant editing every site's file.

So tier 0 moved out of the model release into **one identical fleet release per cluster**. That
release covers every model, discovers placement by health check, and monitors the gateway, not a
model.

Making load dynamic exposed an error in the first design, found in Envoy Gateway's source:
- Envoy Gateway's default balancer is already least-request, but with **locality weighting**, and
  every `backendRef` becomes its own locality.
- Envoy picks a locality by weight first. With one backend per site, least-request then has a
  single host to choose from.
- **For least-request (or hashing) to choose between sites, every site must be an endpoint of one
  `Backend`.** That is one locality, and endpoint weights are fixed at 1.

Static capacity weights and live least-request therefore exclude each other. We chose
least-request: it needs no node counts, and it adapts to what the gateway sees.

---

## 4. The architecture as deployed

```
                              llm.<domain>
                                   |
      Avi GSLB (AMKO), round robin, monitors GET /healthz/gateway: picks the ENTRY gateway only
                                   |
                     site VIP (AKO L4) of any site
                                   |
  +---------- Agent Router, client listener (https): FLEET release, same on every cluster --------+
  |  per model m:                                                                                  |
  |    SecurityPolicy fleet-<m>:  API key -> x-tenant-id (the customer)                            |
  |    AIGatewayRoute fleet-<m>:  pin-<site> rules (x-site-pin) | pool -> AIServiceBackend          |
  |                                                              fleet-all-sites (every site = 1   |
  |                                                              endpoint, one locality)           |
  |    BackendTrafficPolicy fleet-<m>:                                                             |
  |      loadBalancer LeastRequest (or ConsistentHash on a header, off)                            |
  |      healthCheck GET /healthz/<m> on every site  -> membership; panicThreshold 0               |
  |      retry 503/connect-failure/reset, up to sites-1, each to a different site                  |
  |      token metering + budget per customer for m (entry site, once; counters in the hub Redis)  |
  |  /healthz/gateway -> 200 (directResponse)                                                      |
  +-----------------------------------------------+-----------------------------------------------+
                                                  | mTLS (client cert), SNI = shared hostname
                                                  v
  +---- chosen site's Agent Router, peer listener (8443, mTLS): MODEL releases on that site ------+
  |  serving AIGatewayRoute <release> -> InferencePool     health route /healthz/<m> -> InferencePool |
  |  BackendTrafficPolicy <release>-shed: circuit breaker = requestsPerNode x nodes / replicas      |
  +-----------------------------------------------+-----------------------------------------------+
                                                  v
                                InferencePool -> EPP -> vLLM (one pod per node)
```

### 4.1 One request, end to end

1. **DNS.** The client resolves `llm.<domain>`; Avi returns a VIP of any site whose gateway is up
   (round robin).
2. **Authentication.** The entry gateway checks the request's API key and writes the customer's
   ID into `x-tenant-id`, replacing anything the client sent. An unknown key gets 401. If that
   customer's budget for the model is already spent, the request gets 429 here.
3. **Tier 0.** The entry gateway matches the request's model to that model's fleet route. Envoy
   considers only the sites whose `/healthz/<model>` currently answers 200: the sites serving it,
   possibly the entry site itself. It picks two at random and sends to the one with fewer requests
   in flight from this gateway replica.
4. **The hop.** The request goes over mTLS to that site's peer listener and the model release's
   serving route.
5. **Shed or serve.** Under the site's in-flight limit, the request goes to the InferencePool, the
   EPP picks a node, then vLLM. Over the limit, the site answers 503 immediately.
6. **Retry.** On a 503, connect failure or reset, tier 0 retries a different site serving the
   model, up to `sites − 1` times, with a 100 ms to 1 s backoff. It retries only before any response
   bytes, so a stream is never duplicated.
7. **Metering.** Tier 0 reads token usage from the response and charges the customer's budget for
   that model, once, at the entry site. The counter lives in the Redis on the hub, so it is the
   same counter whichever site the customer enters through.

### 4.2 Component by component: what, and why

| Piece | What it is | Why this way |
|---|---|---|
| **Fleet release per cluster** | `fleet/` chart: a route + policy per model, the site Backends, gateway health, global name | Tier 0 serves every model, so it can't live in one model's release. Identical everywhere: any site is a valid entry for any model. |
| **Catalogue** | `values/fleet.yaml`: models (what is served) and sites | One line per model or site. **Where** a model runs isn't listed anywhere. |
| **Membership by health check** | Per model: `GET /healthz/<model>` on every site's peer listener; `panicThreshold: 0` | Deploying a model on a site makes it pass and join; removing it makes it leave. A site without the model is never routed to, even when most sites are down. |
| **One all-sites Backend** | Every site an `fqdn` endpoint of `fleet-all-sites` | Puts the sites in one locality, so least-request and hashing compare sites. Envoy Gateway builds one cluster per route rule, so each model gets its own health checks over the shared Backend. |
| **Least-request** | `loadBalancer: LeastRequest` | A saturated site holds requests longer and gets fewer new ones. No node counts to maintain. |
| **Hostname-only addresses** | Render fails on an IP | An IP among hostnames splits the cluster per site. Least-request, hashing and cross-site retry all depend on one cluster. |
| **Peer listener (mTLS)** | Model releases' serving routes sit on `crossSite.peerListener` | **Loop prevention by construction.** A peer request can only reach a serving route, never tier 0 again. **No bypass.** Clients can't reach the unmetered serving routes without a peer certificate. |
| **Shed at the receiver** | Circuit breaker on each serving route | The receiver knows its capacity. It turns "full" into an instant 503 instead of an unbounded vLLM queue. Sized from vLLM's concurrency log at a realistic context, because the limit counts requests and the real cost is tokens. |
| **Retry at the sender** | `numRetries` = sites − 1, `previous_hosts` | A shed request reaches every other site before failing. Bounded, so a saturated fleet returns 503 instead of looping. |
| **Busy and dead are separate** | Health checks remove; shedding only refuses one request | A loaded site is never marked down, so load can't cascade into an outage. |
| **Raised tier-0 limits** | `fleet.maxParallelRequests` | Envoy's default 1024 per replica would cap a model fleet-wide through one entry gateway. |
| **API keys** | `SecurityPolicy` per model route (`fleet.auth.apiKey`); keys in a Secret, one entry per customer | The gateway derives `x-tenant-id` from the key, so a customer can't forge a tenant or skip the limit by omitting the header. Per route, not per listener, so the Avi monitor needs no key. |
| **Metering at entry** | `llmRequestCosts` + budget on the fleet routes; none on serving routes | Charged once. It fails open: lapsed budgets beat a fleet outage. |
| **One budget fleet-wide** | Every site's rate limit service uses one Redis on the hub; the rule is `shared` | Envoy Gateway keys a counter on the route rule by default, which would give each `pin-<site>` rule its own budget. A shared rule is keyed on the policy's namespace and name, which are identical on every site, so all sites and all rules count into one bucket per customer and model. |
| **Gateway health for GSLB** | `/healthz/gateway`, answered by Envoy | Any gateway can proxy any model, so GSLB monitors the gateway. Per-model health is tier 0's job. |
| **`x-site-pin`** | Per-site rules with an extra header match | Model releases' benchmark hooks pin to their own site, so a rolling upgrade is gated per site. Also useful for debugging. |
| **Consistent hashing (off)** | `models[].hashHeader` | Keeps a conversation on one site, and a retry steps to a stable second site. Off until clients send a session header. Never hash on the tenant. |

### 4.3 Behaviour under failure and change

| Event | What happens |
|---|---|
| A site doesn't serve model m | Its `/healthz/m` fails; it never gets m's traffic, from any entry. |
| Model m is deployed on a new site | It passes the health check after `healthyThreshold × interval` and joins m's pool. Nothing else changes. |
| A new model | One line in `values/fleet.yaml`, a sync of every fleet release, then the model release where it should run. |
| One site's GPUs full for m | It sheds 503; tier 0 retries the other sites serving m. Least-request already steers new requests away. |
| Every site serving m full | Each attempt is shed; after `sites − 1` retries the client gets 503. Load can't amplify. |
| m down on a site | It fails m's health check within ~15 s and leaves m's pool; other models on that site are unaffected. |
| A site's gateway down | Connect failures are retried elsewhere at once, and health checks remove it from every model. Avi stops sending entry traffic there. |
| The entry gateway restarts | Streams entering there are cut, wherever they were served (two gateways per stream). |
| Hub Redis down or slow | Budgets lapse (fail open); inference continues; alerts fire. Slow counts too: past `rateLimit.timeout` the request passes unmetered. |
| Unknown or revoked API key | 401 at the entry gateway; nothing reaches a model. |
| A customer's budget spent | The request that crosses it completes; the next ones get 429 at any site until the window resets. |
| A client sends `x-tenant-id` itself | Overwritten with the key's client ID. |

---

## 5. Configuration

**Catalogue** (`values/fleet.yaml`, identical on every cluster):

```yaml
models:
  - name: glm-5.3
  - name: kimi-k2.7
  - name: qwen3.8-27b
    # hashHeader: x-session-id     # once clients send one
sites:
  - {name: site1, address: llm.site1.<domain>}
  - {name: site2, address: llm.site2.<domain>}
  - {name: site3, address: llm.site3.<domain>}
  - {name: site4, address: llm.site4.<domain>}
fleet:
  auth:
    apiKey: {enabled: true, secretNames: [llm-api-keys]}   # customers' keys, same Secret on every cluster
  defaults:
    rateLimit: {enabled: true, clientHeader: x-tenant-id, tokens: 2000000, unit: Hour}
```

**Per model** (`values/<model>-<hw>.yaml`): the recipe, plus
`crossSite.serving.requestsPerNode`, taken from vLLM's
`Maximum concurrency for <N> tokens per request` at a realistic N, plus a short queue.

**Per cluster** (`values/sites/<cluster>.yaml`, read by both charts):

```yaml
route:
  gateway: {name: ai-gateway, namespace: ai-gateway, sectionName: https}   # client listener
crossSite:
  enabled: true                 # model releases on this cluster serve the fleet
  self: site1                   # this site's name in values/fleet.yaml
  peerListener: peers
  tls:
    caCertificateRef: {kind: ConfigMap, name: llm-peer-ca}
    clientCertificateSecret: llm-peer-client
  serving:
    gatewayReplicas: 2          # fixed
globalName:                     # read by the fleet chart
  enabled: true
  fqdn: llm.<domain>
  hostRule: {namespace: envoy-gateway-system, localFqdn: llm.site1.<domain>}
  gslbHostRule: {create: true, healthMonitorRefs: [fleet-gateway-health]}   # leader only
```

**Per cluster, outside the charts** (the gateway is shared):
- a peer listener (8443, no hostname, a certificate for the shared hostname);
- a `ClientTrafficPolicy` requiring client certificates on it;
- the CA and client certificate;
- `enableBackend: true`;
- an Avi monitor on `/healthz/gateway`;
- the Envoy Gateway rate limit service pointed at the hub Redis, with a raised timeout
  (`values/envoy-gateway.example.yaml`);
- the API-key Secret, identical on every cluster.

**Safety nets:**
- **Both charts refuse to render** on:
  - an IP site address;
  - a missing or identical client/peer listener;
  - duplicate models or sites;
  - `numRetries` > sites − 1;
  - a missing certificate, health route or replica count.
- **`validate.py` checks either render.**
  - Fleet render: one route and one policy per model; retry, health check and `panicThreshold: 0`
    present; one all-sites backendRef per pool rule; hostname endpoints; a gateway health route
    behind GSLB.
  - Model render: a shed policy, a pinned benchmark, the health route on the peer listener, and no
    double metering.

---

## 6. Rolling it out

1. **On every cluster:**
   - set up the peer listener, `ClientTrafficPolicy`, certificates and `enableBackend`;
   - install the fleet release.

   No model is in any pool yet: every health check fails until a model release serves on the peer
   listener.
2. **Enable `crossSite` in one model release on a canary site.** It passes its health check and
   becomes reachable from every entry gateway. Verify with Envoy `/clusters` and one pinned `curl`
   per site and model.
3. **Enable the remaining model releases** site by site, each gated by its pinned benchmark hook.
4. **Point Avi's monitor at `/healthz/gateway`** and use round robin.

---

## 7. What it deliberately does not do (and what comes next)

| Not done | Why | When |
|---|---|---|
| Prefix affinity across sites | With least-request a conversation returns to its warm site only ≈ 1/sites of the time. Inside a site, DP8 behind one port already loses most hits (A9). | Turn on `hashHeader` per model once clients send a session header and Track A fixes A9 |
| Capacity weights | Exclude least-request in Envoy Gateway v1.8. A much bigger site for a model is underused until smaller ones shed. | A picker, or per-model weights if site sizes diverge a lot |
| A fleet-wide load signal | Least-request sees only its own gateway replica. vLLM's ORCA reports are non-streaming and per pod. | Picker: banded-headroom rendezvous hashing, a single τ knob (FINDINGS Step 4) |
| Cross-site KV index | A shared Valkey index collides and goes stale (A8) | Dynamo DC KV Relay pattern via an event-stream shim (FINDINGS Step 6) |
| GSLB `downResponse` | Exact AMKO field not confirmed | When confirmed |

**Track A runs in parallel:**
- per-rank DP endpoints (InferencePool `targetPorts`, up to 8 per pod);
- KV events, precise prefix scoring and CPU KV offloading, sized from node RAM;
- **gate:** measure A9 first (`vllm:prefix_cache_hits_total / vllm:prefix_cache_queries_total` per
  engine).

---

## 8. Open items before production

1. The model recipes: GLM-5.3 FP4 B200, Kimi K2.7 H200, Qwen3.8 27B. Each sets `requestsPerNode`
   from vLLM's concurrency log.
2. Confirm the CRD fields both charts use against the installed versions (FINDINGS, open
   question 11).
3. On the first site, confirm the InferencePool cluster keeps the shed circuit breaker, and each
   fleet cluster lists every site.
4. The Redis on the hub: deployed outside these charts, exposed as a LoadBalancer Service, with
   TLS and a password. Then the cross-site budget test: spend a customer's budget through one
   site and expect 429 through another.
5. How the API-key Secret is distributed to every cluster, and who issues keys.
6. On the first site: a forged `x-tenant-id` is charged to the key's own client ID, and
   `Authorization: Bearer <key>` is accepted.
7. A session header, if consistent hashing is wanted.
