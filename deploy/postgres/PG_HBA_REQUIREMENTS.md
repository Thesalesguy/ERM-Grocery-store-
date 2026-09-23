# PostgreSQL client authentication (pg_hba.conf) — production requirement

M18 discovery finding: this project has no project-owned `pg_hba.conf`
(or Docker equivalent) anywhere in the repository. Per this milestone's
own instruction — "if the project intentionally does not ship a
universal pg_hba.conf, document the exact production requirement
instead of inventing a distribution-specific configuration" — that is
what this file does. **This is intentional, not an oversight**:
`pg_hba.conf`'s real location, format, and even whether it's a plain
file at all differ by deployment model (a bind-mounted file inside the
`postgres:16` Docker image vs. `/etc/postgresql/16/main/pg_hba.conf`
managed by the OS package on a native install), so a single checked-in
file would either be wrong for one of the two deployment models this
project actually documents, or would silently drift from whichever one
nobody is testing against. The requirement below is behavioral, not a
file to copy in.

## The requirement

**Client authentication for any TCP/host connection must never be
`trust` or unauthenticated.** `scram-sha-256` (PostgreSQL's strongest
built-in password method) or stronger is required. This one rule
matters more than the exact file contents, because `trust` silently
defeats every other control in this project (the `erp_app`/`erp_user`
password rotation, the fail-closed secret validator in
`backend/app/core/config.py`, `docker-compose.prod.yml`'s
`${...:?required}` password enforcement) — none of them mean anything
if PostgreSQL itself will authenticate anyone as anyone with no
password check.

## What each deployment model actually does today (verified, not assumed)

- **Docker Compose** (`docker-compose.prod.yml`, the one real production
  path this project documents): uses the stock `postgres:16` image with
  no `POSTGRES_HOST_AUTH_METHOD` override. The official image has
  defaulted host-connection auth to `scram-sha-256` since well before
  the `postgres:16` tag existed (it previously defaulted to `trust` with
  a printed security warning; that default changed years before this
  project's minimum supported image tag). **Operators must never set
  `POSTGRES_HOST_AUTH_METHOD=trust`** to work around a connection
  problem — that disables authentication entirely rather than fixing
  whatever the real issue is (almost always a wrong password or a
  network-reachability problem, not an auth-method problem).
- **Native install** (this project's own CI now provisions one — see
  `.github/workflows/ci.yml`'s `deploy-infra-native` job, and this is
  also how this development sandbox itself runs PostgreSQL): Ubuntu's
  `postgresql-16` apt package ships `peer` for local Unix-socket
  connections and `scram-sha-256` for TCP connections to
  `127.0.0.1`/`::1` by default, out of the box, with no configuration
  needed. Confirmed directly against the real installed
  `pg_hba.conf` in this project's own development/CI environment (never
  `trust`).

Both of this project's real deployment models are therefore already
`scram-sha-256`-by-default without any project-specific `pg_hba.conf`
being necessary — the requirement above exists to state explicitly what
must remain true (and be checked first) if that ever changes, e.g. a
different base image, a different OS, or a managed/cloud Postgres
provider with its own default.

## Compensating control already in place (defense in depth)

Network exposure is the layer this project *does* actively control, and
already does correctly: `docker-compose.prod.yml`'s `db` service
publishes no port (`ports: !reset []` — reachable only on the internal
Compose network), and `deploy/postgres/postgresql.prod.conf.snippet`
requires `listen_addresses = 'localhost'`. `pg_hba.conf`'s auth method
is a second, independent layer beneath that network boundary, not the
only one.

## Production checklist

Before any non-development deployment, an operator must confirm (a
one-line check, not a file to author):

```
psql <connection> -c "SELECT count(*) FROM pg_hba_file_rules WHERE auth_method IN ('trust', 'password');"
```

The result must be `0`. `password` (unencrypted-over-the-wire) is
listed alongside `trust` here because this project's own network
posture (loopback/private-network-only, TLS terminated at nginx, never
at the database) makes `scram-sha-256` the floor, not `password`, for
any credential PostgreSQL itself checks.
