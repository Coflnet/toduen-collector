# toduen-collector

Pushes selected Prometheus metrics from your cluster to [Todün](https://dash.coflnet.com). It reads the PromQL queries you
list from **your** Prometheus and posts one typed value per metric and UTC day to Todün's
`/api/collector/observations` with a write-only token. Outbound only: no Service, Ingress or inbound port.
Standard-library Python, no dependencies.

## For customers: install

Todün (Data sources -> Prometheus (self-hosted)) generates the project id, a one-time write-only token and a
`metrics.yaml`. Then:

```sh
helm install toduen-collector oci://ghcr.io/coflnet/charts/toduen-collector --version <X.Y.Z> \
  --namespace monitoring \
  --set toduen.projectId=<project id> \
  --set toduen.apiToken=<token> \
  -f metrics.yaml
```

The image `ghcr.io/coflnet/toduen-collector:<X.Y.Z>` is public and the chart pulls it by default (chart X.Y.Z uses
image X.Y.Z). All values, the data that leaves your cluster, token rotation and the network policy are described in
[deploy/helm/toduen-collector/README.md](deploy/helm/toduen-collector/README.md).

## Protocol

- `POST /api/collector/observations` with the project's write-only bearer token and
  `{ "projectId": "...", "day": "2026-10-25", "observedAt": "2026-10-26T00:05:00Z", "metrics": [{ "key": "signups", "value": 12, "unit": "count", "aggregation": "sum" }] }`.
- `day` is the UTC period, `observedAt` the revision time. A metric replaces that day's previous value only when its
  revision is newer, so retries never double count. `sum` values are summed across days, `latest` are gauges.
- Metrics must be declared in Todün first; undeclared keys and differing unit/aggregation are rejected.
- A query that returns no data, several series, NaN, or a stale `timestampQuery` is skipped for that run; it is never
  sent as zero. `/metrics` and `/healthz` on port 9090 expose delivery health only.
- Configuration is a JSON file (`COLLECTOR_CONFIG`, default `/etc/collector/config.json`); the chart generates it.
  `metrics` is a list of `{ key, displayName, unit, aggregation, query, timestampQuery? }`; `{range}`, `{day}`,
  `{day_start}`, `{day_end}` are available in queries. Prometheus auth: `prometheusTokenFile` (bearer) or
  `prometheusBasicAuthFile` (`user:password`).

- History backfill: on start and every `backfillIntervalSeconds` (default 21600) the collector reads
  `GET /api/collector/coverage?projectId=...` and, for every metric whose query uses only `{range}` (no `{day}`,
  `{day_start}`, `{day_end}`, no `timestampQuery`), runs one Prometheus `query_range` (range `86400`, step one day,
  evaluated at UTC midnights) over the missing closed days of the last `backfillDays` (0-400, default 400; 0 disables)
  and posts them newest first, at most 31 days, 868 metric values and about 240 KB per request, one batch per 30 seconds, to `POST /api/collector/observations/batch`. Days
  Prometheus has no data for are not sent. A server without the coverage endpoint (404) disables backfill; 403 or 429 on coverage just waits for the next pass.

## AI agents

Todün's MCP server (`https://dash-api.coflnet.com/api/mcp`, OAuth login) lets a coding agent set this up for you. Its
`setup_prometheus_collector` tool creates the project and returns a ready `values.yaml`.

```sh
claude mcp add --transport http toduen https://dash-api.coflnet.com/api/mcp
codex mcp add toduen --url https://dash-api.coflnet.com/api/mcp && codex mcp login toduen
```

Agent-readable docs: https://dash.coflnet.com/docs/prometheus-collector.md. See also [AGENTS.md](AGENTS.md).

## For developers

Layout:

| Path | Purpose |
|---|---|
| `collector.py` | The collector (single file, stdlib only) |
| `tests/test_collector.py` | Collector unit tests |
| `tests/test_chart.py` | Chart assertions (`helm template`; skipped without helm/PyYAML) and release-metadata checks |
| `Dockerfile` | `test` stage runs the unit tests; the final image copies `collector.py` out of it. Non-root UID/GID 65532, read-only-rootfs compatible, base image pinned by digest |
| `deploy/helm/toduen-collector` | The customer Helm chart |
| `.github/workflows/ci.yml` | PR/main CI |
| `.github/workflows/release.yml` | Tag release: image + chart |

Run the tests (add PyYAML and helm to include the chart tests):

```sh
python -m pip install pyyaml
python -m unittest discover -s tests -v
helm lint deploy/helm/toduen-collector --set-string toduen.projectId=e627bc7e-01b6-46ed-9e4d-72ed598e3634 --set-string toduen.apiToken=todc.x
docker build -t toduen-collector:dev .      # also runs the tests
```

### CI and release

- Pull requests and pushes to `main`: `ci.yml` builds the Dockerfile `test` stage and runs the chart tests and
  `helm lint`. Nothing is published.
- Release: bump `version` and `appVersion` in `deploy/helm/toduen-collector/Chart.yaml` to the same `X.Y.Z`
  (they must be equal; a unit test and the release job enforce it), merge, then tag:

  ```sh
  git tag vX.Y.Z && git push origin vX.Y.Z
  ```

  `release.yml` checks the tag against `Chart.yaml`, runs the tests and `helm lint`, builds the image (tests run in
  the build), scans it with Trivy (fixable HIGH/CRITICAL blocks), pushes `ghcr.io/coflnet/toduen-collector:X.Y.Z`,
  and only then packages and pushes the chart to `oci://ghcr.io/coflnet/charts/toduen-collector` version `X.Y.Z`.
  The chart's `image.tag` defaults to its `appVersion`, so chart X.Y.Z always pulls image X.Y.Z. Published versions
  are not overwritten; fix forward with a new patch version.

## License

Apache-2.0, see [LICENSE](LICENSE).
