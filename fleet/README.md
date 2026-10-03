# kserve-llm-fleet

Tier 0 for a fleet of sites serving several models behind one name. One identical release per
cluster renders, for every model in the catalogue, an Agent Router route that spreads that model's
requests over every site serving it. The model releases (`chart/`) only serve.

Written against Agent Router 1.1 (`aigateway.envoyproxy.io/v1beta1`) and Envoy Gateway v1.8
(`gateway.envoyproxy.io/v1alpha1`). Why it is built this way: [`docs/cross-site-architecture.md`](../docs/cross-site-architecture.md).

```
client -> llm.<domain> (Avi GSLB, round robin, monitors /healthz/gateway) -> any site's client listener
  fleet release:  AIGatewayRoute <release>-<model>  ->  AIServiceBackend <release>-all-sites
                  (every site an endpoint: least-request, health-checked per model, retry elsewhere)
  -> chosen site's peer listener (mTLS) -> model release: serving route -> InferencePool -> EPP -> vLLM
```

## Install

```bash
helm template fleet fleet -n <ns> -f values/fleet.yaml -f values/sites/<cluster>.yaml
```

- `values/fleet.yaml` is the catalogue: every model served anywhere, and every site. It is the
  same file on every cluster.
- `values/sites/<cluster>.yaml` is the same site file the model chart reads. The fleet chart uses
  `route.gateway`, `crossSite.peerListener` / `tls` / `pinHeader` and `globalName` from it.
- Argo CD: add an ApplicationSet entry pointing at `fleet/`, with those two value files and
  `argocd.enabled: true`. The chart carries sync waves only.

## What it creates

Per cluster:
- **`Backend` + `AIServiceBackend` `<release>-all-sites`**: every site as an endpoint. Pool rules
  use it.
- **One `Backend` + `AIServiceBackend` `<release>-site-<site>` per site**: used by the pin rules.
- **Per model:**
  - an `AIGatewayRoute` `<release>-<model id>` on the client listener, with rules `pin-<site>`
    and `pool`, plus metering;
  - a `BackendTrafficPolicy` of the same name: load balancer, retry, health check, limits and
    token budget.
- **`HTTPRouteFilter` + `HTTPRoute` `<release>-gateway-health`**: `GET /healthz/gateway` returns
  200 from Envoy itself.
- **With `globalName`**: the AKO `HostRule` on every cluster, and the AMKO `GSLBHostRule` on the
  leader cluster.

## How it works

- **Membership comes from health checks.** Each model's policy health-checks
  `GET /healthz/<model>` on every site's peer listener. That path is the model release's
  `healthRoute`. A site that doesn't serve the model, or whose model is down, fails and is excluded
  within `interval × unhealthyThreshold` (default ~15 s). Deploy the model there and the site
  passes and joins. **No list says where a model runs.** `panicThreshold: 0` keeps Envoy from
  routing to unhealthy sites even when most are down.
- **Load: least-request across sites.** All sites are endpoints of one `Backend`, so Envoy
  Gateway builds one locality and the least-request balancer compares sites. Separate backends per
  site would each be their own locality, picked by weight first, and least-request would have one
  host to choose from (Envoy Gateway v1.8 `cluster.go`). The chart and `validate.py` both refuse
  that shape.
- **Overflow: shed and retry.** A full site's serving route answers 503 at its circuit breaker
  (model chart, `crossSite.serving`). Tier 0 retries a different site (`previous_hosts`) up to
  `len(sites) - 1` times, only before the response starts.
- **Metering happens once, at entry.** Each model's `llmRequestCosts` and optional token budget
  live here. The serving routes behind the peer listener charge nothing. The budget fails open;
  never set `failClosed`, and alert on `ratelimit.error` / `ratelimit.failure_mode_allowed`
  instead. For fleet-wide budgets, point every cluster's rate limit service at one Valkey.
- **`x-site-pin: <site>`** sends to that site only. Model releases' benchmark hooks use it.
- **Consistent hashing (optional, per model):** set `models[].hashHeader` to keep a conversation on
  one site. A retry then moves to the next host for that key, which gives a stable second site. It
  is off everywhere until clients send a stable session header. Never hash on the tenant: a heavy
  tenant would sit on one site and shed constantly.

