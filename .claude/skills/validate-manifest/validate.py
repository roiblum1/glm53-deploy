#!/usr/bin/env python3
"""Offline consistency checks for a rendered manifest (`helm template ... | validate.py -`).

The rules are the ones documented in chart/README.md and chart/values.yaml.
"""
import re
import sys

import yaml

path = sys.argv[1] if len(sys.argv) > 1 else "-"
errors, warns, notes = [], [], []

try:
    if path == "-":
        raw = sys.stdin.read()
    else:
        with open(path) as fh:
            raw = fh.read()
    docs = [d for d in yaml.safe_load_all(raw) if d]
except (OSError, yaml.YAMLError) as e:
    sys.exit(f"FAIL  cannot load {path}: {e}")


def dig(obj, *keys, default=None):
    for k in keys:
        if isinstance(obj, dict):
            obj = obj.get(k)
        elif isinstance(obj, list) and isinstance(k, int) and k < len(obj):
            obj = obj[k]
        else:
            return default
        if obj is None:
            return default
    return obj


def of_kind(kind):
    return [d for d in docs if d.get("kind") == kind]


def name(d):
    return f"{d.get('kind')}/{dig(d, 'metadata', 'name')}"


CLUSTER_SCOPED = {"StorageClass", "PersistentVolume"}
# Gateway-level objects that live in the AKO / AMKO namespaces, not the release namespace.
FOREIGN_NAMESPACE = {"HostRule", "GSLBHostRule"}

# --- namespace -----------------------------------------------------------------
namespaces = set()
for d in docs:
    if d.get("kind") in CLUSTER_SCOPED | FOREIGN_NAMESPACE:
        continue
    ns = dig(d, "metadata", "namespace")
    if ns:
        namespaces.add(ns)
    else:
        errors.append(f"{name(d)}: no metadata.namespace")
if len(namespaces) > 1:
    errors.append(f"namespaced resources span several namespaces: {sorted(namespaces)} "
                  "(pull Jobs must share the llmisvc namespace for the SELinux MCS label)")

# --- storage -------------------------------------------------------------------
storage_classes = {dig(d, "metadata", "name") for d in of_kind("StorageClass")}
pvs = {dig(d, "metadata", "name"): d for d in of_kind("PersistentVolume")}
pvcs = {dig(d, "metadata", "name"): d for d in of_kind("PersistentVolumeClaim")}

pv_nodes = {}
for pv_name, pv in pvs.items():
    hosts = set()
    for term in dig(pv, "spec", "nodeAffinity", "required", "nodeSelectorTerms", default=[]):
        for expr in term.get("matchExpressions", []):
            if expr.get("key") == "kubernetes.io/hostname":
                hosts.update(expr.get("values", []))
    pv_nodes[pv_name] = hosts
    if not hosts:
        errors.append(f"PersistentVolume/{pv_name}: no kubernetes.io/hostname nodeAffinity")
    sc = dig(pv, "spec", "storageClassName")
    if sc not in storage_classes:
        errors.append(f"PersistentVolume/{pv_name}: storageClassName {sc!r} is not defined in the file")

node_sets = {frozenset(h) for h in pv_nodes.values()}
if len(node_sets) > 1:
    errors.append("PersistentVolumes list different nodes: "
                  + ", ".join(f"{k}={sorted(v)}" for k, v in pv_nodes.items()))
nodes = set().union(*pv_nodes.values()) if pv_nodes else set()

for pvc_name, pvc in pvcs.items():
    vol = dig(pvc, "spec", "volumeName")
    if vol not in pvs:
        errors.append(f"PersistentVolumeClaim/{pvc_name}: volumeName {vol!r} has no matching PersistentVolume")
        continue
    if dig(pvc, "spec", "storageClassName") != dig(pvs[vol], "spec", "storageClassName"):
        errors.append(f"PersistentVolumeClaim/{pvc_name}: storageClassName differs from PersistentVolume/{vol}")


def check_claims(owner, volumes):
    for v in volumes or []:
        claim = dig(v, "persistentVolumeClaim", "claimName")
        if claim and claim not in pvcs:
            errors.append(f"{owner}: volume {v.get('name')!r} uses undefined PVC {claim!r}")


images = []

