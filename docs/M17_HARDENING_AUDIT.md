# M17 Hardening Audit

Companion to `docs/M17_DISCOVERY.md`, `docs/M17_DESIGN.md`, and
`docs/M17_TESTING_SESSIONS.md`. Distinguishes fixed, intentionally
unchanged, deferred, unknown, and production-prerequisite items, per the
explicit instruction not to claim something was "verified" unless it
was actually tested, and not to call a local test result "CI verified"
until GitHub Actions itself confirms it.

## 1. Fixed, with evidence, root cause, and regression coverage

| # | Item | Evidence | Root cause | Fix | Regression test | Security impact | Accounting impact | Migration impact |
|---|---|---|---|---|---|---|---|---|
| 1 | `list_cash_movements` had no service-layer store isolation | `M17_DISCOVERY.md` §D: function took no `caller_store_id` parameter at all, confirmed by direct source read | M16 Phase 0 item 5 hardened `get_shift`/`list_shifts` in the same module for this exact reason but did not extend the same treatment to their sibling `list_cash_movements`, which was added in the same M15 milestone | Added optional `caller_store_id` parameter, delegating to `get_shift`'s own check (same `NotFoundError`/information-hiding behavior); simplified the HTTP endpoint, which previously duplicated the same check manually | `test_shifts.py::test_direct_service_call_to_list_cash_movements_enforces_store_isolation`; mutation-tested live (`M17_TESTING_SESSIONS.md` Session I) — caught | Closes a direct-service-layer IDOR path for cash-movement data (till cash-in/cash-out records) — the HTTP endpoint was never vulnerable, only a hypothetical future direct caller | None — read-only, no financial state ever at risk | None — no schema change |
| 2 | AP GL-vs-subledger reconciliation was never tested against a post-reversal state | `M17_DISCOVERY.md` §H: `ap_reconciliation`/`purchase_clearing_reconciliation` existed and were tested for ordinary activity since M6/M7, but no test exercised them after an M16 reversal | A test-coverage gap left by M16 — the reversal capability and the reconciliation capability were each tested in isolation, never together | None needed — the two new tests confirm the reconciliation functions were already correct for this case, by construction (`_outstanding_balance` reads the same `amount_paid`/`amount_credited` fields a reversal correctly decrements) | `test_ap_reconciliation.py::test_ap_reconciliation_holds_after_supplier_payment_reversal`, `::test_ap_reconciliation_holds_after_supplier_credit_note_reversal` — both pass with hand-derived expected numbers | None — no vulnerability, a coverage gap | None — proves an existing invariant holds, does not change it | None |

## 2. Investigated and found to be intentional — no code change

| Item | Finding |
|---|---|
| No `CASH_SHIFT_VARIANCE` reversal / shift-reopen workflow (`M17_DISCOVERY.md` §C) | `accounting/models.py`'s own inline comment states this was a deliberate M15 design decision ("M15 builds no shift-reopen/correction workflow... nothing in this milestone would ever call one"), re-confirmed correct by M16, re-confirmed still correct by M17. Not revisited — no confirmed requirement to build it now, and doing so unprompted would be exactly the speculative functionality M17's own instructions forbid. |
| Read-function store isolation is endpoint-layer-only, repo-wide (`M17_DISCOVERY.md` §L) | Confirmed as a consistent, intentional, documented convention across AP, sales, and (mostly) shifts — not a gap. The one place it deviated from its own module's stated standard (`list_cash_movements` vs. its `get_shift`/`list_shifts` siblings) is item 1 above; everywhere else, the pattern is applied consistently on purpose. |
| No individual cash-movement reversal (only offsetting movements) | Matches the append-only-ledger philosophy already established for `CASH_SHIFT_VARIANCE` and every other automated GL source type — a correction is a new, audited, offsetting entry, never a mutation of history. Confirmed intentional by the uniform pattern across every financial ledger in the system (sales, AP, payroll, shifts), not merely assumed for cash movements alone. |

## 3. Deferred / unknown — reported, not built

| Item | Classification | Why not built |
|---|---|---|
| Stock-adjustment reversal/correction linkage (`M17_DISCOVERY.md` §E) | **Unknown / undesigned** — no design document from M8 through M16 ever specifies this as a requirement | Building a new reversal subsystem for stock adjustments with no confirmed requirement behind it is exactly the speculative functionality M17's instructions explicitly forbid inventing. If this becomes a real requirement, it should follow the exact same pattern already proven twice (AP payment/credit-note reversal, M16): idempotent-by-existence, dedicated automated source type, DB `UNIQUE` backstop, genuine concurrency test. |
| Shift-close correction/reopen workflow | **Deferred feature by explicit prior design decision** (M15) | See §2 above — re-litigating a deliberate, documented, still-valid design decision was not asked for and is out of scope. |

## 4. False-positive findings

None. Every item discovery initially flagged as worth investigating
(the full A–M gap matrix in `M17_DISCOVERY.md`) resolved to one of:
solid-and-verified, a confirmed narrow gap (now fixed), or confirmed
intentional design — none were false alarms requiring retraction, and
none of M17's own findings needed correction after implementation (both
items closed exactly as designed in `M17_DESIGN.md`, first try).

## 5. Exact final counts

- Backend: **933 passed** (`pytest -q`).
- Frontend: **49 passed** (14 files) — unchanged by M17.
- Deploy-infra: **22 passed** — unchanged by M17.
- Migration tests: unchanged, single head `4a83c462dbff` — M17
  introduced no migration.
- `ruff check .` / `black --check .` / `mypy app` — all clean.
- `npx tsc -b` / `oxlint .` / `prettier --check .` — all clean (no
  frontend files changed, re-confirmed rather than assumed).

## 6. Production prerequisites

None new. M17 introduces no new capability, endpoint, permission, or
migration — it hardens one existing service-layer isolation check and
adds regression coverage for an already-correct existing function. No
new production readiness conditions are created by this milestone.

## 7. Remaining limitations (carried forward, not new)

- 51/73 `deploy/tests/` remain outside CI (M16 finding, unchanged —
  still needs a Prometheus/Alertmanager stack, rclone+native-postgres,
  or a `.venv` this CI doesn't create for the remaining files).
- No manual browser smoke test was run against a live backend this
  milestone (no frontend change occurred to smoke-test).
- No stock-adjustment correction workflow exists (§3 above).
- No shift-close correction/reopen workflow exists (§3 above).
