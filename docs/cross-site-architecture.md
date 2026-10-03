# Cross-site serving behind one endpoint: what we built and why

GLM-5.3 runs on several B200 sites, each its own air-gapped OpenShift cluster. This document
explains how one URL in front of them makes the sites behave as **one pool of GPUs**, how we got to
this design, and what was deliberately left out.

- The design audit, with source citations, is in [`FINDINGS.md`](../FINDINGS.md).
- The operator reference (values, pre-reqs, verify commands) is the "Cross-site pooling" section of
  [`chart/README.md`](../chart/README.md).

---

## 1. Goal and constraints

**Goal:** one URL; every site's GPUs usable by every client. The platform owner's priorities, in
order:
1. Use the hardware across all sites (GPU saturation).
2. Keep KV-cache locality where it is real.
3. WAN latency, last.

**Constraints that shaped every choice:**

| Constraint | Consequence |
|---|---|
| Air-gapped clusters, internal registry, internal S3 | Nothing new may need internet. A new component costs an internal image build and mirror. |
| Avi/AKO load balancing, Avi GSLB via AMKO, no MetalLB | No anycast. The global name is DNS GSLB. |
| Inter-site RTT < 10 ms | Distance is cheap. A cross-site hop adds about one RTT to time-to-first-token. |
| Agent Router (Envoy AI Gateway) v1.1 on Envoy Gateway | Routing must be expressible in `AIGatewayRoute` / Envoy Gateway policy, or be a new ext-proc. |
| Fleet: **GLM-5.3 FP4 on B200 only**, a subset of the sites | Homogeneous: one recipe, so capacity = node count. |
| Argo CD delivers the chart; the chart stays generic | Model settings in `values/<model>-<hw>.yaml`, cluster settings in `values/sites/<cluster>.yaml`. |

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
- **The source showed Envoy can already do the important half.** Two properties make it work:
  - **One cluster, many sites.** Envoy Gateway v1.8.5 merges a rule's `backendRefs` into **one**
    Envoy cluster with one locality per backend. That requires every backend to have the same
    address type (`internal/ir/xds.go` `NeedsClusterPerSetting`).
  - **Retries change site.** Retries use the `previous_hosts` predicate (`route.go:800-816`), so a
    retry goes to a *different* backend, which here means a different site.

  Shedding at the destination plus retrying at the sender gives "overflow moves to another site"
  with no load reporting at all.

What we give up: load-aware choice beyond shedding, and any cross-site affinity. Both come back
with the picker once Track A makes affinity worth having.

---

## 4. The architecture as deployed

```
                         llm.<domain>
                              |
          Avi GSLB (AMKO), round robin: picks the ENTRY gateway only
                              |
                site VIP (AKO L4) of any site
                              |
   +--------- Agent Router, client listener (https) ----------------+
   |  tier-0 AIGatewayRoute <release>-sites                          |
   |    rule pin-<site>: x-site-pin: <site> -> that site only        |
   |    rule pool:  every site, weight = node count                  |
   |  BackendTrafficPolicy <release>-sites:                          |
   |    retry 503/connect-failure/reset -> another site              |
   |    active health check /healthz/<model>, panicThreshold 0       |
   |    token metering + token budget (entry site, once)             |
   +-------------------------------+---------------------------------+
                                   | mTLS (client cert), SNI = shared hostname
                                   v
   +---- chosen site's Agent Router, peer listener (8443, mTLS) -----+
   |  serving AIGatewayRoute <release> -> InferencePool               |
   |  BackendTrafficPolicy <release>-shed: circuit breaker            |
   |    = requestsPerNode x nodes / gateway replicas -> 503 past it   |
   +-------------------------------+---------------------------------+
                                   v
                     InferencePool -> EPP -> vLLM (DP8 per node)
```

### 4.1 One request, end to end

1. **DNS.** The client resolves `llm.<domain>` and Avi returns some site's VIP, round robin. DNS
   only spreads *entry*; it no longer decides where the GPUs work.
2. **Tier 0.** The entry gateway's client listener matches the tier-0 route on `x-ai-eg-model`.
   Envoy picks a site at random, weighted by node count, from the sites passing health checks.
   The entry site itself is one of them.
3. **The hop.** The request goes over mTLS to the chosen site's **peer listener**. On the entry
   site itself that is a hairpin through its own gateway.
4. **Shed or serve.** The chosen site's serving route counts in-flight requests:
   - Under its limit, the request goes to the InferencePool, then the EPP picks a node, then vLLM.
   - Over its limit, it answers **503 immediately**.