# --- pull jobs -----------------------------------------------------------------
job_nodes = []
pull_jobs = [j for j in of_kind("Job")
             if dig(j, "metadata", "labels", "app.kubernetes.io/component") == "model-pull"]
for job in pull_jobs:
    pod = dig(job, "spec", "template", "spec", default={})
    host = dig(pod, "nodeSelector", "kubernetes.io/hostname")
    if not host:
        errors.append(f"{name(job)}: no kubernetes.io/hostname nodeSelector")
    else:
        job_nodes.append(host)
        if host not in nodes:
            errors.append(f"{name(job)}: node {host!r} is not in the PersistentVolume nodeAffinity {sorted(nodes)}")
        if host not in dig(job, "metadata", "name", default=""):
            warns.append(f"{name(job)}: name does not contain its node {host!r}")
    check_claims(name(job), pod.get("volumes"))
    images += [(name(job), c.get("image")) for c in pod.get("containers", [])]

for n in sorted(nodes - set(job_nodes)):
    errors.append(f"node {n!r} has no pull Job (its copy of the weights would stay empty)")
for n in sorted({n for n in job_nodes if job_nodes.count(n) > 1}):
    errors.append(f"node {n!r} has more than one pull Job")

# --- mode ------------------------------------------------------------------------
# Model render (chart/): one LLMInferenceService and its serving route.
# Fleet render (fleet/): no LLMInferenceService, tier-0 routes to AIServiceBackends only.


def is_tier0(route):
    refs = [b for rule in dig(route, "spec", "rules", default=[]) for b in rule.get("backendRefs", [])]
    return bool(refs) and all(b.get("kind") in (None, "AIServiceBackend") for b in refs)


fleet_mode = not of_kind("LLMInferenceService") and any(is_tier0(r) for r in of_kind("AIGatewayRoute"))

# --- LLMInferenceService -------------------------------------------------------
FORBIDDEN_FLAGS = {"--model", "--port", "--served-model-name"}
llmisvcs = of_kind("LLMInferenceService")
if len(llmisvcs) != 1 and not fleet_mode:
    errors.append(f"expected exactly one LLMInferenceService, found {len(llmisvcs)}")

for svc in llmisvcs:
    spec = svc.get("spec", {})
    for key in ("parallelism", "worker"):
        if key in spec:
            errors.append(f"{name(svc)}: spec.{key} is set (single-node Deployment path only; pass DP/EP as vLLM args)")

    surge = dig(spec, "rolloutStrategy", "maxSurge")
    if str(surge) != "0":
        errors.append(f"{name(svc)}: rolloutStrategy.maxSurge is {surge!r}, must be 0 "
                      "(a surge pod needs 8 free GPUs that do not exist; rollout deadlocks)")

    replicas = spec.get("replicas")
    if nodes and replicas != len(nodes):
        errors.append(f"{name(svc)}: replicas={replicas} but the PersistentVolumes list {len(nodes)} node(s)")

    uri = dig(spec, "model", "uri", default="")
    m = re.match(r"pvc://([^/]+)", uri)
    if not m:
        warns.append(f"{name(svc)}: model.uri {uri!r} is not pvc:// (air-gapped: no storage-initializer download)")
    elif m.group(1) not in pvcs:
        errors.append(f"{name(svc)}: model.uri references undefined PVC {m.group(1)!r}")

    tmpl = spec.get("template", {})
    check_claims(name(svc), tmpl.get("volumes"))
    for c in tmpl.get("containers", []):
        owner = f"{name(svc)} container {c.get('name')!r}"
        images.append((owner, c.get("image")))

        for a in c.get("args", []):
            if not isinstance(a, str):
                errors.append(f"{owner}: arg {a!r} is not a string")
                continue
            if re.search(r"\s", a) or any(ch in a for ch in "{}[]\"'"):
                errors.append(f"{owner}: arg {a!r} is not a single plain token "
                              "(preset runs through bash -c eval: use --flag=value and dotted keys)")
            if not a.startswith("--"):
                errors.append(f"{owner}: arg {a!r} is not --flag[=value] (split 'flag value' pairs break the rule above)")
            if a.split("=", 1)[0] in FORBIDDEN_FLAGS:
                errors.append(f"{owner}: arg {a!r} is already set by the kserve-config-llm-template preset")

        env = {e.get("name"): str(e.get("value")) for e in c.get("env", [])}
        for var in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
            if env.get(var) != "1":
                errors.append(f"{owner}: env {var} is not \"1\" (air-gapped)")

        req = dig(c, "resources", "requests", "nvidia.com/gpu")
        lim = dig(c, "resources", "limits", "nvidia.com/gpu")
        if str(req) != str(lim):
            errors.append(f"{owner}: nvidia.com/gpu request {req!r} != limit {lim!r}")

