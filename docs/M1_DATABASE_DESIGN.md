# M1 — Transactional Data Model: Design & Decisions

This document records the M1 milestone's design decisions and explains the
schema in `backend/app/modules/*/models.py` and the migrations in
`backend/alembic/versions/`. Read alongside `docs/TECHNICAL_BLUEPRINT.md`,
which remains the authoritative source for anything this document doesn't
override or refine.

M1's objective: the production-grade transactional data model — not the
POS, purchasing, or accounting UI. One real vertical slice (goods
receiving → Weighted Average Cost) is implemented to prove the
transactional pattern; everything else is schema plus targeted domain
tests proving the model supports the business rules correctly.

---

## 1. M0 Architectural Decisions Resolved

### A. Authentication timing — still deferred

M0 built the identity/RBAC *data model* (`stores`, `users`, `roles`,
`permissions`, `role_permissions`, `user_roles`) but no login/JWT/RBAC
enforcement. M1 keeps that deferred. Reasoning:

- M1's explicit objective is data integrity, not user-facing features.
  Building real authentication now — password hashing, JWT issuance,
  refresh-token rotation, RBAC-enforcing middleware — is a meaningfully
  separate unit of work with its own security review surface, and bolting
  it on incompletely would be worse than not having it yet (M1's
  instructions explicitly forbid an "insecure placeholder").
- The M1 API surface added (`POST/GET /products`, `GET
  /inventory/movements`) is a minimal proof of the model/schema/service/
  route separation, not a real user-facing feature that needs to be
  access-controlled today.
- **Decision**: authentication is a dedicated milestone, done properly
  (argon2id hashing, short-lived JWT + rotating refresh tokens, RBAC
  middleware checked on every route), before any endpoint here is
  exposed beyond local development. Every M1 endpoint's docstring says so
  explicitly. This should happen before or alongside M2, since M2's
  Inventory Service adjustment API is the first genuinely sensitive
  write surface.

### B. Nginx/TLS — still deferred

Unchanged from M0's decision: local development works without a reverse
proxy (CORS handles the Vite dev server ↔ FastAPI split), and M1 adds no
new deployment-topology requirement. Nginx + Let's Encrypt TLS remain
scheduled for Milestone M8 (deployment hardening).

### C. Audit-log & ledger write protection — implemented

The M1 task instructions were explicit: *do not falsely claim that
ownership-level PostgreSQL permissions provide this protection.* They
don't — in PostgreSQL, a table's **owner** always retains full privileges
on it regardless of any `GRANT`/`REVOKE`; ownership bypasses the ACL
system entirely. So "revoke UPDATE/DELETE from the app's DB role" only
means something if the running application connects as a role that is
**not** the table owner.

**What was built:**

1. **`erp_app`** — a new, restricted PostgreSQL role. This is now the
   role the running FastAPI application connects as
   (`DATABASE_URL` in `.env`).
2. **`erp_user`** — unchanged from M0, but now used *only* for running
   Alembic migrations (`MIGRATIONS_DATABASE_URL`). It owns every table
   and therefore needs full DDL/DML rights that `erp_app` deliberately
   lacks.
3. **`backend/scripts/bootstrap_db_roles.sql`** — a small, idempotent
   script a superuser runs once per PostgreSQL cluster to create
   `erp_app`. This is deliberately *not* an Alembic migration: role
   creation is cluster-wide administration (roles aren't scoped to one
   database), and it requires `CREATEROLE`, which the migration-owning
   role should not need or have. In Docker Compose, this script is
   mounted into `/docker-entrypoint-initdb.d/` so it runs automatically
   the first time the `db` volume is created.
4. **Migration `9163f992ddc1`** (DCL, not DDL): grants `erp_app` broad
   `SELECT, INSERT, UPDATE, DELETE` on every table (the correct default —
   most operational tables need full DML from the app), sets `ALTER
   DEFAULT PRIVILEGES` so future tables the migration role creates
   automatically extend the same grant to `erp_app`, and then explicitly
   **revokes `UPDATE, DELETE`** on two tables: `audit_logs` and
   `inventory_movements`. If `erp_app` doesn't exist yet (a developer
   who hasn't run the bootstrap script), the migration logs a `NOTICE`
   and skips the grants rather than failing `alembic upgrade`.

