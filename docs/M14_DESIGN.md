# M14 — Return/Void Approval-Threshold Enforcement: Design

## 0. Baseline and scope

Branch `claude/grocery-erp-pos-architecture-h8a53g`, checkpoint HEAD `7f0681c`, migration head `17fb9afe8d39`, M13 closed PASS WITH CONDITIONS, backend 823/823, frontend 36/36. M14 closes the one control gap named as deferred since M5 (`docs/M5_RETURNS_VOIDS_REFUNDS.md` "Known limitations": `SaleReturn.approved_by` exists but is never enforced) and required by the original blueprint (`docs/TECHNICAL_BLUEPRINT.md` assumption #5: "both full and partial returns supported... requiring Manager/Admin approval above a configurable threshold").

**Not in scope**: redesigning the GL (`journal_entries`/`journal_lines`) or inventory ledger (`inventory_movements`), which remain exactly as they were; a generic workflow engine; a `store_settings` table (see §1).

## 1. Discovery discrepancy: `store_settings` does not exist

The M14 discovery report (previous session) stated `store_settings` "already exists and can support a per-store threshold." **This was wrong** — grepped the entire repository: no `store_settings` table, model, or migration exists anywhere. It was only ever sketched in `TECHNICAL_BLUEPRINT.md` §C.7 and never built. Per the task's own instruction to investigate rather than trust the report, this was resolved before writing any code.

The repository's actual, established precedent for per-store configuration is a **typed column directly on `Store`**: `attendance_day_boundary_hour` (M10, `auth/models.py`), with its own CHECK constraint and a doc comment explaining the default. M14 follows this exact precedent: `stores.return_approval_threshold_amount NUMERIC(12, 2) NULL`, not a settings table.

## 2. Transaction model: same-transaction co-approval

Three designs were compared, per the task's explicit requirement not to blindly implement a two-step endpoint:

1. **Same-transaction co-approval** (chosen): the approving user's credentials are supplied in the same API call that creates the return/void, verified inline, before any row is created. No separate "approval" object ever exists.
2. **Pending return + separate approval transaction**: `SaleReturn` gains a `PENDING_APPROVAL` status; a manager later calls a separate endpoint to approve, which is when GL/inventory effects post.
3. No other established workflow pattern exists in this codebase for this shape of problem (`PurchaseOrder`'s draft→ordered→received lifecycle was considered but rejected — see below).

**Rejected (2)** because `SaleReturn`'s own module docstring (`sales/models.py`) is explicit that a return/void is "created already COMPLETED... every Sale in this system is created already COMPLETED with inventory already moved" — deliberately mirroring `Sale`'s all-or-nothing insert, not `PurchaseOrder`'s status-driven lifecycle. Introducing a pending state would be the first status machine in the returns domain, a real scope expansion the task explicitly warns against ("least scope expansion"). It would also introduce a genuinely new risk class: a detached, separately-persisted "approval" record referenced by some later request is exactly the shape of object that invites replay/reuse/cross-operation-binding bugs — the very failure modes the task enumerates.

**Chosen (1)** because:
- **Atomicity**: the approval decision and the return's creation happen in one Python call, one DB transaction, no new persisted state between them. If approval fails, nothing is created (see §7).
- **No replay/reuse surface**: since no detachable "approval" object is ever created, there is nothing to replay or reuse. This is not an enforcement rule added on top — it's a property of not having built the vulnerable shape in the first place.
- **Exact-amount binding by construction**, not convention: the total refund amount used for the threshold check and the total posted to the ledger are produced by the *same function call* (`_compute_line_refund`, see §5) — not two independent computations that could drift.
- **Least scope expansion**: no new endpoint, no new status, one new permission, two new columns.

## 3. Boundary semantics: `>=` ("at or above")

The blueprint's original wording says "above a configurable threshold" (assumption #5). The M14 task's own business-rules text says, twice, "at or above the threshold require... approval," and separately mandates a test named "Exactly-at-threshold behavior" — a test that is only meaningful if the boundary is inclusive. Given the explicit, repeated, most-recent instruction, **`>=` is the implemented boundary**, documented here as a resolved conflict against the older wording, not a silent choice. `test_exactly_at_threshold_requires_approval` and `test_just_below_threshold_does_not_require_approval` pin this exactly.

## 4. Database changes

- `stores.return_approval_threshold_amount NUMERIC(12, 2) NULL`, CHECK `NULL OR >= 0`. `NULL` (the default — no seeded value) means the gate is inactive for that store; every return/void behaves exactly as before M14. No dollar amount is invented; the migration seeds nothing here. Production must explicitly configure a threshold (no admin UI was built for this — see §11, same precedent as `attendance_day_boundary_hour`, which also has none).
- `sale_returns.approval_required BOOLEAN NOT NULL DEFAULT false`. Frozen at creation time (BR-2 discipline — same reasoning as `unit_price_at_sale`): whether *this* return was subject to its store's threshold at the moment it was created, independent of a later threshold edit.
- One new permission, `sales.return.approve`, granted to Admin and Manager only (not Cashier, not Inventory Clerk, not Auditor, not HR Clerk) — a new incremental migration mirroring M9's `_NEW_PERMISSIONS`/`_ROLE_GRANTS`/`ON CONFLICT` pattern exactly.

**Real bug found and fixed during this milestone**: the first draft of the migration used a bare `INSERT INTO permissions` for the new permission, like `36ec624cf083`'s comment describes as the *wrong* approach. `e6180fca2ee0` (M2's seed migration) reads `ALL_PERMISSIONS`/`ROLE_PERMISSIONS` **live** at migration-run time, not a frozen snapshot — so on a from-scratch upgrade (e.g. `tests/test_migrations.py`'s own full-cycle test), M2's seed migration already inserts `sales.return.approve` (since it's now part of the live dict) before M14's own migration runs, causing a `UniqueViolation`. Fixed with the same `ON CONFLICT (code) DO UPDATE SET code = EXCLUDED.code RETURNING id` upsert every migration since M4 uses for exactly this reason — caught by `tests/test_migrations.py::test_full_upgrade_downgrade_upgrade_cycle`, not shipped unnoticed.

## 5. Enforcement logic

In `app/modules/sales/service.py`:
- `_compute_line_refund(sale_item, requested_qty)` — factored out of the existing posting loop (no behavior change to the values it computes): returns `(refund_price, discount_refunded, tax_refunded, line_refund_amount)`, pure, no DB write.
- `create_sale_return` computes `total_refund_amount_for_gate` by calling `_compute_line_refund` for every requested line, **after** resolving and validating `sale_items_by_id`/`requested_qty_by_item` (so the Sale row is already locked and quantities already validated) but **before** locking any product row and **before** `db.add(SaleReturn(...))`. It then calls `_resolve_return_approval`.
- `_resolve_return_approval(store, refund_amount, initiating_user_id, approver_username, approver_password, ...)` returns `(approval_required, approved_by)` or raises. Below threshold: returns `(False, None)` immediately, zero behavior change from pre-M14. At/above threshold: requires `approver_username`/`approver_password`, verifies them, checks self-approval, checks the `sales.return.approve` permission, checks store isolation, logs an audit event, and returns `(True, approver.id)`.
- `void_sale` delegates to `create_sale_return` with `_is_void=True` exactly as it always has — the approval gate applies to both automatically, with no special-casing, since they share one code path.

## 6. Authorization model

- **Approver verification**: a new `auth_service.verify_user_credentials(db, username, password) -> User | None`, deliberately *not* a thin wrapper around the existing `authenticate_user` — that function commits immediately and logs a `LOGIN_FAILURE` audit action on failure, which would (a) leak a premature commit into `create_sale_return`'s transaction and (b) mislabel a failed approval attempt as a failed login in the audit trail. The new function reuses `verify_password` and the same dummy-hash timing-safety idiom (so this new credential-check surface has the same timing characteristics as login), but does no logging/committing itself — callers own that.
- **Self-approval**: `approver.id == initiating_user_id` (the id of the user who called the endpoint), strict equality — not username, not role.
- **Permission**: `sales.return.approve` must be in the approver's resolved permission set (`auth_service.get_user_permissions`).
- **Store isolation**: the existing, already-duplicated-in-this-file `_enforce_store_access(approver.store_id, store.id, ...)` — the identical helper `finalize_sale`/`create_sale_return` already use for every other store check in this module. `approver.store_id is None` (Admin) is unrestricted, matching the existing RBAC model exactly.
- **The endpoint's own existing permission gates (`sales.return.write`, `sales.void`) are unchanged** — approval is an independent, additional check inside the service layer, so it cannot be bypassed by a direct API call that only satisfies the write/void permission.
- **Rate limiting**: a new `approval_rate_limiter` (the existing `FixedWindowRateLimiter` class, a new instance/key namespace — same limit/window as `login_rate_limiter`) applied at the endpoint layer whenever an approval attempt is made, since this is a new credential-verification surface outside `/auth/login`/`/auth/refresh` that nginx's rate limiting (M13) does not cover.

## 7. Failure/orphaned-state safety

The gate call happens strictly before `db.add(SaleReturn(...))`. A rejected approval (missing credentials, wrong password, self-approval, no permission, wrong store) raises before any row is created — nothing to roll back. The one exception is the audit trail of the *failed attempt itself*: each failure branch logs an audit row and calls `db.commit()` (mirroring `authenticate_user`'s own pattern) before raising, so a failed approval attempt is permanently recorded even though the return/void itself never exists — deliberate, not a bug: the audit record and the (non-existent) business row are two different things, and only the business row's absence is the correctness property this section is about.

## 8. Concurrency

No new lock is needed for the gate check itself (it's a pure read against already-locked `SaleItem` data plus a fresh credential check). Two above-threshold attempts against the same sale, one with valid approval and one without, are fully independent — each call's own verification is self-contained, so there is no "unlocked" shared state to race over (see §2 — this is a structural consequence of the chosen design, not a separately-enforced property).

## 9. What is explicitly NOT covered (documented limitation, not a hidden gap)

**Per-transaction threshold, not cumulative.** A refund split across multiple separate return calls against the same sale, each individually below threshold, is not detected as "structuring" — each call is evaluated independently. This is the same scope boundary the task's own business rules describe ("Operations below the threshold continue through the existing flow"), pinned by `test_concurrent_returns_cannot_bypass_threshold_by_splitting` rather than silently assumed. Cumulative/structuring detection would be a materially larger feature (tracking refund totals across a rolling window per sale or per cashier-shift) and was not requested.

**No admin UI to set the threshold.** Same precedent as `attendance_day_boundary_hour` (M10) — no API/UI was built to edit it in that milestone either. A production deployment sets it via direct configuration (SQL/admin script) until a future milestone adds store-configuration management generally.

## 10. Rollback strategy

Application-level: setting a store's `return_approval_threshold_amount` back to `NULL` disables the gate for that store instantly, no code change. Database-level: the migration's `downgrade()` is guarded — it refuses (raises, does not silently drop data) if any store has a configured threshold or any `sale_returns` row has `approval_required = true`, mirroring the guard pattern every M7–M9 downgrade uses. Verified live: downgrading `erp_dev` (which has real M14 test data) correctly refuses; downgrading a fresh, unconfigured database succeeds cleanly.