# --- AIGatewayRoute ------------------------------------------------------------
svc = llmisvcs[0] if llmisvcs else {}
model_name = dig(svc, "spec", "model", "name")
pool = f"{dig(svc, 'metadata', 'name')}-inference-pool"


def section(route):
    return {r.get("sectionName") for r in dig(route, "spec", "parentRefs", default=[])}


tier0_routes = [r for r in of_kind("AIGatewayRoute") if is_tier0(r)]
serving_routes = [r for r in of_kind("AIGatewayRoute") if not is_tier0(r)]
ai_backends = {dig(b, "metadata", "name"): b for b in of_kind("AIServiceBackend")}
backends = {dig(b, "metadata", "name"): b for b in of_kind("Backend")}

if tier0_routes and not fleet_mode:
    errors.append("tier-0 routes in a model render: tier 0 belongs to the fleet release (fleet/)")
route_models = {}
for route in of_kind("AIGatewayRoute"):
    tier0 = route in tier0_routes
    for i, rule in enumerate(dig(route, "spec", "rules", default=[])):
        where = f"{name(route)} rule {i}"
        models = [h.get("value") for m in rule.get("matches", []) for h in m.get("headers", [])
                  if h.get("name") == "x-ai-eg-model"]
        if fleet_mode:
            if len(models) != 1:
                errors.append(f"{where}: x-ai-eg-model matches {models}, expected exactly one model")
            else:
                route_models.setdefault(name(route), set()).add(models[0])
        elif model_name not in models:
            errors.append(f"{where}: x-ai-eg-model matches {models}, llmisvc model.name is {model_name!r}")
        if tier0:
            for b in rule.get("backendRefs", []):
                if b.get("name") not in ai_backends:
                    errors.append(f"{where}: backendRef {b.get('name')!r} has no AIServiceBackend in the render")
        else:
            pools = [b.get("name") for b in rule.get("backendRefs", []) if b.get("kind") == "InferencePool"]
            if pools != [pool]:
                errors.append(f"{where}: InferencePool backendRefs {pools}, expected exactly [{pool!r}]")
        if not dig(rule, "timeouts", "request"):
            errors.append(f"{where}: no timeouts.request (default 60s kills long reasoning)")
    for ref in dig(route, "spec", "parentRefs", default=[]):
        ref_ns = ref.get("namespace")
        if ref_ns and ref_ns != dig(route, "metadata", "namespace"):
            notes.append(f"{name(route)}: cross-namespace parentRef to {ref_ns}/{ref.get('name')} — "
                         f"its listener allowedRoutes must admit {dig(route, 'metadata', 'namespace')}")

for rname, ms in route_models.items():
    if len(ms) > 1:
        errors.append(f"{rname}: rules match different models {sorted(ms)}; one route per model")
seen_models = [m for ms in route_models.values() for m in ms]
for m in sorted({m for m in seen_models if seen_models.count(m) > 1}):
    errors.append(f"model {m!r} has more than one tier-0 route")

# --- cross-site: fleet tier 0 ------------------------------------------------------
t0s_by_name = {dig(r, "metadata", "name"): r for r in tier0_routes}
for t0 in tier0_routes:
    if None in section(t0):
        errors.append(f"{name(t0)}: no sectionName; the tier-0 route would also attach to the peer listener and loop")
    for sr in serving_routes:
        if section(t0) & section(sr):
            errors.append(f"{name(t0)} and {name(sr)} share listener {sorted(section(t0) & section(sr))}: "
                          "clients would reach the serving route unmetered, or peers would re-enter tier 0")
        if dig(sr, "spec", "llmRequestCosts"):
            errors.append(f"{name(sr)}: llmRequestCosts on the serving route as well as tier 0 — tokens are charged twice")
    if not dig(t0, "spec", "llmRequestCosts"):
        warns.append(f"{name(t0)}: no llmRequestCosts — nothing meters client traffic")