**Why `inventory_movements` too, not just `audit_logs`?** The task
instructions scoped this item to audit logs, but
`docs/TECHNICAL_BLUEPRINT.md` independently describes
`inventory_movements` in the same terms: *"Never updated or deleted
(BR-6); corrections are new offsetting rows."* Since the exact same
technique applies and the blueprint had already committed to the same
invariant, extending the protection to this second ledger table was a
small, low-risk, well-justified addition — verified with the same live
privilege test as `audit_logs` (`tests/test_constraints.py`).

**Proven, not asserted**: `tests/test_constraints.py` connects as the
actual configured runtime role and shows `INSERT` succeeding while
`UPDATE`/`DELETE` raise `permission denied` directly from PostgreSQL.

**Production requirement**: `bootstrap_db_roles.sql` creates `erp_app`
with a placeholder password (`CHANGE_ME_IN_PRODUCTION`). Before any
non-local deployment, rotate it (`ALTER ROLE erp_app WITH PASSWORD
'<random>'`) and update `DATABASE_URL` to match — this is called out in
the script itself and in `.env.example`.

**Future migrations must remember**: `ALTER DEFAULT PRIVILEGES` means a
*new* table gets the broad grant automatically. A future migration that
adds another append-only/ledger-style table (there likely will be more —
e.g. a future `tax_submissions` audit trail) must explicitly `REVOKE
UPDATE, DELETE ... FROM erp_app` on it, following migration
`9163f992ddc1`'s pattern. This is not automatic and is easy to forget —
flagged here and in the migration file's own docstring.

---

## 2–13. Schema

### 2.1 Table list (M1 additions)

| Table | Module | Purpose |
|---|---|---|
| `product_categories` | products | Self-referencing category tree |
| `products` | products | Catalog master + inventory cache |
| `product_barcodes` | products | 1:N barcodes per product |
| `suppliers` | purchasing | Supplier directory |
| `purchase_orders` | purchasing | Order header + lifecycle status |
| `purchase_order_items` | purchasing | Ordered lines (never touch inventory) |
| `goods_receipts` | purchasing | One physical receiving event |
| `goods_receipt_items` | purchasing | Received lines — the only thing that moves inventory |
| `purchase_returns` / `purchase_return_items` | purchasing | Goods sent back to a supplier |
| `inventory_movements` | inventory | **The** append-only ledger — system of record for stock |
| `stock_adjustments` | inventory | Human-facing record behind a manual correction |
| `tax_rates` | tax | Versioned, effective-dated tax rates |
| `sales` / `sale_items` | sales | Sale header + frozen historical line items |
| `payments` | sales | 1:N payments per sale (split tender) |
| `sale_returns` / `sale_return_items` | sales | Customer returns against a completed sale |

Combined with M0's `stores`, `users`, `roles`, `permissions`,
`role_permissions`, `user_roles`, `audit_logs`, this is **25 application
tables**. Every model file cites the blueprint section and/or this
document for the reasoning behind its columns and constraints — see the
module docstrings and per-column comments in
`backend/app/modules/*/models.py` rather than duplicating that detail
here.

### 2.2 Naming change from the blueprint

