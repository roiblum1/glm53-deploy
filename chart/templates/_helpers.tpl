{{- define "kserve-llm.fullname" -}}
{{- default .Release.Name .Values.fullnameOverride | trunc 40 | trimSuffix "-" -}}
{{- end -}}

{{/*
No app.kubernetes.io/name or instance here: KServe sets app.kubernetes.io/name
on the workloads it generates from the LLMInferenceService.
*/}}
{{- define "kserve-llm.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: {{ include "kserve-llm.fullname" . }}
{{- end -}}

{{/*
Usage: include "kserve-llm.annotations" (dict "root" $ "wave" "storage" "keep" true)
keep: the resource holds data that must survive uninstall / app deletion / pruning.
*/}}
{{- define "kserve-llm.annotations" -}}
{{- $annotations := dict -}}
{{- $keep := and .keep .root.Values.storage.keepOnUninstall -}}
{{- if $keep -}}
{{- $_ := set $annotations "helm.sh/resource-policy" "keep" -}}
{{- end -}}
{{- if .root.Values.argocd.enabled -}}
{{- $_ := set $annotations "argocd.argoproj.io/sync-wave" (toString (index .root.Values.argocd.syncWaves .wave)) -}}
{{- if $keep -}}
{{- $_ := set $annotations "argocd.argoproj.io/sync-options" "Delete=false,Prune=false" -}}
{{- end -}}
{{- end -}}
{{- if $annotations -}}
{{- toYaml $annotations -}}
{{- end -}}
{{- end -}}

{{/* Usage: include "kserve-llm.image" (dict "root" $ "image" .Values.x.image "what" "x.image") */}}
{{- define "kserve-llm.image" -}}
{{- $tag := required (printf "%s.tag is required (pin a version)" .what) .image.tag -}}
{{- $registry := trimSuffix "/" (.root.Values.global.imageRegistry | default "") -}}
{{- if $registry -}}
{{- printf "%s/%s:%s" $registry .image.repository $tag -}}
{{- else -}}
{{- printf "%s:%s" .image.repository $tag -}}
{{- end -}}
{{- end -}}

{{- define "kserve-llm.benchmarkEnv" -}}
{{- with .Values.benchmark.apiKeySecret }}
{{- if .name }}
- name: API_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .name }}
      key: {{ .key }}
{{- end }}
{{- end }}
{{- range $name, $value := .Values.benchmark.env }}
- name: {{ $name }}
  value: {{ $value | quote }}
{{- end }}
{{- end -}}

{{/* Fails on anything that would not survive the preset's `bash -c ... eval`. */}}
{{- define "kserve-llm.checkArg" -}}
{{- if regexMatch "[\\s{}\\[\\]\"']" . -}}
{{- fail (printf "serving arg %q must be a single plain token: no whitespace, quotes, braces or brackets (use --flag=value and dotted keys)" .) -}}
{{- end -}}
{{- if has (. | trimPrefix "--" | splitList "=" | first) (list "model" "port" "served-model-name") -}}
{{- fail (printf "serving arg %q is already set by the kserve-config-llm-template preset" .) -}}
{{- end -}}
{{- end -}}

{{- define "kserve-llm.servingArgs" -}}
{{- range $flag, $value := .Values.serving.args }}
{{- if kindIs "bool" $value }}
{{- if $value }}
{{- $arg := printf "--%s" $flag }}
{{- include "kserve-llm.checkArg" $arg }}
- {{ $arg }}
{{- end }}
{{- else if not (kindIs "invalid" $value) }}
{{- $arg := printf "--%s=%s" $flag (toString $value) }}
{{- include "kserve-llm.checkArg" $arg }}
- {{ $arg }}
{{- end }}
{{- end }}
{{- range .Values.serving.extraArgs }}
{{- if not (hasPrefix "--" .) }}
{{- fail (printf "serving.extraArgs entry %q must be --flag or --flag=value" .) }}
{{- end }}
{{- include "kserve-llm.checkArg" . }}
- {{ . }}
{{- end }}
{{- end -}}

{{/*
The rateLimit block of a BackendTrafficPolicy spec. Used by ratelimit.yaml (single
site) and by the tier-0 policy in crosssite.yaml, which owns metering when
crossSite is on.
*/}}
{{- define "kserve-llm.rateLimit" -}}
{{- $limit := .Values.rateLimit -}}
{{- $costKeys := list -}}
{{- range .Values.route.llmRequestCosts }}{{ $costKeys = append $costKeys .metadataKey }}{{ end -}}
{{- if not (has $limit.costKey $costKeys) }}
{{- fail (printf "rateLimit.costKey %q is not a metadataKey in route.llmRequestCosts" $limit.costKey) }}
{{- end }}
rateLimit:
  type: Global
  global:
    rules:
      - clientSelectors:
          - headers:
              - name: {{ required "rateLimit.clientHeader is required" $limit.clientHeader }}
                type: Distinct
              - name: x-ai-eg-model
                type: Exact
                value: {{ .Values.model.name }}
        limit:
          requests: {{ int64 $limit.tokens }}
          unit: {{ $limit.unit }}
        cost:
          request:
            from: Number
            number: 0
          response:
            from: Metadata
            metadata:
              namespace: io.envoy.ai_gateway
              key: {{ $limit.costKey }}
      {{- with $limit.extraRules }}
      {{- toYaml . | nindent 6 }}
      {{- end }}
{{- end -}}

{{/*
Validated crossSite settings as YAML (fromYaml it). crossSite puts this release's
serving route on the peer listener, where the fleet release's tier 0 (fleet/)
reaches it from every site. Fails the render on anything that would loop
requests back into tier 0 or expose the unmetered serving route to clients.
*/}}
{{- define "kserve-llm.crossSite" -}}
{{- $cs := .Values.crossSite -}}
{{- $self := required "crossSite.self is required (this site's name in values/fleet.yaml sites; the benchmark pins itself to it)" $cs.self -}}
{{- $peer := required "crossSite.peerListener is required (the mTLS listener the fleet's tier 0 connects to)" $cs.peerListener -}}
{{- $client := required "route.gateway.sectionName is required with crossSite: it is the fleet's client listener, which must not carry the serving route" .Values.route.gateway.sectionName -}}
{{- if eq $peer $client }}
{{- fail "crossSite.peerListener must differ from route.gateway.sectionName: clients would reach the serving route directly, unmetered" }}
{{- end }}
{{- if not .Values.healthRoute.enabled }}
{{- fail "crossSite needs healthRoute.enabled: the fleet's tier 0 health-checks every site on that path to decide where this model is served" }}
{{- end }}
{{- $perNode := int (required "crossSite.serving.requestsPerNode is required (recipe: in-flight requests one node absorbs before this site sheds)" $cs.serving.requestsPerNode) -}}
{{- $replicas := int (required "crossSite.serving.gatewayReplicas is required (Envoy replicas of this site's gateway; circuit breakers count per replica)" $cs.serving.gatewayReplicas) -}}
{{- $total := mul $perNode (len .Values.nodes) -}}
self: {{ $self }}
peerListener: {{ $peer }}
servingMaxParallel: {{ div (add $total (sub $replicas 1)) $replicas }}
{{- end -}}
