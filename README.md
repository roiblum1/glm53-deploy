# LLM serving across sites, behind one name

Several models, each running on some of several air-gapped OpenShift clusters, served to customers
through one URL. A customer sends an OpenAI-style request with an API key; it is answered by
whichever site serving that model has room.

This file is the overview. The details are in:

| Document | Covers |
|---|---|
| [`docs/cross-site-architecture.md`](docs/cross-site-architecture.md) | Why it is built this way, component by component, and how it behaves under failure |
| [`fleet/README.md`](fleet/README.md) | The fleet chart: tier 0, API keys, token budgets, pre-reqs, verify commands |
| [`chart/README.md`](chart/README.md) | The model chart: storage, weight pull, KServe serving, benchmark hook |
| [`FINDINGS.md`](FINDINGS.md) | The source-level audit behind the design, and what is still unverified |

## The fleet

| Model | Hardware | Runs on |
|---|---|---|
| GLM-5.3 | B200 | 2 sites |
| Kimi K2.7 | H200 | 3 sites |
| Qwen3.8 27B | not recorded yet | 2 sites |

Four sites. Each is its own cluster with its own Agent Router (Envoy AI Gateway) gateway, exposed
by Avi (AKO) as an L4 VIP. Nothing reaches the internet: images come from the internal registry,
weights from internal S3.

## Architecture

```
 customer:  POST https://llm.<domain>/v1/chat/completions   Authorization: Bearer sk-...
                                   |
        Avi GSLB (AMKO), round robin over sites whose gateway is up
                                   |
                     any site's gateway, client listener
   +-------------------------- FLEET release (same on every cluster) ---------------------------+
   | 1. API key -> customer ID in x-tenant-id          (401 if unknown)                         |
   | 2. customer's token budget for this model         (429 if spent)   <----> Redis on the hub |
   | 3. pick a site serving this model: health-checked, least requests in flight                |
   | 4. on 503 / connect failure: retry another site                                            |
   | 5. when the response ends: charge the tokens used                  <----> Redis on the hub |
   +---------------------------------------------+----------------------------------------------+
                                                 | mTLS
                     chosen site's gateway, peer listener
   +-------------------------- MODEL release (only where the model runs) -----------------------+
   | over this site's limit -> 503 at once;  else -> InferencePool -> EPP -> vLLM               |
   +---------------------------------------------------------------------------------------------+
```

Two layers, two charts:

- **Tier 0, the fleet release (`fleet/`).** One identical release on every cluster. It
  authenticates, meters and picks a site, for every model, whether or not this cluster serves it.
- **Serving, the model release (`chart/`).** One per model per cluster where that model runs. It
  owns the weights, the `LLMInferenceService`, and the site's shed limit.

### How a site is chosen

- **Where a model runs is not listed anywhere.** Each gateway health-checks every site for every
  model (`GET /healthz/<model>`). Sites that answer are in that model's pool. Deploy a model on a
  site and it joins; remove it and it leaves.
- **Among those sites, least-request.** The gateway sends to the site where it has fewer requests
  in flight. A busy site holds requests longer, so it gets fewer new ones.
- **A full site refuses, and the request moves on.** Each site answers 503 past its own in-flight
  limit, and the entry gateway retries another site. Nothing is duplicated: retries happen only
  before the response starts.
- **Busy and dead are different signals.** Only a failed health check removes a site. A busy site
  just refuses one request.

### Customers, keys and budgets

- **Each customer has an API key.** The keys are entries in one Secret, the same on every cluster.
  The gateway turns a valid key into the customer's ID (`x-tenant-id`) and overwrites anything the
  client sent.
- **Each customer has a token budget per model**, by default 2,000,000 tokens per hour. Past it,
  requests get 429 until the hour ends.
- **The budget is fleet-wide.** Every site's rate limit service counts in the same Redis on the
  hub cluster, so a customer has one counter per model wherever they enter.
- **It fails open.** If the hub Redis is down or slow, requests pass unmetered and alerts fire.
  Inference never stops for a limiter problem.

### Limits to know

- **No conversation stickiness.** Two turns of a conversation land on the same site only by
  chance. Per-model consistent hashing exists (`models[].hashHeader`) and is off until clients
  send a session header.
- **Least-request balances per site, not per node.** A site much larger than its peers for the
  same model is underused until the smaller ones start shedding.
