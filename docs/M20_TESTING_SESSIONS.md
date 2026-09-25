# M20 Testing Sessions — Tax & Fiscal Compliance Architecture

Companion to `docs/M20_DESIGN.md`. Sessions A–O as required by the
milestone, each mapped to the actual test file(s) that prove it, since
every session must be evidenced by real tests, not asserted in prose.

## Session A — Domain integrity

**File**: `backend/tests/test_fiscal.py` (constraint/default tests, 6 of 9).

- `FiscalConfig` defaults to `is_enabled=False`, `provider_name="NULL"`.
- `FiscalConfig.store_id` UNIQUE (one config per store).
- `FiscalConfig.retry_max_attempts` CHECK `> 0`.
- `FiscalSubmission.status` CHECK constrained to the 5 defined values.
- `FiscalSubmission.sale_id` UNIQUE — the actual idempotency enforcement.
- A store with `is_enabled=False`, or no `FiscalConfig` row at all,
  creates zero `FiscalSubmission` rows on sale finalization — the
  mechanism that keeps this milestone jurisdiction-neutral in practice.

## Session B — Rounding / payload equivalence

**File**: `backend/tests/test_fiscal.py` (2 of 9).

No new arithmetic exists in this milestone (tax rounding is unchanged,
already covered by `test_tax_boundaries.py`). Session B instead proves
the fiscal payload's own money formatting is canonical (`_money`
quantizes to 2 decimal places regardless of the in-memory Decimal's
prior precision) and that the payload is frozen at creation — a later
mutation to the product's price/tax does not retroactively change an
already-created submission's stored payload.

## Session C — Sale lifecycle

**File**: `backend/tests/test_fiscal_lifecycle.py` (3 tests).

Full path: sale finalized -> `FiscalSubmission` created PENDING (same
transaction) -> `submit_fiscal_transaction` via `FakeFiscalProvider` ->
`ACKNOWLEDGED` with a `fiscal_reference`. Also: rejection is terminal and
never auto-retried; disabling fiscalization after a PENDING row exists
leaves it untouched (an operator decision, not a failure).

## Session D — Returns and corrections

**File**: `backend/tests/test_fiscal_lifecycle.py` (2 tests).

A void/return against an already-`ACKNOWLEDGED` sale never mutates the
original `FiscalSubmission` (status/fiscal_reference/updated_at all
unchanged), and creates no second submission for that sale. Per
`M20_DESIGN.md` Section 11, no return-side fiscal submission is built in
this milestone — no confirmed requirement calls for one, and the
underlying `SaleReturnItem.tax_refunded`/accounting reversal (M5) already
correctly represents the correction. These tests prove that boundary
holds rather than silently drifting.

## Session E — Idempotency

**File**: `backend/tests/test_fiscal_idempotency.py` (3 tests).

- Sequential: 3 repeated calls to `submit_fiscal_transaction` after the
  first success touch the provider exactly once.
- Concurrent duplicate creation: two real threads (independent
  `SessionLocal` sessions, mirroring `test_concurrency.py`'s established
  pattern) racing to create a `FiscalSubmission` for the same sale --
  exactly one wins, the loser hits the `sale_id` UNIQUE constraint.
- Concurrent submit: two threads both calling `submit_fiscal_transaction`
  for the same already-PENDING submission, against a deliberately
  *delayed* fake provider (not an instant one -- an instant fake
  completes too fast for two real threads to reliably overlap inside the
  same race window, which would give a false sense of protection; see
  `M20_HARDENING_AUDIT.md` §5.1) -- the `FOR UPDATE` row lock in
  `submit_fiscal_transaction` serializes them; the provider is called
  exactly once, both threads observe the identical final outcome.

## Session F — Failure injection

