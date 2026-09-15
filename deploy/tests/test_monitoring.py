"""M13 Phase 6: live tests for the monitoring/alerting stack -- proves
alerts actually fire and reach Alertmanager when the underlying condition
is real, not just that the YAML parses. See docs/M13_DESIGN.md Section 6
and deploy/prometheus/ for the full stack this exercises.

Runtime note: the real deploy/prometheus/alerts.yml uses production-
realistic `for:` durations (1m/2m/5m) deliberately, so a flapping
condition doesn't page anyone. That full timing was proven end-to-end
interactively during Phase 6 development (stopped PostgreSQL, watched
ERPDatabaseDown go pending -> firing -> delivered to Alertmanager ->
resolved after PostgreSQL came back, matching its `for: 1m`; see
docs/M13_HARDENING_AUDIT.md). Re-running that full wait on every
automated test invocation would make this suite too slow to be useful, so
the fixture below loads a COPY of the real alerts.yml with only the
`for:` values shortened -- expressions and every other field are
byte-for-byte identical to the production file. This still proves the
alert EXPRESSIONS and the full Prometheus -> Alertmanager delivery
pipeline are correct against real, live data; it does not re-prove the
exact production `for:` timing on every run (that part is DOCUMENTED as
interactively tested once, not re-tested automatically).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import pytest
import yaml
from conftest import DEPLOY_DIR, SUDO, run, wait_for

PROMETHEUS_PORT = 19090
ALERTMANAGER_PORT = 19093
NODE_EXPORTER_PORT = 19100
POSTGRES_EXPORTER_PORT = 19187
ERP_EXPORTER_PORT = 19101

MONITOR_DB_URL = "postgresql://erp_monitor:erp_monitor_test_password@localhost:5432/erp_dev"


def _wait_for_target_up(prom_url: str, job: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"{prom_url}/api/v1/targets", timeout=3.0)
            for t in resp.json()["data"]["activeTargets"]:
                if t["labels"].get("job") == job:
                    last = t["health"]
                    if last == "up":
                        return
        except Exception as exc:  # noqa: BLE001 -- retry loop
            last = exc
        time.sleep(0.5)
    raise RuntimeError(f"target {job!r} never became up (last: {last!r})")


def _alert_states(prom_url: str) -> dict[str, str]:
    resp = httpx.get(f"{prom_url}/api/v1/alerts", timeout=3.0)
    return {a["labels"]["alertname"]: a["state"] for a in resp.json()["data"]["alerts"]}


def _wait_for_alert_state(prom_url: str, alertname: str, state: str, timeout: float = 40.0) -> None:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = _alert_states(prom_url).get(alertname)
        if last == state:
            return
        time.sleep(1.0)
    raise AssertionError(f"{alertname} never reached state={state!r} (last seen: {last!r})")


def _wait_for_alert_absent(prom_url: str, alertname: str, timeout: float = 40.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if alertname not in _alert_states(prom_url):
            return
        time.sleep(1.0)
    raise AssertionError(f"{alertname} never cleared")


def _write_backup_status(path: Path, *, success: bool) -> None:
    path.write_text(
        "# HELP erp_backup_last_run_timestamp_seconds Unix time of the last backup attempt\n"
        "# TYPE erp_backup_last_run_timestamp_seconds gauge\n"
        f"erp_backup_last_run_timestamp_seconds {int(time.time())}\n"
        "# HELP erp_backup_last_success 1 if the last backup attempt succeeded, else 0\n"
        "# TYPE erp_backup_last_success gauge\n"
        f"erp_backup_last_success {1 if success else 0}\n"
    )


@pytest.fixture(scope="module")
def monitoring_stack(production_stack):
    """Depends on production_stack (real nginx + uvicorn already up) and
    adds node_exporter, postgres_exporter, erp_exporter, Prometheus, and
    Alertmanager -- the complete M13 monitoring topology, all real
    processes talking to the real application and the real database."""
    work_dir = Path(tempfile.mkdtemp(prefix="erp_m13_monitoring_"))
    textfile_dir = work_dir / "textfile_collector"
    textfile_dir.mkdir()
    prom_data_dir = work_dir / "prom_data"
    prom_data_dir.mkdir()
    am_data_dir = work_dir / "am_data"
    am_data_dir.mkdir()

    # Idempotent: creates the least-privilege erp_monitor role if it
    # doesn't already exist (see deploy/scripts/bootstrap_monitoring_role.sql
    # for why a dedicated pg_monitor-only role is used instead of erp_user).
    run(
        [
            *SUDO,
            "-u",
            "postgres",
            "psql",
            "-d",
            "erp_dev",
            "-v",
            "ON_ERROR_STOP=1",
            "-f",
            str(DEPLOY_DIR / "scripts" / "bootstrap_monitoring_role.sql"),
        ]
    )
    run(
        [
            *SUDO,
            "-u",
            "postgres",
            "psql",
            "-d",
            "erp_dev",
            "-c",
            "ALTER ROLE erp_monitor WITH PASSWORD 'erp_monitor_test_password'",
        ]
    )

    real_alerts = (DEPLOY_DIR / "prometheus" / "alerts.yml").read_text()
    test_alerts = re.sub(r"for: \d+m\b", "for: 3s", real_alerts)
    (work_dir / "alerts.yml").write_text(test_alerts)

    (work_dir / "prometheus.yml").write_text(
        f"""