- **Every stream crosses two gateways.** Restarting the entry site's gateway cuts streams served
  elsewhere. Roll gateways one site at a time, off-peak.
- **One hardware type per model.** A model pooled across sites with different recipes could accept
  a request on one site that fails on another.

## Repository layout

| Path | Holds |
|---|---|
| `fleet/` | The fleet chart (tier 0) |
| `chart/` | The model chart (serving) |
| `values/fleet.yaml` | The catalogue: models, sites, budgets, API-key Secret. Identical on every cluster |
| `values/<model>-<hardware>.yaml` | A model recipe: vLLM args, resources, probes, shed limit per node |
| `values/sites/<cluster>.yaml` | One cluster: registry, nodes, gateway, listeners, certificates, global name. Read by both charts |
| `values/envoy-gateway.example.yaml` | Envoy Gateway install values for the hub Redis. Not read by these charts |
| `deploy.yaml` | The original single-site manifest, kept for comparison |

Delivery is Argo CD: an ApplicationSet points at `fleet/` and `chart/` with these value files.
The charts carry only sync-wave and hook annotations.

## Setting it up

In this order. Steps 1 to 3 are outside these charts.

1. **The hub Redis.** Deploy it on the hub cluster, expose it as a LoadBalancer Service, with TLS
   and a password.
2. **Envoy Gateway on every site.** Merge `values/envoy-gateway.example.yaml` into its install
   values: the Redis address, the raised rate limit timeout, the password and CA.
3. **The gateway on every site.**
   - a client listener and a peer listener (8443, mTLS) with its `ClientTrafficPolicy`;
   - the peer CA and client certificate in the fleet namespace;
   - the Envoy Gateway `Backend` API enabled.

   The full list is in `fleet/README.md`, "Pre-reqs".
4. **The API-key Secret** in the fleet namespace on every site, identical everywhere.
5. **The fleet release on every site**, with the same release name and namespace everywhere:

   ```bash
   helm template fleet fleet -n <fleet-ns> -f values/fleet.yaml -f values/sites/<cluster>.yaml
   ```

6. **The model releases**, site by site, each gated by its benchmark hook:

   ```bash
   helm template glm53 chart -n llm-glm53 -f values/glm53-b200.yaml -f values/sites/<cluster>.yaml
   ```

7. **Avi:** a federated HTTPS monitor on `GET /healthz/gateway`, and round robin for the global
   name.

## Day-to-day

| Task | What to change |
|---|---|
| Add a customer | Add an entry (ID and key) to the API-key Secret on every site. Commands in `fleet/README.md`, "Customer API keys" |
| Revoke a customer | Remove the entry |
| Change the default budget | `fleet.defaults.rateLimit` in `values/fleet.yaml` |
| A different budget for one model | `models[].rateLimit` in `values/fleet.yaml` |
| Add a model to the fleet | One line under `models` in `values/fleet.yaml`, sync every fleet release, then deploy the model release where it should run |
| Serve an existing model on another site | Deploy its model release there. Nothing else changes |
| Add a site | One line under `sites` in `values/fleet.yaml`, a new `values/sites/<cluster>.yaml`, sync every fleet release |
| Send a test request to one site | Add the header `x-site-pin: <site>` |

## Checking a change

Both charts are checked offline, with no cluster access:

```bash
helm lint fleet -f values/fleet.yaml -f values/sites/<cluster>.yaml
helm template fleet fleet -n <fleet-ns> -f values/fleet.yaml -f values/sites/<cluster>.yaml \
  | python3 .claude/skills/validate-manifest/validate.py -

helm lint chart -f values/glm53-b200.yaml -f values/sites/<cluster>.yaml
helm template glm53 chart -n llm-glm53 -f values/glm53-b200.yaml -f values/sites/<cluster>.yaml \
  | python3 .claude/skills/validate-manifest/validate.py -
```

## Not yet verified on a cluster

The design was read from upstream source at the deployed versions. None of it has run on the
clusters. Confirm on the first site:

1. A site past its limit sheds 503 and the request succeeds on another site.
2. Each model's tier-0 cluster lists every site, and sites without the model show a failed health
   check.
3. A customer's budget spent through one site gives 429 through another.
4. A forged `x-tenant-id` is charged to the key's own customer, and `Authorization: Bearer <key>`
   is accepted.
5. The CRD fields both charts use exist in the installed versions (`FINDINGS.md`, open
   questions).
