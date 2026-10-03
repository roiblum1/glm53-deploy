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
