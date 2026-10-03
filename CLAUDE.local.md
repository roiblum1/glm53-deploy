# CLAUDE.local.md

Personal instructions for this project (not shared).

## Me

- Platform/DevOps/SRE and ML/AI engineer. I wrote the original manifest and know KServe, vLLM, Envoy Gateway and OpenShift well — skip walkthroughs and background.
- Be terse. When a change involves a choice, state the tradeoff and the alternative in a line or two.

## Disconnected network

The target cluster is air-gapped and unreachable from this Mac. Manifests are authored here and applied on the other side.

- Never run `oc`/`kubectl` against a cluster from this machine. The local kube contexts are unrelated sandbox clusters. Give me the commands to run instead (`/runbook` builds the staged sequence).
- Cluster state is unknown to you. Don't assume installed versions, CRDs, node names or current resource state — ask, or tell me what to check.
- Nothing proposed may need internet at deploy or run time: images come from the internal registry (`REGISTRY/...`), weights from internal S3, no Hugging Face or public registry pulls, no `curl | bash` installs.
- Tools on the disconnected side are limited; prefer plain `oc` with `-o jsonpath` over `yq`/`jq` in commands meant to run there.

## Working on the chart

- `chart/` is meant as a reference for KServe `LLMInferenceService` + Envoy AI Gateway deployments, so keep it generic: model settings go in `values/<model>-<hardware>.yaml`, per-cluster settings in `values/sites/<cluster>.yaml`, never in `chart/values.yaml` or the templates.
- `chart/README.md` ("Design decisions", "Cluster pre-reqs") and the comments in `chart/values.yaml` are the spec. Read them before changing behaviour and update them with the change.
- The KServe and Agent Router API fields in the templates come from my running cluster's versions. Don't add or rename fields on those CRDs from memory; ask me to confirm against the cluster.
- All `helm` commands need both values files: `-f values/glm53-b200.yaml -f values/sites/<cluster>.yaml`.
- Delivery is Argo CD: my own ApplicationSet points at `chart/`. Don't write Application, ApplicationSet or other Argo CD resources; the chart only carries sync-wave and hook annotations (`argocd.enabled`).
- Run `/validate-manifest` after editing. `deploy.yaml` is the pre-chart original, kept only for comparison.