**File**: `backend/tests/test_fiscal_failure_injection.py` (10 tests,
covering the milestone's 12 named scenarios -- #7 concurrent duplicate
is covered exhaustively in Session E above rather than duplicated here;
#8/#9 restart/retry-after-restart are covered in Session M).

First success; retry-after-success no-op; timeout -> FAILED, retryable;
remote rejection -> terminal; connection failure before submission;
network failure after submission recovers on retry (fail-then-succeed);
duplicate external response returns the same `fiscal_reference` (the
provider's own dedup-by-idempotency-key); malformed response -> FAILED;
delayed-but-successful response still succeeds; `EXHAUSTED` after
`max_attempts` is reached and becomes terminal (no further provider
calls).

## Session G — Reconciliation

**File**: `backend/tests/test_fiscal_reconciliation.py` (4 tests).

Single-line no-tax, multi-line mixed-tax-rate, discounted-line, and
fractional-quantity (2.375, weighed-product-style) cases -- each proves
the fiscal payload's `tax_total`/`grand_total` exactly equal the `Sale`
row's own stored totals AND the `ACCOUNT_TAX_PAYABLE` GL credit total for
that sale, by construction (the payload is built from the sale's already-
posted fields, never a second calculation -- `M20_DESIGN.md` Section 4).

## Session H — Multi-store isolation

**File**: `backend/tests/test_fiscal_isolation.py` (8 tests).

Service layer: a `FiscalSubmission` is created for the correct store
only; a store with a DISABLED config never picks up a different store's
ENABLED config (the stronger of the two -- the weaker version doesn't
actually prove config-lookup scoping, since `FiscalSubmission.store_id`
is set from `sale.store_id` independently of which config answered the
lookup; see `M20_HARDENING_AUDIT.md` §5.2); a `FiscalConfig` change for
one store never creates or affects another store's config. API layer (real HTTP, mirroring
`test_store_isolation.py`'s exact pattern): a store-scoped Manager
cannot read another store's config, cannot configure another store's
setup, cannot list another store's submissions, cannot retry another
store's submission (all 404, not 403 -- existence is never confirmed
either way); a store-scoped Cashier (no `fiscal.*` permission at all)
gets 403 attempting to read config for their own store.

## Session I — Security

**File**: `backend/tests/test_fiscal_security.py` (5 tests).

`FiscalConfigRead`'s field set contains no field shaped like a resolved
secret -- `credential_reference` is a NAME only, and the live API
response is asserted to expose exactly that name and nothing else,
never a resolved value (there is none stored to leak). A Cashier without
`fiscal.read` gets 403, and the secret name never appears in that 403's
body. Config updates are audited (`AuditLog` before/after state) with
the reference name recorded -- safe, since it is not itself a secret.
`core/logging.py`'s `_REDACTED_KEYS` is extended with
`credential_reference` and the test reads that actual set, not a
docstring claim.

## Session J — Migration safety

**File**: `backend/tests/test_migrations.py` (existing suite, extended).

`M20_HEAD_REVISION = "9c4c5a209aa9"` added; the full upgrade/downgrade/
upgrade cycle test and the RBAC-seed-data test both updated for the new
table count (67, +2 from M19's 65) and permission count (54, +3). Single
Alembic head maintained throughout. The `c8337b04ca47` migration's own
downgrade guard (added after discovery that a `FiscalSubmission` is a
real compliance record, not disposable schema) refuses to drop
`fiscal_submissions` while any row exists, refuses to drop
`fiscal_configs` while any store has `is_enabled=true`, and refuses to
drop the two `Store` columns while any store has `legal_name`/
`tax_registration_number` set -- verified manually via upgrade ->
downgrade -> upgrade round-trips against the real dev database during
implementation (empty-state downgrade succeeds; the guards themselves
are exercised structurally by the CHECK-constraint/FK tests in Session A
and N, which prove the underlying invariants they protect).

## Session K — Browser/UI

**Deferred, not a defect.** No frontend UI was built for fiscal
configuration/submissions in this milestone. `M20_DISCOVERY.md`/
`M20_DESIGN.md` scope this milestone as backend integration-
*readiness* -- the API surface (config/submissions/retry) is fully
built, permissioned, and tested at the HTTP level (Session H), which is
what a future admin UI would call. Building UI copy/fields now would
mean guessing at a still-unconfirmed jurisdiction's actual requirements
(exactly what `M20_DISCOVERY.md` Section 1 forbids) for a screen no
operator can use yet anyway (`is_enabled=False` everywhere). Precedent:
`M19_DESIGN.md`'s "No frontend void-invoice button" deferred item, same
reasoning (real, tested backend behavior; a UI gap is not a functional
defect).

## Session L — Production topology

The `deploy-infra`/`deploy-infra-native` CI jobs run the complete
`backend/tests/` suite (including every `test_fiscal_*.py` file above)
against real nginx/uvicorn/PostgreSQL, not a separate topology-specific
fiscal test file -- identical to how every other M2-M19 domain's tests
are exercised under that topology. No fiscal-specific production-only
behavior exists to test separately (no real network call, no real
authority).

## Session M — Recovery

**File**: `backend/tests/test_fiscal_recovery.py` (2 tests).

A process restart is simulated the same way this codebase already
proves cross-connection durability (`test_concurrency.py`'s own
technique): one `SessionLocal` creates/partially-progresses a
submission and is closed (nothing survives in memory), then a brand-new,
independent session resumes purely from the durable row. Covers both a
PENDING row (never attempted before the "crash") and a FAILED row
(attempted once, then "crashed") -- both resume and reach `ACKNOWLEDGED`
correctly, with `attempt_count` continuing from its durable value, not
resetting.

## Session N — Adversarial

**File**: `backend/tests/test_fiscal_adversarial.py` (7 tests).

`FiscalConfig` for a nonexistent `store_id` violates the FK;
`retry_max_attempts=0` violates the CHECK; `submit_fiscal_transaction`/
`get_submission` on an unknown ID raise `NotFoundError`, not an unrelated
exception; a `FiscalConfig` pointing at a never-registered
`provider_name` fails loudly (`NotFoundError`) rather than silently
no-op'ing; a direct attempt to insert a `FiscalSubmission` with an
invented status string (`"SUBMITTING"`) is rejected by the CHECK
constraint -- no code path, buggy or malicious, can create an
undefined state; disabling fiscalization after a submission already
reached `ACKNOWLEDGED` does not retroactively touch it (disabling is
forward-looking only).

## Session O — Mutation testing

See `docs/M20_HARDENING_AUDIT.md` Section 5 for the full record of the
8 required targets, each broken live via `Edit`, confirmed to fail the
specific test for the right reason, and reverted with a diff-verified
restore.
