# kserve-llm

Full-node vLLM replicas served by a KServe `LLMInferenceService` and exposed through an Agent Router (Envoy AI Gateway) `AIGatewayRoute`, for air-gapped clusters with node-local model storage.

Written against KServe v0.21 (`serving.kserve.io/v1alpha2`), Agent Router 1.1 (`aigateway.envoyproxy.io/v1beta1`) and Gateway API Inference Extension `InferencePool` v1.

## Layout

| Path | Holds |
|---|---|
| `chart/` | The chart. `values.yaml` is generic and carries no model settings. |
| `values/<model>-<hardware>.yaml` | The model recipe: vLLM args, env, resources, probes, image tag. |
| `values/sites/<cluster>.yaml` | What differs per cluster: registry, node hostnames, S3 source, gateway, benchmark URL. One file per cluster, named after it. Start from `sites/example.yaml`. |

A new model is a new recipe file; a new cluster is a new site file. Every site serves the same recipe, so the model behaves identically wherever a request lands.

## What it creates

- `StorageClass` plus one `PersistentVolume`/`PersistentVolumeClaim` pair for weights and one for compile caches, as static `local` volumes.
- One pull `Job` per node that syncs the weights from S3 onto that node.
- One `LLMInferenceService` with `replicas = len(nodes)`.
- One `AIGatewayRoute` pointing at the `InferencePool` KServe creates (`<release>-inference-pool`).
- Optional: a `BackendTrafficPolicy` token budget (`rateLimit`), a health `HTTPRoute` for the GSLB monitor (`healthRoute`), and an AIPerf hook `Job` (`benchmark`).
- With `crossSite`: one `Backend` + `AIServiceBackend` per site, a tier-0 `AIGatewayRoute` (`<release>-sites`) with its `BackendTrafficPolicy`, and a shed policy on the serving route (`<release>-shed`). See "Cross-site pooling".

The namespace and the S3 secret (`modelPull.existingSecret`, key `s3cfg`) are not created by the chart.

## Design decisions

