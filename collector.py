"""Private Prometheus snapshots to a project-scoped Tod collector; standard library only."""
import datetime as dt
import json
import math
import base64
import os
import re
import signal
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

UTC = dt.timezone.utc
CATALOG = {"total_users": ("users", "latest"), "tracked_profit": ("coins", "sum")}
# Must match the units/aggregations Todün accepts for declared custom metrics.
UNITS = {"count", "users", "requests", "eur", "usd", "gbp", "coins", "seconds", "ms", "bytes", "percent"}
AGGREGATIONS = {"sum", "latest"}
KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
MAX_METRICS = 100
BACKFILL_BATCH_DAYS = 31
MAX_BATCH_BYTES = 240_000  # the server rejects bodies above 262144 bytes
MAX_REJECTED_DAYS_PER_PASS = 5
MAX_BATCH_FAILURES = 3
RANGE_TIMEOUT_SECONDS = 60
POST_SPACING_SECONDS = 10
BATCH_SPACING_SECONDS = 30  # batches have their own ingest window, separate from the live one
MAX_BATCH_VALUES = 868  # metric values (sum over all days) the server accepts per batch
DEFAULT_RETRY_AFTER = 10
# Cloudflare in front of Todün rejects the default "Python-urllib" agent with a bare 403.
USER_AGENT = "toduen-collector/" + (os.environ.get("TODUEN_COLLECTOR_VERSION") or "dev")


