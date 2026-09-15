# M13 Phase 6: monitoring stack systemd units

These units run the monitoring stack as native host services (not
containers — the application itself is containerized via
`docker-compose.prod.yml`, but Prometheus/Alertmanager/the exporters are
simple enough, and few enough, that a second container layer for them
would be more moving parts than the "prefer simple open-source
components" objective calls for; see `docs/M13_DESIGN.md` Section 6).

All five units were exercised as real native processes during Phase 6
development (see `deploy/tests/test_monitoring.py`) — that proves the
process invocations, flags, and metrics/alert pipeline. The unit files
themselves (their `User=`/`ReadWritePaths=`/dependency ordering) are
DOCUMENTED, following the same systemd hardening conventions used
elsewhere, but not booted under a real systemd instance in this sandbox
(no init system here to test against) — verify with `systemctl
daemon-reload && systemctl start erp-node-exporter` etc. on the actual
target host before relying on them.

## One-time setup, in order

```bash
# 1. A dedicated, unprivileged system account shared by every monitoring
#    service. One account (not five) because they only need to share
#    read access to nginx's log and the textfile-collector directory —
#    a single group boundary is simpler to reason about than five.
sudo useradd --system --no-create-home --shell /usr/sbin/nologin erp-monitor
sudo usermod -aG adm erp-monitor   # read access to /var/log/nginx/*.log

# 2. The shared textfile-collector directory (backup_offbox.sh writes
#    here; node_exporter reads it). Group-writable so both the
#    erp-monitor account and whatever account runs the backup cron job
#    (commonly root, via a system cron entry) can write the same file.
sudo mkdir -p /var/lib/erp/textfile_collector
sudo chown root:erp-monitor /var/lib/erp/textfile_collector
sudo chmod 2775 /var/lib/erp/textfile_collector   # setgid: new files inherit the group
sudo mkdir -p /var/lib/erp/prometheus-data /var/lib/erp/alertmanager-data
sudo chown erp-monitor:erp-monitor /var/lib/erp/prometheus-data /var/lib/erp/alertmanager-data

# 3. The least-privilege database role (idempotent; safe to re-run):
sudo -u postgres psql -d erp_prod -f deploy/scripts/bootstrap_monitoring_role.sql
# then rotate its placeholder password as the script's own comment says:
sudo -u postgres psql -d erp_prod -c "ALTER ROLE erp_monitor WITH PASSWORD '<long random value>';"

# 4. /etc/erp/monitoring.env (root-owned, mode 600) — consumed by
#    erp-postgres-exporter.service and erp-exporter.service via
#    EnvironmentFile=:
#
#   DATA_SOURCE_NAME=postgresql://erp_monitor:<password>@localhost:5432/erp_prod?sslmode=disable
#   ERP_MONITOR_DB_URL=postgresql://erp_monitor:<password>@localhost:5432/erp_prod

# 5. Install and enable:
sudo cp deploy/systemd/erp-*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now erp-node-exporter erp-postgres-exporter erp-exporter \
    erp-alertmanager erp-prometheus
```

## Why not DynamicUser=

`node_exporter`'s textfile-collector directory must be writable by both
node_exporter's own service account and whatever separately-scheduled
process runs `backup_offbox.sh` (a cron job, commonly running as root).
systemd's `DynamicUser=yes` allocates a new UID on every service start,
which cannot be shared with a process outside that unit — so a fixed,
named `erp-monitor` system account with a real, persistent group is used
instead, with the shared directory's group and setgid bit doing the
sharing.

## Certificate/secret handling

None of these five services need TLS certificates or application
secrets (`SECRET_KEY`, `ERP_APP_PASSWORD`) — they only need the
`erp_monitor` database credential (read-only, `pg_monitor` role) and, for
`erp_exporter`, read access to nginx's own log file. Neither Prometheus's
nor Alertmanager's own web UI is exposed outside loopback in this
topology (`--web.listen-address=127.0.0.1:...` on every unit) — an
operator reaches them via SSH port-forwarding, never a public URL,
consistent with "PostgreSQL must NOT be unnecessarily exposed publicly"
extended to every other internal-only service in this topology.
