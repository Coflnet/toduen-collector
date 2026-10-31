import datetime as dt
import importlib.util
from pathlib import Path
import tempfile
import unittest
import unittest.mock
import urllib.parse

spec = importlib.util.spec_from_file_location("collector", Path(__file__).parents[1] / "collector.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def vector(value):
    return {"status": "success", "data": {"resultType": "vector", "result": [{"value": [0, str(value)]}]}}


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.token = Path(self.directory.name) / "token"
        self.token.write_text("private-fixture-token")
        self.now = dt.datetime(2026, 10, 25, 15, 0, tzinfo=dt.timezone.utc)
        self.sent = []
        self.age = 0
        self.value = -120
        self.config = {"prometheusUrl": "http://prometheus.monitoring.svc:9090", "todUrl": "https://dash-api.coflnet.com",
                       "projectId": "e627bc7e-01b6-46ed-9e4d-72ed598e3634", "todTokenFile": str(self.token),
                       "metrics": {"tracked_profit": {"query": "private_daily_profit", "timestampQuery": "max(timestamp(private_daily_profit))"}}}

    def transport(self, url, token=None, payload=None):
        if payload is not None:
            self.sent.append((url, token, payload))
            return None
        self.assertIsNone(token, "Tod credential must never reach Prometheus")
        return vector(self.now.timestamp() - self.age if "timestamp" in url else self.value)

    def test_signed_daily_snapshot_and_idempotent_retry_payload(self):
        collector = module.Collector(self.config, self.transport)
        first = collector.collect(self.now)
        second = collector.collect(self.now)
        self.assertEqual(first, second)
        self.assertEqual(first["metrics"], [{"key": "tracked_profit", "value": -120, "unit": "coins", "aggregation": "sum"}])
        self.assertEqual(first["observedAt"], "2026-10-25T15:00:00Z")
        self.assertEqual(self.sent[0][1], "private-fixture-token")
        self.assertNotIn("private-fixture-token", collector.exposition())
        self.assertNotIn("tracked_profit", collector.exposition())

    def test_missing_ambiguous_nonfinite_and_warned_data(self):
        for response in [vector("NaN"), vector("Inf"), {"status": "success", "data": {"resultType": "vector", "result": []}},
                         {**vector(1), "warnings": ["partial"]},
                         {"status": "success", "data": {"resultType": "vector", "result": [{"value": [0, "1"]}] * 2}}]:
            with self.subTest(response=response), self.assertRaises(module.CollectorError):
                module.scalar(response)

    def test_stale_source_never_posts_zero(self):
        self.age = 301
        with self.assertRaisesRegex(module.CollectorError, "stale_source"):
            module.Collector(self.config, self.transport).collect(self.now)
        self.assertEqual(self.sent, [])

    def test_day_is_independent_of_revision_and_credentials_rotate(self):
        self.config["dayOffset"] = 1
        collector = module.Collector(self.config, self.transport)
        payload = collector.collect(self.now)
        self.assertEqual(payload["day"], "2026-10-24")
        self.assertEqual(payload["observedAt"], "2026-10-25T15:00:00Z")
        self.token.write_text("rotated-fixture-token")
        collector.collect(self.now)
        self.assertEqual(self.sent[-1][1], "rotated-fixture-token")

    def test_bounded_period_queue_finalizes_yesterday_with_rate_spacing(self):
        self.config["dayOffsets"] = list(range(8))
        collector = module.Collector(self.config, self.transport)
        collect = collector.collect
        collector.collect = lambda day_offset: collect(self.now, day_offset)
        class Stop:
            def __init__(self):
                self.delays = []
            def wait(self, delay):
                self.delays.append(delay)
                return False
        stop = Stop()
        collector.collect_periods(stop)
        self.assertEqual(stop.delays, [10] * 7)
        self.assertEqual(len(self.sent), 8)
        self.assertEqual(self.sent[1][2]["day"], "2026-10-24")
        self.assertEqual(self.sent[1][2]["observedAt"], "2026-10-25T15:00:00Z")
        self.assertEqual(len({item[2]["day"] for item in self.sent}), 8)

    def test_fractional_users_and_unknown_keys_are_rejected(self):
        self.config["metrics"] = {"total_users": {"query": "count", "timestampQuery": "timestamp(count)"}}
        self.value = 1.5
        with self.assertRaisesRegex(module.CollectorError, "invalid_metric_value"):
            module.Collector(self.config, self.transport).collect(self.now)
        self.config["metrics"] = {"arbitrary": {"query": "count", "timestampQuery": "timestamp(count)"}}
        with self.assertRaises(module.CollectorError):
            module.Collector(self.config, self.transport)

    def test_urls_reject_public_http_credentials_and_redirects(self):
        for url in ["http://public.example", "https://user:secret@example.com", "https://example.com?secret=token"]:
            with self.assertRaises(module.CollectorError):
                module.endpoint(url)
        with self.assertRaisesRegex(module.CollectorError, "redirect_rejected"):
            module.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example")


class GenericMetricTests(unittest.TestCase):
    """Declared custom metrics from the Helm chart's mounted configuration."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.token = Path(self.directory.name) / "token"
        self.token.write_text("write-only-token")
        self.now = dt.datetime(2026, 10, 25, 15, 0, tzinfo=dt.timezone.utc)
        self.sent = []
        self.queries = []
        self.values = {"signups": "12.5", "depth": "7"}
        self.config = {"prometheusUrl": "http://prometheus-operated.monitoring.svc:9090", "todUrl": "https://dash-api.coflnet.com",
                       "projectId": "e627bc7e-01b6-46ed-9e4d-72ed598e3634", "todTokenFile": str(self.token),
                       "metrics": [
                           {"key": "signups", "displayName": "Signups", "unit": "count", "aggregation": "sum", "query": "signups_query[{range}s] {day_start} {day_end} {day}"},
                           {"key": "depth", "displayName": "Depth", "unit": "count", "aggregation": "latest", "query": "depth_query"}]}

    def transport(self, url, token=None, payload=None):
        if payload is not None:
            self.sent.append((url, token, payload))
            return None
        expression = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.queries.append((expression["query"][0], float(expression["time"][0]), token))
        for name, value in self.values.items():
            if expression["query"][0].startswith(name):
                return {} if value is None else vector(value)
        return {"status": "success", "data": {"resultType": "vector", "result": []}}

    def test_declared_metrics_are_sent_with_declared_unit_and_aggregation_at_running_time(self):
        payload = module.Collector(self.config, self.transport).collect(self.now)
        self.assertEqual(payload["metrics"], [{"key": "signups", "value": 12.5, "unit": "count", "aggregation": "sum"},
                                              {"key": "depth", "value": 7, "unit": "count", "aggregation": "latest"}])
        self.assertEqual(payload["day"], "2026-10-25")
        self.assertEqual(payload["observedAt"], "2026-10-25T15:00:00Z")
        self.assertEqual(self.queries[0][0], "signups_query[54000s] 1792886400 1792972800 2026-10-25")
        self.assertEqual(self.sent[0][0], "https://dash-api.coflnet.com/api/collector/observations")

    def test_closed_day_is_evaluated_at_day_end_and_is_reproducible(self):
        collector = module.Collector({**self.config, "dayOffsets": [1]}, self.transport)
        first = collector.collect(self.now, 1)
        again = collector.collect(self.now + dt.timedelta(hours=3), 1)
        self.assertEqual(first["day"], "2026-10-24")
        self.assertEqual(first["observedAt"], "2026-10-25T00:00:00Z")
        self.assertEqual(first, again)
        self.assertEqual(self.queries[0][1], dt.datetime(2026, 10, 25, tzinfo=dt.timezone.utc).timestamp())
        self.assertTrue(self.queries[0][0].startswith("signups_query[86400s]"))

    def test_missing_metric_is_omitted_never_zero_and_others_still_deliver(self):
        self.values["signups"] = None
        collector = module.Collector(self.config, self.transport)
        payload = collector.collect(self.now)
        self.assertEqual([m["key"] for m in payload["metrics"]], ["depth"])
        self.assertEqual(collector.status, "partial")
        self.assertEqual(collector.errors, 1)

    def test_nothing_is_delivered_when_every_metric_is_unavailable(self):
        self.values = {"signups": None, "depth": "NaN"}
        collector = module.Collector(self.config, self.transport)
        with self.assertRaises(module.CollectorError):
            collector.collect(self.now)
        self.assertEqual(self.sent, [])

    def test_optional_source_timestamp_enforces_freshness_when_configured(self):
        self.config["metrics"][1]["timestampQuery"] = "depth_timestamp"
        self.values["depth_timestamp"] = str(self.now.timestamp() - 3600)
        payload = module.Collector(self.config, self.transport).collect(self.now)
        self.assertEqual([m["key"] for m in payload["metrics"]], ["signups"])

    def test_declarations_are_validated_before_anything_is_sent(self):
        good = self.config["metrics"][0]
        for bad in [{**good, "key": "Signups"}, {**good, "key": "1x"}, {**good, "unit": "furlongs"}, {**good, "aggregation": "avg"},
                    {**good, "query": ""}, {**good, "key": "tracked_profit"}]:
            with self.subTest(bad=bad), self.assertRaises(module.CollectorError):
                module.Collector({**self.config, "metrics": [bad]}, self.transport)
        for metrics in [[], [good, good], "signups"]:
            with self.subTest(metrics=metrics), self.assertRaises(module.CollectorError):
                module.Collector({**self.config, "metrics": metrics}, self.transport)

    def test_prometheus_auth_never_uses_the_tod_credential_and_basic_auth_is_supported(self):
        basic = Path(self.directory.name) / "basic"
        basic.write_text("user:p@ss\n")
        collector = module.Collector({**self.config, "prometheusBasicAuthFile": str(basic)}, self.transport)
        collector.collect(self.now)
        self.assertEqual({q[2] for q in self.queries}, {"Basic dXNlcjpwQHNz"})
        self.assertEqual(self.sent[0][1], "write-only-token")

    def test_in_cluster_service_names_are_allowed_over_http_but_public_http_is_not(self):
        for url in ["http://prometheus:9090", "http://prometheus-server.monitoring.svc.cluster.local"]:
            self.assertEqual(module.endpoint(url), url)
        with self.assertRaises(module.CollectorError):
            module.endpoint("http://prometheus.example.com:9090")

    def test_tod_rejection_reason_is_reported(self):
        import io, urllib.error
        error = urllib.error.HTTPError("https://dash-api.coflnet.com", 400, "Bad Request", {}, io.BytesIO(b'{"error":"Metric \'x\' is not declared"}'))
        with unittest.mock.patch("urllib.request.OpenerDirector.open", side_effect=error), self.assertRaisesRegex(module.CollectorError, "http_400: Metric 'x' is not declared"):
            module.request_json("https://dash-api.coflnet.com/api/collector/observations", "t", {"a": 1})


    def test_requests_send_collector_user_agent(self):
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"{}"
        with unittest.mock.patch("urllib.request.OpenerDirector.open", return_value=response) as opened:
            module.request_json("https://dash-api.coflnet.com/api/collector/observations", "t", {"a": 1})
        agent = opened.call_args.args[0].get_header("User-agent")
        self.assertTrue(agent.startswith("toduen-collector/"), agent)


if __name__ == "__main__":
    unittest.main()
