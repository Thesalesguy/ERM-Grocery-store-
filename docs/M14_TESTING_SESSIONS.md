# M14 Testing Sessions

## Session 1: focused new-behavior tests

**File**: `backend/tests/test_return_void_approval.py` (26 tests, all new).

**Objective**: cover the 20 mandatory categories from the M14 task brief: threshold boundary (below/exactly-at/just-below/above/unconfigured), authorization (unauthorized approver, authorized approver, self-approval, cross-store, admin-unrestricted, wrong password), non-reuse/non-replay/exact-amount binding, orphaned-state safety on both a rejected-for-policy and a rejected-for-bad-credentials approval, concurrency (deterministic outcome per independent request; splitting cannot bypass), existing return/void idempotency unaffected, identical GL/inventory effects when approved, audit-log content (actor/target/store/state, both on grant and on self-approval failure), and direct-API-bypass rejection (including a second-cashier-as-approver attempt).

**Environment**: real PostgreSQL (`erp_dev`), the project's standard `db`/`client` fixtures — no mocking of the database or auth.

**Procedure and result**: all 26 written and run in one pass; one required a fix to the test's own expectation, not the implementation (see below); all others passed on first correct implementation of the design in `docs/M14_DESIGN.md`.

**Defect found in test expectation, not implementation**: `test_void_idempotency_unaffected_below_threshold` initially asserted that retrying an identical void call returns `201` twice (mirroring the existing return-idempotency test). Actual, pre-existing (not M14-introduced) behavior: `void_sale` recomputes its "remaining quantity" lines fresh on every call, from `sale.items`, *before* `create_sale_return`'s own `client_transaction_id` idempotency check ever runs — so after a full void, a literal retry of the same call finds nothing left to void and returns `409 NOTHING_TO_VOID`, not the original `201`. Verified this is not a financial-safety issue (no double posting, no double inventory movement — it fails closed) and not something any test in the repository had previously exercised (grepped `test_sales_returns*.py` for a void-retry test: none existed). Not fixed (out of M14's scope — a change to `void_sale`'s idempotency-check ordering is unrelated to the approval-threshold feature and would itself need its own hardening pass); the test now pins the actual behavior with an explanatory docstring, so a future change to it will be caught rather than silently drifting.

**Final status**: 26/26 passing.

## Session 2: regression

**Files**: `test_sales_returns.py`, `test_sales_returns_api.py`, `test_sales_returns_concurrency.py`, `test_sales_returns_failure_injection.py`, `test_sales_returns_mutation.py`, `test_sales_returns_reconciliation.py`, `test_auth.py`, `test_auth_hardening.py`, `test_audit_log_endpoint.py`, `test_audit_hardening.py` — 126 tests, run after refactoring the existing per-line refund computation into the shared `_compute_line_refund` helper (a pure extraction, same formula, same rounding) and after adding the `store`/`user` entity-type to the audit-log store-scoping map.

