---
name: runbook
description: Build the bundle to carry to the disconnected cluster — renders the kserve-llm chart into two staged manifests under dist/ and writes dist/RUNBOOK.md with the exact oc commands in order. Use when the chart is ready to be applied, or when asked for the deploy/verify/smoke-test commands.
---

Serving must not start before the pull Jobs finish syncing the weights, so the bundle has two stages. Nothing here touches a cluster; the output is files for the user to carry across and run.

`$ARGUMENTS`: `[release] [namespace] [recipe values] [site values]`. Defaults: `glm53`, `llm-glm53`, `values/glm53-b200.yaml`; the site file has no default — ask which `values/sites/<cluster>.yaml` to use, and refuse `example.yaml` (it contains placeholders).

This is the manual path. Clusters managed by Argo CD get the same ordering from the chart's sync waves and need no bundle.

## Steps

1. Run the `validate-manifest` checks with the same values files. Stop and report on any error.

2. Render both stages:

   ```bash
   mkdir -p dist
   helm template <release> chart -n <namespace> -f <recipe> -f <site> \
     --no-hooks --set argocd.enabled=false --set serving.enabled=false > dist/01-storage-and-pull.yaml
   helm template <release> chart -n <namespace> -f <recipe> -f <site> \
     --no-hooks --set argocd.enabled=false > dist/02-full.yaml
   ```

   Stage 2 is the full render; applying it after stage 1 leaves the storage and Jobs unchanged and adds serving and the route.

3. Read the two rendered files and write `dist/RUNBOOK.md` from the outline below. Take every name from the render (namespace, nodes, Job names, llmisvc name, model name, images, secret, gateway, host paths), never from memory or from this skill.

4. Tell the user what to carry over: the three files in `dist/`. If Helm is available on the cluster side, the alternative is `helm package chart -d dist` plus the two values files, installed as described in `chart/README.md`.

## RUNBOOK.md outline

Commands only, with one short line of purpose and the expected result per step. Plain `oc` with `-o jsonpath`; do not assume `yq`, `jq` or `helm` exist on the disconnected side.

0. **Context** — `oc whoami --show-server` and `oc get nodes <nodes>`; confirm it is the right cluster.
1. **Pre-reqs** — the checks from "Cluster pre-reqs" in `chart/README.md`: KServe ingress Gateway, the InferencePool / LLMInferenceService / AIGatewayRoute / LeaderWorkerSet CRDs, the serving-image version check.
2. **Namespace and secret** — not in the render. Check they exist; give the create commands (`oc create namespace`, `oc create secret generic <secret> --from-file=s3cfg=<path>`).
3. **Node disk prep** — the `oc debug node/...` command per node, plus a free-space check on the host paths.
4. **Stage 1** — `oc apply -f 01-storage-and-pull.yaml`, then `oc wait --for=condition=complete job -l app.kubernetes.io/component=model-pull` with a long timeout, and an `oc logs -f` line for progress.
5. **Stage 2** — `oc apply -f 02-full.yaml`, then watch the llmisvc until `Ready=True`. Cold start can take up to the startupProbe budget in the render.
6. **Verify** — InferencePool exists, AIGatewayRoute conditions, and the generated Deployment's args / startupProbe / readinessProbe via `-o jsonpath` (confirms the preset merge kept `httpGet` and applied the overrides).
7. **Rate limit and health route** — if rendered: `oc get backendtrafficpolicy` conditions, and `curl` the health path on the site-local gateway.
7b. **Cross-site** — if `<release>-sites` is rendered: check the peer listener and certificates exist (README "Cross-site pooling" pre-reqs), the tier-0 route and both policies are Accepted, the Envoy `/clusters` check from the README, then one pinned `curl` per site (`x-site-pin`).
8. **Smoke test** — `curl` chat-completions with the model name from the render; leave `AGENT_ROUTER_URL` for the user to set.
9. **Tuning** — the `KV cache size|Maximum concurrency` log grep and which values to adjust from it.
10. **Changes and rollback** — a serving change takes one node down per rollout step; a changed pull Job renders under a new name, so delete the old Job by hand when applying with `oc`; PVs are `Retain`, so deleting PVCs leaves PVs `Released` and host data in place.

Nothing in the runbook may need internet access.
