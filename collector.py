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


class CollectorError(Exception):
    pass


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


def request_json(url, token=None, payload=None):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = token if token.startswith("Basic ") else "Bearer " + token
    data = None if payload is None else json.dumps(payload, allow_nan=False).encode()
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
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
        raise CollectorError("http_" + str(error.code) + reason) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise CollectorError("transport_failed") from None
    except (ValueError, UnicodeError):
        raise CollectorError("invalid_response") from None


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
        self.destination = endpoint(config["todUrl"]) + "/api/collector/observations"
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
        self.last_success = 0.0
        self.errors = 0
        self.status = "no_data"

    def query(self, expression, now, token):
        encoded = urllib.parse.urlencode({"query": expression, "time": now.timestamp(), "timeout": "15s"})
        return scalar(self.transport(self.prometheus + "/api/v1/query?" + encoded, token))

    def collect(self, now=None, day_offset=None):
        now = now or dt.datetime.now(UTC)
        auth = None
        if self.config.get("prometheusTokenFile"):
            auth = read_token(self.config["prometheusTokenFile"])
        elif self.config.get("prometheusBasicAuthFile"):
            auth = read_basic_auth(self.config["prometheusBasicAuthFile"])
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
                if abs(value) > 9_007_199_254_740_991 or key in CATALOG and (not value.is_integer() or key == "total_users" and value < 0):
                    raise CollectorError("invalid_metric_value")
            except CollectorError as error:
                # One broken metric never blocks the others, and a missing value is never replaced by zero.
                failure = failure or error
                self.errors += 1
                continue
            metrics.append({"key": key, "value": int(value) if value.is_integer() else value, "unit": definition["unit"], "aggregation": definition["aggregation"]})
            timestamps.append(dt.datetime.fromtimestamp(timestamp, UTC))
        if not metrics:
            raise failure or CollectorError("no_metrics")
        payload = {"projectId": self.project, "day": day.isoformat(), "observedAt": min(timestamps).isoformat().replace("+00:00", "Z"), "metrics": metrics}
        self.transport(self.destination, read_token(self.config["todTokenFile"]), payload)
        self.last_success = now.timestamp()
        self.status = "ok" if failure is None else "partial"
        return payload

    def collect_periods(self, stop):
        for index, day_offset in enumerate(self.day_offsets):
            if index > 0 and stop.wait(10):
                break  # The project credential permits one observation per ten seconds.
            try:
                self.collect(day_offset=day_offset)
            except (CollectorError, ValueError, KeyError, OSError) as error:
                self.errors += 1
                if self.status != "source_or_delivery_failed":
                    print("collector source or delivery failed (" + (str(error) if isinstance(error, CollectorError) else "invalid_input") + "); inspect private source and credential configuration", flush=True)
                self.status = "source_or_delivery_failed"

    def exposition(self):
        # Business values and credentials stay out of this diagnostics endpoint.
        healthy = int(self.last_success > time.time() - self.interval * 2)
        return ("# TYPE tod_collector_ready gauge\n" + f"tod_collector_ready {healthy}\n"
                "# TYPE tod_collector_last_success_timestamp_seconds gauge\n" + f"tod_collector_last_success_timestamp_seconds {self.last_success}\n"
                "# TYPE tod_collector_errors_total counter\n" + f"tod_collector_errors_total {self.errors}\n")


def main():
    try:
        collector = Collector(json.loads(Path(os.environ.get("COLLECTOR_CONFIG", "/etc/collector/config.json")).read_text()))
    except (CollectorError, ValueError, KeyError, OSError):
        print("collector configuration failed", flush=True)
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
        stop.wait(max(0, collector.interval - (time.monotonic() - started)))
    server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
