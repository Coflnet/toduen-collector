# Todün collector (self-hosted Prometheus)

Reads the PromQL queries you list from **your** Prometheus and **pushes** the results to Todün over HTTPS.
Nothing in your cluster is exposed: the chart creates no Service, Ingress or inbound port, and Todün never connects to you.
No operator approval of a hostname is needed.

## Install

1. In Todün open **Data sources -> Prometheus (self-hosted)**, name the project and declare the metrics you want
   (key, display name, unit, `sum` or `latest`). Todün shows the project id, a one-time write-only token and a ready
   `values.yaml` for this chart.
2. Install:

   ```sh
   helm install toduen-collector oci://ghcr.io/coflnet/charts/toduen-collector --version 0.1.3 \
     --namespace monitoring \
     --set toduen.projectId=<project id> \
     --set toduen.apiToken=<token> \
     -f metrics.yaml        # the generated `metrics:` list (or edit values.yaml)
   ```

   To keep the token out of Helm values, create a Secret yourself and pass `--set toduen.existingSecret=<name>`
   (key `token`). `helm template`/`install` fails with a clear message if the token, project id or metric list is
   missing or invalid.
3. Check: `kubectl logs deploy/toduen-collector-toduen-collector`; the pod becomes Ready after the first delivery.

## What leaves your cluster

Only the numeric results of the queries under `metrics`, one value per key and UTC day, plus the key, unit and
aggregation. Todün rejects any key you did not declare there and any unit/aggregation that differs from the
declaration. Query text, label values, series and everything else stay in your cluster.

## Values

| Value | Meaning |
|---|---|
| `toduen.url` | Todün API base, HTTPS only (default `https://dash-api.coflnet.com`) |
| `toduen.projectId` | Project id from Todün (required) |
| `toduen.apiToken` / `toduen.existingSecret` / `toduen.tokenFile` | Write-only token: inline (chart creates a Secret), an existing Secret with key `token`, or an absolute path to a token file provided by a Vault/OpenBao agent or CSI volume. Exactly one is required |
| `prometheus.url` | `https://...` or `http://` to an in-cluster Service (`name` or `*.svc[.cluster.local]`) |
| `prometheus.auth.type` / `.existingSecret` | `none`, `bearer` (key `token`) or `basic` (key `basic` = `user:password`) |
| `intervalSeconds` | Run every N seconds, 30 to 3600 (default 900) |
| `dayOffsets` | UTC days re-sent each run: `0` today, `1` yesterday, up to `7` (default `[0, 1]`) |
| `metrics[]` | `key`, `displayName`, `unit`, `aggregation`, `query`, optional `timestampQuery` (see `values.yaml`) |
| `image.repository` / `tag` / `digest` | Collector image; `digest` pins it |
| `networkPolicy.enabled` | Egress-only policy (DNS, Prometheus, Todün on 443); default on |
| `networkPolicy.extraEgress` | Extra egress rules appended verbatim (default `[]`), e.g. for the injector sidecar |
| `serviceAccount.annotations` / `serviceAccount.automountToken` | Annotations (IRSA etc.); mount the service account token (default `false`, injectors need `true`) |
| `podLabels` / `podAnnotations` | Extra pod labels / annotations |
| `extraVolumes` / `extraVolumeMounts` | Extra volumes and mounts on the collector container, e.g. a CSI secrets-store volume |

Query placeholders: `{range}` seconds elapsed in the UTC day (write `[{range}s]`), `{day}`, `{day_start}`, `{day_end}`.
A closed day is evaluated at its end, so `increase(x[{range}s])` yields that day's total. A query that returns no data,
several series or NaN is skipped for that run and never sent as zero; other metrics in the same run are still delivered.
Use `sum(...)`/`max(...)` so the query returns exactly one series.

## Security

Non-root, read-only root filesystem, all capabilities dropped, seccomp `RuntimeDefault`, no service account token.
The token is only in a Secret mounted read-only at `/var/run/secrets/toduen/token` (mode 0400; the pod `fsGroup`
makes it group-readable for the non-root user), never in an environment variable or ConfigMap, and the collector rereads
it each run, so a rotated Secret takes effect without a restart. The service account token is not mounted unless `serviceAccount.automountToken` is true. A plain NetworkPolicy cannot match host names: by default
443 is allowed to public addresses only; restrict `networkPolicy.todun.cidrs` or use your CNI's FQDN policy for more.

## Token from OpenBao / Vault (Agent injector)

Keep the token out of Helm values and Kubernetes Secrets entirely: let the Vault/OpenBao Agent injector write it to a
file and point the collector at it with `toduen.tokenFile`. With `tokenFile` set the chart creates no Secret and mounts
no token volume.

```yaml
toduen:
  projectId: <project id>
  tokenFile: /vault/secrets/toduen
serviceAccount:
  automountToken: true      # the injector authenticates with the pod's service account token (Kubernetes auth)
podAnnotations:
  vault.hashicorp.com/agent-inject: "true"
  vault.hashicorp.com/role: toduen-collector
  vault.hashicorp.com/agent-inject-secret-toduen: kv/data/toduen/collector
  vault.hashicorp.com/agent-inject-template-toduen: |
    {{- with secret "kv/data/toduen/collector" -}}{{ .Data.data.token }}{{- end -}}
  vault.hashicorp.com/agent-inject-perms-toduen: "0400"
networkPolicy:
  extraEgress:              # the injector sidecar must reach your Vault/OpenBao server
    - to:
        - namespaceSelector: {matchLabels: {kubernetes.io/metadata.name: openbao}}
      ports: [{protocol: TCP, port: 8200}]
```

The template writes only the `token` field, so the file contains the bare token. The injector sidecar runs as its own
user; the pod's `readOnlyRootFilesystem`, `runAsNonRoot` (65532) and dropped capabilities still apply to the collector
container and the secrets are written to the injector's in-memory volume. The collector rereads the file each run, so a
rotated secret is picked up without a restart. For a CSI secrets-store volume, declare it in `extraVolumes`, mount it
with `extraVolumeMounts` and set `toduen.tokenFile` to the file inside that mount.

## Token expiry

The token expires (see the date in Todün). Rotate it in Todün and `helm upgrade --reuse-values --set toduen.apiToken=<new>`
(or update your Secret / OpenBao value) before then; after expiry Todün answers 401 and the pod stays not Ready.

## Development

`python -m unittest discover -s ../../../tests -v` (from this directory) includes template assertions when `helm` and PyYAML are installed.