tls_settings = set()
for bname, b in backends.items():
    for ep in dig(b, "spec", "endpoints", default=[]):
        if "fqdn" not in ep:
            errors.append(f"Backend/{bname}: endpoint is not fqdn — mixed address types split the tier-0 cluster per site, "
                          "so a retry can no longer move to another site")
    tls_settings.add(repr(dig(b, "spec", "tls")))
    if not dig(b, "spec", "tls", "clientCertificateRef", "name"):
        errors.append(f"Backend/{bname}: no tls.clientCertificateRef — the peer listener requires a client certificate")
if len(tls_settings) > 1:
    warns.append("Backends differ in tls settings — Envoy Gateway may build one cluster per site; check retries still cross sites")
for asb_name, asb in ai_backends.items():
    ref = dig(asb, "spec", "backendRef", "name")
    if ref not in backends:
        errors.append(f"AIServiceBackend/{asb_name}: backendRef {ref!r} has no Backend in the render")
fleet_sites = max((len(dig(b, "spec", "endpoints", default=[])) for b in backends.values()), default=0)
for t0 in tier0_routes:
    for rule in dig(t0, "spec", "rules", default=[]):
        refs = rule.get("backendRefs", [])
        if len(refs) > 1:
            errors.append(f"{name(t0)} rule {rule.get('name')!r}: {len(refs)} backendRefs — each is its own locality, picked "
                          "by weight first; list sites as endpoints of one Backend so least-request and hashing compare sites")

# A Distinct client header only identifies the client if the gateway writes it.
forwarded = {}
for sp in of_kind("SecurityPolicy"):
    header = dig(sp, "spec", "apiKeyAuth", "forwardClientIDHeader")
    for ref in dig(sp, "spec", "targetRefs", default=[]):
        forwarded.setdefault(ref.get("name"), set()).add(header)
    if dig(sp, "spec", "apiKeyAuth") and not dig(sp, "spec", "apiKeyAuth", "credentialRefs"):
        errors.append(f"{name(sp)}: apiKeyAuth without credentialRefs")
for btp in of_kind("BackendTrafficPolicy") if fleet_mode else []:
    for ref in dig(btp, "spec", "targetRefs", default=[]):
        for i, rule in enumerate(dig(btp, "spec", "rateLimit", "global", "rules", default=[])):
            for sel in rule.get("clientSelectors", []):
                for h in sel.get("headers", []):
                    if h.get("type") == "Distinct" and h.get("name") not in forwarded.get(ref.get("name"), set()):
                        warns.append(f"{name(btp)} rule {i}: budget counts on header {h.get('name')!r} but no SecurityPolicy on "
                                     f"route {ref.get('name')} writes it — clients can forge it, or omit it and go unlimited")
            if len(dig(t0s_by_name.get(ref.get("name"), {}), "spec", "rules", default=[])) > 1 and not rule.get("shared"):
                warns.append(f"{name(btp)} rule {i}: not shared — each route rule (pin-<site>, pool) gets its own budget")

# --- cross-site: serving side of a model release -----------------------------------
# Peer mode: the serving route stops metering and the health route is on two listeners.
peer_mode = (not fleet_mode and bool(serving_routes)
             and not any(dig(r, "spec", "llmRequestCosts") for r in serving_routes)
             and any(len(dig(h, "spec", "parentRefs", default=[])) > 1 for h in of_kind("HTTPRoute")))
if (not fleet_mode and any(dig(r, "spec", "llmRequestCosts") for r in serving_routes)
        and any(len(dig(h, "spec", "parentRefs", default=[])) > 1 for h in of_kind("HTTPRoute"))):
    errors.append("crossSite is on (health route on two listeners) but the serving route still meters — "
                  "the fleet's tier 0 already charges, so tokens would be charged twice")
if peer_mode:
    for sr in serving_routes:
        shed = [b for b in of_kind("BackendTrafficPolicy")
                if any(ref.get("name") == dig(sr, "metadata", "name") for ref in dig(b, "spec", "targetRefs", default=[]))
                and dig(b, "spec", "circuitBreaker", "maxParallelRequests")]
        if not shed:
            errors.append(f"{name(sr)}: serving on the peer listener without a shed policy — a full site queues in vLLM "
                          "instead of answering 503 for the sender to retry elsewhere")