class CollectorError(Exception):
    def __init__(self, message="", status=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise CollectorError("redirect_rejected")


def endpoint(value):
    parsed = urllib.parse.urlsplit(value)
    # Plain HTTP is only for in-cluster services: a single-label Service name or a *.svc[.cluster.local] name.
    private_http = parsed.scheme == "http" and parsed.hostname and ("." not in parsed.hostname or parsed.hostname.endswith((".svc", ".svc.cluster.local")))
    if (parsed.scheme != "https" and not private_http) or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or parsed.query:
        raise CollectorError("invalid_endpoint")
    return value.rstrip("/")


def request_json(url, token=None, payload=None, timeout=20):
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = token if token.startswith("Basic ") else "Bearer " + token
    data = None if payload is None else json.dumps(payload, allow_nan=False).encode()
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
            body = response.read(1_048_577)
            if len(body) > 1_048_576:
                raise CollectorError("response_too_large")
            return json.loads(body) if body else None
    except urllib.error.HTTPError as error:
        reason = ""
        if 400 <= error.code < 500:
            try:  # Todün explains rejections (e.g. an undeclared metric); never includes secrets.
                reason = ": " + str(json.loads(error.read(2048)).get("error", ""))[:300]
            except (ValueError, AttributeError, OSError):
                pass
        raise CollectorError("http_" + str(error.code) + reason, error.code, parse_retry_after(error.headers)) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise CollectorError("transport_failed") from None
    except (ValueError, UnicodeError):
        raise CollectorError("invalid_response") from None


def parse_retry_after(headers):
    try:
        return min(3600, max(1, int(headers.get("Retry-After"))))
    except (AttributeError, TypeError, ValueError):
        return DEFAULT_RETRY_AFTER


def matrix_series(response):
    try:
        data = response["data"]
        if response["status"] != "success" or response.get("warnings") or data["resultType"] != "matrix":
            raise CollectorError("missing_or_ambiguous_data")
        return data["result"]
    except (KeyError, TypeError):
        raise CollectorError("invalid_response") from None


def series_days(series):
    """The point at UTC midnight T is the value of the day ending at T. Non-finite points count as no data."""
    days = {}
    for timestamp, text in series["values"]:
        value = float(text)
        if math.isfinite(value):
            days[(dt.datetime.fromtimestamp(float(timestamp), UTC) - dt.timedelta(seconds=1)).date()] = value
    return days


def merge_days(days, extra):
    if days.keys() & extra.keys():  # two series with a value at the same timestamp cannot be reduced to one
        raise CollectorError("missing_or_ambiguous_data")
    days.update(extra)


def matrix_by_day(response):
    """Maps a range result to {day: value}. Series that never overlap (e.g. labels changed after a restart) are merged."""
    days = {}
    try:
        for series in matrix_series(response):
            merge_days(days, series_days(series))
    except (KeyError, TypeError, IndexError, ValueError, OverflowError):
        raise CollectorError("invalid_response") from None
    return days


def check_value(key, value):
    """Per-value rules shared by the live and the backfill path."""
    if abs(value) > 9_007_199_254_740_991 or key in CATALOG and (not value.is_integer() or key == "total_users" and value < 0):
        raise CollectorError("invalid_metric_value")


def valid_value(key, value):
    try:
        check_value(key, value)
    except CollectorError:
        return False
    return True


def bounded_int(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise CollectorError("invalid_backfill_configuration: " + name + " must be an integer from " + str(low) + " to " + str(high))
    return value


def day_end(day):
    return dt.datetime.combine(day, dt.time(), UTC) + dt.timedelta(days=1)


def number(value):
    return int(value) if value.is_integer() else value


def scalar(response):
    try:
        result = response["data"]["result"]
        if response["status"] != "success" or response.get("warnings") or response["data"]["resultType"] != "vector" or len(result) != 1:
            raise CollectorError("missing_or_ambiguous_data")
        value = float(result[0]["value"][1])
        if not math.isfinite(value):
            raise CollectorError("nonfinite_data")
        return value
    except (KeyError, TypeError, IndexError, ValueError):
        raise CollectorError("invalid_response") from None


def read_token(path):
    token = Path(path).read_text().strip()
    if not token or len(token) > 4096 or any(c.isspace() for c in token):
        raise CollectorError("invalid_credential_file")
    return token


def read_basic_auth(path):
    """File content is `username:password`; returned as a ready Authorization header value."""
    value = Path(path).read_text().strip()
    if ":" not in value or len(value) > 4096:
        raise CollectorError("invalid_credential_file")
    return "Basic " + base64.b64encode(value.encode()).decode()


def normalize_metrics(raw):
    """Accepts the generic list format or the legacy {key: {query, timestampQuery}} mapping; returns uniform definitions."""
    if isinstance(raw, dict):  # legacy: fixed catalog, mandatory source timestamp, integer values
        if not raw or not set(raw) <= CATALOG.keys():
            raise CollectorError("invalid_configuration")
        items = [{"key": key, "unit": CATALOG[key][0], "aggregation": CATALOG[key][1], **(definition if isinstance(definition, dict) else {})}
                 for key, definition in raw.items()]
        legacy = True
    elif isinstance(raw, list) and raw:
        items, legacy = raw, False
    else:
        raise CollectorError("invalid_configuration")
    if len(items) > MAX_METRICS:
        raise CollectorError("invalid_configuration")
    result, seen = [], set()
    for item in items:
        if not isinstance(item, dict):
            raise CollectorError("invalid_query_configuration")
        key, unit, aggregation = item.get("key"), item.get("unit"), item.get("aggregation")
        query, timestamp_query = item.get("query"), item.get("timestampQuery")
        if not isinstance(key, str) or not KEY_PATTERN.match(key) or key in seen or unit not in UNITS or aggregation not in AGGREGATIONS:
            raise CollectorError("invalid_metric_declaration")
        if key in CATALOG and (unit, aggregation) != CATALOG[key]:
            raise CollectorError("invalid_metric_declaration")
        if not isinstance(query, str) or not 1 <= len(query) <= 4000:
            raise CollectorError("invalid_query_configuration")
        if timestamp_query is None and legacy or timestamp_query is not None and (not isinstance(timestamp_query, str) or not 1 <= len(timestamp_query) <= 4000):
            raise CollectorError("invalid_query_configuration")
        seen.add(key)
        result.append({"key": key, "unit": unit, "aggregation": aggregation, "query": query, "timestampQuery": timestamp_query})
    return result, legacy


class Collector:
    def __init__(self, config, transport=request_json):
        import uuid
        self.config = config
        self.transport = transport
        self.prometheus = endpoint(config["prometheusUrl"])
        self.base = endpoint(config["todUrl"])
        self.destination = self.base + "/api/collector/observations"
        self.project = str(uuid.UUID(config["projectId"]))
        self.interval = int(config.get("intervalSeconds", 300))
        self.max_age = int(config.get("maxSourceAgeSeconds", 300))
        self.day_offsets = config.get("dayOffsets", [config.get("dayOffset", 0)])
        if not isinstance(self.day_offsets, list) or not 1 <= len(self.day_offsets) <= 8 or any(type(value) is not int or not 0 <= value <= 7 for value in self.day_offsets) or len(set(self.day_offsets)) != len(self.day_offsets):
            raise CollectorError("invalid_period_configuration")
        self.day_offset = self.day_offsets[0]
        if not 30 <= self.interval <= 3600 or not 30 <= self.max_age <= 3600:
            raise CollectorError("invalid_configuration")
        self.metrics, self.legacy = normalize_metrics(config.get("metrics"))
        self.backfill_days = bounded_int(config.get("backfillDays", 400), "backfillDays", 0, 400)
        self.backfill_interval = bounded_int(config.get("backfillIntervalSeconds", 21600), "backfillIntervalSeconds", 300, 604800)
        self.plan = None
        self.pending_batches = []
        self.retention_days = 400
        self.batch_failures = 0
        self.rejected_in_pass = 0
        self.rejected_days = set()
        self.no_data_date = None
        self.next_backfill = 0.0
        self.blocked_until = 0.0
        self.last_post = 0.0
        self.last_batch_post = 0.0
        self.coverage_forbidden_logged = False
        self.coverage_unsupported = False
        self.ineligibility_logged = False
        self.no_data_days = {}
        self.last_success = 0.0
        self.errors = 0
        self.status = "no_data"

    def query(self, expression, now, token):
        encoded = urllib.parse.urlencode({"query": expression, "time": now.timestamp(), "timeout": "15s"})
        return scalar(self.transport(self.prometheus + "/api/v1/query?" + encoded, token))

    def prometheus_auth(self):
        if self.config.get("prometheusTokenFile"):
            return read_token(self.config["prometheusTokenFile"])
        if self.config.get("prometheusBasicAuthFile"):
            return read_basic_auth(self.config["prometheusBasicAuthFile"])
        return None

    def collect(self, now=None, day_offset=None):
        now = now or dt.datetime.now(UTC)
        auth = self.prometheus_auth()
        metrics = []
        timestamps = []
        day = now.date() - dt.timedelta(days=self.day_offset if day_offset is None else day_offset)
        start = dt.datetime.combine(day, dt.time(), UTC)
        end = start + dt.timedelta(days=1)
        # Generic metrics are evaluated at the end of their UTC day (or now, for the running day), so a closed day is
        # reproducible and {range} covers exactly the elapsed part of the day. Legacy metrics evaluate at now.
        evaluated = now if self.legacy else min(now, end)
        elapsed = max(60, int((evaluated - start).total_seconds()))
        def expression(value):
            return (value.replace("{day}", day.isoformat()).replace("{day_start}", str(int(start.timestamp())))
                    .replace("{day_end}", str(int(end.timestamp()))).replace("{range}", str(elapsed)))
        failure = None
        for definition in self.metrics:
            key = definition["key"]
            try:
                value = self.query(expression(definition["query"]), evaluated, auth)
                if definition["timestampQuery"]:
                    timestamp = self.query(expression(definition["timestampQuery"]), evaluated, auth)
                    if not now.timestamp() - self.max_age <= timestamp <= now.timestamp() + 60:
                        raise CollectorError("stale_source")
                else:
                    timestamp = evaluated.timestamp()
                check_value(key, value)
            except CollectorError as error:
                # One broken metric never blocks the others, and a missing value is never replaced by zero.
                failure = failure or error
                self.errors += 1
                continue
            metrics.append({"key": key, "value": number(value), "unit": definition["unit"], "aggregation": definition["aggregation"]})
            timestamps.append(dt.datetime.fromtimestamp(timestamp, UTC))
        if not metrics:
            raise failure or CollectorError("no_metrics")
        payload = {"projectId": self.project, "day": day.isoformat(), "observedAt": min(timestamps).isoformat().replace("+00:00", "Z"), "metrics": metrics}
        self.transport(self.destination, read_token(self.config["todTokenFile"]), payload)
        self.last_post = time.monotonic()
        self.last_success = now.timestamp()
        self.status = "ok" if failure is None else "partial"
        return payload

    def collect_periods(self, stop):
        for index, day_offset in enumerate(self.day_offsets):
            # The project credential permits one observation per ten seconds, also after a backfill batch.
            if (stop.wait(10) if index > 0 else self.wait_for_post_slot(stop)):
                break
            try:
                self.collect(day_offset=day_offset)
            except (CollectorError, ValueError, KeyError, OSError) as error:
                self.errors += 1
                if self.status != "source_or_delivery_failed":
                    print("collector source or delivery failed (" + (str(error) if isinstance(error, CollectorError) else "invalid_input") + "); inspect private source and credential configuration", flush=True)
                self.status = "source_or_delivery_failed"

    # ---- history backfill -------------------------------------------------------------------------------------

    def eligible_metrics(self):
        """Returns (eligible definitions, {key: reason}); only plain {range} expressions without a source timestamp can be replayed."""
        eligible, skipped = [], {}
        for definition in self.metrics:
            if definition["timestampQuery"]:
                skipped[definition["key"]] = "uses timestampQuery (freshness is checked against the present)"
            elif any(placeholder in definition["query"] for placeholder in ("{day}", "{day_start}", "{day_end}")):
                skipped[definition["key"]] = "uses {day}, {day_start} or {day_end}; only {range} can be replayed"
            else:
                eligible.append(definition)
        return eligible, skipped

    def log_ineligible(self, skipped):
        if self.ineligibility_logged:
            return
        self.ineligibility_logged = True
        for key, reason in skipped.items():
            print("history backfill skipped for metric " + key + ": " + reason, flush=True)

    def fetch_coverage(self):
        """(retentionDays, {metric: covered days}), or None when the server predates the coverage endpoint."""
        url = self.base + "/api/collector/coverage?" + urllib.parse.urlencode({"projectId": self.project})
        try:
            response = self.transport(url, read_token(self.config["todTokenFile"]))
        except CollectorError as error:
            self.note_coverage_refusal(error)
            return None
        return int(response.get("retentionDays", 400)), {key: set(days) for key, days in response["metrics"].items()}

    def note_coverage_refusal(self, error):
        """404 (old server) disables backfill; 403 (category disabled) and 429 (one per minute) just wait for the next pass."""
        if error.status == 404:
            self.coverage_unsupported = True
            print("history backfill disabled: the server has no coverage endpoint", flush=True)
        elif error.status == 403 and not self.coverage_forbidden_logged:
            self.coverage_forbidden_logged = True
            print("history backfill skipped: the server refused coverage access (category disabled?); retrying next pass", flush=True)
        elif error.status == 429:
            print("history backfill postponed: coverage rate limit; retrying next pass", flush=True)
        elif error.status != 403:
            raise error

    def forget_stale_no_data(self, today):
        if self.no_data_date != today:  # a temporarily empty Prometheus must not block a metric until restart
            self.no_data_days = {}
            self.no_data_date = today

    def missing_days(self, key, window, covered):
        known = covered.get(key, set()) | {day.isoformat() for day in self.no_data_days.get(key, set())}
        return [day for day in window if day.isoformat() not in known]

    def start_plan(self, now):
        """Fetches coverage and queues the metrics that miss days; None when there is nothing to do."""
        eligible, skipped = self.eligible_metrics()
        self.log_ineligible(skipped)
        coverage = self.fetch_coverage() if eligible else None
        if coverage is None:
            return None
        self.retention_days, covered = coverage
        today = now.date()
        self.forget_stale_no_data(today)
        oldest = min(self.backfill_days, self.retention_days - 1)  # the server accepts today-(retention-1) at the earliest
        window = [today - dt.timedelta(days=offset) for offset in range(oldest, 0, -1)]
        queue = [(definition, missing) for definition in eligible if (missing := self.missing_days(definition["key"], window, covered))]
        self.rejected_in_pass = 0
        return {"queue": queue, "results": {}, "definitions": eligible} if queue else None

    def range_query(self, definition, missing, auth):
        """One Prometheus range query for the span of missing days; returns the valid {day: value} and remembers days without data."""
        key = definition["key"]
        expression = definition["query"].replace("{range}", "86400")
        params = {"query": expression, "start": int(day_end(missing[0]).timestamp()), "end": int(day_end(missing[-1]).timestamp()),
                  "step": 86400, "timeout": "50s"}
        response = self.transport(self.prometheus + "/api/v1/query_range?" + urllib.parse.urlencode(params), auth, timeout=RANGE_TIMEOUT_SECONDS)
        values = matrix_by_day(response)
        self.no_data_days.setdefault(key, set()).update(day for day in missing if day not in values)
        return {day: values[day] for day in missing if day in values and valid_value(key, values[day])}

    def run_plan_queries(self, stop, deadline):
        """Queries at least one metric, then more while the idle window lasts; a failed metric waits for the next pass."""
        auth = self.prometheus_auth()
        queue = self.plan["queue"]
        first = True
        while queue:
            if stop.is_set() or not first and deadline is not None and time.monotonic() >= deadline:
                return
            first = False
            definition, missing = queue.pop(0)
            self.query_metric(definition, missing, auth)

    def query_metric(self, definition, missing, auth):
        try:
            self.plan["results"][definition["key"]] = self.range_query(definition, missing, auth)
        except (CollectorError, ValueError, KeyError, OSError) as error:
            self.errors += 1
            print("history backfill query failed for metric " + definition["key"] + " (" + str(error) + ")", flush=True)

    def group_by_day(self, per_metric, definitions):
        by_day = {}
        for definition in definitions:
            for day, value in per_metric.get(definition["key"], {}).items():
                by_day.setdefault(day, []).append({"key": definition["key"], "value": number(value), "unit": definition["unit"], "aggregation": definition["aggregation"]})
        return by_day

    def batches(self, by_day):
        """Newest days first, at most BACKFILL_BATCH_DAYS observations and MAX_BATCH_BYTES per request."""
        observations = [{"day": day.isoformat(), "observedAt": day_end(day).isoformat().replace("+00:00", "Z"), "metrics": by_day[day]}
                        for day in sorted(by_day, reverse=True)]
        overhead = len(json.dumps({"projectId": self.project, "observations": []}))
        batches, current, size, values = [], [], overhead, 0
        for observation in observations:
            added, rows = len(json.dumps(observation)) + 1, len(observation["metrics"])
            if current and (len(current) >= BACKFILL_BATCH_DAYS or size + added > MAX_BATCH_BYTES or values + rows > MAX_BATCH_VALUES):
                batches.append({"projectId": self.project, "observations": current})
                current, size, values = [], overhead, 0
            current.append(observation)
            size += added
            values += rows
        if current:
            batches.append({"projectId": self.project, "observations": current})
        return batches

    def advance_plan(self, stop, now, deadline):
        """Plans incrementally; True once batches are ready to send."""
        if self.plan is None:
            if time.monotonic() < self.next_backfill:
                return False
            self.next_backfill = time.monotonic() + self.backfill_interval
            self.plan = self.start_plan(now)
        if self.plan is None:
            return False
        self.run_plan_queries(stop, deadline)
        if self.plan["queue"]:
            return False
        plan, self.plan = self.plan, None
        self.pending_batches = self.batches(self.group_by_day(plan["results"], plan["definitions"]))
        return bool(self.pending_batches)

    def post_delay(self, batch=False):
        """Seconds until the next POST may go out; batches additionally respect their own 30s window."""
        now = time.monotonic()
        delay = POST_SPACING_SECONDS - (now - self.last_post)
        if batch:
            delay = max(delay, BATCH_SPACING_SECONDS - (now - self.last_batch_post))
        return max(0.0, delay)

    def wait_for_post_slot(self, stop, deadline=None, batch=False):
        """True when the caller must give up: stopped, or the idle window is used up."""
        delay = self.post_delay(batch)
        if deadline is not None and time.monotonic() + delay > deadline:
            return True
        return bool(delay and stop.wait(delay))

    def fresh_batch(self, batch, now):
        """Drops days that fell out of the server's window since planning, and days the server rejected."""
        today = now.date()
        floor = today - dt.timedelta(days=self.retention_days - 1)
        kept = [o for o in batch["observations"] if o["day"] not in self.rejected_days and floor <= dt.date.fromisoformat(o["day"]) < today]
        return {"projectId": batch["projectId"], "observations": kept} if kept else None

    def send_pending_batches(self, stop, now, deadline):
        while self.pending_batches:
            batch = self.fresh_batch(self.pending_batches[0], now)
            if batch is None:
                self.pending_batches.pop(0)
                continue
            self.pending_batches[0] = batch
            if self.wait_for_post_slot(stop, deadline, batch=True) or not self.post_batch(batch):
                return

    def post_batch(self, batch):
        """True when the next batch may follow immediately."""
        try:
            self.transport(self.base + "/api/collector/observations/batch", read_token(self.config["todTokenFile"]), batch)
        except CollectorError as error:
            self.last_post = self.last_batch_post = time.monotonic()
            return self.handle_batch_failure(error)
        self.last_post = self.last_batch_post = time.monotonic()
        self.batch_failures = 0
        self.pending_batches.pop(0)
        return True

    def handle_batch_failure(self, error):
        if error.status == 429:  # keep the batch; the live pushes share this rate limit
            self.blocked_until = time.monotonic() + (error.retry_after or DEFAULT_RETRY_AFTER)
            return False
        if error.status in (400, 413):
            return self.isolate_rejection(str(error))
        self.errors += 1
        self.batch_failures += 1
        print("history backfill delivery failed (" + str(error) + ")", flush=True)
        if self.batch_failures >= MAX_BATCH_FAILURES:
            self.pending_batches, self.batch_failures = [], 0  # re-plan from the server's coverage
        return False

    def isolate_rejection(self, reason):
        """Splits a rejected batch in halves until the bad day is alone, then remembers it."""
        observations = self.pending_batches[0]["observations"]
        project = self.pending_batches[0]["projectId"]
        if len(observations) > 1:
            middle = len(observations) // 2
            self.pending_batches[0:1] = [{"projectId": project, "observations": observations[:middle]}, {"projectId": project, "observations": observations[middle:]}]
            return True
        self.pending_batches.pop(0)
        self.rejected_days.add(observations[0]["day"])
        self.rejected_in_pass += 1
        self.errors += 1
        print("history backfill: the server rejected day " + observations[0]["day"] + " (" + reason + ")", flush=True)
        if self.rejected_in_pass >= MAX_REJECTED_DAYS_PER_PASS:
            self.pending_batches = []  # most likely a systematic rejection (e.g. an undeclared metric); stop hammering
            return False
        return True

    def backfill_blocked(self):
        return self.backfill_days == 0 or self.legacy or self.coverage_unsupported or time.monotonic() < self.blocked_until

    def backfill(self, stop, now=None, deadline=None):
        """Runs between live pushes: plans a pass in steps, then sends batches while the idle window lasts. Never raises."""
        if self.backfill_blocked():
            return
        now = now or dt.datetime.now(UTC)
        try:
            if self.pending_batches or self.advance_plan(stop, now, deadline):
                self.send_pending_batches(stop, now, deadline)
        except (CollectorError, ValueError, KeyError, TypeError, AttributeError, OSError) as error:
            self.errors += 1
            self.plan, self.pending_batches = None, []
            print("history backfill failed (" + (str(error) if isinstance(error, CollectorError) else "invalid_input") + ")", flush=True)

    def exposition(self):
        # Business values and credentials stay out of this diagnostics endpoint.
        healthy = int(self.last_success > time.time() - self.interval * 2)
        return ("# TYPE tod_collector_ready gauge\n" + f"tod_collector_ready {healthy}\n"
                "# TYPE tod_collector_last_success_timestamp_seconds gauge\n" + f"tod_collector_last_success_timestamp_seconds {self.last_success}\n"
                "# TYPE tod_collector_errors_total counter\n" + f"tod_collector_errors_total {self.errors}\n")


def main():
    try:
        collector = Collector(json.loads(Path(os.environ.get("COLLECTOR_CONFIG", "/etc/collector/config.json")).read_text()))
    except (CollectorError, ValueError, KeyError, OSError) as error:
        print("collector configuration failed" + (" (" + str(error) + ")" if isinstance(error, CollectorError) else ""), flush=True)
        return 1
    stop = threading.Event()
    for event in (signal.SIGTERM, signal.SIGINT):
        signal.signal(event, lambda *_: stop.set())

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ("/metrics", "/healthz"):
                self.send_error(404)
                return
            healthy = collector.last_success > time.time() - collector.interval * 2
            self.send_response(200 if self.path == "/metrics" or healthy else 503)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.end_headers()
            self.wfile.write(collector.exposition().encode() if self.path == "/metrics" else collector.status.encode())

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("0.0.0.0", 9090), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    while not stop.is_set():
        started = time.monotonic()
        collector.collect_periods(stop)
        collector.backfill(stop, deadline=started + collector.interval)
        stop.wait(max(0, collector.interval - (time.monotonic() - started)))
    server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