5. **Retry.** On a 503, connect failure or reset, the entry site's tier 0 retries a **different**
   site (up to `numRetries`, 2 by default), with a 100 ms to 1 s backoff. Retries happen only
   before any response bytes reach the client, so a stream is never duplicated.
6. **Metering.** As the response finishes, tier 0 reads token usage and charges the tenant's
   budget, once, at the entry site.

### 4.2 Component by component: what, and why

| Piece | What it is | Why this way |
|---|---|---|
| **GSLB round robin** | Avi picks the entry gateway | With tier 0, DNS no longer carries the routing decision. Consistent hashing on a few resolver IPs would just concentrate proxy load. |
| **Tier-0 route on the client listener** | `AIGatewayRoute <release>-sites`, one `AIServiceBackend` + `Backend` per site | The CEL rule forbids mixing `InferencePool` with `AIServiceBackend`, so the local site is a backend like the rest. Identical semantics on every site. |
| **Capacity weights** | `crossSite.sites[].weight` = node count | Homogeneous B200 fleet: capacity is proportional to nodes. Static weights are good enough because overflow is handled by shedding, not by guessing load. |
| **Hostname-only site addresses** | Render fails on an IP | An IP next to hostnames makes Envoy Gateway build one cluster per site, and retries could no longer change site. The guarantee depends on this. |
| **Peer listener (mTLS)** | Serving route moved to `crossSite.peerListener` | **Loop prevention by construction.** A peer request can only reach the serving route, never tier 0 again. **No bypass.** Clients can't reach the unmetered serving route without a peer certificate. The chart requires the client and peer listeners to be different and named. |
| **Shed at the receiver** | Circuit breaker on the serving route | The receiver knows its own capacity exactly (nodes × slots), with no polling and no stale data. The breaker turns "full" into an instant 503 instead of an unbounded vLLM queue. Limits count per Envoy replica, hence the division by `gatewayReplicas`. |
| **Retry at the sender** | `retry` with `previous_hosts` | Turns a shed into "try the next site" within milliseconds. Bounded by `numRetries`, so a fully saturated fleet returns 503 instead of looping. |
| **Health checks, separate from load** | Active check of `/healthz/<model>` through each peer listener; `panicThreshold: 0` | **Busy and dead are separate signals.** Only "dead" removes a site; "busy" just sheds a request. That avoids the cascade where one loaded site is pulled, its load moves to the others, and they get pulled too. `panicThreshold: 0` stops Envoy's default "panic" mode from sending traffic to dead sites when most are down. |
| **Raised tier-0 limits** | `crossSite.maxParallelRequests` (100000) | Envoy's default 1024 per replica would silently cap the *whole fleet* through one entry gateway. The real limits are each site's shed points. |
| **Metering at entry** | `llmRequestCosts` + token budget on tier 0 only | Charged exactly once. The serving route has no untrusted clients to charge. For fleet-wide budgets, point every site's rate limit service at one Valkey. |
| **Fail-open budgets** | Never `failClosed` | A limiter outage that stopped all inference would be far worse than budgets lapsing for minutes. Alert on `ratelimit.error` / `ratelimit.failure_mode_allowed` instead. |
| **`x-site-pin`** | Per-site rules with an extra header match | Gateway API precedence means a rule with more header matches wins. The benchmark hook pins itself so it still gates **this** site during a rolling upgrade. Also useful for debugging. |
| **Health route on both listeners** | `healthRoute` attached to client and peer listeners | The client listener serves the Avi monitor; the peer listener serves the other sites' tier-0 checks. |

### 4.3 Behaviour under failure

| Event | What happens |
|---|---|
| One site's GPUs full | That site sheds 503 instantly; tier 0 retries other sites. Its capacity stays in use up to its limit. |
| Every site full | Each attempt is shed; after `numRetries` the client gets 503. Load can't amplify, because retries are bounded. |
| A site's model down, gateway up | Its health check fails within interval × threshold (default ~15 s), and tier 0 everywhere stops sending to it. Avi's monitor on the client listener also stops sending entry traffic to it. |
| A site's gateway down | Connect failures are retried elsewhere at once. The health check removes the site for good within ~15 s. Avi stops sending entry traffic there. |
| Most sites down | `panicThreshold: 0`: traffic goes only to healthy sites, never to dead ones. |
| Rate-limit Valkey down | Budgets lapse (fail open); inference continues. Alerts fire. |
| A site not yet migrated (rollout) | Its peer listener doesn't carry the serving route, so health checks fail and it gets no cross-site traffic. **A mixed fleet is safe.** |
| Client sends `x-site-pin` | Goes to that site only, still metered. A client can choose a site, but not skip metering. |

