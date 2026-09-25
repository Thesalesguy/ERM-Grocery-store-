# M20 Hardening Audit — Tax & Fiscal Compliance Architecture

Companion to `docs/M20_DESIGN.md` and `docs/M20_TESTING_SESSIONS.md`.

## 1. Jurisdiction compliance re-check

Re-verified at the end of implementation, not just at discovery: no
authority name, payload shape, signing mechanism, numbering rule, or
tax-category taxonomy was invented anywhere in the code written for this
milestone. `FiscalConfig.is_enabled` defaults to `False` and is never
set to `True` by any migration or seed data — every store, in every
environment this code has ever run against, has fiscalization off by
default. The only `provider_name` ever registered outside tests is
`"NULL"` (`NullFiscalProvider`, which raises if ever actually called).

## 2. Security re-check

- Grepped the full diff for `credential_reference` usage: it is read,
  written, and returned by the API as a plain string field — never
  resolved, never used to construct an HTTP header or auth token
  anywhere in this codebase (no such call exists at all — see Section 3).
- `core/logging.py`'s `_REDACTED_KEYS` extended with
  `credential_reference` (verified by `test_logging_redaction_list_covers_credential_reference`).
- `FiscalConfigRead`'s field set contains no field that could carry a
  resolved secret (verified by
  `test_fiscal_config_read_schema_has_no_field_that_could_carry_a_resolved_secret`).
- `FiscalConfig` mutations are audited via the existing `AuditLog`
  before/after mechanism, identical to every other config-mutation path
  in this codebase.

## 3. What does NOT exist (explicit, verified)

Confirms `M20_DISCOVERY.md`/`M20_DESIGN.md`'s non-goals held through
implementation:

- No real tax-authority HTTP client. `grep -rn "requests\.\|httpx\." app/modules/fiscal/`
  returns nothing — `FiscalProvider.submit` is a plain Python method; no
  network call is made by any code shipped in this milestone.
- No cryptographic signing anywhere in `app/modules/fiscal/`.
- No background worker/queue process added — `run_pending_fiscal_submission_in_background`
  is a `BackgroundTasks` callback (in-process, request-scoped trigger),
  not a new daemon.
- No new currency/multi-tax/tax-inclusive-pricing support.
- No frontend UI (Session K, deferred — see `M20_TESTING_SESSIONS.md`).

## 4. Migration safety re-check

- `c8337b04ca47` (schema) and `9c4c5a209aa9` (RBAC seed) verified as the
  single Alembic head (`python -m alembic heads` → one line).
- Full upgrade → downgrade (2 steps) → upgrade round-trip performed
  manually against the real dev database during implementation;
  byte-for-byte schema recovery confirmed both directions.