The blueprint originally named the purchase header/lines `purchases` /
`purchase_items`. The M1 task instructions asked for `purchase_orders` /
`purchase_order_items`, to make the order/receipt distinction
unambiguous directly in the schema (an "order" and a "receipt" are very
different things, and conflating their names invited confusion between
`purchase_items.quantity_received` meaning "ordered-and-received" vs. a
receipt's own quantity). `docs/TECHNICAL_BLUEPRINT.md` Section C.3 has
been updated to match.

### 2.3 Products: barcode vs. SKU, weighed goods

- **A barcode is not assumed to equal the SKU.** `product_barcodes` is a
  separate 1:N table (`barcode` globally unique, `sku` unique per
  store), exactly so a product can have a case-pack barcode, a unit
  barcode, and (later) a weight-embedded barcode without any of them
  being confused with its internal SKU.
- **Weighed goods are explicitly out of scope for M1.** `Product.
  is_weighed` and `unit_of_measure` (`each|kg|g|l|ml`) exist in the
  schema so a weighed product can be represented, but no
  weight-embedded-barcode parser and no scale integration exist yet —
  matching the blueprint's own assumption #6, which scoped the actual
  scale/label-printer integration out as a future hardware project. The
  schema decision (columns exist now) versus the integration decision
  (parser/hardware later) are deliberately separate.

### 2.4 Money and quantity precision (as required, documented explicitly)

| Kind | Type | Used for |
|---|---|---|
| Currency amounts (prices, discounts, tax, totals, payments) | `NUMERIC(12, 2)` | `products.current_price`, `sale_items.unit_price_at_sale`, `sales.grand_total`, `payments.amount`, ... |
| Cost / Weighted Average Cost | `NUMERIC(14, 6)` | `products.current_cost`, `inventory_movements.unit_cost_at_movement`, `sale_items.unit_cost_at_sale`, `purchase_order_items.unit_cost`, `goods_receipt_items.unit_cost` |
| Quantities | `NUMERIC(14, 3)` | `inventory_movements.quantity_delta`, `sale_items.quantity`, `purchase_order_items.quantity_ordered`, ... |
| Tax rate percentage | `NUMERIC(6, 3)` | `tax_rates.rate_percent` |

**No `FLOAT`/`REAL`/`DOUBLE` appears anywhere in the schema**, and every
Python-side ORM attribute backing a `NUMERIC` column is typed
`Mapped[Decimal]` (not `float`) — SQLAlchemy returns `Decimal` for
`Numeric` columns by default, and typing them as `float` would have
silently invited float arithmetic bugs into the one place this project
can least afford them. All WAC/COGS test assertions use `Decimal`
literals for exactly this reason.

**Why 6 decimal places for cost, not 2**: `docs/TECHNICAL_BLUEPRINT.md`
assumption #10, kept as-is. 100 units @ 10 + 50 units @ 14 = 1700/150 =
11.333333... (repeating) — storing only 2 decimal places would round
that to 11.33, and every subsequent WAC recompute would compound that
error. 6 decimal places bounds the rounding error introduced at each
recompute to at most ~5×10⁻⁷, which is immaterial even after thousands
of receipts, and matches the precision real-world ERP systems use for
the same reason.

### 2.5 CHECK constraints — the aggressive, "prove it" set

Beyond ordinary `NOT NULL`/FK/unique constraints, the schema uses `CHECK`
constraints to enforce invariants that would otherwise depend on every
future caller getting the application code right:

- **Non-negativity** on every price/cost/quantity/tax/discount/payment
  column, *except* where a specific business rule requires otherwise
  (see below).
- **Sign-consistency for inventory movements**
  (`ck_inventory_movements_direction`): `PURCHASE_RECEIPT`, `SALE_RETURN`,
  and `STOCK_ADJUSTMENT_IN` must have a positive `quantity_delta`; `SALE`,
  `PURCHASE_RETURN`, and `STOCK_ADJUSTMENT_OUT` must have a negative one.
  This is enforced at the database level — `tests/test_inventory.py::
  test_movement_type_and_sign_must_agree` proves a wrong-signed row is
  rejected even when inserted directly, bypassing the service layer.
- **No blanket "quantity must be positive" on `inventory_movements`**:
  direction is intentionally carried by the sign of a single
  `quantity_delta` column (docs/TECHNICAL_BLUEPRINT.md's own
  representation), so the only universal rule is `quantity_delta <> 0`
  (a zero-quantity movement is meaningless) — never a positivity check
  that would contradict the sign convention.
- **Stored-total consistency**: `sales.grand_total = subtotal -
  discount_total + tax_total` and `sale_items.line_total = round(quantity
  * unit_price_at_sale - discount_amount + tax_amount, 2)` are both
  enforced as `CHECK` constraints tying a stored total to the arithmetic
  of its own row's other columns. These totals are stored (not computed
  on read) because BR-1/BR-5 freeze them once at finalization — the
  `CHECK` is belt-and-suspenders against a future bug ever writing an
  inconsistent value, not a substitute for computing them correctly in
  the first place.
- **Products' negative-stock policy is itself a `CHECK`**:
  `allow_negative_stock OR current_qty_on_hand >= 0`. See §4 below.
- **Deliberately *not* constrained**: `purchase_order_items.
  quantity_received <= quantity_ordered`. `docs/TECHNICAL_BLUEPRINT.md`
  Section F explicitly allows over-receipt ("allowed, flagged"), so no
  such constraint exists — `tests/test_purchasing.py::
  test_over_receipt_is_allowed` proves it.
- **Partial unique indexes** close two real gaps a plain `UNIQUE`
  constraint leaves open in PostgreSQL (NULLs are distinct in a regular
  unique constraint): `product_categories` gets a partial unique index
  on `name WHERE parent_id IS NULL` (two top-level categories can't
  share a name, even though `UNIQUE(parent_id, name)` alone wouldn't
  catch that), and `product_barcodes` gets one on `product_id WHERE
  is_primary` (a product can have at most one primary barcode).

