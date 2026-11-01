import datetime as dt
import importlib.util
from pathlib import Path
import json
import tempfile
import time
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


DAY = 86400


class BackfillTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.token = Path(self.directory.name) / "token"
        self.token.write_text("write-only-token")
        self.now = dt.datetime(2026, 11, 1, 10, 0, tzinfo=dt.timezone.utc)
        self.today = self.now.date()
        self.gets, self.posts, self.ranges = [], [], []
        self.coverage = {"retentionDays": 400, "metrics": {}}
        self.coverage_error = None
        self.post_errors = []
        self.clock = [1000.0]
        patcher = unittest.mock.patch.object(module.time, "monotonic", lambda: self.clock[0])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.range_points = lambda start, end: [[t, "5"] for t in range(start, end + 1, DAY)]
        self.config = {"prometheusUrl": "http://prometheus-operated.monitoring.svc:9090", "todUrl": "https://dash-api.coflnet.com",
                       "projectId": "e627bc7e-01b6-46ed-9e4d-72ed598e3634", "todTokenFile": str(self.token), "backfillDays": 5,
                       "metrics": [
                           {"key": "flips", "unit": "count", "aggregation": "sum", "query": "sum(increase(flips[{range}s]))"},
                           {"key": "depth", "unit": "count", "aggregation": "latest", "query": "depth_query[{range}s]"},
                           {"key": "daily", "unit": "count", "aggregation": "sum", "query": "x{day}"},
                           {"key": "fresh", "unit": "count", "aggregation": "sum", "query": "f[{range}s]", "timestampQuery": "ts"}]}

    def transport(self, url, token=None, payload=None, timeout=20):
        parts = urllib.parse.urlsplit(url)
        if parts.path == "/api/collector/coverage":
            self.gets.append(url)
            if self.coverage_error:
                raise self.coverage_error
            return self.coverage
        if parts.path == "/api/collector/observations/batch":
            if self.post_errors:
                raise self.post_errors.pop(0)
            self.posts.append(payload)
            return None
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
        self.assertEqual(parts.path, "/api/v1/query_range")
        self.assertEqual(timeout, 60)
        self.ranges.append(query)
        values = self.range_points(int(query["start"]), int(query["end"]))
        return {"status": "success", "data": {"resultType": "matrix", "result": [{"metric": {}, "values": values}] if values else []}}

    def collector(self, **extra):
        return module.Collector({**self.config, **extra}, self.transport)

    def run_backfill(self, collector, stop_after=False, **kw):
        clock = self.clock
        class Stop:
            def __init__(self):
                self.delays = []
                self.stopped = False
            def is_set(self):
                return self.stopped
            def wait(self, delay):
                self.delays.append(delay)
                clock[0] += delay
                return self.stopped
        stop = Stop()
        stop.stopped = stop_after
        collector.backfill(stop, now=self.now, **kw)
        return stop

    def midnight(self, day):
        return int(dt.datetime.combine(day, dt.time(), dt.timezone.utc).timestamp())

    def test_only_range_only_metrics_without_timestamp_are_eligible_and_reasons_logged_once(self):
        collector = self.collector()
        eligible, skipped = collector.eligible_metrics()
        self.assertEqual([m["key"] for m in eligible], ["flips", "depth"])
        self.assertEqual(set(skipped), {"daily", "fresh"})
        with unittest.mock.patch("builtins.print") as printed:
            collector.log_ineligible(skipped)
            collector.log_ineligible(skipped)
        self.assertEqual(printed.call_count, 2)

    def test_range_query_uses_day_range_step_and_midnight_timestamps_for_missing_span(self):
        collector = self.collector()
        self.run_backfill(collector)
        query = self.ranges[0]
        self.assertEqual(query["query"], "sum(increase(flips[86400s]))")
        self.assertEqual(query["step"], "86400")
        self.assertEqual(int(query["start"]), self.midnight(self.today - dt.timedelta(days=4)))
        self.assertEqual(int(query["end"]), self.midnight(self.today))

    def test_point_at_midnight_is_the_value_of_the_day_before(self):
        response = {"status": "success", "data": {"resultType": "matrix", "result": [{"values": [[self.midnight(dt.date(2026, 10, 31)), "7"], [self.midnight(dt.date(2026, 11, 1)), "9.5"]]}]}}
        self.assertEqual(module.matrix_by_day(response), {dt.date(2026, 10, 30): 7.0, dt.date(2026, 10, 31): 9.5})

    def test_multiple_overlapping_series_and_warnings_are_rejected(self):
        series = {"values": [[DAY, "1"]]}
        for response in [{"status": "success", "data": {"resultType": "matrix", "result": [series, series]}},
                         {"status": "success", "warnings": ["w"], "data": {"resultType": "matrix", "result": [series]}}]:
            with self.subTest(response=response), self.assertRaises(module.CollectorError):
                module.matrix_by_day(response)

    def test_non_overlapping_series_are_merged_and_nonfinite_points_are_no_data(self):
        first = {"values": [[DAY, "1"], [2 * DAY, "NaN"]]}
        second = {"values": [[2 * DAY, "4"], [3 * DAY, "+Inf"]]}
        response = {"status": "success", "data": {"resultType": "matrix", "result": [first, second]}}
        self.assertEqual(module.matrix_by_day(response), {dt.date(1970, 1, 1): 1.0, dt.date(1970, 1, 2): 4.0})

    def test_covered_days_are_skipped_and_span_shrinks(self):
        covered = [(self.today - dt.timedelta(days=n)).isoformat() for n in (1, 2)]
        self.coverage["metrics"] = {"flips": covered, "depth": covered}
        self.run_backfill(self.collector())
        self.assertEqual(int(self.ranges[0]["end"]), self.midnight(self.today - dt.timedelta(days=2)))
        days = [o["day"] for batch in self.posts for o in batch["observations"]]
        self.assertEqual(days, ["2026-10-29", "2026-10-28", "2026-10-27"])

    def test_fully_covered_metrics_send_nothing(self):
        covered = [(self.today - dt.timedelta(days=n)).isoformat() for n in range(1, 6)]
        self.coverage["metrics"] = {"flips": covered, "depth": covered}
        self.run_backfill(self.collector())
        self.assertEqual((self.ranges, self.posts), ([], []))

    def test_observation_groups_metrics_per_day_with_end_of_day_observed_at(self):
        self.run_backfill(self.collector())
        observation = self.posts[0]["observations"][0]
        self.assertEqual(self.posts[0]["projectId"], "e627bc7e-01b6-46ed-9e4d-72ed598e3634")
        self.assertEqual(observation["day"], "2026-10-31")
        self.assertEqual(observation["observedAt"], "2026-11-01T00:00:00Z")
        self.assertEqual(observation["metrics"], [{"key": "flips", "value": 5, "unit": "count", "aggregation": "sum"},
                                                  {"key": "depth", "value": 5, "unit": "count", "aggregation": "latest"}])

    def test_batches_have_at_most_31_days_newest_first(self):
        collector = self.collector(backfillDays=70)
        self.run_backfill(collector)
        sizes = [len(batch["observations"]) for batch in self.posts]
        self.assertEqual(sizes, [31, 31, 8])
        days = [o["day"] for batch in self.posts for o in batch["observations"]]
        self.assertEqual(days, sorted(days, reverse=True))
        self.assertEqual(len(set(days)), 70)

    def test_days_without_samples_are_not_sent_and_not_requeried(self):
        cutoff = self.midnight(self.today - dt.timedelta(days=2))
        self.range_points = lambda start, end: [[t, "1"] for t in range(start, end + 1, DAY) if t >= cutoff]
        collector = self.collector()
        self.run_backfill(collector)
        self.assertEqual(len(self.posts[0]["observations"]), 3)
        collector.next_backfill = 0
        self.ranges.clear()
        self.run_backfill(collector)
        self.assertEqual(int(self.ranges[0]["start"]), cutoff)

    def test_coverage_404_disables_backfill_and_logs_once(self):
        self.coverage_error = module.CollectorError("http_404", 404)
        collector = self.collector()
        with unittest.mock.patch("builtins.print") as printed:
            self.run_backfill(collector)
            collector.next_backfill = 0
            self.run_backfill(collector)
        self.assertEqual((len(self.gets), printed.call_count, self.ranges, self.posts), (1, 3, [], []))

    def test_other_coverage_errors_do_not_raise_and_retry_next_pass(self):
        self.coverage_error = module.CollectorError("http_500", 500)
        collector = self.collector()
        with unittest.mock.patch("builtins.print"):
            self.run_backfill(collector)
        self.assertFalse(collector.coverage_unsupported)
        self.assertEqual(collector.errors, 1)

    def test_rate_limit_keeps_batch_and_honours_retry_after(self):
        self.post_errors = [module.CollectorError("http_429", 429, 42)]
        collector = self.collector()
        before = self.clock[0]
        self.run_backfill(collector)
        self.assertEqual(self.posts, [])
        self.assertEqual(len(collector.pending_batches), 1)
        self.assertGreaterEqual(collector.blocked_until, before + 42)
        self.run_backfill(collector)
        self.assertEqual(self.posts, [])
        collector.blocked_until = 0
        self.run_backfill(collector)
        self.assertEqual(len(self.posts), 1)
        self.assertEqual(collector.pending_batches, [])

    def test_retry_after_header_is_parsed_and_bounded(self):
        for header, expected in [("42", 42), ("0", 1), ("99999", 3600), ("soon", 10), (None, 10)]:
            with self.subTest(header=header):
                self.assertEqual(module.parse_retry_after({"Retry-After": header} if header else {}), expected)

    def test_batches_wait_for_post_spacing_and_stay_inside_the_idle_window(self):
        collector = self.collector(backfillDays=70)
        collector.last_post = self.clock[0]
        stop = self.run_backfill(collector, deadline=self.clock[0] + 15)
        self.assertEqual(len(self.posts), 1)
        self.assertTrue(all(0 < d <= 10 for d in stop.delays))
        self.assertEqual(len(collector.pending_batches), 2)

    def test_disabled_legacy_and_interval_gate(self):
        self.run_backfill(self.collector(backfillDays=0))
        legacy = module.Collector({**self.config, "metrics": {"tracked_profit": {"query": "q", "timestampQuery": "t"}}}, self.transport)
        self.run_backfill(legacy)
        self.assertEqual((self.gets, self.posts), ([], []))
        collector = self.collector()
        self.run_backfill(collector)
        self.gets.clear()
        self.run_backfill(collector)
        self.assertEqual(self.gets, [])

    def test_backfill_configuration_is_validated(self):
        for extra in [{"backfillDays": 401}, {"backfillDays": -1}, {"backfillDays": True}, {"backfillDays": "5"},
                      {"backfillIntervalSeconds": 10}, {"backfillIntervalSeconds": 604801}]:
            with self.subTest(extra=extra), self.assertRaises(module.CollectorError):
                self.collector(**extra)
        defaults = module.Collector({k: v for k, v in self.config.items() if k != "backfillDays"}, self.transport)
        self.assertEqual((defaults.backfill_days, defaults.backfill_interval), (400, 21600))

    def test_window_floor_follows_server_retention(self):
        self.coverage["retentionDays"] = 3
        self.run_backfill(self.collector())
        days = [o["day"] for batch in self.posts for o in batch["observations"]]
        self.assertEqual(days, ["2026-10-31", "2026-10-30"])
        self.assertEqual(int(self.ranges[0]["start"]), self.midnight(self.today - dt.timedelta(days=1)))

    def test_default_window_never_reaches_beyond_retention_minus_one_days(self):
        collector = self.collector(backfillDays=400)
        self.run_backfill(collector)
        oldest = self.posts[-1]["observations"][-1]["day"]
        self.assertEqual(oldest, (self.today - dt.timedelta(days=399)).isoformat())

    def test_days_below_the_floor_after_midnight_rollover_are_dropped_at_send_time(self):
        collector = self.collector(backfillDays=400)
        observation = lambda n: {"day": (self.today - dt.timedelta(days=n)).isoformat(), "observedAt": "x", "metrics": []}
        batch = {"projectId": collector.project, "observations": [observation(1), observation(399)]}
        self.assertEqual(len(collector.fresh_batch(batch, self.now)["observations"]), 2)
        tomorrow = self.now + dt.timedelta(days=1)
        kept = collector.fresh_batch(batch, tomorrow)["observations"]
        self.assertEqual([o["day"] for o in kept], [observation(1)["day"]])
        self.assertIsNone(collector.fresh_batch({"projectId": collector.project, "observations": [observation(399)]}, tomorrow))

    def test_rejected_batch_is_bisected_and_only_the_bad_day_is_dropped(self):
        bad = "2026-10-29"
        real = self.transport
        def transport(url, token=None, payload=None, timeout=20):
            if payload and any(o["day"] == bad for o in payload["observations"]):
                raise module.CollectorError("http_400: bad", 400)
            return real(url, token, payload, timeout)
        collector = module.Collector({**self.config, "backfillDays": 8}, transport)
        with unittest.mock.patch("builtins.print"):
            self.run_backfill(collector)
        days = [o["day"] for batch in self.posts for o in batch["observations"]]
        self.assertEqual(sorted(days), sorted(d for d in ["2026-10-%02d" % n for n in range(25, 32)] + ["2026-10-24"] if d != bad))
        self.assertEqual(collector.rejected_days, {bad})
        self.assertEqual(collector.pending_batches, [])
        collector.next_backfill = 0
        self.posts.clear()
        with unittest.mock.patch("builtins.print"):
            self.run_backfill(collector)
        self.assertNotIn(bad, [o["day"] for b in self.posts for o in b["observations"]])

    def test_systematic_rejection_stops_the_pass_early(self):
        def transport(url, token=None, payload=None, timeout=20):
            if payload:
                raise module.CollectorError("http_400: undeclared", 400)
            return self.__class__.real(url, token, payload, timeout)
        self.__class__.real = self.transport
        collector = module.Collector({**self.config, "backfillDays": 40}, transport)
        with unittest.mock.patch("builtins.print"):
            self.run_backfill(collector)
        self.assertEqual(len(collector.rejected_days), module.MAX_REJECTED_DAYS_PER_PASS)
        self.assertEqual(collector.pending_batches, [])

    def test_transient_failure_keeps_pending_batches_until_repeated(self):
        self.post_errors = [module.CollectorError("http_500", 500)]
        collector = self.collector(backfillDays=70)
        with unittest.mock.patch("builtins.print"):
            self.run_backfill(collector)
        self.assertEqual(len(collector.pending_batches), 3)
        self.run_backfill(collector)
        self.assertEqual(collector.pending_batches, [])

    def test_invalid_values_are_dropped_per_day_like_the_live_path(self):
        config = {**self.config, "metrics": [{"key": "total_users", "unit": "users", "aggregation": "latest", "query": "u[{range}s]"}]}
        values = {1: "-3", 2: "4.5", 3: "8"}
        base = self.midnight(self.today - dt.timedelta(days=5))
        self.range_points = lambda start, end: [[base + n * DAY, v] for n, v in values.items()]
        collector = module.Collector(config, self.transport)
        self.run_backfill(collector)
        days = [o["day"] for b in self.posts for o in b["observations"]]
        self.assertEqual(days, ["2026-10-29"])
        self.assertEqual(self.posts[0]["observations"][0]["metrics"][0]["value"], 8)
        with self.assertRaises(module.CollectorError):
            module.check_value("tracked_profit", 1.5)
        with self.assertRaises(module.CollectorError):
            module.check_value("total_users", -1.0)

    def test_batches_respect_the_byte_limit(self):
        metrics = [{"key": "m%d" % n, "unit": "count", "aggregation": "sum", "query": "q%d[{range}s]" % n} for n in range(100)]
        long_values = lambda start, end: [[t, "123456789.123456"] for t in range(start, end + 1, DAY)]
        self.range_points = long_values
        collector = module.Collector({**self.config, "metrics": metrics, "backfillDays": 31}, self.transport)
        by_day = {self.today - dt.timedelta(days=n): [{"key": "m%d" % i, "value": 123456789.123456, "unit": "count", "aggregation": "sum"} for i in range(100)] for n in range(1, 32)}
        batches = collector.batches(by_day)
        self.assertGreater(len(batches), 1)
        for batch in batches:
            self.assertLess(len(json.dumps(batch)), module.MAX_BATCH_BYTES)
            self.assertLessEqual(len(batch["observations"]), 31)
        self.assertEqual(sum(len(b["observations"]) for b in batches), 31)

    def test_first_live_push_waits_for_the_post_spacing_after_a_backfill_batch(self):
        collector = self.collector()
        collector.last_post = self.clock[0] - 3
        stop = self.run_backfill(module.Collector({**self.config, "backfillDays": 0}, self.transport))
        waits = []
        class Stop:
            def wait(self, delay):
                waits.append(delay)
                return True
        collector.collect_periods(Stop())
        self.assertEqual(waits, [7])

    def test_planning_is_incremental_checks_stop_and_the_deadline(self):
        collector = self.collector()
        collector.metrics[2:] = []  # flips and depth are eligible
        self.run_backfill(collector, deadline=self.clock[0] - 1)  # past deadline: exactly one query per iteration
        self.assertEqual(len(self.ranges), 1)
        self.assertEqual(self.posts, [])
        self.run_backfill(collector)
        self.assertEqual(len(self.ranges), 2)
        self.assertEqual(len(self.posts), 1)

    def test_stop_aborts_planning_before_any_query(self):
        collector = self.collector()
        self.run_backfill(collector, stop_after=True)
        self.assertEqual((self.ranges, self.posts), ([], []))

    def test_failed_metric_query_is_not_retried_within_the_pass_and_others_continue(self):
        real = self.transport
        def transport(url, token=None, payload=None, timeout=20):
            if "flips" in url:
                raise module.CollectorError("transport_failed")
            return real(url, token, payload, timeout)
        collector = module.Collector(self.config, transport)
        with unittest.mock.patch("builtins.print"):
            self.run_backfill(collector)
        self.assertEqual(len(self.ranges), 1)
        self.assertEqual([m["key"] for m in self.posts[0]["observations"][0]["metrics"]], ["depth"])

    def test_no_data_memory_is_cleared_when_the_utc_date_changes(self):
        self.range_points = lambda start, end: []
        collector = self.collector()
        self.run_backfill(collector)
        collector.next_backfill = 0
        self.ranges.clear()
        self.run_backfill(collector)
        self.assertEqual(self.ranges, [])
        self.now += dt.timedelta(days=1)
        collector.next_backfill = 0
        self.run_backfill(collector)
        self.assertEqual(len(self.ranges), 2)

    def test_backfill_configuration_errors_name_the_field(self):
        with self.assertRaisesRegex(module.CollectorError, "backfillDays must be an integer from 0 to 400"):
            self.collector(backfillDays=401)
        with self.assertRaisesRegex(module.CollectorError, "backfillIntervalSeconds must be an integer from 300 to 604800"):
            self.collector(backfillIntervalSeconds=1)

    def test_destination_is_built_from_the_base_url(self):
        collector = self.collector(todUrl="https://dash-api.coflnet.com/")
        self.assertEqual(collector.destination, "https://dash-api.coflnet.com/api/collector/observations")

    def test_backfill_batches_are_spaced_30_seconds_apart(self):
        collector = self.collector(backfillDays=70)
        collector.last_batch_post = self.clock[0] - 5
        stop = self.run_backfill(collector)
        self.assertEqual(len(self.posts), 3)
        self.assertEqual(stop.delays, [25, 30, 30])

    def test_batch_spacing_does_not_delay_live_pushes(self):
        collector = self.collector()
        collector.last_batch_post = self.clock[0]
        collector.last_post = self.clock[0] - 20
        self.assertEqual(collector.post_delay(), 0.0)
        self.assertEqual(collector.post_delay(batch=True), 30)
        waits = []
        class Stop:
            def wait(self, delay):
                waits.append(delay)
                return True
        self.assertFalse(collector.wait_for_post_slot(Stop()))
        self.assertEqual(waits, [])

    def test_batches_are_chunked_to_868_metric_values(self):
        collector = self.collector()
        by_day = {self.today - dt.timedelta(days=n): [{"key": "m%d" % i, "value": 1, "unit": "count", "aggregation": "sum"} for i in range(100)]
                  for n in range(1, 31)}
        batches = collector.batches(by_day)
        values = [sum(len(o["metrics"]) for o in b["observations"]) for b in batches]
        self.assertTrue(all(v <= module.MAX_BATCH_VALUES for v in values))
        self.assertEqual(values, [800, 800, 800, 600])
        self.assertEqual(sum(values), 3000)

    def test_coverage_403_logs_once_and_waits_for_the_next_pass(self):
        self.coverage_error = module.CollectorError("http_403", 403)
        collector = self.collector()
        with unittest.mock.patch("builtins.print") as printed:
            self.run_backfill(collector)
            self.run_backfill(collector)  # not due yet: no new request
            collector.next_backfill = 0
            self.run_backfill(collector)
        forbidden = [c for c in printed.call_args_list if "refused coverage" in c.args[0]]
        self.assertEqual((len(self.gets), len(forbidden)), (2, 1))
        self.assertFalse(collector.coverage_unsupported)
        self.assertEqual((self.ranges, self.posts), ([], []))
        self.coverage_error = None
        collector.next_backfill = 0
        self.run_backfill(collector)
        self.assertTrue(self.posts)

    def test_coverage_429_retries_at_the_next_pass_without_hot_looping(self):
        self.coverage_error = module.CollectorError("http_429", 429, 60)
        collector = self.collector()
        with unittest.mock.patch("builtins.print"):
            for _ in range(3):
                self.run_backfill(collector)
        self.assertEqual(len(self.gets), 1)
        self.assertFalse(collector.coverage_unsupported)
        self.assertEqual(collector.errors, 0)
        self.coverage_error = None
        collector.next_backfill = 0
        self.run_backfill(collector)
        self.assertTrue(self.posts)