- Downgrade guards added (not part of the original migration draft, added
  after recognizing a `FiscalSubmission` is a real compliance record):
  refuses to drop `fiscal_submissions` while any row exists; refuses to
  drop `fiscal_configs` while any store has `is_enabled=true`; refuses to
  drop `stores.legal_name`/`tax_registration_number` while any store has
  either set (mirrors `e0d2359bb08a`'s proven pattern exactly). Actually
  tripped, not just structurally reviewed: with a real
  `FiscalSubmission` row present (left over from concurrency/recovery
  test runs, which — like `test_concurrency.py` — deliberately commit
  real rows rather than rolling back), `alembic downgrade -2` was run
  against the real dev database and confirmed to raise exactly the
  `fiscal_submissions`-guard exception, leaving the schema at head
  (unchanged). The rows were then cleared and the same downgrade
  succeeded cleanly, followed by a re-upgrade back to head — the
  guard blocks exactly when it should and only then.
- `tests/test_migrations.py` updated: `M20_HEAD_REVISION`, table count
  67 (+2 from M19's 65), permission count 54 (+3).

## 5. Mutation testing (Session O) — 8 required targets

Methodology identical to M19 (`M19_HARDENING_AUDIT.md` §5): each target
broken live via `Edit`, confirmed to fail the *specific* test for the
stated reason (not an unrelated error), reverted via `cp` from a
file-backup, and the revert diff-verified byte-identical before moving
to the next target. All 8 were caught; none required documentation as a
"structurally impossible" pass.

| # | Target (mission category) | Mutation | Test(s) that caught it | Result |
|---|---|---|---|---|
| 1 | Tax calculation (as reflected in the fiscal payload) | `_money()` in `fiscal/service.py`: removed `.quantize(Decimal("0.01"))`, returning raw `str(value)` | `test_fiscal_payload_totals_exactly_match_sale_totals` | Caught — `'0' == '0.00'` assertion failure |
| 2 | Duplicate prevention / retry-terminal-state guard | `submit_fiscal_transaction`: `if is_terminal_fiscal_status(...)` replaced with `if False` | `test_rejection_is_terminal_and_never_auto_retried`, `test_retry_after_success_is_a_no_op`, `test_remote_rejection_is_terminal` | Caught — all 3 failed (provider called a 2nd time) |
| 3 | Fiscal idempotency under concurrency | `submit_fiscal_transaction`: removed `.with_for_update()` from the submission SELECT | `test_concurrent_submit_attempts_on_the_same_submission_resolve_consistently` | Caught, but only after the test was strengthened — see Section 5.1 below |
| 4 | Duplicate prevention / jurisdiction-neutral gate | `create_fiscal_submission_if_enabled`: `if config is None or not config.is_enabled` weakened to `if config is None` | `test_disabled_fiscal_config_creates_no_submission` | Caught |
| 5 | Store isolation (service layer, config lookup) | `create_fiscal_submission_if_enabled`: config lookup changed from `.where(FiscalConfig.store_id == sale.store_id)` to an unscoped `select(FiscalConfig).scalars().first()` | `test_disabled_store_config_lookup_never_falls_through_to_another_stores_enabled_config` (new test, added because of this mutation — see Section 5.2) | Caught |
| 6 | Store isolation (API layer) | `fiscal.py`'s `_check_store_access` body replaced with `pass` | All 4 `test_store_scoped_manager_cannot_*` tests in `test_fiscal_isolation.py` | Caught — all 4 failed |
| 7 | Fiscal rejection handling | `submit_fiscal_transaction`: `submission.status = outcome.status` hardcoded to `"ACKNOWLEDGED"` | `test_rejection_is_terminal_and_never_auto_retried`, `test_remote_rejection_is_terminal` | Caught |
| 8 | Retry/recovery state transition (EXHAUSTED threshold) | `attempt_count >= max_attempts` changed to `attempt_count > max_attempts` (off-by-one) | `test_exhausted_after_max_attempts_becomes_terminal` | Caught |

### 5.1 A genuine finding, not a false pass (target #3)

The first version of the concurrency test used `FakeFiscalProvider(behavior="success")`
— an effectively instant fake. With the `with_for_update()` lock removed,
this test still **passed 5/5 runs**, not because the lock is
unnecessary, but because two real threads' natural scheduling (Python
GIL + fast localhost psycopg round-trips) rarely land inside the same
few-millisecond race window without a lock artificially widening it. A
manual reproduction (`python3 -c` script, not part of the test suite)
using a `behavior="delayed", delay_seconds=0.3` provider confirmed the
real defect the lock protects against: **without the lock, the provider
was called twice** (`len(fake.calls) == 2`), even though both calls
happened to return the same `fiscal_reference` (because the fake, like a
real compliant authority, deduplicates by the payload's own
`idempotency_key` — the second line of defense described in
`M20_DESIGN.md` Section 3). The test was fixed to use a delayed
provider (`delay_seconds=0.2`) so the race window is wide enough for two
real threads to reliably overlap, and re-verified: it now fails 3/3 runs
with the lock removed and passes 3/3 with it present. This mirrors
`M19_HARDENING_AUDIT.md`'s own documented pattern of an instant
mutation-testing surprise leading to a **stronger** test, not a weaker
audit — the lock's necessity was proven by demonstration, not merely
asserted.

### 5.2 A genuine finding, not a false pass (target #5)

The original store-isolation service-layer test
(`test_fiscal_submission_created_for_correct_store_only`) did not catch
the unscoped-config-lookup mutation, because `FiscalSubmission.store_id`
is set directly from `sale.store_id` regardless of which `FiscalConfig`
row answered the lookup — the submission's own store attribution stayed
correct even when the WRONG store's config (provider/retry policy)
would have been silently applied. A new, stronger test
(`test_disabled_store_config_lookup_never_falls_through_to_another_stores_enabled_config`)
was added: store A's config is `is_enabled=False`, store B's is
`is_enabled=True`; a sale for store A must create zero submissions. This
is the actual observable consequence of a cross-store config leak (a
disabled store would wrongly start fiscalizing), and it does fail
without the fix.

## 5.3 A real CI-only failure, not a mutation-testing finding

Independent of mutation testing: the first CI run against the full M20
implementation (`f88f814`) failed the `backend` job. Several new tests
(`test_sequential_duplicate_submit_calls_are_a_no_op_after_first_success`,
`test_store_a_config_change_does_not_affect_store_b`, and others) called
`fiscal_service.upsert_config(..., updated_by=1)` with a **hardcoded
literal user ID** instead of a real, test-created user's ID.
`FiscalConfig.updated_by` is a real foreign key to `users.id`. This
passed locally because the local dev database had accumulated a real
user with `id=1` from many prior manual verification runs across this
entire multi-milestone session — but a fresh CI database has no such
row, so every one of these inserts failed with `ForeignKeyViolation:
Key (updated_by)=(1) is not present in table "users"`. Fixed by
creating a real user in each affected test and passing its actual
`.id` instead of a literal. Re-verified locally against the same dev
database (which still has a real `id=1` row) to confirm the fix doesn't
merely coincidentally work — the tests no longer reference `1` at all.
This is exactly the class of environment-divergence bug the milestone's
own regression discipline (real CI verification, never trusting local
state alone) exists to catch, and it did.

## 5.4 A pre-existing flaky deploy test, fixed (not this milestone's domain, but blocking its CI gate)

`deploy-infra-native`'s `test_monitoring_exporter_itself_going_down_fires_an_alert`
(in `deploy/tests/test_failure_injection_disaster_recovery.py`, a file no
M20 commit touches otherwise) failed twice in a row on real CI on this
branch (`f88f814`'s CI parent commit, then again on a confirming re-run
of `9a3930f`) with an identical `httpx.ConnectError: [Errno 111]
Connection refused`, already documented as a known startup-timing race
in `M19_HARDENING_AUDIT.md` Session Q. Root cause: the test's own
`_target_up()` poll helper already retried through Prometheus's
"still loading TSDB" 503 response (a comment in the code documents that
earlier fix), but let an `httpx.ConnectError` propagate unhandled and
crash the poll loop when Prometheus's process hadn't opened its
listening socket at all yet -- the same startup race, one step earlier,
under CI's higher load. Per this engagement's established CI-failure
protocol (one confirming re-run only; a second identical failure is
real and must be fixed, not re-run again), fixed by catching
`httpx.TransportError` in the poll helper and treating it the same as
the already-handled 503 case (retry, don't crash). Verified: 5/5 passes
locally against the real `prometheus`/`prometheus-alertmanager`
binaries for the specific test, plus a full re-run of the file (7/7
passed). This is not a fiscal/tax-domain fix and is called out
separately from Sections 5.1-5.3 for that reason, but it was necessary
to reach the milestone's own "CI completely green job-by-job" gate.

## 6. Reconciliation with M19's closure

Confirmed the M19 fiscal-unrelated final state remains untouched: no
M20 commit modifies any file under `app/modules/purchasing/`,
`app/modules/ap/`, or `app/modules/sales/service.py` beyond the single,
narrow addition documented in `M20_DESIGN.md` Section 9 (the one
`fiscal_service.create_fiscal_submission_if_enabled(db, sale)` call
inside `finalize_sale`, after the existing accounting-post step).
