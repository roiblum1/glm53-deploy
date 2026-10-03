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

# --- LLMInferenceService -------------------------------------------------------
FORBIDDEN_FLAGS = {"--model", "--port", "--served-model-name"}
llmisvcs = of_kind("LLMInferenceService")
if len(llmisvcs) != 1:
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

for route in of_kind("AIGatewayRoute"):
    for i, rule in enumerate(dig(route, "spec", "rules", default=[])):
        where = f"{name(route)} rule {i}"
        models = [h.get("value") for m in rule.get("matches", []) for h in m.get("headers", [])
                  if h.get("name") == "x-ai-eg-model"]
        if model_name not in models:
            errors.append(f"{where}: x-ai-eg-model matches {models}, llmisvc model.name is {model_name!r}")
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

# --- rate limit ----------------------------------------------------------------
route_names = {dig(r, "metadata", "name") for r in of_kind("AIGatewayRoute")}
cost_keys = {c.get("metadataKey") for r in of_kind("AIGatewayRoute")
             for c in dig(r, "spec", "llmRequestCosts", default=[])}
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
print(f"{'<stdin>' if path == '-' else path}: {len(docs)} documents, nodes {sorted(nodes)}, namespace {sorted(namespaces)} — "
      f"{len(errors)} error(s), {len(warns)} warning(s)")
sys.exit(1 if errors else 0)
