#!/usr/bin/env python3
"""M13 Phase 6: a small custom Prometheus exporter for the metrics that
Prometheus's own standard exporters (node_exporter, postgres_exporter)
cannot see: this application's own /health/* endpoints, per-status-code
request rates and slow requests parsed from nginx's own JSON access log
(deploy/nginx/nginx.conf's erp_json format), and authentication-failure
counts from the audit log. Deliberately a single ~250-line script, not
a new platform — "prefer simple open-source components"
(docs/M13_DESIGN.md Section 6).

Backup success/failure is NOT polled here: deploy/scripts/backup_offbox.sh
already writes a Prometheus textfile-format status file
(BACKUP_STATUS_FILE, default /var/lib/erp/backup_status.prom) —
node_exporter's own `--collector.textfile.directory` picks that up
directly, so this exporter doesn't duplicate it.

Usage: python3 erp_exporter.py [--port 9101]
Exposes GET /metrics in Prometheus text exposition format.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

DEFAULT_APP_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_NGINX_LOG = "/var/log/nginx/access.log"
DEFAULT_DB_URL = "postgresql://erp_user:erp_password@localhost:5432/erp_dev"
SLOW_REQUEST_THRESHOLD_SECONDS = 1.0

_lock = threading.Lock()
_state: dict[str, Any] = {
    "api_up": 0,
    "db_up": 0,
    "migration_current": 0,
    "requests_total": {},  # status_class ("2xx", "4xx", "5xx") -> count
    "requests_429_total": 0,
    "requests_slow_total": 0,
    "auth_failures_total": 0,
    "auth_reuse_detected_total": 0,
    "log_bytes_read": 0,
}


def _poll_health(app_base_url: str) -> None:
    for name, path in (
        ("api_up", "/health"),
        ("db_up", "/health/db"),
        ("migration_current", "/health/migration"),
    ):
        try:
            with urllib.request.urlopen(f"{app_base_url}{path}", timeout=3) as resp:
                ok = 1 if resp.status == 200 else 0
        except Exception:  # noqa: BLE001 -- any failure means the check is down
            ok = 0
        with _lock:
            _state[name] = ok


def _tail_nginx_log(log_path: Path) -> None:
    """Reads only the bytes appended since the last poll -- a real tail,
    not re-parsing the whole file every cycle."""
    if not log_path.exists():
        return
    with _lock:
        start = _state["log_bytes_read"]
    try:
        with open(log_path, "rb") as f:
            f.seek(start)
            new_data = f.read()
    except OSError:
        return
    with _lock:
        _state["log_bytes_read"] = start + len(new_data)

    for line in new_data.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        status = entry.get("status")
        if status is None:
            continue
        status_class = f"{status // 100}xx"
        with _lock:
            _state["requests_total"][status_class] = (
                _state["requests_total"].get(status_class, 0) + 1
            )
            if status == 429:
                _state["requests_429_total"] += 1
            try:
                if float(entry.get("request_time", 0)) > SLOW_REQUEST_THRESHOLD_SECONDS:
                    _state["requests_slow_total"] += 1
            except (TypeError, ValueError):
                pass


def _poll_auth_failures(db_url: str) -> None:
    try:
        import psycopg

        with psycopg.connect(db_url, connect_timeout=3) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM audit_logs WHERE action = 'LOGIN_FAILURE'")
            failures_row = cur.fetchone()
            failures = failures_row[0] if failures_row else 0
            cur.execute(
                "SELECT count(*) FROM audit_logs WHERE action = 'REFRESH_TOKEN_REUSE_DETECTED'"
            )
            reuse_row = cur.fetchone()
            reuse = reuse_row[0] if reuse_row else 0
        with _lock:
            _state["auth_failures_total"] = failures
            _state["auth_reuse_detected_total"] = reuse
    except Exception:  # noqa: BLE001 -- a DB hiccup here must not crash the exporter
        pass


def _poll_loop(app_base_url: str, nginx_log: Path, db_url: str, interval: float) -> None:
    while True:
        _poll_health(app_base_url)
        _tail_nginx_log(nginx_log)
        _poll_auth_failures(db_url)
        time.sleep(interval)


def _render_metrics() -> str:
    with _lock:
        snapshot = {
            "api_up": _state["api_up"],
            "db_up": _state["db_up"],
            "migration_current": _state["migration_current"],
            "requests_total": dict(_state["requests_total"]),
            "requests_429_total": _state["requests_429_total"],
            "requests_slow_total": _state["requests_slow_total"],
            "auth_failures_total": _state["auth_failures_total"],
            "auth_reuse_detected_total": _state["auth_reuse_detected_total"],
        }

    lines = [
        "# HELP erp_api_up 1 if GET /health returned 200, else 0",
        "# TYPE erp_api_up gauge",
        f"erp_api_up {snapshot['api_up']}",
        "# HELP erp_db_up 1 if GET /health/db returned 200, else 0",
        "# TYPE erp_db_up gauge",
        f"erp_db_up {snapshot['db_up']}",
        "# HELP erp_migration_current 1 if GET /health/migration returned 200, else 0",
        "# TYPE erp_migration_current gauge",
        f"erp_migration_current {snapshot['migration_current']}",
        "# HELP erp_nginx_requests_total Requests observed in nginx's access log, by status class",
        "# TYPE erp_nginx_requests_total counter",
    ]
    for status_class, count in sorted(snapshot["requests_total"].items()):
        lines.append(f'erp_nginx_requests_total{{status_class="{status_class}"}} {count}')
    lines += [
        "# HELP erp_nginx_requests_429_total Requests rejected by nginx rate limiting",
        "# TYPE erp_nginx_requests_429_total counter",
        f"erp_nginx_requests_429_total {snapshot['requests_429_total']}",
        "# HELP erp_nginx_requests_slow_total "
        f"Requests slower than {SLOW_REQUEST_THRESHOLD_SECONDS}s",
        "# TYPE erp_nginx_requests_slow_total counter",
        f"erp_nginx_requests_slow_total {snapshot['requests_slow_total']}",
        "# HELP erp_auth_failures_total Cumulative LOGIN_FAILURE audit events",
        "# TYPE erp_auth_failures_total counter",
        f"erp_auth_failures_total {snapshot['auth_failures_total']}",
        "# HELP erp_auth_reuse_detected_total Cumulative REFRESH_TOKEN_REUSE_DETECTED audit events",
        "# TYPE erp_auth_reuse_detected_total counter",
        f"erp_auth_reuse_detected_total {snapshot['auth_reuse_detected_total']}",
    ]
    return "\n".join(lines) + "\n"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:  # noqa: D102 -- silence default stderr logging
        pass

    def do_GET(self) -> None:  # noqa: N802 -- required BaseHTTPRequestHandler method name
        if self.path != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        body = _render_metrics().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=9101)
    parser.add_argument("--app-base-url", default=DEFAULT_APP_BASE_URL)
    parser.add_argument("--nginx-log", default=DEFAULT_NGINX_LOG)
    parser.add_argument("--db-url", default=DEFAULT_DB_URL)
    parser.add_argument("--interval", type=float, default=5.0)
    args = parser.parse_args()

    poll_thread = threading.Thread(
        target=_poll_loop,
        args=(args.app_base_url, Path(args.nginx_log), args.db_url, args.interval),
        daemon=True,
    )
    poll_thread.start()

    server = HTTPServer(("127.0.0.1", args.port), _Handler)
    print(f"erp_exporter listening on 127.0.0.1:{args.port}/metrics")
    server.serve_forever()


if __name__ == "__main__":
    main()