global:
  scrape_interval: 2s
  evaluation_interval: 2s
rule_files:
  - "{work_dir}/alerts.yml"
alerting:
  alertmanagers:
    - static_configs:
        - targets: ["127.0.0.1:{ALERTMANAGER_PORT}"]
scrape_configs:
  - job_name: "erp_exporter"
    static_configs:
      - targets: ["127.0.0.1:{ERP_EXPORTER_PORT}"]
  - job_name: "node_exporter"
    static_configs:
      - targets: ["127.0.0.1:{NODE_EXPORTER_PORT}"]
  - job_name: "postgres_exporter"
    static_configs:
      - targets: ["127.0.0.1:{POSTGRES_EXPORTER_PORT}"]
"""
    )
    (work_dir / "alertmanager.yml").write_text(
        """
route:
  receiver: default
  group_by: ["alertname"]
  group_wait: 1s
  group_interval: 1s
  repeat_interval: 1h
receivers:
  - name: default
"""
    )

    procs: dict[str, subprocess.Popen] = {}
    logs: dict[str, object] = {}

    def _start(name: str, cmd: list[str], **kw) -> None:
        log = open(work_dir / f"{name}.log", "w")
        logs[name] = log
        procs[name] = subprocess.Popen(
            cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **kw
        )

    try:
        _start(
            "node_exporter",
            [
                "/usr/bin/prometheus-node-exporter",
                f"--web.listen-address=127.0.0.1:{NODE_EXPORTER_PORT}",
                f"--collector.textfile.directory={textfile_dir}",
            ],
        )
        _start(
            "postgres_exporter",
            [
                "/usr/bin/prometheus-postgres-exporter",
                f"--web.listen-address=127.0.0.1:{POSTGRES_EXPORTER_PORT}",
            ],
            env={**os.environ, "DATA_SOURCE_NAME": f"{MONITOR_DB_URL}?sslmode=disable"},
        )
        _start(
            "erp_exporter",
            [
                sys.executable,
                str(DEPLOY_DIR / "prometheus" / "erp_exporter.py"),
                "--port",
                str(ERP_EXPORTER_PORT),
                "--app-base-url",
                "http://127.0.0.1:8000",
                "--nginx-log",
                "/var/log/nginx/access.log",
                "--db-url",
                MONITOR_DB_URL,
                "--interval",
                "1",
            ],
        )
        _start(
            "alertmanager",
            [
                "/usr/bin/prometheus-alertmanager",
                f"--config.file={work_dir}/alertmanager.yml",
                f"--web.listen-address=127.0.0.1:{ALERTMANAGER_PORT}",
                f"--storage.path={am_data_dir}",
                "--cluster.listen-address=",
            ],
        )
        _start(
            "prometheus",
            [
                "/usr/bin/prometheus",
                f"--config.file={work_dir}/prometheus.yml",
                f"--storage.tsdb.path={prom_data_dir}",
                f"--web.listen-address=127.0.0.1:{PROMETHEUS_PORT}",
            ],
        )

        wait_for(f"http://127.0.0.1:{PROMETHEUS_PORT}/-/healthy", verify=False)
        wait_for(f"http://127.0.0.1:{ALERTMANAGER_PORT}/-/healthy", verify=False)
        for job in ("erp_exporter", "node_exporter", "postgres_exporter"):
            _wait_for_target_up(f"http://127.0.0.1:{PROMETHEUS_PORT}", job)

        yield {
            "prom_url": f"http://127.0.0.1:{PROMETHEUS_PORT}",
            "am_url": f"http://127.0.0.1:{ALERTMANAGER_PORT}",
            "textfile_dir": textfile_dir,
        }
    finally:
        for proc in procs.values():
            proc.terminate()
        for proc in procs.values():
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        for log in logs.values():
            log.close()
        # A failure-injection test below stops the real postgresql
        # service -- guarantee it's back up before this fixture tears
        # down, regardless of whether that test passed, so later test
        # modules never inherit a dead cluster.
        subprocess.run(
            [*SUDO, "service", "postgresql", "start"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        shutil.rmtree(work_dir, ignore_errors=True)


def test_all_monitoring_targets_are_scraped_successfully(monitoring_stack):
    resp = httpx.get(f"{monitoring_stack['prom_url']}/api/v1/targets", timeout=5.0)
    targets = resp.json()["data"]["activeTargets"]
    healths = {t["labels"]["job"]: t["health"] for t in targets}
    assert healths["erp_exporter"] == "up"
    assert healths["node_exporter"] == "up"
    assert healths["postgres_exporter"] == "up"


def test_stopping_postgres_fires_erp_database_down_and_reaches_alertmanager(monitoring_stack):
    """The strongest test in this file: kills the real PostgreSQL service
    (not a mock, not a flag flip) and proves the alert travels the whole
    real path -- app health check -> exporter -> Prometheus rule
    evaluation -> Alertmanager -- then proves it clears on recovery."""
    prom_url = monitoring_stack["prom_url"]
    am_url = monitoring_stack["am_url"]

    subprocess.run([*SUDO, "service", "postgresql", "stop"], check=True, capture_output=True)
    try:
        _wait_for_alert_state(prom_url, "ERPDatabaseDown", "firing", timeout=40.0)
        resp = httpx.get(f"{am_url}/api/v2/alerts", timeout=5.0)
        states = {a["labels"].get("alertname"): a["status"]["state"] for a in resp.json()}
        assert states.get("ERPDatabaseDown") == "active"
    finally:
        subprocess.run([*SUDO, "service", "postgresql", "start"], check=True, capture_output=True)
        wait_for("http://127.0.0.1:8000/health/db", verify=False, timeout=20.0)

    _wait_for_alert_absent(prom_url, "ERPDatabaseDown", timeout=40.0)


def test_a_failed_backup_status_file_fires_erp_backup_failed(monitoring_stack):
    """Proves the node_exporter textfile-collector bridge from
    backup_offbox.sh's status file (M13 Phase 5) through to a firing,
    delivered alert -- without needing to run a real backup cycle."""
    prom_url = monitoring_stack["prom_url"]
    am_url = monitoring_stack["am_url"]
    status_file = monitoring_stack["textfile_dir"] / "backup_status.prom"

    _write_backup_status(status_file, success=False)
    try:
        _wait_for_alert_state(prom_url, "ERPBackupFailed", "firing", timeout=40.0)
        resp = httpx.get(f"{am_url}/api/v2/alerts", timeout=5.0)
        states = {a["labels"].get("alertname"): a["status"]["state"] for a in resp.json()}
        assert states.get("ERPBackupFailed") == "active"
    finally:
        _write_backup_status(status_file, success=True)

    _wait_for_alert_absent(prom_url, "ERPBackupFailed", timeout=40.0)


def test_alert_rules_file_is_syntactically_valid():
    result = subprocess.run(
        ["promtool", "check", "rules", str(DEPLOY_DIR / "prometheus" / "alerts.yml")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_prometheus_config_is_syntactically_valid():
    result = subprocess.run(
        ["promtool", "check", "config", str(DEPLOY_DIR / "prometheus" / "prometheus.yml")],
        capture_output=True,
        text=True,
        cwd=str(DEPLOY_DIR / "prometheus"),
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_every_alert_has_a_severity_and_an_actionable_annotation():
    """The task's own requirement: 'do not create alerts that are
    impossible to act upon' -- every rule must name a severity and a
    concrete action, not a vague 'investigate'."""
    data = yaml.safe_load((DEPLOY_DIR / "prometheus" / "alerts.yml").read_text())
    rule_count = 0
    for group in data["groups"]:
        for rule in group["rules"]:
            rule_count += 1
            assert rule["labels"].get("severity") in ("critical", "warning", "info"), (
                f"{rule['alert']} is missing a valid severity label"
            )
            action = rule.get("annotations", {}).get("action", "")
            assert len(action) > 10, f"{rule['alert']} has no actionable 'action' annotation"
    assert rule_count >= 10, "expected a meaningful number of alert rules, found fewer"