# --- rate limit ----------------------------------------------------------------
route_names = {dig(r, "metadata", "name") for r in of_kind("AIGatewayRoute")}
cost_keys = {c.get("metadataKey") for r in of_kind("AIGatewayRoute")
             for c in dig(r, "spec", "llmRequestCosts", default=[])}
policy_targets = {}
for btp in of_kind("BackendTrafficPolicy"):
    for ref in dig(btp, "spec", "targetRefs", default=[]):
        policy_targets.setdefault((ref.get("kind"), ref.get("name")), []).append(name(btp))
for (kind, target), owners in policy_targets.items():
    if len(owners) > 1:
        errors.append(f"{kind}/{target} is targeted by several BackendTrafficPolicies {owners}; only one takes effect")
tier0_names = {dig(r, "metadata", "name") for r in tier0_routes}
tier0_policies = set()
for btp in of_kind("BackendTrafficPolicy"):
    targets = {ref.get("name") for ref in dig(btp, "spec", "targetRefs", default=[])}
    if targets & tier0_names:
        tier0_policies |= targets & tier0_names
        if not dig(btp, "spec", "retry"):
            errors.append(f"{name(btp)}: tier-0 policy without retry — a site shedding 503 fails the request instead of moving it")
        retries = dig(btp, "spec", "retry", "numRetries")
        if retries is not None and fleet_sites and retries > fleet_sites - 1:
            errors.append(f"{name(btp)}: numRetries {retries} > sites-1 ({fleet_sites - 1})")
        elif retries is not None and fleet_sites and retries < fleet_sites - 1:
            warns.append(f"{name(btp)}: numRetries {retries} < sites-1 ({fleet_sites - 1}) — a request can fail while a site is free")
        if dig(btp, "spec", "healthCheck", "panicThreshold") != 0:
            warns.append(f"{name(btp)}: healthCheck.panicThreshold is not 0 — with most sites down Envoy routes to dead ones")
        if not dig(btp, "spec", "healthCheck", "active", "http", "path"):
            errors.append(f"{name(btp)}: no active HTTP health check — membership (which sites serve the model) comes from it")
        if not dig(btp, "spec", "loadBalancer", "type"):
            warns.append(f"{name(btp)}: no loadBalancer.type — Envoy Gateway's default applies (least-request)")
for t0 in sorted(tier0_names - tier0_policies):
    errors.append(f"AIGatewayRoute/{t0}: no BackendTrafficPolicy — no retry, no health checks, Envoy's 1024 request cap")
for btp in of_kind("BackendTrafficPolicy"):
    for ref in dig(btp, "spec", "targetRefs", default=[]):
        if ref.get("kind") == "Gateway":
            errors.append(f"{name(btp)}: targets a Gateway (one policy per Gateway; per-model policies conflict)")
        elif ref.get("kind") == "HTTPRoute" and ref.get("name") not in route_names:
            errors.append(f"{name(btp)}: targets HTTPRoute {ref.get('name')!r}, no AIGatewayRoute of that name")
    for i, rule in enumerate(dig(btp, "spec", "rateLimit", "global", "rules", default=[])):
        key = dig(rule, "cost", "response", "metadata", "key")
        if key and key not in cost_keys:
            errors.append(f"{name(btp)} rule {i}: cost key {key!r} is not in the route's llmRequestCosts")
        headers = [h for sel in rule.get("clientSelectors", []) for h in sel.get("headers", [])]
        if not any(h.get("type") == "Distinct" for h in headers):
            warns.append(f"{name(btp)} rule {i}: no Distinct client header, the budget is shared by all clients")

