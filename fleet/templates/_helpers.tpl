{{- define "fleet.labels" -}}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: {{ .Release.Name }}
app.kubernetes.io/component: fleet-router
{{- end -}}

{{/* Usage: include "fleet.annotations" (dict "root" $ "wave" "policy") */}}
{{- define "fleet.annotations" -}}
{{- if .root.Values.argocd.enabled -}}
argocd.argoproj.io/sync-wave: {{ index .root.Values.argocd.syncWaves .wave | toString | quote }}
{{- end -}}
{{- end -}}

{{/* DNS-label form of a model name: glm-5.3 -> glm-5-3. */}}
{{- define "fleet.modelId" -}}
{{- .id | default (regexReplaceAll "[^a-z0-9-]" (lower .name) "-") | trunc 50 | trimSuffix "-" -}}
{{- end -}}

{{/*
Validated fleet settings as YAML (fromYaml it). Fails the render on anything that
would split a model's cluster per site, loop requests back into tier 0, or leave
a model or site ambiguous.
*/}}
{{- define "fleet.config" -}}
{{- $v := .Values -}}
{{- $client := required "route.gateway.sectionName is required: without it the tier-0 routes also attach to the peer listener and peer requests loop back into tier 0" $v.route.gateway.sectionName -}}
{{- $peer := required "crossSite.peerListener is required (the mTLS listener every site's serving routes sit on)" $v.crossSite.peerListener -}}
{{- if eq $client $peer }}
{{- fail "crossSite.peerListener must differ from route.gateway.sectionName: clients would reach the serving routes directly, unmetered" }}
{{- end }}
{{- $hostname := required "crossSite.tls.hostname (or globalName.fqdn) is required: SNI and health-check host sent to every site" ($v.crossSite.tls.hostname | default $v.globalName.fqdn) -}}
{{- if lt (len $v.sites) 1 }}{{ fail "sites is empty" }}{{ end }}
{{- $names := list -}}
{{- range $v.sites }}
{{- $n := required "sites[].name is required" .name }}
{{- if has $n $names }}{{ fail (printf "sites: duplicate name %q" $n) }}{{ end }}
{{- $names = append $names $n }}
{{- if not (regexMatch "^[a-z0-9]([-a-z0-9]*[a-z0-9])?$" $n) }}{{ fail (printf "sites[].name %q must be a DNS label" $n) }}{{ end }}
{{- $addr := required (printf "sites[%s].address is required" $n) .address }}
{{- if regexMatch "^[0-9.]+$|:" $addr }}
{{- fail (printf "sites[%s].address %q must be a hostname, not an IP: mixed address types split a model's cluster per site and least-request, hashing and cross-site retry stop working" $n $addr) }}
{{- end }}
{{- end }}
{{- $ids := list -}}
{{- $models := list -}}
{{- range $v.models }}
{{- $name := required "models[].name is required (the model name clients send)" .name }}
{{- $id := include "fleet.modelId" . }}
{{- if has $id $ids }}{{ fail (printf "models: %q and another model map to the same id %q; set models[].id" $name $id) }}{{ end }}
{{- $ids = append $ids $id }}
{{- $models = append $models (dict "name" $name "id" $id) }}
{{- end }}
{{- $maxRetries := sub (len $v.sites) 1 -}}
{{- $retries := $maxRetries -}}
{{- if not (kindIs "invalid" $v.fleet.retry.numRetries) }}
{{- $retries = int $v.fleet.retry.numRetries }}
{{- if gt $retries $maxRetries }}
{{- fail (printf "fleet.retry.numRetries %d exceeds len(sites)-1 = %d: a retry never goes back to a site already tried" $retries $maxRetries) }}
{{- end }}
{{- end }}
clientListener: {{ $client }}
peerListener: {{ $peer }}
hostname: {{ $hostname }}
numRetries: {{ $retries }}
models: {{ toJson $models }}
{{- end -}}

{{/* The rateLimit block of a BackendTrafficPolicy spec for one model. Usage: include "fleet.rateLimit" (dict "model" $m "limit" $limit "costs" $costs) */}}
{{- define "fleet.rateLimit" -}}
{{- $limit := .limit -}}
{{- $costKeys := list -}}
{{- range .costs }}{{ $costKeys = append $costKeys .metadataKey }}{{ end -}}
{{- if not (has $limit.costKey $costKeys) }}
{{- fail (printf "model %s: rateLimit.costKey %q is not a metadataKey in its llmRequestCosts" .model.name $limit.costKey) }}
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
                value: {{ .model.name }}
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
