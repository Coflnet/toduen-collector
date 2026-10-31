{{- define "toduen-collector.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}{{ .Release.Name | trunc 63 | trimSuffix "-" }}{{- else -}}{{ printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" }}{{- end -}}
{{- end -}}

{{- define "toduen-collector.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | quote }}
{{- end -}}

{{- define "toduen-collector.selector" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "toduen-collector.tokenSecret" -}}
{{- if .Values.toduen.existingSecret -}}{{ .Values.toduen.existingSecret }}{{- else -}}{{ include "toduen-collector.fullname" . }}-token{{- end -}}
{{- end -}}

{{/* Path of the token file inside the container. */}}
{{- define "toduen-collector.tokenPath" -}}
{{- if .Values.toduen.tokenFile -}}{{ .Values.toduen.tokenFile }}{{- else -}}/var/run/secrets/toduen/token{{- end -}}
{{- end -}}

{{/* Validates all user input once, with messages that say what to set. */}}
{{- define "toduen-collector.validate" -}}
{{- if not (regexMatch "^[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}$" (.Values.toduen.projectId | toString)) -}}
{{- fail "toduen.projectId must be the project id (a UUID) shown in Todün under Data sources -> Prometheus (self-hosted)." -}}
{{- end -}}
{{- $sources := 0 -}}
{{- if .Values.toduen.apiToken }}{{ $sources = add1 $sources }}{{ end -}}
{{- if .Values.toduen.existingSecret }}{{ $sources = add1 $sources }}{{ end -}}
{{- if .Values.toduen.tokenFile }}{{ $sources = add1 $sources }}{{ end -}}
{{- if gt $sources 1 -}}
{{- fail "Set exactly one of toduen.apiToken, toduen.existingSecret or toduen.tokenFile, not several." -}}
{{- end -}}
{{- if eq $sources 0 -}}
{{- fail "Set one of toduen.apiToken (the write-only token from Todün), toduen.existingSecret (a Secret with key `token`) or toduen.tokenFile (an absolute path to a token file provided by a Vault/OpenBao agent or CSI volume)." -}}
{{- end -}}
{{- if and .Values.toduen.tokenFile (not (hasPrefix "/" (.Values.toduen.tokenFile | toString))) -}}
{{- fail "toduen.tokenFile must be an absolute path (for example /vault/secrets/toduen)." -}}
{{- end -}}
{{- if not (hasPrefix "https://" .Values.toduen.url) -}}
{{- fail "toduen.url must start with https://" -}}
{{- end -}}
{{- if not .Values.metrics -}}
{{- fail "metrics is empty: list at least one metric (key, unit, aggregation, query). Only listed metrics are read and sent to Todün." -}}
{{- end -}}
{{- $seen := dict -}}
{{- range $index, $metric := .Values.metrics -}}
{{- if or (not (kindIs "string" $metric.key)) (not (regexMatch "^[a-z][a-z0-9_]{0,62}$" $metric.key)) -}}
{{- fail (printf "metrics[%d].key must match ^[a-z][a-z0-9_]{0,62}$" $index) -}}
{{- end -}}
{{- if hasKey $seen $metric.key -}}{{- fail (printf "metrics: duplicate key %q" $metric.key) -}}{{- end -}}
{{- $_ := set $seen $metric.key true -}}
{{- if not (has $metric.unit (list "count" "users" "requests" "eur" "usd" "gbp" "coins" "seconds" "ms" "bytes" "percent")) -}}
{{- fail (printf "metrics[%d] (%s): unit must be one of count, users, requests, eur, usd, gbp, coins, seconds, ms, bytes, percent" $index $metric.key) -}}
{{- end -}}
{{- if not (has $metric.aggregation (list "sum" "latest")) -}}
{{- fail (printf "metrics[%d] (%s): aggregation must be sum or latest" $index $metric.key) -}}
{{- end -}}
{{- if not $metric.query -}}{{- fail (printf "metrics[%d] (%s): query is required" $index $metric.key) -}}{{- end -}}
{{- end -}}
{{- if not (has .Values.prometheus.auth.type (list "none" "bearer" "basic")) -}}
{{- fail "prometheus.auth.type must be none, bearer or basic." -}}
{{- end -}}
{{- if and (ne .Values.prometheus.auth.type "none") (not .Values.prometheus.auth.existingSecret) -}}
{{- fail "prometheus.auth.existingSecret is required when prometheus.auth.type is bearer or basic." -}}
{{- end -}}
{{- end -}}
