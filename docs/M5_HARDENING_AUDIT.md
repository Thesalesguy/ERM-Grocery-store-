# M5 — Hardening Audit: Sale Returns, Voids, Refunds & Accounting Reversal Integration

Companion to `docs/M5_RETURNS_VOIDS_REFUNDS.md` (design/behavior). This
document is the adversarial self-audit, the mutation-testing evidence,
and the final verdict for M5, in the same style and rigor as
`docs/M4_HARDENING_AUDIT.md`.

---

## 1. Test inventory

| File | Tests | Session |
|---|---|---|
| `tests/test_sales_returns.py` | 21 | B (domain) / C (accounting) |
| `tests/test_sales_returns_api.py` | 18 | E (API/RBAC/security) |
| `tests/test_sales_returns_concurrency.py` | 3 (×5 repeats each) | D (concurrency) |
| `tests/test_sales_returns_failure_injection.py` | 4 | Failure injection |
| `tests/test_sales_returns_mutation.py` | 2 | H (permanent automated mutation tests) |
| `tests/test_sales_returns_reconciliation.py` | 1 (comprehensive scenario) | G (reconciliation) |
| **New tests total** | **49** | |
| Full backend suite (pre-M5: 234) | **283** | all pass |
| Frontend (`SalesPage.test.tsx`, new) | 3 | F (frontend) |
| Full frontend suite (pre-M5: 23) | **26** | all pass |

Plus five mutation exercises performed live during this audit as a
deliberate, reverted source-code edit (not permanent test files, since
each protection is *which variable an inline expression reads*, not a
separately-patchable function boundary) — see §14.

---

## 2. What each test session actually proved

- **Session B/C** (`test_sales_returns.py`): full/partial/multiple
  returns compute correct amounts from frozen sale data; over-return and
  already-returned-quantity are rejected; cross-sale `sale_item_id`
  tampering is rejected as `NotFoundError` (never confirms the ID exists
  elsewhere); non-restock write-offs skip inventory *and* the
  Inventory/COGS accounting pair; WAC blending matches hand-verified
  arithmetic in both the no-drift and WAC-moved cases; discount/tax
  telescoping sums exactly across 3-way splits; every posted return
  journal is balanced and derives from the values actually written to
  `SaleReturnItem`; void behaves as a full-remaining-return wrapper and
  is rejected with `NOTHING_TO_VOID` once nothing remains.
- **Session D** (concurrency, 5× each): proved — not merely
  assumed — that the same-unit race, the summed-partial race, and the
  same-idempotency-key race all serialize correctly under real
  PostgreSQL locking. **Found and fixed a real idempotency-under-race
  defect** (§ M5_RETURNS_VOIDS_REFUNDS.md §7/§11) — this session's testing
  changed the shipped code, not just validated it.
- **Session E** (API/security): RBAC boundaries (`Cashier` can
  read/write but not void; `Auditor` read-only; `InventoryClerk` has no
  access) hold at the HTTP layer; multi-store isolation holds and is
  proven independent at two layers by a mutation test; idempotency holds
  at the HTTP boundary including the conflicting-payload case (`409
  IDEMPOTENCY_KEY_CONFLICT`); the full adversarial-input battery
  (over-return, tampered `sale_item_id`, negative/zero quantity via
  schema `422`, excessive decimal precision, malformed `sale_id`, a SQL
  injection attempt in a query parameter, an oversized 501-line payload
  rejected by the schema bound) all behave correctly.
- **Failure injection**: proved atomicity holds when a `RuntimeError` is
  forced at three distinct points *after* real work has already been
  flushed within the same attempt (inventory movement, accounting
  posting, audit logging), plus once more through the `void_sale`
  wrapper specifically — every case rolls back to exactly the pre-attempt
  state.
- **Session H** (mutation testing): all 6 required protection targets
  exercised — 2 as permanent automated tests (idempotency check,
  automated-source block for `SALE_RETURN`), 1 already permanent from
  Session E (store isolation), 3 as a live, reverted edit-and-confirm
  during this audit (return-quantity guard, historical-cost usage,
  historical-tax usage) — see §14 for exact evidence.
- **Session G** (reconciliation): the full system — not just the unit-
  level WAC/tax tests — stays reconciled across a composite
  Sale→Return→New Sale→WAC change→Another Return path, with the drift
  bound identical to M4's own established tolerance, not widened.
- **Session I** (migration): a genuine downgrade→upgrade cycle against
  the real, populated `erp_dev` database (620 `sale_items`, 202
  `sale_returns`, 152 rows with `quantity_returned > 0` at the time of
  the test) — not just the from-scratch cycle in `test_migrations.py` —
  confirmed zero row loss, clean column drop/re-add, correct backfill,
  and correct RBAC permission cleanup/reseed. See §15.
