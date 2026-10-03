# kserve-llm

Full-node vLLM replicas served by a KServe `LLMInferenceService` and exposed through an Agent Router (Envoy AI Gateway) `AIGatewayRoute`, for air-gapped clusters with node-local model storage.

Written against KServe v0.21 (`serving.kserve.io/v1alpha2`), Agent Router 1.1 (`aigateway.envoyproxy.io/v1beta1`) and Gateway API Inference Extension `InferencePool` v1.

## Layout

| Path | Holds |
|---|---|
| `chart/` | The chart. `values.yaml` is generic and carries no model settings. |
| `values/<model>-<hardware>.yaml` | The model recipe: vLLM args, env, resources, probes, image tag. |
| `values/sites/<cluster>.yaml` | What differs per cluster: registry, node hostnames, S3 source, gateway, peer listener, global name, benchmark URL. One file per cluster, named after it, read by both charts. Start from `sites/example.yaml`. |
| `fleet/` | The fleet chart: tier 0 for every model, one release per cluster (`fleet/README.md`). |
| `values/fleet.yaml` | The fleet catalogue: every model and every site, identical on every cluster. |

A new model is a new recipe file plus one line in `values/fleet.yaml`; a new cluster is a new site file plus one line there. A model runs on one hardware type with one recipe, so it behaves identically on every site that serves it.

## What it creates

- `StorageClass` plus one `PersistentVolume`/`PersistentVolumeClaim` pair for weights and one for compile caches, as static `local` volumes.
- One pull `Job` per node that syncs the weights from S3 onto that node.
- One `LLMInferenceService` with `replicas = len(nodes)`.
- One `AIGatewayRoute` pointing at the `InferencePool` KServe creates (`<release>-inference-pool`).
- Optional: a `BackendTrafficPolicy` token budget (`rateLimit`), a health `HTTPRoute` for the GSLB monitor (`healthRoute`), and an AIPerf hook `Job` (`benchmark`).
- With `crossSite`: a shed policy on the serving route (`<release>-shed`). Tier 0 lives in the fleet chart (`fleet/`). See "Cross-site serving".

The namespace and the S3 secret (`modelPull.existingSecret`, key `s3cfg`) are not created by the chart.

## Design decisions

- **Static `local` PVs, not a dynamic PVC.** `model.uri: pvc://<claim>` names one claim for all replicas. A TopoLVM/LVMO volume is pinned to one node, so the second replica would stay Pending. A `local` PV whose nodeAffinity lists every node gives each pod its own node's copy at the same path.
- **One replica per node, sized to the whole node.** Each pod requests all GPUs on the node, which is what keeps replicas on separate nodes. Data and expert parallelism stay inside the node.
- **`rolloutStrategy.maxSurge: 0`.** The KServe default surges one pod, which needs a free full node that does not exist, and the rollout deadlocks. The cost is that every spec change takes one node out of service for a cold start.
- **Pull Jobs carry a spec hash in their name.** Job specs are immutable; a changed spec becomes a new Job instead of a failed upgrade.
- **Storage survives `helm uninstall`** (`storage.keepOnUninstall`). PVs are `Retain`, so deleting a PVC by hand leaves the PV `Released` until its `claimRef` is cleared.
- **vLLM args are a map, validated at render time.** The KServe preset launches through `bash -c ... eval` and already sets the model path, `--served-model-name` and `--port`. The chart fails the render on those flags and on any arg that is not a single plain token.
- **Two gateways, one pool.** KServe's own `HTTPRoute` sits on the KServe gateway and the `AIGatewayRoute` on the Agent Router gateway, both targeting the same `InferencePool`. If they share a gateway they must use different listeners (`serving.router.gatewayRefs`, `route.gateway.sectionName`); on the same listener the KServe route can win the match and bypass token metering and rate limits.
- **Cross-site: the model release only serves.** One fleet release per cluster (`fleet/`) owns tier 0 for every model, metering and the global name. A model release with `crossSite` puts its serving route on the peer listener and sheds at its own capacity. Where a model runs is never listed: the fleet health-checks every site for every model.
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
8. With `crossSite`: the fleet release and its pre-reqs (peer listener, certificates). See `fleet/README.md`.

## Argo CD

Point the Application at `chart/` with the recipe file and the cluster's site file as `valueFiles`, and set `argocd.enabled: true`. The chart then orders the sync itself:

