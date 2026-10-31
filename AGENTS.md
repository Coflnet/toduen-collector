# Instructions for coding agents

## Working in this repo

- `collector.py`: the collector (single file, stdlib only). `tests/`: `test_collector.py` (unit), `test_chart.py`
  (`helm template` assertions). `deploy/helm/toduen-collector`: the customer chart. `Dockerfile` runs the tests.
- Run tests: `python -m unittest discover -s tests -v` (needs `helm` and PyYAML for the chart tests), then
  `helm lint deploy/helm/toduen-collector --set-string toduen.projectId=e627bc7e-01b6-46ed-9e4d-72ed598e3634 --set-string toduen.apiToken=todc.x`
  and `docker build -t toduen-collector:dev .`.
- Every bug fix gets a regression test. Do not commit credentials.
- Release rule: `version` and `appVersion` in `deploy/helm/toduen-collector/Chart.yaml` must be equal and match the git tag `vX.Y.Z`.
- The Todün API is `https://dash-api.coflnet.com`, the product is `https://dash.coflnet.com`.

## Installing the collector for a user

1. Use the Todün MCP server (`https://dash-api.coflnet.com/api/mcp`) tool `setup_prometheus_collector`, or the
   docs at https://dash.coflnet.com/docs/prometheus-collector.md. It returns a project id and a ready `values.yaml`.
2. Confirm the explicit list of metrics (key, unit, `sum`/`latest`, PromQL returning one series) with the user. Only listed metrics leave the cluster.
3. Never commit the token or print it in logs. Prefer `toduen.existingSecret` or `toduen.tokenFile` (OpenBao/Vault
   agent or CSI) with GitOps; use `toduen.apiToken` only for a one-off `helm install --set`.
4. Install with `helm install toduen-collector oci://ghcr.io/coflnet/charts/toduen-collector --version <X.Y.Z> -f values.yaml`
   and verify with `kubectl logs deploy/...`; the pod is Ready after the first delivery.
