"""Template assertions for the Helm chart; skipped when helm is not installed or the chart is not part of the build context."""
import importlib.util
import json
import shutil
import subprocess
import unittest
from pathlib import Path

CHART = Path(__file__).parents[1] / "deploy" / "helm" / "toduen-collector"
PROJECT = "e627bc7e-01b6-46ed-9e4d-72ed598e3634"

spec = importlib.util.spec_from_file_location("collector", Path(__file__).parents[1] / "collector.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def merge(base, extra):
    result = dict(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        elif value is None:
            result.pop(key, None)
        else:
            result[key] = value
    return result


def render(values=None, namespace="shop"):
    """Values are a nested dict layered over a valid base; None removes a key. Rendered through a JSON values file."""
    import tempfile
    merged = merge({"toduen": {"projectId": PROJECT, "apiToken": "todc.fixture-secret-token"}}, values or {})
    with tempfile.NamedTemporaryFile("w", suffix=".json") as handle:
        json.dump(merged, handle)
        handle.flush()
        return subprocess.run(["helm", "template", "rel", str(CHART), "-n", namespace, "-f", handle.name], capture_output=True, text=True)


def documents(output):
    import yaml
    return {(d["kind"]): d for d in yaml.safe_load_all(output) if d}


@unittest.skipUnless(shutil.which("helm") and CHART.is_dir(), "helm or chart not available")
class ChartTests(unittest.TestCase):
    def setUp(self):
        try:
            import yaml  # noqa: F401
        except ImportError:
            self.skipTest("PyYAML not installed")

    def render_ok(self, values=None, **kwargs):
        result = render(values, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_lint_passes(self):
        result = subprocess.run(["helm", "lint", str(CHART), "--set-string", f"toduen.projectId={PROJECT}", "--set-string", "toduen.apiToken=todc.x"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_hardened_pod_and_token_only_in_secret_file(self):
        output = self.render_ok()
        docs = documents(output)
        pod = docs["Deployment"]["spec"]["template"]["spec"]
        container = pod["containers"][0]
        self.assertIs(pod["automountServiceAccountToken"], False)
        self.assertTrue(pod["securityContext"]["runAsNonRoot"])
        self.assertEqual(pod["securityContext"]["seccompProfile"]["type"], "RuntimeDefault")
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertIs(container["securityContext"]["allowPrivilegeEscalation"], False)
        self.assertEqual(container["securityContext"]["capabilities"]["drop"], ["ALL"])
        self.assertTrue(container["resources"]["requests"] and container["resources"]["limits"])
        secret_volume = next(v for v in pod["volumes"] if v["name"] == "toduen-token")["secret"]
        self.assertEqual(secret_volume["defaultMode"], 0o400)
        self.assertTrue(all(m["readOnly"] for m in container["volumeMounts"]))
        self.assertEqual(docs["Secret"]["stringData"]["token"], "todc.fixture-secret-token")
        # The credential must appear nowhere else: not in env, ConfigMap or the pod spec.
        self.assertNotIn("fixture-secret-token", json.dumps(docs["ConfigMap"]) + json.dumps(docs["Deployment"]))
        self.assertEqual(output.count("fixture-secret-token"), 1)
        self.assertNotIn("Service", docs)
        self.assertNotIn("Ingress", docs)

    def test_generated_config_is_accepted_by_the_collector(self):
        config = json.loads(documents(self.render_ok())["ConfigMap"]["data"]["config.json"])
        self.assertEqual(config["projectId"], PROJECT)
        self.assertEqual(config["metrics"][0]["key"], "signups")
        config["todTokenFile"] = __file__  # exists; only structural validation is asserted
        collector = module.Collector(config)
        self.assertEqual(collector.metrics[0]["unit"], "count")
        self.assertFalse(collector.legacy)

    def test_backfill_settings_render_as_integers(self):
        config = json.loads(documents(self.render_ok({"backfillDays": 30, "backfillIntervalSeconds": 3600}))["ConfigMap"]["data"]["config.json"])
        self.assertIs(type(config["backfillDays"]), int)
        self.assertIs(type(config["backfillIntervalSeconds"]), int)
        self.assertEqual((config["backfillDays"], config["backfillIntervalSeconds"]), (30, 3600))
        defaults = json.loads(documents(self.render_ok())["ConfigMap"]["data"]["config.json"])
        self.assertEqual((defaults["backfillDays"], defaults["backfillIntervalSeconds"]), (400, 21600))

    def test_existing_secret_renders_no_secret(self):
        docs = documents(self.render_ok({"toduen": {"apiToken": None, "existingSecret": "mine"}}))
        self.assertNotIn("Secret", docs)
        volume = next(v for v in docs["Deployment"]["spec"]["template"]["spec"]["volumes"] if v["name"] == "toduen-token")
        self.assertEqual(volume["secret"]["secretName"], "mine")

    def test_clear_failures(self):
        metric = {"key": "signups", "unit": "count", "aggregation": "sum", "query": "up"}
        cases = [
            ({"toduen": {"apiToken": None}}, "toduen.apiToken"),
            ({"toduen": {"apiToken": None, "existingSecret": "x", "projectId": None}}, "toduen.projectId"),
            ({"toduen": {"existingSecret": "x"}}, "exactly one"),
            ({"toduen": {"projectId": "nope"}}, "toduen.projectId"),
            ({"toduen": {"url": "http://dash-api.coflnet.com"}}, "https://"),
            ({"toduen": {"apiToken": None, "tokenFile": "/vault/secrets/toduen", "existingSecret": "x"}}, "exactly one"),
            ({"toduen": {"tokenFile": "/vault/secrets/toduen"}}, "exactly one"),
            ({"toduen": {"apiToken": None, "tokenFile": "vault/secrets/toduen"}}, "absolute path"),
            ({"metrics": []}, "metrics is empty"),
            ({"metrics": [{**metric, "unit": "furlongs"}]}, "unit must be one of"),
            ({"metrics": [{**metric, "aggregation": "avg"}]}, "aggregation must be"),
            ({"metrics": [{**metric, "key": "Bad-Key"}]}, "key must match"),
            ({"metrics": [{**metric, "query": ""}]}, "query is required"),
            ({"metrics": [metric, metric]}, "duplicate key"),
            ({"prometheus": {"auth": {"type": "basic"}}}, "existingSecret is required"),
        ]
        for values, message in cases:
            with self.subTest(values=values):
                result = render(values)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)

    def test_network_policy_is_egress_only_and_can_be_disabled(self):
        policy = documents(self.render_ok())["NetworkPolicy"]["spec"]
        self.assertEqual(policy["policyTypes"], ["Egress"])
        ports = [[p["port"] for p in rule["ports"]] for rule in policy["egress"]]
        self.assertEqual(ports, [[53, 53], [9090], [443]])
        self.assertEqual(policy["egress"][1]["to"][0]["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"], "monitoring")
        self.assertNotIn("NetworkPolicy", documents(self.render_ok({"networkPolicy": {"enabled": False}})))

    def test_prometheus_namespace_and_port_follow_the_url(self):
        policy = documents(self.render_ok({"prometheus": {"url": "http://prom.obs.svc.cluster.local"}}))["NetworkPolicy"]["spec"]
        self.assertEqual(policy["egress"][1]["ports"][0]["port"], 80)
        self.assertEqual(policy["egress"][1]["to"][0]["namespaceSelector"]["matchLabels"]["kubernetes.io/metadata.name"], "obs")

    def test_prometheus_auth_secret_is_mounted_read_only_and_referenced_by_file(self):
        docs = documents(self.render_ok({"prometheus": {"auth": {"type": "basic", "existingSecret": "prom-auth"}}}))
        config = json.loads(docs["ConfigMap"]["data"]["config.json"])
        self.assertEqual(config["prometheusBasicAuthFile"], "/var/run/secrets/prometheus/basic")
        mounts = docs["Deployment"]["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
        self.assertTrue(next(m for m in mounts if m["name"] == "prometheus-auth")["readOnly"])
        self.assertNotIn("prom-auth", docs["ConfigMap"]["data"]["config.json"])

    def test_default_todun_url_is_the_api_host(self):
        # Regression: the default was https://toduen.com, which has no DNS, so deliveries silently failed.
        config = json.loads(documents(self.render_ok())["ConfigMap"]["data"]["config.json"])
        self.assertEqual(config["todUrl"], "https://dash-api.coflnet.com")

    def test_token_file_renders_no_secret_or_token_volume(self):
        docs = documents(self.render_ok({"toduen": {"apiToken": None, "tokenFile": "/vault/secrets/toduen"}}))
        self.assertNotIn("Secret", docs)
        config = json.loads(docs["ConfigMap"]["data"]["config.json"])
        self.assertEqual(config["todTokenFile"], "/vault/secrets/toduen")
        pod = docs["Deployment"]["spec"]["template"]["spec"]
        self.assertNotIn("toduen-token", [v["name"] for v in pod.get("volumes", [])])
        self.assertNotIn("toduen-token", [m["name"] for m in pod["containers"][0]["volumeMounts"]])

    def test_default_token_path_unchanged(self):
        config = json.loads(documents(self.render_ok())["ConfigMap"]["data"]["config.json"])
        self.assertEqual(config["todTokenFile"], "/var/run/secrets/toduen/token")

    def test_extra_volumes_mounts_labels_and_annotations(self):
        docs = documents(self.render_ok({
            "toduen": {"apiToken": None, "tokenFile": "/mnt/secrets/token"},
            "extraVolumes": [{"name": "csi", "csi": {"driver": "secrets-store.csi.k8s.io", "readOnly": True}}],
            "extraVolumeMounts": [{"name": "csi", "mountPath": "/mnt/secrets", "readOnly": True}],
            "podLabels": {"team": "shop"},
            "podAnnotations": {"vault.hashicorp.com/agent-inject": "true"},
            "serviceAccount": {"annotations": {"eks.amazonaws.com/role-arn": "arn:x"}}}))
        template = docs["Deployment"]["spec"]["template"]
        self.assertIn({"name": "csi", "csi": {"driver": "secrets-store.csi.k8s.io", "readOnly": True}}, template["spec"]["volumes"])
        self.assertIn({"name": "csi", "mountPath": "/mnt/secrets", "readOnly": True}, template["spec"]["containers"][0]["volumeMounts"])
        self.assertEqual(template["metadata"]["labels"]["team"], "shop")
        self.assertEqual(template["metadata"]["labels"]["app.kubernetes.io/name"], "toduen-collector")
        self.assertEqual(template["metadata"]["annotations"]["vault.hashicorp.com/agent-inject"], "true")
        self.assertIn("checksum/config", template["metadata"]["annotations"])
        self.assertEqual(docs["ServiceAccount"]["metadata"]["annotations"]["eks.amazonaws.com/role-arn"], "arn:x")

    def test_automount_token_defaults_false_and_can_be_enabled(self):
        docs = documents(self.render_ok())
        self.assertIs(docs["Deployment"]["spec"]["template"]["spec"]["automountServiceAccountToken"], False)
        self.assertIs(docs["ServiceAccount"]["automountServiceAccountToken"], False)
        docs = documents(self.render_ok({"serviceAccount": {"automountToken": True}}))
        self.assertIs(docs["Deployment"]["spec"]["template"]["spec"]["automountServiceAccountToken"], True)
        self.assertIs(docs["ServiceAccount"]["automountServiceAccountToken"], True)

    def test_network_policy_extra_egress_is_appended(self):
        rule = {"to": [{"ipBlock": {"cidr": "10.1.2.3/32"}}], "ports": [{"protocol": "TCP", "port": 8200}]}
        policy = documents(self.render_ok({"networkPolicy": {"extraEgress": [rule]}}))["NetworkPolicy"]["spec"]
        self.assertEqual(len(policy["egress"]), 4)
        self.assertEqual(policy["egress"][-1], rule)

    def test_image_digest_overrides_tag(self):
        docs = documents(self.render_ok({"image": {"digest": "sha256:" + "a" * 64}}))
        self.assertTrue(docs["Deployment"]["spec"]["template"]["spec"]["containers"][0]["image"].endswith("@sha256:" + "a" * 64))


@unittest.skipUnless(CHART.is_dir(), "chart not part of the build context")
class ReleaseMetadataTests(unittest.TestCase):
    """The release scheme (tag vX.Y.Z = image X.Y.Z = chart X.Y.Z) relies on these staying consistent. No helm needed."""

    def chart_field(self, name):
        import re
        match = re.search(rf'^{name}: *"?([^"\n]+?)"? *$', (CHART / "Chart.yaml").read_text(), re.M)
        self.assertIsNotNone(match, name)
        return match.group(1)

    def test_chart_version_equals_app_version_and_is_semver(self):
        import re
        self.assertEqual(self.chart_field("version"), self.chart_field("appVersion"))
        self.assertRegex(self.chart_field("version"), r"^\d+\.\d+\.\d+$")

    def test_default_image_is_the_public_ghcr_image_with_no_pinned_tag(self):
        values = (CHART / "values.yaml").read_text()
        self.assertIn("repository: ghcr.io/coflnet/toduen-collector\n", values)
        self.assertIn('tag: ""', values)  # falls back to appVersion

    def test_dockerfile_user_matches_chart_security_context(self):
        dockerfile_path = Path(__file__).parents[1] / "Dockerfile"
        if not dockerfile_path.exists():
            self.skipTest("Dockerfile not in build context")
        dockerfile = dockerfile_path.read_text()
        deployment = (CHART / "templates" / "deployment.yaml").read_text()
        self.assertIn("USER 65532:65532", dockerfile)
        self.assertIn("runAsUser: 65532", deployment)
        self.assertIn("runAsGroup: 65532", deployment)


    def test_runtime_image_drops_pip(self):
        # Regression: pip's vendored urllib3/msgpack failed the v0.1.0 release scan although the collector never uses pip.
        dockerfile_path = Path(__file__).parents[1] / "Dockerfile"
        if not dockerfile_path.exists():
            self.skipTest("Dockerfile not in build context")
        runtime_stage = dockerfile_path.read_text().rsplit("\nFROM ", 1)[1]
        self.assertIn("pip uninstall -y pip", runtime_stage)
        self.assertNotIn("pip install", runtime_stage)

if __name__ == "__main__":
    unittest.main()
