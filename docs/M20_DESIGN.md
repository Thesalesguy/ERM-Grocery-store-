# M20 Design — Tax & Fiscal Compliance Architecture and Integration Readiness

Companion to `docs/M20_DISCOVERY.md`. Per that document's Section 1
conclusion — **target tax jurisdiction is not established in the current
project source** — everything below is jurisdiction-neutral
infrastructure: a real, tested integration *boundary*, not an
integration. No authority name, payload shape, signing mechanism, or
numbering rule is assumed anywhere in this design.

## 0. What already works and is left alone

Per `M20_DISCOVERY.md` §2, the tax **domain** model (`TaxRate`, per-line
frozen `tax_rate_id`/`tax_amount`, `ROUND_HALF_UP` Decimal arithmetic,
`ACCOUNT_TAX_PAYABLE` GL posting, return/void tax reversal) is correct
and fully tested today. M20 does not touch `app/modules/tax/`,
`app/modules/sales/service.py`'s tax calculation, or any accounting
posting logic. This milestone is additive: a new `app/modules/fiscal/`
module that observes a sale *after* it is already correctly taxed and
posted, and optionally forwards it toward an authority — never a second,
competing calculation engine (Phase 4's explicit warning).

## 1. Data model

Two new tables, in a new module `app/modules/fiscal/`. No existing table
is altered except one addition to `Store` (see §1.3).

### 1.1 `fiscal_configs` — the configuration boundary, one row per store

```python
class FiscalConfig(TimestampMixin, Base):
    __tablename__ = "fiscal_configs"
    id: int (PK)
    store_id: int (FK stores.id, UNIQUE, NOT NULL)   # one config per store
    is_enabled: bool, NOT NULL, default False         # master switch; False = fiscalization is a complete no-op
    provider_name: str(50), NOT NULL, default "NULL"  # "NULL" | "FAKE" (tests only) | a real name once one exists
    credential_reference: str(255) | None             # a NAME/POINTER (e.g. env var name or secrets-manager key),
                                                        # NEVER the credential value itself (see Section 6)
    submission_endpoint: str(500) | None               # opaque URL; not validated against any known authority
    retry_max_attempts: int, NOT NULL, default 5
    updated_by: int | None (FK users.id)
```

- `is_enabled=False` (the default for every store, forever, until a real
  jurisdiction is confirmed) means the fiscal pipeline never runs — no
  row is written to `fiscal_submissions`, no adapter is invoked. This is
  the mechanism that keeps M20 jurisdiction-neutral in practice, not just
  in intent.
- One row per store (`UNIQUE(store_id)`), following the `Store`-column
  precedent's spirit (per-store, not global) while using a separate
  table (like `TaxRate`) because this configuration has many optional,
  sensitive, and independently-audited fields — a poor fit for a plain
  typed column on `Store`.
- No row for a store at all is equivalent to `is_enabled=False` with
  every other field unset — a store is never required to have one.

### 1.2 `fiscal_submissions` — the durable submission ledger (the "outbox")

```python
class FiscalSubmission(TimestampMixin, Base):
    __tablename__ = "fiscal_submissions"
    id: int (PK)
    sale_id: int (FK sales.id, UNIQUE, NOT NULL)   # one submission per sale — this IS the idempotency key
    store_id: int (FK stores.id, NOT NULL)          # denormalized for isolation queries/indexes, mirrors GoodsReceipt.store_id precedent
    status: str, NOT NULL, default "PENDING"
        # PENDING -> FAILED -> PENDING (retry) -> ACKNOWLEDGED | REJECTED | EXHAUSTED
    provider_name: str(50), NOT NULL                # copied from FiscalConfig at creation time (historical fact, not a live join)
    attempt_count: int, NOT NULL, default 0
    max_attempts: int, NOT NULL                      # copied from FiscalConfig at creation time, same reasoning
    last_error: str | None (Text)
    last_attempted_at: datetime | None
    submitted_at: datetime | None                    # set on the attempt that first got a definitive ACKNOWLEDGED/REJECTED
    fiscal_reference: str(255) | None                 # extracted from response_payload for quick lookup/receipt display
    request_payload: JSONB, NOT NULL                  # opaque; built once, frozen (see 1.2.1)
    response_payload: JSONB | None                    # opaque; whatever the adapter returned/raised, last attempt only
```

Constraints: `ck_fiscal_submissions_status` (the 5 values above),
`ck_fiscal_submissions_attempt_bounds` (`attempt_count >= 0`),
`UNIQUE(sale_id)` — the actual enforcement, exactly mirroring every
other idempotency key in this codebase (`Sale.client_transaction_id`,
`GoodsReceipt.client_transaction_id`, etc., per
`M19_HARDENING_AUDIT.md`): a second attempt to create a submission for
the same sale hits the unique constraint, not just an application-level
check.

#### 1.2.1 Why `sale_id`, not a separate client-generated key

`Sale` already has a durable, unique `client_transaction_id` from
checkout-time idempotency (M2). A `FiscalSubmission` is *derived from* an
already-finalized `Sale` — it is never independently retried by a client
across process boundaries the way a checkout POST is. `sale_id UNIQUE` is
therefore both simpler and stronger than inventing a second key: it is
structurally impossible to create two submissions for one sale, and the
frozen `request_payload` (built once from the sale's already-frozen
totals) can never drift between attempts.

The payload itself also carries an idempotency key outward, for the
provider's own benefit (Phase 3): `request_payload["idempotency_key"] =
f"submission-{submission.id}"`. **This is the honest limit of what this
architecture can guarantee** (see §3) — whether a real authority actually
deduplicates by that key is a fact about that authority, unknowable
today.

### 1.3 `Store` addition — legal/business identity (nullable, optional)

`docs/M20_DISCOVERY.md` §2 found zero legal-identity fields on `Store`
and flagged this as a real gap (a fiscal document needs a seller
identity) that must be filled without guessing a jurisdiction's specific
required fields. Two nullable columns, following the exact
`attendance_day_boundary_hour`/`return_approval_threshold_amount`
precedent (typed columns directly on `Store`, not a new table, since
these are simple per-store facts with no independent lifecycle):

```python
legal_name: str(255) | None       # the registered business name, if different from the display `name`
tax_registration_number: str(100) | None   # opaque string; format is authority-specific and unknown today
```

Both NULL by default on every existing and new store. Nothing reads or
requires them unless `FiscalConfig.is_enabled` is True for that store —
at which point the fiscal payload builder includes whatever is present
and leaves the rest absent, rather than rejecting a sale over missing
identity fields no confirmed authority has actually asked for.

## 2. Fiscal transaction lifecycle

```
SALE CREATED (finalize_sale, existing, unchanged)
  -> TAX CALCULATED (existing, unchanged — happens before/during finalize_sale)
  -> ACCOUNTING POSTED (existing, unchanged — same transaction as the sale)
  -> [if FiscalConfig.is_enabled for sale.store_id]
       FISCAL SUBMISSION CREATED, status=PENDING
         (same DB transaction as the sale — cheap, local, no network;
          this is the "outbox" write)
  -> [best-effort, out of the request/response transaction]
       FISCAL SUBMISSION ATTEMPTED via the configured provider adapter
         -> ACKNOWLEDGED (terminal success) + fiscal_reference stored
         -> REJECTED (terminal — authority explicitly refused; needs
            manual reconciliation, never auto-retried)
         -> FAILED (transient — network/timeout/5xx; attempt_count += 1,
            eligible for retry while attempt_count < max_attempts)
         -> EXHAUSTED (attempt_count reached max_attempts while still
            FAILED — permanent-failure state, needs manual reconciliation,
            never silently dropped)
  -> RECEIPT ISSUED (existing on-screen receipt, unchanged; optionally
       displays fiscal_reference once ACKNOWLEDGED — see §5)
```

**The sale itself never waits on any of the post-accounting steps.**
`finalize_sale`'s existing transaction commits with the sale COMPLETED
and the `FiscalSubmission` row PENDING; the cashier's screen and the
receipt render immediately. The actual network attempt happens
afterward — see §2.1 — matching `TECHNICAL_BLUEPRINT.md` Section J's
original, still-correct principle ("sale finalization is never blocked
waiting on the tax authority").

### 2.1 How "afterward" is implemented without a background-worker daemon

`M20_DISCOVERY.md` §9 risk 2 flags that this deployment has never run a
worker process (the blueprint's own `worker` container was never built
through M19) and that adding one now would be disproportionate to a
single-VPS, low-cost target. Two mechanisms, both fitting the existing
architecture:

1. **Opportunistic same-process attempt**: FastAPI's built-in
   `BackgroundTasks` (already part of the framework dependency, not a
   new one) schedules one submission attempt to run after the `POST
   /sales` response is sent. This covers the overwhelmingly common case
   (provider reachable, normal latency) with no added infrastructure.
2. **Durable catch-up via an explicit retry surface**: because
   `BackgroundTasks` work is lost if the process crashes or restarts
   mid-attempt (Phase 3 test #8/#9), a `PENDING`/`FAILED` row is never
   the *only* record of intent — it is a durable DB row, found again by:
   - `POST /api/v1/fiscal/submissions/{id}/retry` (an operator or a
     simple external scheduler — e.g. a systemd timer calling a script,
     exactly the existing `backup_offbox.sh`/cron precedent from M13 —
     can call this on a schedule), and
   - `submit_fiscal_transaction` itself being naturally re-entrant: any
     call, from any trigger, first re-reads the submission's current
     status and no-ops if it is already terminal (§3).

This keeps the retry mechanism honest about what this deployment
actually runs, instead of inventing a queue/worker system the project
has never needed.

## 3. Idempotency and achievable guarantees

Per Phase 3's explicit instruction, this section states what is actually
achievable, not an aspirational "exactly once" claim.

**Guaranteed by this architecture (enforced by the DB, tested)**:
- Exactly one `FiscalSubmission` row ever exists per `Sale`
  (`UNIQUE(sale_id)` — a second attempt to create one is rejected by
  Postgres itself, not just caught in application code, mirroring the
  established `client_transaction_id` pattern).
- A retry of `submit_fiscal_transaction(sale_id)` — sequential, concurrent,
  or after a process restart — always finds and reuses the SAME row.
  Reading the row's current status first means a retry against an
  already-`ACKNOWLEDGED` or `REJECTED` submission is a pure no-op: it
  never calls the adapter again.
- `request_payload` is built once, when the row is created, from the
  sale's own already-frozen totals — never rebuilt or recomputed, so a
  retry always sends byte-identical content.
- The outgoing payload always carries a locally-unique idempotency key
  (`submission-{id}`) so a *compliant* provider can itself deduplicate a
  genuine double-send.

**NOT guaranteed (and not claimed)**:
- True network-level exactly-once delivery. If the local process sends a
  request, the authority processes it, and the response is lost before
  we record it (Phase 2's "fiscal submission succeeds but HTTP response
  is lost" scenario), our local state is `FAILED` and a retry will be
  attempted. Whether that retry becomes a harmless no-op or a real
  duplicate on the authority's side depends entirely on whether that
  authority deduplicates by the idempotency key we send — a fact about
  an authority this project does not have. This limitation is recorded
  here, in the submission's own `last_error`, and surfaced by the
  `EXHAUSTED`/manual-reconciliation state precisely so a human resolves
  it instead of the system silently assuming success or silently
  duplicating.

## 4. Fiscal/accounting reconciliation

No second, independent financial calculation exists (Phase 4's
warning). `request_payload` is built directly from `Sale`'s own already-
posted, already-frozen fields (`subtotal`, `discount_total`, `tax_total`,
`grand_total`, and each `SaleItem`'s frozen `tax_amount`/`line_total`) —
by construction, the fiscal totals and the accounting/GL totals cannot
diverge, because they are the same numbers read twice, not recomputed.
The reconciliation test session (Session G) proves this construction
holds — not that two independent engines happen to agree.

## 5. Fiscal documents (receipt/invoice)

`M20_DISCOVERY.md` §2 found the existing on-screen `Receipt` component
minimal (no seller identity, no fiscal identifier, no per-line tax
breakdown) but deliberately so (`M2_AUTH_AND_POS.md` §11). Per Phase 5,
the minimum required fields are unknown without a confirmed jurisdiction
— so M20 does not mandate new required receipt fields. It does add,
additively and only when data exists:
- `Store.legal_name`/`tax_registration_number` displayed on the receipt
  header when set (falls back to the existing `Store.name` when not).
- `FiscalSubmission.fiscal_reference` displayed once `ACKNOWLEDGED`
  (absent otherwise — never a placeholder or a guessed format).

The underlying `Sale`/`SaleItem` rows already carry everything a fiscal
document would need (line items, quantities, unit prices, discounts,
tax amounts, totals, payment method, timestamp) — no new document table
is needed; a document is a read-time projection of `Sale` +
`FiscalSubmission`, kept consistent with this codebase's existing
"frontend/reports read frozen source rows, never a second copy" pattern.
Historical immutability is automatic: `Sale`/`SaleItem` are already
never mutated after finalization (BR-2/BR-6, pre-existing), and
`FiscalSubmission` rows are never deleted, only status-transitioned
forward.

## 6. Security

- `FiscalConfig.credential_reference` stores a **name**, never a secret
  value — e.g. `"FISCAL_API_KEY_STORE_7"`, resolved to an actual
  credential only at the moment of an adapter call, from the same
  environment-variable mechanism `core/config.py` already uses for the
  app's own `SECRET_KEY`/`DATABASE_URL`. No new secret-storage location
  is introduced.
- `GET /api/v1/fiscal/config` never returns `credential_reference`'s
  resolved value (there isn't one to return — only the reference name
  itself is ever in the DB) and the response schema does not expose any
  field that could contain one.
- `core/logging.py`'s existing redaction list (already strips
  `secret`/`secret_key`/`token`) is extended with `credential_reference`
  and `response_payload`/`request_payload` are logged only as a presence
  flag (e.g. `has_payload: true`), never their content, at INFO level —
  full payloads remain queryable via the authenticated
  `fiscal.read`-gated API, not in logs.
- `FiscalConfig` changes are audited via the existing `AuditLog`
  (before/after state), exactly like every other config-mutation path in
  this codebase — `credential_reference`'s value is a name, not a secret,
  so recording it in the audit trail is safe (it is not the credential
  itself).
- No signing is implemented (Phase 6's explicit instruction: "do not
  implement cryptographic signing from an invented specification").

## 7. Multi-store

`FiscalConfig` and `FiscalSubmission` both carry `store_id` and are
scoped by it in every service-layer query, identically to every other
per-store table in this codebase (`M20_DISCOVERY.md` §2's "pervasive"
finding). `fiscal_submissions.store_id` is denormalized from
`sale_id`'s own store (set once, at creation, from `Sale.store_id` —
never independently supplied by a caller) so a request can never submit
or read across a store boundary by passing a mismatched `store_id`.
Permission checks (§8) are applied per-store exactly like
`purchasing.*`/`ap.*`/`payroll.*`.

## 8. Admin configuration surface

New permissions, following this codebase's established
read/write/heavier-action split (`docs/M19...`/`M10...` precedent):

```
FISCAL_READ = "fiscal.read"                 # view config (minus secrets) + submissions, per store
FISCAL_CONFIG_WRITE = "fiscal.config.write" # enable/disable, set provider/credential-reference/endpoint — Admin only
FISCAL_RETRY = "fiscal.retry"               # manually retry one FAILED/EXHAUSTED submission
```

Endpoints (`app/api/v1/endpoints/fiscal.py`):
- `GET /api/v1/fiscal/config?store_id=` — `fiscal.read`
- `PUT /api/v1/fiscal/config` — `fiscal.config.write`, Admin-only in the
  seed RBAC matrix (mirrors `ACCOUNTING_ADMIN`'s Admin-only scope — this
  is at least as sensitive)
- `GET /api/v1/fiscal/submissions?store_id=&status=` — `fiscal.read`
- `POST /api/v1/fiscal/submissions/{id}/retry` — `fiscal.retry`

`PUT /api/v1/fiscal/config` is store-scoped like `store.settings.write`;
a non-Admin can never configure a store they don't belong to, and
self-approval is not a concept here (no approval workflow exists for
config changes — Admin authority is the same single-actor authority
already used for `users.manage`/`accounting.admin`).

## 9. Implementation plan

New module `app/modules/fiscal/` mirroring the shape of every other
module (`models.py`, `schemas.py`, `service.py`), plus:
- `app/modules/fiscal/provider.py` — the adapter interface:
  ```python
  class FiscalSubmissionOutcome:
      status: Literal["ACKNOWLEDGED", "REJECTED", "FAILED"]
      fiscal_reference: str | None
      raw_response: dict

  class FiscalProvider(Protocol):
      def submit(self, payload: dict) -> FiscalSubmissionOutcome: ...
  ```
- `NullFiscalProvider` — never actually invoked (is_enabled=False skips
  the pipeline entirely), kept only as the harmless default
  `provider_name` value so a `FiscalConfig` row is never left pointing
  at a provider that doesn't exist.
- `FakeFiscalProvider` (tests only, `backend/tests/fakes.py` or
  colocated with fiscal tests) — deterministically configurable to
  return/raise: success, rejection, timeout, connection failure,
  duplicate-request (same idempotency key twice -> same
  `fiscal_reference` both times), malformed response, delayed response.
  This satisfies Phase 9's "do not build a fake tax authority [meaning:
  do not build a real one]... create a deterministic fake authority
  adapter" instruction precisely — it simulates transport/protocol
  behavior, not any real authority's business rules.

Migration (new tables only, no backfill needed — brand new, empty
tables; two nullable columns added to `Store`, safe add-nullable with no
existing-row impact):
1. `ALTER TABLE stores ADD COLUMN legal_name ...NULL`,
   `ADD COLUMN tax_registration_number ...NULL`.
2. `CREATE TABLE fiscal_configs (...)`.
3. `CREATE TABLE fiscal_submissions (...)`.
4. No data migration/backfill step — no prior data exists for either new
   table, and the two `Store` columns are nullable with no default
   requiring backfill.
5. Downgrade drops both new tables and the two `Store` columns — no
   financial data lives in either (a `FiscalSubmission` is a
   status/attempt ledger about a `Sale`, not the sale's own financial
   record, which is untouched), so the M20 downgrade requires no special
   data-loss guard beyond what every other additive migration in this
   codebase already has.
6. Exactly one Alembic head maintained, based on `3a0d50ccc909` (the
   confirmed M19 head).

`finalize_sale` gains exactly one new, narrow responsibility: after its
existing accounting-post step, if `FiscalConfig.is_enabled` for
`sale.store_id`, create the `FiscalSubmission` row (PENDING) in the SAME
transaction, then schedule the best-effort attempt via
`BackgroundTasks` (§2.1) — nothing about the existing tax/accounting
logic changes.

## 10. Non-goals (explicit, matching `M20_DISCOVERY.md` §8)

No real tax-authority adapter. No cryptographic signing. No
multi-currency. No background-worker/queue daemon. No tax-inclusive
pricing, multi-tax-per-line, or exemption taxonomy beyond what already
exists. No real receipt printing/PDF generation. No AR-side fiscal
documents (this business has no AR — `M20_DISCOVERY.md` §2).

## 11. Testing plan (Sessions A–O, mapped to what this design needs proven)

- **A** — `FiscalConfig`/`FiscalSubmission` constraints, defaults,
  `is_enabled=False` no-op behavior.
- **B** — N/A for new tables (no new arithmetic; existing tax rounding
  is unchanged and already covered by `test_tax_boundaries.py`) — instead
  proves `request_payload` totals equal the sale's own stored totals
  exactly, byte-for-byte, including rounding edge cases already exercised
  by the sales suite.
- **C** — sale -> (config enabled) -> submission created PENDING ->
  fake-adapter ACKNOWLEDGED -> `fiscal_reference` stored and shown.
- **D** — a return/void against an already-`ACKNOWLEDGED` sale does not
  retroactively alter the original `FiscalSubmission`; documents whatever
  correction mechanism (a new, separate submission representing the
  reversal, keyed by the `SaleReturn`, not a mutation of the original) —
  built only if this milestone's implementation actually needs it to
  make the architecture coherent, not speculatively.
- **E** — sequential and concurrent (real threads, matching this
  codebase's established concurrency-test pattern) duplicate calls to
  `submit_fiscal_transaction(sale_id)` create/touch exactly one row.
- **F** — all 12 Phase 3 failure scenarios against `FakeFiscalProvider`.
- **G** — fiscal totals vs. `Sale`'s own totals vs. GL `ACCOUNT_TAX_PAYABLE`
  postings, across multi-line/mixed-rate/discount/return/void cases.
- **H** — store A cannot read/retry/configure store B's fiscal data
  (service-layer and API-layer, mirroring `test_store_isolation.py`).
- **I** — `credential_reference` never appears in a GET response, log
  line, or committed fixture; audit trail records config changes without
  leaking a secret value (there is none to leak).
- **J** — upgrade from the real M19 head (`3a0d50ccc909`), single head
  maintained, downgrade safety.
- **K** — a real browser flow: Admin enables fiscalization with the fake
  provider in a test environment, cashier completes a sale, submission
  reaches `ACKNOWLEDGED`, receipt shows the fiscal reference.
- **L** — against the real `deploy-infra`/`deploy-infra-native` topology.
- **M** — kill/restart the process between "submission row created" and
  "adapter attempted"; the retry endpoint recovers it correctly.
- **N** — malformed config (bad store_id, disabled-but-retried, invalid
  status transition attempted directly), unauthorized store access,
  concurrent enable/disable races.
- **O** — mutation testing on: tax-payload construction (must break if it
  stops reading from `Sale`'s frozen fields), `sale_id` uniqueness
  enforcement, submission state-transition guard (no forward jump past
  a terminal state), store-isolation check, rejection handling (must not
  be silently retried), retry/attempt-count increment logic, and two more
  drawn from whatever the implementation phase's own review surfaces —
  mirroring M19's exact "8 required targets, manual/live" methodology.

## Next step

Phase 9 implementation, starting with the migration and `fiscal/models.py`,
then the provider interface and fake adapter, then the service layer and
`finalize_sale` integration, then the API/permissions surface, then the
Session A–O tests above, in that order — each step re-running the full
regression suite before proceeding, per the milestone's own regression
requirement.