---

## 5. Configuration

**Generic defaults** are in `chart/values.yaml` (`crossSite.*`: retry, health check, limits, pin
header).

**Per model** (`values/<model>-<hw>.yaml`):

```yaml
crossSite:
  serving:
    requestsPerNode: 640     # FP8 reference: 8 DP ranks x 64 seqs + ~25% queue. Set from the FP4 recipe.
```

**Per site** (`values/sites/<cluster>.yaml`). The `sites` list is identical everywhere; only `self`
and `gatewayReplicas` differ:

```yaml
route:
  gateway: {name: ai-gateway, namespace: ai-gateway, sectionName: https}   # client listener, required
crossSite:
  enabled: true
  self: site1
  peerListener: peers
  sites:
    - {name: site1, address: llm.site1.<domain>, weight: <nodes>}
    - {name: site2, address: llm.site2.<domain>, weight: <nodes>}
  tls:
    caCertificateRef: {kind: ConfigMap, name: llm-peer-ca}
    clientCertificateSecret: llm-peer-client
  serving:
    gatewayReplicas: <Envoy replicas of ai-gateway>
globalName:
  gslbHostRule:
    poolAlgorithmSettings: {lbAlgorithm: GSLB_ALGORITHM_ROUND_ROBIN}
```

**Per cluster, outside the chart** (the gateway is shared):
- A peer listener on 8443 with no hostname and a certificate for the shared hostname.
- A `ClientTrafficPolicy` requiring client certificates on that listener.
- The CA and client certificate in the release namespace.
- `enableBackend: true` in the Envoy Gateway config.

**Safety nets:**
- The render refuses a misconfiguration: an IP address, missing or identical listeners, `self` not
  in `sites`, the health route off, or a missing certificate or replica count.
- `validate.py` independently checks the rendered manifest for:
  - double metering;
  - shared listeners;
  - non-hostname backends;
  - two policies on one route;
  - a tier-0 policy without retry;
  - an unpinned benchmark;
  - a health route missing from the peer listener.

---

## 6. Rolling it out

1. **On every site, first:** the peer listener, its `ClientTrafficPolicy`, the certificates, and
   `enableBackend`. Nothing routes there yet.
2. **Enable `crossSite` on one canary site.** Its tier 0 sends to itself and to peers that pass
   health checks. Peers not yet migrated fail the check and get nothing.
3. **Verify** (README "Cross-site pooling"):
   - Route and policy conditions are accepted.
   - Envoy `/clusters` shows **one** tier-0 cluster listing every site.
   - The InferencePool cluster carries the shed limit as `max_requests`.
   - One pinned `curl` per site succeeds.
4. **Enable the remaining sites one at a time**, each gated by its pinned benchmark hook.
5. **Switch GSLB to round robin** once every site has tier 0.

---

## 7. What it deliberately does not do (and what comes next)

| Not done | Why | When |
|---|---|---|
| Prefix affinity across sites | Worth ~1/8 of its value until per-rank DP routing (A9) | After Track A raises the measured per-rank hit rate |
| Load-aware choice beyond shedding | Needs a picker; static weights plus shedding cover overflow | Picker: banded-headroom rendezvous hashing, a single τ knob (FINDINGS Step 4) |
| Load reporters | Nothing consumes them without a picker | With the picker |
| Cross-site KV index | A shared Valkey index collides and goes stale (A8) | Dynamo DC KV Relay pattern, via an event-stream shim (FINDINGS Step 6) |
| GSLB `downResponse` | Exact AMKO field not confirmed | When confirmed |

**Track A, in parallel:**
- Make DP ranks individually routable. InferencePool `targetPorts` allows up to 8 ports per pod,
  exactly DP8. The open question is whether KServe v0.21 exposes it.
- Turn on KV events, precise prefix scoring and CPU KV offloading, sized from node RAM.
- **Gate:** run the A9 hit-rate measurement first. It takes about a minute
  (`vllm:prefix_cache_hits_total / vllm:prefix_cache_queries_total` per engine).

---

## 8. Open items before production

1. Which sites take part, and their node counts.
2. The FP4 recipe, including `requestsPerNode`.
3. Confirm the CRD fields the chart uses against the installed versions (list in FINDINGS, open
   question 12).
4. Confirm on the first site that Agent Router's InferencePool cluster keeps the shed circuit
   breaker. Agent Router rewrites that cluster, and this could not be proven from source.
5. Decide which site hosts the shared rate-limit Valkey (optional).