| Wave | Resources | Why |
|---|---|---|
| -3 | StorageClass, PVs, PVCs | Marked `Delete=false,Prune=false`; Argo CD ignores `helm.sh/resource-policy`. |
| -2 | Pull Jobs | Argo CD waits for the Jobs to complete, so serving never starts on half-synced weights. |
| 0 | LLMInferenceService | |
| 1 | Token-limit policy (single-site), or the shed policy with `crossSite` | Before the routes, so the model is never reachable unmetered or unprotected. |
| 2 | AIGatewayRoute(s), health route | |
| PostSync | AIPerf Job | |

Argo CD has no built-in health check for `LLMInferenceService` and treats it as healthy on creation. The AIPerf Job therefore waits for the model itself. Add a custom health check for the kind in `argocd-cm` if you also want the Application to show Progressing during a cold start.

## Token rate limiting

`rateLimit` creates a `BackendTrafficPolicy` that charges each client the tokens reported in `route.llmRequestCosts`.

- **It targets the generated HTTPRoute, not the Gateway.** A Gateway accepts one policy, so per-model policies aimed at it would conflict. A route-level policy replaces any Gateway-level one for this route.
- **The client header must come from the gateway.** Derive it from the verified JWT claim or API key and strip it from client input; otherwise a client can name another tenant.
- **Enforcement is after the fact.** The cost is known only when the response ends, so the request that crosses the budget completes and the next one gets 429.
- **Budgets are per cluster** unless every site's rate limit service uses the same Redis/Valkey.
- **It fails open.** If the rate limit service or its Redis is unreachable, requests pass unmetered. That is deliberate: lapsed budgets for a few minutes beat no inference anywhere. Never set `failClosed`; alert on the rate limit filter's `ratelimit.error` and `ratelimit.failure_mode_allowed` counters instead.
- With `crossSite` this chart renders no budget: it lives in the fleet release, charged once at the site the client entered.
- It needs the Envoy Gateway rate limit service with a Redis backend.

## Benchmark hook

`benchmark` runs AIPerf after every install, upgrade or sync, as a gate: the hook fails if the model does not come up, if too many requests fail, or if a `goodput` SLO is missed.

- Keep the default load small. It runs against production on every sync and its tokens count against the benchmark client's budget.
- `benchmark.url` is the site's own gateway, never the global name. With `crossSite` the hook also sends `crossSite.pinHeader: <self>`, so it measures this site and not the fleet.
- The tokenizer is read from the models volume, so the pod runs on a serving node and fetches nothing from Hugging Face.
- The AIPerf image needs `python3` for the wait step.

## Multiple sites behind one URL

The global name (AKO `HostRule`, AMKO `GSLBHostRule`), tier 0 and per-model metering belong to the fleet chart: [`fleet/README.md`](../fleet/README.md). They describe the gateway, not a model. A model release joins the fleet with `crossSite` (below). Each one also needs:

- **Credentials valid everywhere**: the same JWT issuer or API-key store on every site.
- **Roll one site at a time.** A serving change takes part of a site's capacity down for a cold start. Sync a canary site first, then the rest one by one, gated by the benchmark hook.

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

## Cross-site serving

Full rationale: [`docs/cross-site-architecture.md`](../docs/cross-site-architecture.md). Tier 0 and its pre-reqs: [`fleet/README.md`](../fleet/README.md).

`crossSite` makes this model release serve the whole fleet:

- **The serving route moves to `crossSite.peerListener`.** That is a peer-only mTLS listener the fleet's tier 0 dials from every site. It also stops metering, because the fleet charges once at entry. `route.gateway.sectionName` (the fleet's client listener) is required and must differ, or clients could reach the unmetered route.
- **The health route is attached to the peer listener too.** The fleet health-checks `/healthz/<model>` on every site; a 200 is what puts this site in the model's pool.
- **Shed at capacity.** `<release>-shed` is a circuit breaker on the serving route at `requestsPerNode × len(nodes) / gatewayReplicas` per Envoy replica. Past it the site answers 503 at once and the sender retries another site.
  - Size `requestsPerNode` from work, not slots. vLLM logs `Maximum concurrency for <N> tokens per request` at startup; use a realistic N (agent requests run around 200K tokens), plus a short queue.
  - Keep gateway replicas fixed.
- **The benchmark pins itself** with `crossSite.pinHeader: <self>`, so it measures this site, not the fleet.

Verify after sync: the shed limit must be on the InferencePool cluster. Agent Router rewrites that cluster but leaves `circuit_breakers` alone; confirm it once.

```bash
oc port-forward -n <gateway-ns> pod/<envoy-pod> 19000:19000 &
curl -s localhost:19000/clusters | grep inference-pool | grep max_requests
```
