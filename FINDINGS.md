# Findings

Audit of the cross-site routing design brief (AGENT.md, 2026-10-03). Research done 2026-10-03 from
upstream source at the deployed tags where they could be identified, plus vendor docs and blogs.
**No measurements were taken.** The clusters are air-gapped and unreachable from here. Every number
under *Measurements* is either external or a procedure for you to run.

Sources read at these revisions:

| Component | Revision read | Matches deployed? |
|---|---|---|
| Envoy AI Gateway (Agent Router) | `v1.1.0` (c217da8) | Yes, per chart README ("Agent Router 1.1") |
| Envoy Gateway | `v1.8.5` | README says ">= v1.8.1"; exact version unknown |
| Envoy (ext_proc) | `main` 587588d | Close enough; the behaviour cited has been stable for a long time |
| vLLM | `v0.30.0` (ced6857) | Yes (`llm-d-cuda:v0.9.0-vllm0.30.0`) |
| llm-d-kv-cache | `main` (v0.9.0-7) | Library version in the EPP image unknown |
| llm-d-inference-scheduler (llm-d-router) | `main`, release notes to 2026-08 | KServe 0.21's bundled EPP version unknown |
| Gateway API Inference Extension | `main` (v1.6.2) | n/a |
| NVIDIA Dynamo docs | `main` 7509799 | n/a |

---

## Verdict

**Sound in shape, wrong about what to do next.** The tier-0 mechanism holds up at the source level.
An ext-proc header mutation plus `clear_route_cache` re-selects the `AIGatewayRoute` rule (A1), and
Agent Router v1.1.0 depends on that same mechanism for its own `x-ai-eg-model` routing. The ordering
of the user ext-proc after the AI Gateway ext-proc is deterministic (A2). The InferencePool
constraint still holds (A3).

The design rests on a premise nobody listed: that prefix affinity already works **inside** a site.
In this chart it doesn't. Each pod is DP8 behind one port. The EPP picks a pod, and then vLLM's
internal DP load balancer picks one of 8 independent KV pools **by load alone**. The llm-d index is
also pod-level, not rank-level. No KV events are configured either. So the cache-warm site that
tier 0 would aim for mostly does not turn into a cache hit. Fix tier 3 first. It is cheaper, it
helps every site today, and it is what makes K measurable at all.

Several other claims are wrong:
- The load metric name in D8 doesn't exist in vLLM 0.30.
- The D11 circuit breaker returns 503s rather than queueing.
- Polling faster (D9) does not prevent a herd.
- The A8 shared-Valkey index fails on pod-identity collisions and has no TTL.
- Prior art exists. NVIDIA Dynamo shipped an experimental multi-datacenter KV relay in v1.4
  (2026-08).

---

## Assumptions