**Real gap found and fixed**: the audit-log store-scoping code (`app/modules/audit/service.py`) has no entry for `entity_type="store"` (M14's new `SALE_RETURN_APPROVAL_GRANTED`/`_FAILED` events use `entity_type="store"`, since there is no per-approval row to point at). Without a fix, these events would silently fall into "unknown entity_type → excluded, not guessed," meaning a store-scoped Manager could never see their own store's approval audit trail through the existing `/audit-logs` endpoint. First attempt at the fix (`Store` in `_DIRECT_STORE_ENTITY_MODELS`, which filters `model.store_id == store_id`) crashed — `Store` has no `store_id` column, it *is* the store; `entity_id` for `entity_type="store"` is the store's own id. Fixed by adding `"store"` as a join-resolved special case (`select(Store.id).where(Store.id == store_id)`), caught immediately by the existing `test_audit_log_endpoint.py`/`test_audit_hardening.py` suite (2 failures on the wrong fix, 0 after the correct one).

**Final status**: 126/126 passing.

## Session 3: full-suite migration regression

**Files**: `test_migrations.py` (13 tests), `test_migration_operations_audit.py`.

**Real defect found and fixed**: `test_migrations.py::test_full_upgrade_downgrade_upgrade_cycle` failed with `UniqueViolation` on a from-scratch (`downgrade base` → `upgrade head`) cycle — root cause and fix documented in full in `docs/M14_DESIGN.md` §4 (the M2-seed-reads-permissions.py-live behavior; fixed with `ON CONFLICT DO UPDATE ... RETURNING`, matching every migration since M4). Reproduced deterministically via the Alembic CLI directly (`alembic upgrade 36ec624cf083` then `alembic upgrade head` against a freshly created `erp_test`), isolating it from an unrelated, separately-diagnosed issue: the sandbox's persistent local Postgres had stale `erp_test` state left over from earlier interrupted runs in this same session (confirmed via direct `psql` inspection — `alembic_version` and actual table contents disagreed) that produced a superficially similar symptom on the first pass; that was resolved by recreating `erp_test` cleanly and is unrelated to the migration bug itself, which reproduced identically on a genuinely fresh database.

Two existing tests in this file hardcode the exact head revision and permission count as of M12 (`test_full_upgrade_downgrade_upgrade_cycle`'s `_current_revision(...) == M12_HEAD_REVISION`, `test_rbac_seed_data_present_after_upgrade`'s `permission_count == 44`) — updated to `M14_HEAD_REVISION`/`45`, the same routine bump every milestone from M9 onward made to this same file for its own new migration/permissions. The M12-*specific* test (`test_m12_alembic_version_privilege_revoked_on_upgrade_and_restored_on_downgrade`), which targets `M12_HEAD_REVISION` deliberately (not "head"), was correctly left unchanged.

**Final status**: 13/13 (`test_migrations.py`) + full `test_migration_operations_audit.py` passing after the fix; both failures were real and are now closed, not weakened.

## Session 4: mutation testing

**Method**: identical to every prior milestone since M9 — a small targeted change applied directly to `app/modules/sales/service.py`, the relevant test(s) run and confirmed to fail, the change reverted via exact string-restore, tests rerun to confirm clean, `grep -c MUTATION` checked empty before the next cycle. No mutation was ever left applied between cycles.

**Result**: 7 of the 9 named mutation targets applied live and detected; 2 assessed and not applied, with reasons:

1. Remove the threshold check → 18/26 tests failed. Detected.
2. Change `>=` to `>` (boundary) → `test_exactly_at_threshold_requires_approval` failed exactly. Detected.
3. Remove the `sales.return.approve` permission check → `test_unauthorized_cashier_cannot_approve`, `test_direct_api_call_with_cashier_as_approver_is_rejected` failed. Detected.
4. Permit self-approval → `test_initiating_cashier_cannot_self_approve`, `test_audit_log_records_self_approval_failure` failed. Detected.
5. Remove store isolation from approval → `test_manager_from_another_store_cannot_cross_store_approve` failed. Detected.
6. Break the binding between the approval decision and the exact posted amount (hardcoded `Decimal("0")` in place of the real computed total) → 18/26 failed. Detected.
7. Allow an already-used approval to be reused → **not applied as a live mutation**. This design has no detachable, referenceable "approval" object at all (see `docs/M14_DESIGN.md` §2) — approval verification is inline, per-call, against live credentials, not a token or flag that persists between requests. There is no code location whose removal would "allow reuse," because nothing is ever cached to reuse. `test_approval_cannot_be_reused_for_another_return` proves this empirically against the real, unmutated code: a manager's successful approval of one return has zero effect on whether the next above-threshold return, with no approver supplied, also requires and enforces its own approval. Documented architectural reason, not a weakened mutation — same discipline M13 applied to its two skipped mutations.
8. Move the approval check outside the required transaction boundary → **not applied as a live mutation**. A physical relocation of the gate call to after `db.add(SaleReturn(...))` was assessed but not performed: this codebase's standard session-per-request pattern rolls back on any unhandled exception regardless of where in a function it's raised, so a naive relocation mutation would likely be masked by that surrounding infrastructure rather than by this feature's own logic, risking a misleading "detected" result for the wrong reason, or requiring destabilizing surgery on the function to construct a meaningful case. Verified instead by static reading: the gate call (`_resolve_return_approval`) precedes the first `db.add(SaleReturn(...))` in `create_sale_return` (confirmed by line order in `app/modules/sales/service.py`), and `test_rejected_approval_leaves_no_sale_return_row`/`test_failed_approval_credentials_leave_no_partial_state` prove no row survives a rejected approval either way.
9. Bypass approval for voids → `test_void_also_subject_to_approval_gate_via_api` failed. Detected.

**Final status**: 7/7 attempted mutations detected; 2/9 assessed and documented as not meaningfully applicable to this design, per the task's own "if the implementation makes that meaningful" / "document an architectural reason" allowance.