- **Static `local` PVs, not a dynamic PVC.** `model.uri: pvc://<claim>` names one claim for all replicas. A TopoLVM/LVMO volume is pinned to one node, so the second replica would stay Pending. A `local` PV whose nodeAffinity lists every node gives each pod its own node's copy at the same path.
- **One replica per node, sized to the whole node.** Each pod requests all GPUs on the node, which is what keeps replicas on separate nodes. Data and expert parallelism stay inside the node.
- **`rolloutStrategy.maxSurge: 0`.** The KServe default surges one pod, which needs a free full node that does not exist, and the rollout deadlocks. The cost is that every spec change takes one node out of service for a cold start.
- **Pull Jobs carry a spec hash in their name.** Job specs are immutable; a changed spec becomes a new Job instead of a failed upgrade.
- **Storage survives `helm uninstall`** (`storage.keepOnUninstall`). PVs are `Retain`, so deleting a PVC by hand leaves the PV `Released` until its `claimRef` is cleared.
- **vLLM args are a map, validated at render time.** The KServe preset launches through `bash -c ... eval` and already sets the model path, `--served-model-name` and `--port`. The chart fails the render on those flags and on any arg that is not a single plain token.
- **Two gateways, one pool.** KServe's own `HTTPRoute` sits on the KServe gateway and the `AIGatewayRoute` on the Agent Router gateway, both targeting the same `InferencePool`. If they share a gateway they must use different listeners (`serving.router.gatewayRefs`, `route.gateway.sectionName`); on the same listener the KServe route can win the match and bypass token metering and rate limits.
- **Cross-site pooling is Envoy-native, without a picker.** Static capacity weights spread requests, each site sheds 503 at its own capacity, and the sender retries another site. There is no per-request prefix affinity across sites. Within a site, DP8 behind one port already defeats most prefix affinity (vLLM's internal DP balancer routes on load only), so cross-site affinity would buy little until per-rank routing exists. See `FINDINGS.md` (A9).
- **Offline by default.** `serving.offline` sets `HF_HUB_OFFLINE` and `TRANSFORMERS_OFFLINE`; every image goes through `global.imageRegistry` and must have a tag.

## Cluster pre-reqs

1. Envoy Gateway >= v1.8.1 with the Agent Router InferencePool addon:

   ```yaml
   config.envoyGateway.extensionManager.backendResources:
     - {group: inference.networking.k8s.io, kind: InferencePool, version: v1}
   ```

2. Gateway API Inference Extension CRDs (`InferencePool` v1) and LeaderWorkerSet installed.
3. The serving image has the vLLM and transformers versions the recipe needs:

   ```bash
   oc run -it --rm chk --image=<registry>/<serving image>:<tag> -- \
     python -c "import vllm,transformers;print(vllm.__version__,transformers.__version__)"
   ```

4. `storage.*.hostPath` exists on every node, on a data NVMe with enough free space (not the RHCOS root disk), for example an NVMe excluded from the LVMCluster `deviceSelector`, formatted and mounted with a MachineConfig mount unit. Then, per node:

   ```bash
   oc debug node/<node> -- chroot /host bash -c \
     'mkdir -p /var/mnt/llm/models /var/mnt/llm/cache &&
      chmod 1777 /var/mnt/llm/models /var/mnt/llm/cache &&
      chcon -R -t container_file_t /var/mnt/llm'
   ```

5. KServe's default Gateway exists and is an Envoy Gateway Gateway (`oc get gateway -n kserve kserve-ingress-gateway`). The service only turns Ready once that gateway accepts its InferencePool. If it does not exist, use `serving.router.gatewayRefs`.
6. `ingress.enableLLMInferenceServiceTLS` stays unset/false. If true, vLLM and the EPP serve TLS and the Agent Router needs a `BackendTLSPolicy`.
7. The Agent Router Gateway listener's `allowedRoutes` admits the release namespace.
8. With `crossSite`: the peer listener, its certificates and the shared rate-limit store. See "Cross-site pooling".

## Argo CD

Point the Application at `chart/` with the recipe file and the cluster's site file as `valueFiles`, and set `argocd.enabled: true`. The chart then orders the sync itself:

| Wave | Resources | Why |
|---|---|---|
| -3 | StorageClass, PVs, PVCs | Marked `Delete=false,Prune=false`; Argo CD ignores `helm.sh/resource-policy`. |
| -2 | Pull Jobs | Argo CD waits for the Jobs to complete, so serving never starts on half-synced weights. |
| 0 | LLMInferenceService | |
| 1 | Token-limit policy; with `crossSite` also the site `Backend`s/`AIServiceBackend`s, the tier-0 policy and the shed policy | Before the routes, so the model is never reachable unmetered or unprotected. |
| 2 | AIGatewayRoute(s), health route | |
| PostSync | AIPerf Job | |

Argo CD has no built-in health check for `LLMInferenceService` and treats it as healthy on creation. The AIPerf Job therefore waits for the model itself. Add a custom health check for the kind in `argocd-cm` if you also want the Application to show Progressing during a cold start.

## Token rate limiting

`rateLimit` creates a `BackendTrafficPolicy` that charges each client the tokens reported in `route.llmRequestCosts`.

- **It targets the generated HTTPRoute, not the Gateway.** A Gateway accepts one policy, so per-model policies aimed at it would conflict. A route-level policy replaces any Gateway-level one for this route.
- **The client header must come from the gateway.** Derive it from the verified JWT claim or API key and strip it from client input; otherwise a client can name another tenant.
- **Enforcement is after the fact.** The cost is known only when the response ends, so the request that crosses the budget completes and the next one gets 429.
- **Budgets are per cluster** unless every site's rate limit service uses the same Redis/Valkey. See "Cross-site pooling".
- **It fails open.** If the rate limit service or its Redis is unreachable, requests pass unmetered. That is deliberate: lapsed budgets for a few minutes beat no inference anywhere. Never set `failClosed`; alert on the rate limit filter's `ratelimit.error` and `ratelimit.failure_mode_allowed` counters instead.
- With `crossSite` the budget moves into the tier-0 policy: metered once, at the site the client entered.
- It needs the Envoy Gateway rate limit service with a Redis backend.

## Benchmark hook

`benchmark` runs AIPerf after every install, upgrade or sync, as a gate: the hook fails if the model does not come up, if too many requests fail, or if a `goodput` SLO is missed.

- Keep the default load small. It runs against production on every sync and its tokens count against the benchmark client's budget.
- `benchmark.url` is the site's own gateway, never the global name. With `crossSite` the hook also sends `crossSite.pinHeader: <self>`, so it measures this site and not the fleet.
- The tokenizer is read from the models volume, so the pod runs on a serving node and fetches nothing from Hugging Face.
- The AIPerf image needs `python3` for the wait step.

## Multiple sites behind one URL

The same release runs on every cluster; a global name in front picks a site per client. Avi GSLB resolves it to one site's gateway VIP.

```
client -> llm.example.internal (GSLB) -> site VIP (L4, per cluster) -> Agent Router -> InferencePool -> vLLM
```

`globalName` implements this with AKO and AMKO:

| Object | Where | Created by |
|---|---|---|
| AKO `HostRule`: local gateway FQDN -> global FQDN | every cluster | this chart (`globalName.hostRule`) |
| AMKO `GSLBHostRule`: monitors, algorithm, TTL, weights for the global FQDN | AMKO leader cluster only | this chart (`globalName.gslbHostRule.create`, set in the leader's site file) |
| AMKO `GlobalDeploymentPolicy` | AMKO leader cluster | you; it is a singleton and must select the gateway's LoadBalancer Service by label |
| Federated HTTPS health monitor | Avi controller | you; it has no CRD. `GET /healthz/<model>`, expect 200 |
| Gateway listener hostname and certificate for the global FQDN | every cluster | you; the Gateway is shared |

These objects describe the gateway, not the model. If several releases share a gateway on a cluster, enable `globalName` in only one of them.

- **AMKO must run in custom global FQDN mode** (`useCustomGlobalFqdn: true` in its `GSLBConfig`) for the `HostRule` mapping to be read. That setting is fixed at AMKO install time. In default mode AMKO groups Services by identical hostname instead; set `globalName.hostRule.create: false`.
- **Keep Avi at L4.** The Envoy gateway Service is type LoadBalancer, which AKO turns into an L4 virtual service. TLS ends at Envoy, which is where tokens are metered.
- **Every site's gateway accepts both names**: the global one and its own. The site name is for the benchmark, the health monitor and debugging. The chart refuses a `benchmark.url` that uses the global name.
- **Monitor the model, not the gateway.** The default monitor for an L4 member only proves the port is open. Reference a federated HTTPS monitor that requests `healthRoute`'s path, so a site whose gateway is up but whose model is down is taken out.
- **Site affinity over round robin, without `crossSite`.** The prefix cache lives in one site's GPUs; a conversation that moves between sites recomputes its whole context. The default is consistent hashing. The hash is on the DNS resolver's address unless resolvers send EDNS client subnet, so with only a few resolvers it can concentrate traffic on a few sites; then use round robin or topology. With `crossSite`, GSLB only picks the entry gateway, and round robin spreads the proxy load.
- **GSLB is per name, not per model.** One global name works only if every site behind it serves the same models. Otherwise give each model its own name and its own GSLB service.
- **Rate limits do not span sites** unless the rate limit services share one Redis. With affinity a client mostly spends one site's budget; without it, up to sites x budget.
- **Credentials must be valid everywhere**: the same JWT issuer or API-key store on every site.
- **Roll one site at a time.** A serving change takes half of a site's capacity down for a cold start. Sync a canary site first, then the rest one by one, gated by the benchmark hook.

## Install with Helm

Without Argo CD, serving must not start before the weights are fully synced, so install in two steps.

With Helm on the cluster side:

```bash
helm install glm53 chart -n llm-glm53 \
  -f values/glm53-b200.yaml -f values/sites/<cluster>.yaml --set serving.enabled=false
oc wait --for=condition=complete job -n llm-glm53 \
  -l app.kubernetes.io/component=model-pull --timeout=12h
helm upgrade glm53 chart -n llm-glm53 \
  -f values/glm53-b200.yaml -f values/sites/<cluster>.yaml
```

Without Helm on the cluster side, render both stages here and apply them with `oc apply -f` in order:

```bash
helm template glm53 chart -n llm-glm53 -f values/glm53-b200.yaml -f values/sites/<cluster>.yaml \
  --set serving.enabled=false > dist/01-storage-and-pull.yaml
helm template glm53 chart -n llm-glm53 -f values/glm53-b200.yaml -f values/sites/<cluster>.yaml \
  > dist/02-full.yaml
```

The release name becomes the `LLMInferenceService` name and the prefix of the PV, PVC and InferencePool names.

## Cross-site pooling

`crossSite` makes every site's gateway a tier 0 that spreads the model's requests over all sites, itself included. Behind the one global name, the fleet behaves as a single pool of GPUs instead of isolated sites with failover.

```
client -> llm.example.internal (GSLB, round robin) -> any site's client listener
       -> tier-0 AIGatewayRoute <release>-sites -> AIServiceBackend per site
       -> chosen site's peer listener (mTLS) -> serving AIGatewayRoute <release> -> InferencePool -> EPP -> vLLM
```

How a request finds capacity:

1. Tier 0 picks a site at random, weighted by `crossSite.sites[].weight` (capacity, e.g. node count). Envoy Gateway merges the backends into one cluster, one locality per site.
2. A full site sheds. Its serving route has a circuit breaker at `requestsPerNode x len(nodes)` in-flight requests, split across the gateway's Envoy replicas. Past it, the site answers 503 at once instead of letting vLLM queue without bound.
3. Tier 0 retries a 503, connect failure or reset on a different site (`previous_hosts`), up to `retry.numRetries` times. It can only retry before the response starts, so a stream is never duplicated.
4. Active health checks on `healthRoute`'s path, through each peer listener, take a dead site out after `interval x unhealthyThreshold`. `panicThreshold: 0` stops Envoy routing to dead sites when most are down.

Design decisions:

- **Busy and dead are separate signals.** Shedding (busy) is per request and immediate. Health checks (dead) are the only thing that removes a site. A loaded site is never marked down, so load can't cascade into an outage (FINDINGS D2).
- **The local site is an `AIServiceBackend` like the others.** An `AIGatewayRoute` rule can't mix an `InferencePool` with `AIServiceBackend`s (Agent Router v1.1 CEL rule), so local traffic takes one extra hop through the gateway. In exchange, every site has the same semantics.
- **Two listeners, so no loops.** The tier-0 route sits on the client listener (`route.gateway.sectionName`, required). The serving route moves to `crossSite.peerListener`. A request from a peer can only reach the serving route, never tier 0 again. Clients can't reach the serving route, and its unmetered path, without a peer certificate.
- **Metering happens once, at entry.** `llmRequestCosts` and the token budget move to the tier-0 route; the serving route charges nothing. Point every site's rate limit service at one Valkey for fleet-wide budgets.
- **Site addresses must be hostnames.** An IP next to hostnames makes Envoy Gateway build one cluster per site. Retries then stay on the site that failed. The chart rejects IPs.
- **`x-site-pin: <site>`** routes to one site only. The benchmark uses it; it is also useful for debugging. Pinned requests are still metered.
- **What it does not do.** No load-aware choice beyond shedding, no prefix affinity across sites, no hysteresis. The weights are static. The next step is a picker (FINDINGS "Revised plan", Track B Step 4).

Pre-reqs, per cluster (the gateway is shared, so these are not in the chart):

- **Envoy Gateway `Backend` API enabled**: `config.envoyGateway.extensionApis.enableBackend: true`. Agent Router's own install values set it, so check rather than assume: `oc get cm -n envoy-gateway-system envoy-gateway-config -o jsonpath='{.data.envoy-gateway\.yaml}' | grep -A3 extensionApis`.

- **A peer listener** on `route.gateway`: HTTPS on `crossSite.port`, no `hostname`, a certificate covering `crossSite.tls.hostname` (default `globalName.fqdn`), and client certificate validation via a `ClientTrafficPolicy` targeting that listener (`tls.clientValidation.caCertificateRefs`). Its `allowedRoutes` must admit the release namespace. The gateway's LoadBalancer Service then exposes the port, and AKO's L4 virtual service carries it.
- **Certificates** in the release namespace: the CA ConfigMap or Secret (`crossSite.tls.caCertificateRef`, key `ca.crt`) and a `kubernetes.io/tls` client certificate Secret (`crossSite.tls.clientCertificateSecret`), signed by a CA every peer listener trusts.
- **The same `crossSite.sites` list on every site.** Each site's file sets its own `self` and `serving.gatewayReplicas`.
- **Buffer limits.** A retry needs the request body buffered. The Agent Router already buffers it whole, so a body that fits today also retries.
- **Shared Valkey (optional)** in the Envoy Gateway install config (`rateLimit.backend.redis.url`) on every site, for fleet-wide budgets.

Turning it on: deploy the peer listener and certificates on every site first. Then enable `crossSite` site by site. A site whose peer listener does not yet carry the serving route fails the health check and gets no traffic, so a mixed fleet is safe.

Verify on each site after sync:

```bash
oc get aigatewayroute <release>-sites -n <ns> -o jsonpath='{.status.conditions}'
oc get backendtrafficpolicy <release>-sites <release>-shed -n <ns> -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.ancestors[*].conditions[*].reason}{"\n"}{end}'
# Envoy admin (text output, no jq): the pool rule must be ONE cluster listing every site's host,
# and the InferencePool cluster must carry the shed limit as max_requests
oc port-forward -n <gateway-ns> pod/<envoy-pod> 19000:19000 &
curl -s localhost:19000/clusters | grep -E '<release>-sites|inference-pool' | grep -E 'hostname|max_requests|health_flags'
# per-site pin (all four should answer)
curl -s https://<site gateway>/v1/chat/completions -H 'x-site-pin: <site>' -H 'content-type: application/json' \
  -d '{"model":"<model>","messages":[{"role":"user","content":"hi"}],"max_tokens":1}'
```