| ID | Claim | Verdict | Evidence | Consequence |
|----|-------|---------|----------|-------------|
| A1 | An ext-proc can change which backend is selected | **Confirmed by source, not measured** | Envoy `source/extensions/filters/http/ext_proc/processor_state.cc` `clearRouteCache()`: the route cache is cleared when the filter is downstream, the response carries a `header_mutation`, and `route_cache_action` is `DEFAULT` with `clear_route_cache: true`. Envoy Gateway v1.8.5 `internal/xds/translator/extproc.go` sets neither `route_cache_action` nor `disable_clear_route_cache`, so `DEFAULT` applies. Agent Router v1.1.0 `internal/extproc/processor_impl.go:322-327` sets `x-ai-eg-model` and returns `ClearRouteCache: true` in the request-body response. That is how every `AIGatewayRoute` header match works today. | D5 survives. The picker **must** return its header mutation and `clear_route_cache: true` in the same response, or Envoy ignores the flag and counts it in `clear_route_cache_ignored`. Still run the sandbox test before building. A client can send `x-target-site` itself, so the picker must always overwrite it, and the peer serving listener must not route on it. |
| A2 | AI Gateway ext-proc and a user ext-proc coexist with predictable order | **Confirmed by source; loop prevention unresolved** | Agent Router `internal/extensionserver/post_translate_modify.go` `insertAIGatewayExtProcFilter()` puts the AI Gateway ext-proc **before** the first Envoy Gateway `ext_proc`/wasm/lua/rbac/local-ratelimit filter. It is disabled by default and enabled per route on AI-Gateway-generated routes (`enableRouterLevelAIGatewayExtProcOnRoute`). It runs with `RequestBodyMode: BUFFERED` (lines 798-805). EPP filters are inserted just before the router (`patchListenerWithInferencePoolFilters`). | The picker sees `x-ai-eg-model` and runs on a re-selected route. To read the body it must ask for `processingMode.request.body: Buffered` itself, which is a second full-body buffer. For loops: attach the picker's `EnvoyExtensionPolicy` to the **tier-0 HTTPRoute** (the generated one), not the Gateway, and put the serving `AIGatewayRoute` on a separate peer-only mTLS listener. Add a hop header (`x-glm-hop: 1`, rejected on tier 0) as a second guard. Verify with `egctl config envoy-proxy listener`. |
| A3 | One InferencePool per rule, no mixing, weight/priority ignored | **Confirmed** at v1.1.0 | `api/v1beta1/ai_gateway_route.go:214-215` (CEL: "cannot mix InferencePool and AIServiceBackend references in the same rule", "only one InferencePool backend is allowed per rule"). Field docs at 355-390: "This field is ignored when referencing InferencePool resources." The constraint is unchanged on `main` (2026-10-02). | D4 stands. Weight and priority **are** honoured on `AIServiceBackend` refs. Use that for overflow (see E3). |
| A4 | Internal resolvers don't send ECS | **Unresolved**: needs the disconnected side | Not testable from here. Prior: Windows DNS, BIND without ECS configured, and default Unbound don't send ECS. | Doesn't block anything (as AGENT.md says). Test below. |
| A5 | GKE weights locality only because of ~100 ms RTT | **Unresolved; the premise is weak** | The GKE blog (2026-09-21) gives **no** reason for locality-first and doesn't explain the 40% threshold ("the moment the cluster crossed its 40% KV-cache utilization threshold"). The behaviour is Cloud Load Balancing's general capacity-chasing with PREFERRED/DEFAULT backends, a product default for all traffic. It wasn't chosen for inference. The benchmark ran at 0.72→2.10 req/s on an SGLang MoE. "17,000 nodes" is the fleet size, not the test. GKE's multi-cluster routing doesn't mention prefix affinity across clusters. | GKE is evidence neither for nor against D6. Don't cite it either way. Its 40% is probably a demo `maxUtilization` setting, not a quality knee. The real knee question is answered better by llm-d's calibrated saturation threshold (see E5). |
| A6 | K ≈ 2.5 | **Unresolved; probably too low for agent contexts, and not a constant** | External data points. (1) Qwen3-32B, a 10K-token prompt: TTFT 4.3 s cold, 0.6 s warm (≈7×), from llm-d blog 2025-09-24. (2) GLM-5.2 agentic run: cached turns reach first token 2.8× faster, queue-inclusive. Recomputing ~45K tokens took ~5.7 s server time, against a 59 ms CPU-tier restore (llm-d blog 2026-07-22). | K depends on context length, on how much of the request is uncached, **and on queue depth**. A cold request also adds prefill work in front of everyone else's. Model it as a predicted-TTFT cost, not a multiplier (E5). Measure it per DP rank (procedure below). |
| A7 | A shared cross-site KV tier isn't worth building | **Refuted on economics, upheld on priority** | GLM-5.2 MLA (FP8) ≈ **44 KB/token** (llm-d GLM-5.2 blog). GLM-5.3 is assumed to be close; the DSA indexer cache adds a little. 100K tokens ≈ 4.4 GB, which takes ≈ 3.5 s at 10 Gb/s, 1.4 s at 25 Gb/s and 0.35 s at 100 Gb/s, against roughly 10 s+ to recompute. | Transfer beats recompute at ≥ 25 Gb/s of **available** inter-site bandwidth. But the first and much cheaper win is **intra-site CPU KV offloading** (59 ms restore), which this chart doesn't enable. Do that first, then revisit the cross-site tier. |
| A8 | A cross-site index on the shared Valkey backend | **Refuted as framed** | llm-d-kv-cache `pkg/kvevents/engineadapter/common.go`: the pod ID is the middle field of the ZMQ topic `kv@<pod-id>@<model>`, which llm-d sets to pod IP:port. OpenShift's default `clusterNetwork` (10.128.0.0/14) is identical on every cluster unless it was changed at install, so IDs **collide across sites**. `pkg/kvcache/kvblock/redis.go` has **no TTL or expiry** (0 matches), so a partitioned site's entries live until an `AllBlocksCleared` that will never arrive. Each `Lookup` is a pipelined `HKEYS` per block, which puts one WAN RTT on every score and on every event write. In pod-discovery mode the host connects to each pod's ZMQ socket, so pod IPs would have to be routable between clusters. The architecture doc itself says Redis is "rarely necessary since each in-memory replica converges from the event stream". The DP-rank dimension is also missing (`event_dedup_filter.go:28-41`, TODO #370). | Don't share an index across the WAN. The viable pattern is what Dynamo built: a **per-site relay** keeps exact ownership local and publishes a compact, lossy, site-namespaced projection (a Cuckoo filter per pool) plus load, over gRPC. See Prior art. It is a v2 at best. |
| **A9** (new) | Prefix affinity works *within* a site, so a "warm site" means a cache hit | **Refuted** | `values/glm53-b200.yaml:3-4` says "vLLM balances across the 8 local ranks". At vLLM v0.30.0 `vllm/v1/engine/core_client.py:1548-1560`, `DPLBAsyncMPClient` picks the engine from `waiting+running` and KV pressure, with no prefix input, unless the request carries `X-data-parallel-rank` (`entrypoints/generate/base/serving.py:243`). The recipe sets no `kv-events-config`. llm-d's own GLM-5.2 reference exposes each rank as its own endpoint (`--data-parallel-multi-port-external-lb`, guides/wide-ep/modelserver/gpu/vllm-glm-5.2). | Today a repeat prefix lands on the rank holding it about 1 time in 8, so tier-3 hit rates and any naive K measurement are both wrong. **This is the top item in the revised plan.** |
| **A10** (new) | The circuit breaker in D11 queues overflow locally | **Refuted** | Envoy circuit breakers reject overflow with 503 (`x-envoy-overloaded`). `maxPendingRequests` only bounds requests waiting for a pool connection. Over HTTP/2, one connection multiplexes, so the effective limit is `maxParallelRequests`, and past it the request fails fast. | Overflow must be routed, not queued. Use a priority fallback on the rule (see Revised plan). |
| **A11** (new) | Every site serves an identical recipe, so any request fits anywhere | **At risk** | Only `glm53-b200.yaml` exists. H200 and B300 recipes sized to their hardware will likely differ in `max-model-len` (the recipe comment ties it to one rank's KV pool) and `max-num-seqs`. | A 200K-token request routed to a site with a smaller `max-model-len` returns 400. Either pin `max-model-len` fleet-wide, or have the load reporter export it and the picker filter on it. |

---

## Measurements

None taken. Run these on the disconnected side. Plain `oc` and python3 from the AIPerf image; no `jq`.

**A6: K per context length, with DP effects removed.** Pin the rank so you measure the cache, not
the DP balancer. vLLM honours `X-data-parallel-rank` at v0.30.0.

```bash
oc -n llm-glm53 get pod -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.podIP}{"\n"}{end}'
# then from a pod on the cluster network (e.g. the AIPerf image):
oc -n llm-glm53 run kprobe --rm -it --restart=Never --image=REGISTRY/tools/aiperf:<tag> -- python3 - <<'EOF'
import json, time, urllib.request, uuid
POD="http://<podIP>:<vllm port>/v1/completions"   # direct to vLLM (port from the pod spec), rank pinned
def ttft(prompt):
    req=urllib.request.Request(POD, data=json.dumps({"model":"glm-5.3","prompt":prompt,
        "max_tokens":1,"stream":True}).encode(),
        headers={"Content-Type":"application/json","X-data-parallel-rank":"0"})
    t=time.time(); r=urllib.request.urlopen(req)
    for line in r:
        if line.startswith(b"data:"): return time.time()-t
for n in (4_000, 16_000, 64_000, 128_000, 200_000):
    p=f"{uuid.uuid4()} " + "lorem ipsum " * (n//3)   # ~n tokens, unique head = cold
    cold=ttft(p); warm=ttft(p + "x")
    print(n, round(cold,3), round(warm,3), "K=%.1f" % (cold/warm))
EOF
```

Run it once idle and once under the AIPerf load, since K grows with queue depth. Also read
`vllm:prefix_cache_hits_total / vllm:prefix_cache_queries_total` per `engine` label on a busy pod.
That gives the **current** tier-3 hit rate, the baseline for A9.

**Inter-site RTT and bandwidth.** Check the connection and transfer times from a pod in each site
to each peer's gateway:

```bash
for i in $(seq 20); do curl -sko /dev/null -w '%{time_connect} %{time_appconnect} %{time_starttransfer}\n' \
  https://llm.site2.<domain>/healthz/glm-5.3; done
```

Bandwidth, and whether it's shared, has to come from the network team. A7 depends on it, not on RTT.

**Request body sizes.** Use the Envoy access logs on the Agent Router (`bytes_received`) over a normal
day. llm-d's agentic GLM trace had a **median of ~195K input tokens**, roughly 0.8 MB of JSON. Check
this against the Envoy Gateway `ClientTrafficPolicy` `connection.bufferLimit` and the ext-proc
message limits. Local traffic in the D4 design would be fully buffered three times: twice by AI
Gateway (tier 0 and serving) and once by the picker.

**A4: ECS.** Enable non-significant logs on the Avi DNS VS and check whether queries from the internal
resolvers carry a client-subnet option. Alternatively run `tcpdump -nvvv port 53` on the path and look
for `CLIENT-SUBNET` in the OPT RR.

**Step 0 imbalance.** Per site, over a week, compare `sum(rate(vllm:request_success_total[1h]))`
with `sum(vllm:num_requests_waiting)` and `max by (engine)(vllm:kv_cache_usage_perc)`. If waiting is
near zero everywhere, tier 0 is solving a hypothetical.

**Real K:** unknown until the procedure above runs.

---

## Prior art

| System | Site-level? | Prefix-aware across sites? | Notes |
|---|---|---|---|
| **NVIDIA Dynamo DC KV Relay** (experimental, v1.4.0, 2026-08-14) | Yes | **Yes** | Exact block ownership stays inside the DC. Each pool publishes a Cuckoo-filter projection plus readiness and load over gRPC. Pool IDs are `(identity_version, IndexerDomainId, DcId)`, so colliding pools are fenced, not merged. Matches are documented as "possible prefix presence, not a guaranteed reusable prefix". "Missing observations and unknown capacity are not zero load." It publishes no routing policy; the consumer decides. It needs the Dynamo runtime and workers, so it is not drop-in for KServe/llm-d. `docs/.../router/multi-dc-kv-routing.md`. |
| **GCORE "AI Grid"** (with NVIDIA, contributed to Dynamo) | Yes | Yes | Weighs cache locality, network distance and live load together. |
| **GORGO** (arXiv 2602.11688) | Yes (cross-region) | Yes | Puts network latency, prefill cost and queueing into one TTFT cost model with online-tuned parameters. Baselines: least-load, prefix-similarity, and a centralised proxy. It reports that pathological cross-region forwarding is a real failure mode of prefix-only routing. |
| **GKE multi-cluster Inference Gateway** | Yes | No | Locality-first with spill on a custom KV-utilisation metric (`vllm:kv_cache_usage_perc` via `AutoscalingMetric`/`GCPBackendPolicy`). Uses `GCPInferencePoolImport`. |
| **Gateway API Inference Extension `InferencePoolImport`** (alpha since v1.1, proposal 1374 still *Draft*) | Yes | No (selection is implementation-specific; examples are metrics or active-passive) | Its "Parent Mode" (local IG → remote Gateway → remote EPP) is exactly our tier-0 → peer serving gateway. **Neither Envoy Gateway nor Agent Router implements it** (no matches in either repo on `main`). Worth tracking as the eventual standard API for `peers:`. |
| **llm-d router "sticky until saturated"** (2026-08-17, now the llm-d default) | No (intra-pool) | n/a | Drops the weighted multi-signal blend in favour of: keep affinity until a calibrated token-load threshold τ, then route by load alone. τ comes from one hardware calibration. This is the right shape for D7 at the site level too. |
| **AIBrix router** | No | n/a | An Envoy Gateway ext-proc that selects the target by header. More evidence for A1's mechanism. |

Where the brief agrees with the field: two tiers, saturation from queue and KV, moving requests
rather than caches as the default.

Where it diverges:
1. It shares the index (A8), where Dynamo projects a summary per site.
2. It uses a multiplicative blend (D7), where llm-d now uses filter-then-load.
3. It drops distance entirely (D6). That is defensible at < 10 ms, but GORGO's result says to keep a
   small latency term so you never forward for a marginal gain.

---

## Errors found in AGENT.md

1. **"We found no one doing site-level prefix-aware routing."** Wrong. Dynamo's DC KV Relay, GCORE AI
   Grid and GORGO all do it.
2. **D8 uses a metric that doesn't exist.** vLLM v0.30.0 exports `vllm:kv_cache_usage_perc`
   (`vllm/v1/metrics/loggers.py:567`). `vllm:gpu_cache_usage_perc` is gone. Also, "averaged" across
   a DP8 pod hides the hot rank, which is what admits or queues the next request. Use the max per
   `engine`.
3. **D11's breaker doesn't queue.** It fails fast with 503 (A10). "Converts that into local queueing"
   is false.
4. **D9: fresher data doesn't prevent herding.** Four tier 0s (times their Envoy replicas) reading the
   same 500 ms snapshot all choose the same argmax site at the same moment. That is synchronised
   decision-making, not staleness. You need randomisation (power of two choices, or weighted-random
   over eligible sites) and local in-flight accounting. vLLM's own DP balancer carries
   `# TODO use P2C alg for larger DP sizes`. This repeats Known Error 3: a binary dynamic, one layer
   up.
5. **The D7 score is mis-shaped.**
   - It is the multiplicative blend llm-d abandoned because its "emergent behavior was hard to
     predict and harder to tune".
   - "Normalised by site capacity" is ambiguous. A ratio headroom treats 20% free on a B300 site the
     same as 20% free on an H200 site.
   - `prefix_warm` is binary, where the real quantity is the length of the cached prefix.
   - Site-level warmth ignores *which* pod is warm and whether that pod is saturated.
6. **Affinity at tier 0 is worth little until A9 is fixed.** The design never checked that tier 3
   turns a warm site into a hit. Here it can't, because of DP8 behind one port, a load-only DP
   balancer, no KV events, and a pod-level index.
7. **D6: "every site's picker config is byte-identical, no per-site variable".** False. Self-identity
   (to reach the local serving listener in-cluster rather than hairpinning through the Avi VIP),
   peer mTLS identities and endpoints all vary per site.
8. **D4: "local traffic pays one extra in-cluster hop".** It also pays two more full-body buffers and
   parses (picker and serving AI Gateway). That is cheap for chat and not free for 0.8 MB agent
   bodies.
9. **The 57×/170× figures are not transferable.** They come from llm-d's synthetic "b2b-saas"
   workload (150 groups × 5 prompts, 6K-token system prompt plus 1.2K question, Qwen-32B,
   8 pods × 2 H100) ramped to 60 QPS. The P90 TTFT gap measures queue collapse under overload, not
   prefill savings. llm-d's own later post calls this workload "a stress test on a fixed, small
   corpus" and "a poor benchmark for hyperparameter sweeps". The "approximate" baseline is the GIE
   approximate prefix scorer. Don't quote 57× internally.
10. **The GKE figures are misread.** 17,000 nodes is the fleet; the benchmark ran 0.72–2.10 req/s. GKE
    states no reason for locality-first (A5).
11. **Rate limiting gets worse, not just "still behaves".** With a tier-0 hop both gateways meter
    tokens. Either a tenant is charged twice, or you drop `llmRequestCosts` on one side and choose
    where. After Step 5 (round-robin GSLB) a tenant's entry site is effectively random, so the
    effective budget tends towards sites × `rateLimit.tokens`. The chart README already warns about
    this; the plan makes it the norm.
12. **Mixed revisions (the brief's candidate failure mode) is mostly a non-issue for the index.**
    llm-d request keys are computed from tokens with the indexer's own seed, independent of
    `PYTHONHASHSEED` (`docs/configuration.md:460-465`). The real risks are a tokenizer or chat-template
    change between revisions, and different weights with identical tokens, where a "warm" hit on the
    other revision serves different numerics. The load reporter must export the revision, and the
    picker must route only to sites on the entry site's revision during a rollout.

---

## Revised plan

The original Step 0 stays. A new Step 1 comes before everything cross-site.

**Step 0: Measure** (as above). Add the per-engine prefix-hit-rate baseline.

**Step 1 (new): Fix tier-3 affinity.** No cross-site work yet.
- Make DP ranks individually routable: `--data-parallel-multi-port-external-lb` with the EPP scoring
  per-rank endpoints, as llm-d's GLM reference does, or an EPP that injects `X-data-parallel-rank`.
- Turn on KV events and the precise prefix scorer, with the current llm-d default "sticky until
  saturated" scheduler config.
- Turn on CPU KV offloading (A7: a 59 ms restore against seconds of recompute).

All three touch `LLMInferenceService` fields (`spec.router.scheduler`, worker ports, vLLM args).
**You need to confirm the field names against KServe v0.21 on the cluster before I template them.**
Then re-measure hit rate and K.

**Step 2: Load reporters.** As before, with these changes:
- Use `vllm:kv_cache_usage_perc`, max per engine, and the sum of waiting.
- Report `max-model-len` and revision.
- Report "unknown" explicitly; missing is never zero load.

**Step 3: Peers and the serving listener.**
- Add `peers:` to the site values.
- Add a peer-only mTLS listener for the serving `AIGatewayRoute`.
- Render one `AIServiceBackend` per peer.
- Make the metering decision (open question 4).
- Drop the D11 breaker as an overflow mechanism. Each `x-target-site=siteN` rule lists siteN at
  priority 0 and the local site at priority 1. Priority is honoured on `AIServiceBackend`.

**Step 4: Tier 0 and the picker, v1 without an index.** A **rendezvous hash with bounded load over a
prefix fingerprint**:
- Hash the system prompt plus the first N KB of messages to a home site.
- Use the home site unless it is over a calibrated τ (llm-d's method, at site level). Above τ, pick
  among sites under τ, weighted-random by absolute headroom.

This needs no shared state. Every tier 0 agrees on the home site without coordinating, and that
removes most of the herd. It is not llm-d's "approximate" routing-history scheme. It is deterministic
placement and has no index to go stale. Run the A1/A2 sandbox test first.

**Step 5: Demote GSLB.** As before, plus `downResponse`.

**Step 6 (deferred): Precise cross-site index** via a per-site relay publishing a compact projection
(Dynamo's pattern). Either adopt the Dynamo relay if your Dynamo deployments can feed it, or build a
thin relay on llm-d-kv-cache with site-namespaced pod IDs. Only do this if Step 0 and Step 4 data
show home-site hashing leaves real hits on the table.

**Air-gap check:**
- Everything above is mirrorable: Valkey, the llm-d EPP and tokenizer images, Dynamo relay images,
  and the picker (Go, built internally).
- The picker needs the GLM-5.3 tokenizer and chat template only for precise scoring (Step 6), not
  for Step 4 hashing. Load them from the S3 model store and set `HF_HUB_OFFLINE=1`.
- The only phone-home risk found is a tokenizer library's default Hugging Face download.

---

## Open questions for the platform owner

1. **Versions on the cluster.** Exact Envoy Gateway and Agent Router versions, and the EPP image
   and scheduler config that KServe 0.21 deploys for `scheduler: {}`:
   `oc -n llm-glm53 get deploy -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.spec.template.spec.containers[*].image}{"\n"}{end}'`
   and the EPP ConfigMap.
2. **Step 1 fields.** Will you confirm the `LLMInferenceService` fields for per-rank ports, KV events
   and a custom scheduler config, so Step 1 can be templated?
3. **Pod CIDRs.** Each site's pod CIDR:
   `oc get network.config cluster -o jsonpath='{.spec.clusterNetwork[*].cidr}'`. Identical CIDRs
   rule out any shared pod-keyed index.
4. **Metering.** Where should token metering live: at the entry site's tier 0 (one charge, but
   per-entry-site budgets) or at the serving site (budget follows the GPUs, but a tenant's spend is
   split across sites)?
5. **Bandwidth.** Inter-site bandwidth and contention, not RTT. This decides A7.
6. **Pinning.** Are there residency or failure-domain rules that pin any tenant or data to a site?
   Tier 0 would need a filter for them.
7. **Recipes.** Will the H200 and B300 recipes keep `max-model-len` at 262144? If not, the picker
   needs a per-site context filter (A11).
8. **Dynamo.** Is the Dynamo relay acceptable as a dependency, given Dynamo already runs elsewhere
   in the org, or must tier 0 stay within the llm-d/KServe stack?