---

## 3. Inventory Ledger

`inventory_movements` is the single source of truth for stock quantity
and cost — never `products.current_qty_on_hand` alone. That column (and
`current_cost`) is a **maintained cache**, updated transactionally
alongside every ledger insert (`app.modules.inventory.service.
record_movement`), and must always equal `SUM(inventory_movements.
quantity_delta)` for that product —
`tests/test_inventory.py::test_qty_on_hand_matches_ledger_sum` asserts
exactly this by running both computations and comparing them, not by
assuming they'll always agree.

**Movement types implemented**: `PURCHASE_RECEIPT`, `SALE`,
`SALE_RETURN`, `PURCHASE_RETURN`, `STOCK_ADJUSTMENT_IN`,
`STOCK_ADJUSTMENT_OUT`.

**`TRANSFER_IN`/`TRANSFER_OUT` are deliberately not included.** The
blueprint's assumption #1 scopes v1 to one store per product row
(`products.store_id`); there is nothing to transfer between yet, so
adding transfer semantics now would be unused, untested surface area.
Add them (and the multi-store product-sharing model they'd require) only
when multi-store is actually implemented.

**Reference pointer is polymorphic, not an enforced FK.**
`inventory_movements.reference_id` points at `purchase_orders.id`,
`sales.id`, `sale_returns.id`, `purchase_returns.id`, or
`stock_adjustments.id` depending on `reference_type` — PostgreSQL has no
native "FK to one of several tables" construct. This is an intentional,
documented denormalization; the alternative (a nullable FK column per
possible reference table) was rejected as needless width for a
lookup-only field that's never joined in a way that requires referential
enforcement (the referencing row is created transactionally alongside
the movement by the same service call in every case that exists today).

**Row-level locking**: `inventory_service.lock_product_for_update`
issues `SELECT ... FOR UPDATE` on the product row before any movement
touches it, so two concurrent operations against the same product
serialize rather than racing (docs/TECHNICAL_BLUEPRINT.md Section D,
edge case 7). `purchasing.service.receive_goods` uses this for every
line it processes.

---

## 4. Weighted Average Cost

Implemented exactly as `docs/TECHNICAL_BLUEPRINT.md` Section D
specifies, in `app.modules.inventory.service.compute_new_wac`:

```
new_wac = (existing_qty * existing_wac + received_qty * received_unit_cost)
          / (existing_qty + received_qty)
```

Computed **fresh from the two (qty, cost) pairs at full `Decimal`
precision** every time — never incrementally adjusted — then rounded
once, at the very end, to the storage precision (6 decimal places).
`tests/test_purchasing.py::test_wac_matches_worked_example_100_at_10_plus_50_at_14`
reproduces the task's exact example (100 @ 10 + 50 @ 14 → 150 @
11.333333) and `test_wac_after_third_purchase_at_a_different_cost` chains
a third receipt at yet another cost to confirm the formula composes
correctly across repeated recomputes.

**Behavior at each scenario the task asked about:**

| Scenario | Behavior |
|---|---|
| Opening inventory (0 → first receipt) | WAC = the receipt's own unit cost exactly (existing side of the formula is zero) |
| Purchase received | WAC recomputed via the formula above; this is the *only* time WAC changes |
| Sale occurs | WAC **unchanged** — a sale consumes stock at the current WAC and freezes that value into `sale_items.unit_cost_at_sale`, but does not move the running average |
| Sales return (restocked) | Physical goods coming back at the cost they left at — **not** re-averaged as if it were a new purchase (docs/TECHNICAL_BLUEPRINT.md Section D) |
| Purchase return | Reverses using the *specific* `goods_receipt_item`'s original cost (the lot being reversed), not the current WAC — WAC has no lot memory, so this is the standard, documented approximation for a pure-WAC system (vs. FIFO) |
| Stock adjustment (in or out) | Removing/adding units at the *existing* average doesn't move the average — WAC unchanged either direction |
| Stock reaches exactly zero, then a new purchase arrives | Handled by the same formula with `existing_qty = 0` — collapses to "opening inventory" again, proven in `test_wac_resets_cleanly_after_stock_reaches_zero_and_reopens` |
| Both sides of the formula are zero (existing = 0 and received = 0) | Would be a division by zero; `compute_new_wac` falls back to the incoming cost rather than raising — this path is a defensive edge case (e.g. reversing the very last unit via a return) rather than a normal flow |
| Negative inventory | See §5 — allowed only per an explicit per-product override, and WAC math still executes correctly against a negative `existing_qty` if that override is set, though the resulting average no longer represents a physical count and should be treated as a data-quality signal, not a "price" |

