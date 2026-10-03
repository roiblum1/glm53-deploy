---
name: validate-manifest
description: Offline check of the kserve-llm chart — helm lint, render, then consistency checks on the rendered manifest (KServe preset arg rules, node/namespace/PVC cross-references, rollout strategy, route-to-InferencePool wiring). Use after editing anything under chart/ or values/, or before handing a render over for apply on the disconnected cluster.
---

Needs no cluster access. Run from the project root.

`$ARGUMENTS` may name the values files to use (space-separated). Default: `values/glm53-b200.yaml` combined with each file in `values/sites/` in turn.

```bash
helm lint chart -f <recipe> -f <site>
helm template check chart -n check -f <recipe> -f <site> \
  | python3 .claude/skills/validate-manifest/validate.py -
```

Also render once with `--set serving.enabled=false` and confirm it contains no `LLMInferenceService` or `AIGatewayRoute` (stage 1 of the install).

Report tersely:

- A `helm lint` / `helm template` failure — schema violation or one of the chart's own `fail` guards. Quote the message and the value that caused it.
- `ERROR` from `validate.py` — the render breaks a rule from `chart/README.md`. Say which rule and propose the fix; do not apply it without being asked.
- `WARN` — legal but risky (unpinned image tag, non-`pvc://` model URI).
- `NOTE` — things to confirm on the cluster side (`REGISTRY/` placeholders, cross-namespace `allowedRoutes`).

Not covered by the script; review by reading when the relevant values changed:

- The rate-limit client header is one the gateway sets from the verified credential, not one a client can send.
- `benchmark.url` is the site-local gateway, not the global name.
- New vLLM flags exist in the vLLM version of the serving image tag.
- `max-num-seqs` and `max-num-batched-tokens` are per DP rank, not per pod.
- `resources.limits.memory` fits node RAM (page cache counts against it).
- The KServe HTTPRoute and the AIGatewayRoute are not on the same Gateway listener.

When a rule is added to the chart or its README, add the matching check to `validate.py`.