## Rules and limits

- **One hardware type per model.** Membership by health check pools any site that answers. If a
  model ran on sites with different recipes (`max-model-len`, slots per node), a request that fits
  one site could fail on another.
- **Least-request balances in-flight requests per site, not per node, and from this Envoy replica's
  view only.** A site much larger than its peers for the same model is underused until the smaller
  ones shed. It is not a fleet-wide load signal; that is the job of a future picker.
- **Every stream crosses two gateways**: the entry site's and the serving site's. Restarting
  either cuts the streams through it, and drain timeouts don't cover hour-long streams. Roll
  gateways one site at a time, off-peak.
- **Keep gateway replicas fixed.** Shed points are divided per replica (model chart
  `serving.gatewayReplicas`).
- **No affinity yet.** With least-request, two turns of a conversation land on the same site only
  by chance (≈ 1/sites). Inside a site, DP8 behind one port already loses most prefix hits
  (FINDINGS A9). Turn on hashing once Track A fixes that.
- **Site addresses must be hostnames.** An IP among hostnames splits the cluster per site, and
  least-request, hashing and cross-site retry all stop working.

## Pre-reqs (per cluster; the gateway is shared, so not in any chart)

1. **A peer listener** on the Agent Router Gateway:
   - HTTPS on `crossSite.port` (8443), with no `hostname`;
   - a certificate covering `crossSite.tls.hostname` (default `globalName.fqdn`);
   - a `ClientTrafficPolicy` on that listener requiring client certificates
     (`tls.clientValidation.caCertificateRefs`);
   - `allowedRoutes` admitting every model namespace.
2. **A client listener** (`route.gateway.sectionName`) admitting the fleet namespace.
3. **Certificates in the fleet namespace**: the CA (`crossSite.tls.caCertificateRef`, key
   `ca.crt`) and a `kubernetes.io/tls` client certificate (`crossSite.tls.clientCertificateSecret`).
4. **Envoy Gateway's `Backend` API enabled** (`extensionApis.enableBackend: true`):
   `oc get cm -n envoy-gateway-system envoy-gateway-config -o jsonpath='{.data.envoy-gateway\.yaml}' | grep -A3 extensionApis`
5. **Each model release** has `crossSite.enabled: true` and `healthRoute.enabled: true`. Its
   serving route then sits on the peer listener and stops metering.
6. **Optional:** a shared Valkey in the Envoy Gateway rate-limit config
   (`rateLimit.backend.redis.url`) on every cluster.
7. **Avi:** a federated HTTPS monitor `GET /healthz/gateway`, expecting 200, referenced in
   `globalName.gslbHostRule.healthMonitorRefs`.

## Rollout

1. Pre-reqs on every cluster.
2. Install the fleet release on every cluster. Until a model release has `crossSite` on, its
   site fails the health checks, so nothing is routed there.
3. Enable `crossSite` in the model releases site by site. Each one gated by its pinned benchmark.
4. Point GSLB at the fleet's gateway monitor, with round robin.

**Adding a model:** add a line to `values/fleet.yaml`, sync every fleet release, then deploy the
model release wherever it should run. **Adding a site:** add it to `sites`, sync every fleet
release.

## Verify

```bash
oc get aigatewayroute,backendtrafficpolicy -n <ns> -l app.kubernetes.io/component=fleet-router
oc get aigatewayroute <release>-<model> -n <ns> -o jsonpath='{.status.conditions}'
# Envoy admin, text output: each model's pool cluster lists every site; sites without the model
# show a failed health flag (/failed_active_hc)
oc port-forward -n <gateway-ns> pod/<envoy-pod> 19000:19000 &
curl -s localhost:19000/clusters | grep -E '<release>-<model>' | grep -E 'hostname|health_flags|max_requests'
# gateway health (GSLB monitor)
curl -s https://<site gateway>/healthz/gateway
# one pinned request per site and model
curl -s https://<site gateway>/v1/chat/completions -H 'x-site-pin: <site>' -H 'content-type: application/json' \
  -d '{"model":"<model>","messages":[{"role":"user","content":"hi"}],"max_tokens":1}'
```

On the serving side, also confirm that the model's InferencePool cluster carries the shed limit as
`max_requests` (model chart README).