**Rounding**: WAC values round-half-up to 6 decimal places
(`ROUND_HALF_UP`) only at the point of storage — never mid-calculation.
Displayed monetary values (receipts, reports) round to the currency's 2
decimal places at render time, in a later milestone; nothing in this
schema or code rounds twice.

---

## 5. COGS & Historical Reproducibility

`sale_items` freezes **all four** of the values the task asked about,
independent of later catalog/rate changes:

- `unit_price_at_sale` — the selling price charged
- `unit_cost_at_sale` — the product's WAC at the moment of sale (the COGS basis)
- `discount_amount` — resolved to a fixed amount, never a live formula
- `tax_amount` (+ `tax_rate_id` pointing at the specific historical `tax_rates` row)

`tests/test_sales.py::test_sale_item_freezes_cost_independent_of_later_wac_changes`
proves this concretely: it records a sale at WAC=10, then receives a
second purchase at WAC=50 (moving the product's live cost dramatically),
and asserts the already-recorded sale line is completely unaffected.
`tests/test_tax.py::test_sale_item_keeps_its_historical_tax_rate_after_the_rate_changes`
does the equivalent for tax: retiring a rate and introducing a new one
doesn't touch a sale line that already referenced the old one.

**COGS for a period** = `Σ (sale_items.quantity × sale_items.
unit_cost_at_sale)` — a pure sum over frozen values, with zero
recomputation risk, exactly as `docs/TECHNICAL_BLUEPRINT.md` Section D
specifies. No sale-finalization service exists yet to compute this
automatically (that's M4/M6); M1 proves the *data model* supports it.

---

## 6. Transactional Integrity

The one real vertical slice built in M1 —
`app.modules.purchasing.service.receive_goods` — demonstrates the full
pattern the blueprint's Sales Service will need in M4:

1. Look up and validate the purchase order and its items.
2. Lock the affected product row (`SELECT ... FOR UPDATE`).
3. Recompute WAC from the actual received cost.
4. Insert the `inventory_movements` row and update the product's cached
   qty/cost.
5. Insert the `goods_receipt_items` row.
6. Update `purchase_order_items.quantity_received`.
7. Advance `purchase_orders.status`.

All of this runs inside the caller's ambient session — `receive_goods`
never calls `commit()` itself, so a failure on any line (e.g. line 2 of a
3-line receipt fails `INSUFFICIENT_STOCK` at some future point) rolls
back the *entire* receipt, not just the failed line. `tests/
test_purchasing.py::test_cannot_receive_against_a_cancelled_purchase_order`
and `test_insufficient_stock_blocked_without_allow_negative_stock` prove
the guard rails raise before any partial state is written.

**Deliberately not built in M1**: the full sale-finalization service
(cart → discount → tax → payment → sale). That needs POS-specific
concerns (live cart state, tax-rate resolution, payment reconciliation)
that M1's instructions explicitly said not to build before they're
needed. Instead, `tests/test_sales.py` and `tests/test_tax.py` construct
sales/sale_items/payments rows directly and assert the schema's
invariants hold — proving the data model is correct without requiring
the orchestration code to exist yet.

---

## 7. Returns & Adjustments

| Mechanism | Quantity impact | Cost impact | Financial impact | Audit |
|---|---|---|---|---|
| Sale return (restocked) | `+quantity` via a `SALE_RETURN` movement | Cost = what it left at, not re-averaged | Refund recorded on `sale_returns.refund_amount`; original sale untouched (BR-6) | `sale_returns`/`sale_return_items` are new rows, never edits |
| Sale return (not restocked, e.g. damaged) | None — `restock=false` means no inventory movement | N/A | Refund still recorded | Same as above |
| Purchase return | `-quantity` via a `PURCHASE_RETURN` movement, reversing WAC using the specific lot's cost | WAC recomputed per §4 | No refund record yet (out of scope; suppliers aren't paid through this schema) | New `purchase_returns`/`purchase_return_items` rows |
| Stock adjustment (in/out) | `±quantity` via a `STOCK_ADJUSTMENT_IN`/`_OUT` movement | Unchanged (adjusting at the existing average doesn't move it) | None directly; visible in inventory valuation | `stock_adjustments` row + linked movement, `reason_code` required |

**No mechanism allows editing a historical quantity or amount in place.**
Every one of these is "insert a new, auditable row that offsets the
original," matching BR-6. There is no `UPDATE` path in any service
function that touches `inventory_movements`, `sales`, `sale_items`,
`purchase_orders` (post-receipt), or `audit_logs` — and for the two
tables that most need it enforced beyond "the code doesn't do it,"
§1.C's database-level `REVOKE` backs that up.

---

## 8. Tax Foundation

`tax_rates` is versioned and effective-dated
(`effective_from`/`effective_to`, `is_active`). Nothing in the codebase
hardcodes 18% (or any figure) as a permanent rule — the only place "18%"
appears anywhere in this milestone is as an illustrative value inside a
test docstring, explicitly noted as an example. A `sale_item` references
a specific `tax_rates.id`, so retiring or changing a rate never alters
historical sales (§5).

No tax-authority integration exists yet — `docs/TECHNICAL_BLUEPRINT.md`
Section J's `TaxProvider` interface and `tax_submissions` table remain
scheduled for Milestone M7.

---

## 9. Payments

`payments` supports split tender: a `sales` row can have any number of
`payments` rows (`CASH`, `CARD`, `MOBILE_MONEY`, `BANK_TRANSFER`,
`OTHER`). This was **not** deferred — the blueprint had already committed
to supporting it (assumption #7), and modeling it up front is cheap
(one extra table) versus retrofitting it later (a schema migration plus
rewriting whatever assumed exactly one payment per sale).

There is no database-level constraint tying `SUM(payments.amount)` to
`sales.grand_total` — PostgreSQL `CHECK` constraints cannot span rows or
tables. `tests/test_sales.py::test_split_tender_payments_sum_to_grand_total`
proves the schema can represent a correctly-reconciled split payment;
enforcing that reconciliation at write time is the sale-finalization
service's job in a later milestone (docs/TECHNICAL_BLUEPRINT.md Section
E, step 8).

---

## 10. Audit Model

Unchanged structurally from M0's `audit_logs` (actor, action, entity
type/ID, before/after JSON, timestamp). M1's contribution is the
database-level write protection described in §1.C, extended to
`inventory_movements` as the same kind of ledger. No code in M1 writes
audit log entries yet — that begins in earnest with Milestone M5, once
there are privileged mutations (voids, refunds, overrides) worth logging.

---

## 11. Unresolved Business Decisions Requiring Confirmation

1. **Negative-stock policy default.** The blueprint's assumption #12 says
   "disallowed by default, configurable per product." M1 implements
   exactly that (`products.allow_negative_stock`, defaulting to
   `false`, enforced by a `CHECK` constraint). This is a safe default,
   not a business-confirmed one — flag for sign-off before any store
   actually needs to sell backordered items.
2. **Purchase-return WAC approximation.** Reversing at the specific
   `goods_receipt_item`'s cost (not current WAC) is the standard
   approach for a pure-WAC system, but it is an approximation compared
   to true FIFO lot tracking. Documented in the blueprint (Section D,
   edge case 6) as an accepted limitation — flagged again here since it
   compounds if a product has had many receipts at different costs
   between the original receipt and the return.
3. **`erp_app` production password rotation** is a manual step
   (`bootstrap_db_roles.sql`'s trailing comment) — there is no automated
   secrets-rotation mechanism in this milestone. Must happen before any
   non-local deployment.
4. **Extending ledger write-protection to more tables.** `sales`,
   `sale_items`, `purchase_orders` (post-receipt), and similar
   "financially significant, never edited" tables per BR-6 are *not*
   yet locked down at the database-grant level the way `audit_logs`/
   `inventory_movements` are — application code simply doesn't provide
   an update path today. Worth revisiting once the sale-finalization
   and void/return services exist and their exact required operations
   are known (over-restricting now risks blocking a legitimate update
   path a later milestone needs, like transitioning `sales.status`).
5. **`purchase_order_items` over-receipt has no upper flag/report yet.**
   The schema allows it (per the blueprint); nothing surfaces "this PO
   was over-received" to an operator yet — that's a reporting concern
   for a later milestone.