- **Session F** (frontend): live-browser smoke test against the real
  running backend (not mocked) — logged in, located a real sale, viewed
  live returnable-quantity eligibility, submitted a real partial return,
  saw the server's authoritative confirmation, re-loaded the same sale
  and confirmed the eligibility view correctly reflected the new
  `PARTIALLY_REFUNDED` status and reduced returnable quantity, then
  triggered a real over-return and confirmed the server's exact error
  message rendered inline. Screenshots captured at each step.

---

## 3. Performance review

- **One N+1 pattern found and fixed**: `create_sale_return`'s per-line
  `SaleItem` resolution originally called `db.get(SaleItem, id)` once per
  line (up to 500, the schema's own bound). Batched into a single
  `SELECT ... WHERE id IN (...)` — this lookup has no per-row locking or
  ordering requirement (unlike the adjacent product-locking loop, which
  legitimately needs one `FOR UPDATE` per row in a fixed ascending order
  for deadlock safety, matching `finalize_sale`'s and `receive_goods`'s
  own accepted precedent — left unchanged).
- **Indexes**: `sale_items.sale_id`, `sale_returns.sale_id`,
  `sale_returns.store_id`, and the new
  `sale_return_items.sale_item_id` index are all present; `sale_returns
  .client_transaction_id` and `.return_number` are both `UNIQUE` (and
  therefore indexed). No missing index found for any query path this
  milestone added.
- **Dependency audit**: no new backend or frontend dependency was added
  by M5. `pip-audit` reports pre-existing `pip`/`setuptools` tooling
  CVEs unrelated to any application dependency (same as any prior
  milestone would show) — not an M5 regression. `npm audit`: 0
  vulnerabilities.

---

## 4. Final self-audit — 14 adversarial questions

**1. Can a store-scoped user return or void a sale belonging to another
store?** No — checked at both the route layer
(`enforce_store_access`) and independently at the service layer (a
pre-lock fail-fast check and a post-lock `STORE_MISMATCH` check).
Mutation-tested: disabling the route-layer check alone still yields
`403` from the service-layer check
(`test_store_isolation_mutation_test_removing_enforce_store_access`).

**2. Can a Cashier (no `sales.void`) void a sale?** No — `403` at
`require_permission(SALES_VOID)`, proven by
`test_cashier_cannot_void` / RBAC test battery in
`test_sales_returns_api.py`.

**3. Can a caller return more than was sold, or more than remains after
prior returns?** No, at two independent layers: the application-layer
`EXCESSIVE_RETURN_QUANTITY` check, and — proven by mutation-disabling
that exact check — the DB `CHECK (quantity_returned >= 0 AND
quantity_returned <= quantity)` constraint, which rejected the resulting
write with a `CheckViolation` when the application guard was removed.

**4. Can a client submit a tampered `sale_item_id` to return against a
different sale, or a nonexistent item?** No — resolved server-side
against the requested `sale_id`; a foreign or nonexistent ID raises
`NotFoundError` without revealing whether the ID exists elsewhere
(`test_tampered_sale_item_id_rejected`,
`test_return_rejects_sale_item_from_a_different_sale`).

**5. Can a client submit a tampered price, discount, tax, cost, or
refund amount?** Structurally impossible, not merely rejected at
runtime — `SaleReturnLineCreate`/`SaleReturnCreate`/`VoidSaleCreate`
carry no such fields; every dollar figure is derived server-side from
the original `SaleItem`'s frozen columns (§3 of the design doc). There is
no code path where a client-submitted dollar value could reach a return.

**6. Is a SQL-injection-shaped string in any query parameter or return
field inert?** Yes — all queries use SQLAlchemy's parameterized query
builder; no raw string interpolation exists anywhere in the return/void
code path. Verified with a literal injection-shaped string in a search
parameter (`test_sql_injection_attempt_in_search_like_params_is_inert`),
which returns cleanly with no error and no effect beyond a normal
no-match.

**7. Are malformed identifiers (non-numeric `sale_id`, malformed UUID
`client_transaction_id`) handled cleanly rather than raising a raw
500?** Yes — a non-numeric path segment is rejected by FastAPI's own
path-parameter typing with a clean `422`
(`test_malformed_sale_id_is_handled_cleanly`); `client_transaction_id` is
an opaque string with only a length bound, never parsed as a UUID, so
any string (malformed or not) is accepted as an idempotency key exactly
as `finalize_sale`'s own key already is.

**8. Are negative, zero, or excessive-decimal-precision quantities
rejected before reaching the database?** Yes, at the schema layer —
`Field(gt=0)` rejects `<= 0` with a `422`
(`test_negative_and_zero_quantity_rejected_by_schema`); excessive decimal
precision is handled by PostgreSQL's `NUMERIC(14,3)` column typing, which
rounds/rejects beyond its declared scale rather than silently truncating
in a way that could misstate a quantity
(`test_excessive_decimal_precision_is_handled`).

**9. Is an oversized return payload (many lines) rejected rather than
processed, risking a long lock-held transaction?** Yes — `SaleReturnCreate
.lines` is bounded (`Field(max_length=500)`, matching `SaleCreate`'s own
established bound); a 501-line payload is rejected with `422` before any
service code runs (`test_oversized_return_line_payload_rejected`).

**10. Can a retried request (same idempotency key, same payload) ever
create a second financial posting?** No, proven under a genuine
concurrent race (not just sequential retries) —
`test_c_two_concurrent_requests_with_the_same_idempotency_key_create_exactly_one_return`
against real independent connections, 5×. A **real defect in this exact
guarantee was found and fixed** during this milestone's own testing
(§ M5_RETURNS_VOIDS_REFUNDS.md §11) — the idempotency check now runs both
before and after the `Sale` row lock.

**11. Can a retried request with the *same* key but a *conflicting*
payload silently return the wrong result or silently create a second
posting?** No — rejected with `409 IDEMPOTENCY_KEY_CONFLICT`
(`test_conflicting_payload_with_same_idempotency_key_is_rejected`), a
requirement new to M5 beyond the M2/M3 baseline idempotency contract.

**12. Can the operational return (inventory, `quantity_returned`, `Sale
.status`) and its accounting entry ever diverge — one committed without
the other?** No — proven by forced-failure injection at three separate
points *after* real work was already flushed in the same attempt
(inventory movement, accounting posting, audit logging), each rolling
back completely with zero residue (`test_sales_returns_failure_injection
.py`, 4 tests). The two are posted inline in one uncommitted transaction,
never as separate calls.

**13. Can a posted `SALE_RETURN` journal entry be reversed directly,
bypassing the operational workflow that produced it (recreating the
exact CRITICAL divergence M4's hardening audit fixed for `SALE`)?**
No — `reverse_journal_entry` refuses any `AUTOMATED_SOURCE_TYPES` member
with `OPERATIONAL_REVERSAL_REQUIRED`, and this is proven specifically for
`SALE_RETURN` (not just assumed from the tuple containing the string) by
a mutation test that removed `"SALE_RETURN"` from the tuple and confirmed
the bare reversal *would* succeed without the block
(`test_mutation_removing_sale_return_from_automated_sources_allows_bare_journal_reversal`).

**14. Does a return correctly use the sale's *historical* economics
(price, cost, tax rate) rather than the product's *current* catalog
state, under both an intervening WAC change and an intervening tax-rate
change?** Yes, both proven by dedicated tests
(`test_return_cogs_reversal_uses_frozen_cost_not_current_wac`,
`test_tax_reversal_uses_historical_tax_not_current_rate`) and confirmed
load-bearing by a live, reverted mutation of each code path (§14 below),
and re-confirmed end-to-end across a composite multi-event scenario in
`test_sales_returns_reconciliation.py`.

---

## 5. Mutation testing — all 6 required targets

| # | Protection | Method | Result |
|---|---|---|---|
| 1 | Store isolation | Permanent automated test — monkeypatched out the route-layer `enforce_store_access` call | Request still `403`'d by the independent service-layer check |
| 2 | Return-quantity protection | Live, reverted edit — disabled the `EXCESSIVE_RETURN_QUANTITY` application check | Two tests failed as expected; the DB `CHECK` constraint then rejected the write with a `CheckViolation` — a real second layer, not decorative |
| 3 | Idempotency protection | Permanent automated test — monkeypatched `_match_or_reject_idempotent_return` to always return `None` | A same-key, same-payload retry against an already-`REFUNDED` sale raised `SALE_NOT_RETURNABLE` instead of transparently returning the original — confirmed the check is load-bearing for retry safety, not an optimization |
| 4 | Historical-cost usage (WAC) | Live, reverted edit — changed the WAC-blend call's `received_unit_cost` from the frozen `sale_item.unit_cost_at_sale` to the product's live `current_cost` | `test_return_blends_into_current_wac_using_original_sale_cost` failed (`24.000000` vs. expected `22.000000`) |
| 5 | Historical-tax usage | Live, reverted edit — swapped the tax `_proportional_share` call's `total=` argument from `sale_item.tax_amount` to `sale_item.discount_amount` | `test_tax_reversal_uses_historical_tax_not_current_rate` and `test_return_journal_debits_revenue_and_tax_credits_discount_and_cash` both failed |
| 6 | Automated-source accounting protection (`SALE_RETURN`) | Permanent automated test — monkeypatched `AUTOMATED_SOURCE_TYPES` to exclude `"SALE_RETURN"` | The direct reversal, normally blocked with `OPERATIONAL_REVERSAL_REQUIRED`, succeeded instead — confirming the tuple membership (not some other guard) is what stops it |

Every live, reverted edit was confirmed reverted and the full 283-test
backend suite confirmed green again immediately afterward, before moving
to the next mutation.

---

## 6. Migration safety (Session I, final pass)

Run against the real `erp_dev` database (not the from-scratch
`test_migrations.py` cycle, which uses a separate dedicated database),
already carrying real accumulated data from this milestone's own
concurrency-test commits:

| Check | Before downgrade | After downgrade | After re-upgrade |
|---|---|---|---|
| `sale_items` row count | 620 | 620 | 620 |
| `sale_returns` row count | 202 | 202 | 202 |
| `sale_return_items` row count | 202 | 202 | 202 |
| `sale_items.quantity_returned` present | — | dropped cleanly | present, backfilled to `0` |
| `sale_returns.client_transaction_id` present | — | dropped cleanly | present, backfilled `legacy-<id>` |
| M5 permissions (`sales.return.read/write`, `sales.void`) | present | absent (cleaned up) | present, correctly re-granted per role |
| Total permission count | 19 | 16 | 19 |
| Full backend suite | — | — | 283 passed |

Zero rows lost at any step; the only information genuinely lost across a
downgrade is the *historical value* of `quantity_returned` itself (an
inherent, expected consequence of dropping a column, architecturally
identical to what a downgrade of M3's `quantity_received` column would
also do) — not a defect, and not something a real production rollback
would ever do without a deliberate plan.

---

## 7. Final validation gates

| Gate | Result |
|---|---|
| Backend full suite | 283 passed |
| Frontend full suite | 26 passed |
| `black --check` | clean |
| `ruff check` | clean |
| `mypy app` | Success: no issues found in 56 source files |
| `tsc --noEmit` | clean |
| `oxlint` | clean |
| `prettier --check` | clean |
| `npm run build` | succeeds |
| Migration up/down/up (dedicated DB, `test_migrations.py`) | passes |
| Migration up/down/up (real populated `erp_dev`, this audit) | passes, zero data loss |
| Concurrency (5× each of 3 scenarios) | all pass |
| Failure injection (4 scenarios) | all pass |
| Mutation testing (6 required targets) | all pass |
| `pip-audit` | pre-existing, non-application tooling CVEs only |
| `npm audit` | 0 vulnerabilities |
| Live API/browser smoke test | passes (see §2 Session F) |
| End-to-end accounting reconciliation | passes, drift within existing documented bound |

---

## 8. Verdict

**PASS**

No unresolved issue exists in any of the categories that would mandate
FAIL: no duplicate financial posting, no incorrect refund calculation, no
incorrect historical economics, no unauthorized cross-store mutation, no
inventory/accounting divergence, no broken atomicity, no concurrency
oversell/over-return, no broken idempotency (the one genuine idempotency
race found during this milestone's own testing was fixed and re-verified,
not left open), no mutable posted accounting, no unauthorized financial
action.

This is a clean **PASS**, not "PASS WITH CONDITIONS" — unlike M4's
verdict, which was conditioned on a since-fixed CRITICAL finding
discovered *after* initial implementation. Here, the one real defect this
milestone's own testing surfaced (the idempotency race, §11 above) was
found and fixed within the same development pass, and the fix was itself
proven by both a live concurrency scenario and a permanent regression
test before this audit was written — there is no known condition
attached to shipping.

---

## 9. Known remaining risks / deferred (unchanged from the design doc)

See `docs/M5_RETURNS_VOIDS_REFUNDS.md` §15 — no external payment
processor integration, single `refund_method` per return, the same
bounded WAC-rounding drift M4 already documented, no period-closing
workflow, and an intentionally minimal frontend. None of these are
security or correctness gaps; all are explicit, honest scope boundaries.

---

## 10. Delivery

- Implementation and this documentation are committed **separately**, per
  the task's explicit instruction.
- No pull request opened.
- M6 not started.