# --- benchmark hook ------------------------------------------------------------
for job in of_kind("Job"):
    if dig(job, "metadata", "labels", "app.kubernetes.io/component") != "benchmark":
        continue
    pod = dig(job, "spec", "template", "spec", default={})
    check_claims(name(job), pod.get("volumes"))
    for c in pod.get("containers", []) + pod.get("initContainers", []):
        images.append((f"{name(job)} container {c.get('name')!r}", c.get("image")))
    args = [a for c in pod.get("containers", []) for a in c.get("args", [])]
    if not any(a.startswith("--tokenizer=/") for a in args):
        errors.append(f"{name(job)}: no local --tokenizer path (AIPerf would fetch it from Hugging Face)")
    if peer_mode and not any(a == "--header" for a in args):
        errors.append(f"{name(job)}: crossSite is on but the benchmark is not pinned to this site; it would measure the fleet")
    hooks = dig(job, "metadata", "annotations", default={})
    if "helm.sh/hook" not in hooks:
        errors.append(f"{name(job)}: not a hook; it would run before the model is deployed")

# --- global name ---------------------------------------------------------------
mapped = {dig(h, "spec", "virtualhost", "gslb", "fqdn") for h in of_kind("HostRule")}
health_paths = [dig(r, "spec", "rules", 0, "matches", 0, "path", "value") for r in of_kind("HTTPRoute")]
for rule in of_kind("GSLBHostRule"):
    fqdn = dig(rule, "spec", "fqdn")
    if mapped and fqdn not in mapped:
        errors.append(f"{name(rule)}: fqdn {fqdn!r} differs from the HostRule global fqdn {sorted(mapped)}")
    if not dig(rule, "spec", "healthMonitorRefs"):
        warns.append(f"{name(rule)}: no healthMonitorRefs — the default L4 monitor cannot see a site whose model is down")
    elif not health_paths:
        warns.append(f"{name(rule)}: health monitors set but healthRoute is off, so there is no model health path to probe")
serving_sections = set().union(*[section(r) for r in serving_routes]) if serving_routes else set()
for hr in of_kind("HTTPRoute"):
    if peer_mode and not (section(hr) & serving_sections):
        errors.append(f"{name(hr)}: not attached to the peer listener {sorted(serving_sections)}; the fleet's health checks would fail")
for rule in of_kind("GSLBHostRule"):
    if fleet_mode and dig(rule, "spec", "poolAlgorithmSettings", "lbAlgorithm") == "GSLB_ALGORITHM_CONSISTENT_HASH":
        warns.append(f"{name(rule)}: consistent hash with tier 0 — entry concentrates on few sites; use round robin")
    gateway_health = [r for r in of_kind("HTTPRoute")
                      if any(f.get("type") == "ExtensionRef" for rr in dig(r, "spec", "rules", default=[])
                             for f in rr.get("filters", []))]
    if fleet_mode and not gateway_health:
        warns.append(f"{name(rule)}: no gateway health route — the GSLB monitor has no gateway-level path to probe")
for h in of_kind("HostRule"):
    if dig(h, "spec", "virtualhost", "fqdn") == dig(h, "spec", "virtualhost", "gslb", "fqdn"):
        errors.append(f"{name(h)}: local and global fqdn are the same")
for job in of_kind("Job"):
    for c in dig(job, "spec", "template", "spec", "containers", default=[]):
        for a in c.get("args", []):
            if a.startswith("--url=") and any(g and g in a for g in mapped):
                errors.append(f"{name(job)}: benchmark targets the global name; it must be this site's gateway")

# --- images / placeholders -----------------------------------------------------
for owner, image in images:
    if not image:
        errors.append(f"{owner}: no image")
        continue
    tag = image.rsplit("/", 1)[-1].partition(":")[2]
    if "@" not in image and tag in ("", "latest"):
        warns.append(f"{owner}: image {image} is not pinned to a version")

placeholders = sorted(set(re.findall(r"^[^#\n]*?\b(REGISTRY/\S+)", raw, flags=re.M)))
if placeholders:
    notes.append("unreplaced REGISTRY/ placeholders: " + ", ".join(placeholders))

# --- report --------------------------------------------------------------------
for label, items in (("ERROR", errors), ("WARN ", warns), ("NOTE ", notes)):
    for item in items:
        print(f"{label} {item}")
print(f"{'<stdin>' if path == '-' else path}: {'fleet' if fleet_mode else 'model'} render, {len(docs)} documents, nodes {sorted(nodes)}, namespace {sorted(namespaces)} — "
      f"{len(errors)} error(s), {len(warns)} warning(s)")
sys.exit(1 if errors else 0)
