# Grocery Store ERP/POS — Technical Blueprint

Status: **Analysis phase — no application code written.** This document is the authoritative technical blueprint for implementation. It converts the project specification into requirements, architecture, database design, business logic, workflows, APIs, and a delivery roadmap.

---

## Ambiguities Identified & Assumptions Made

The specification does not pin down several operational details. To keep the design concrete, the following assumptions are made explicitly. Each can be revisited before/at the milestone where it first matters.

| # | Ambiguity | Assumption | Why safe |
|---|---|---|---|
| 1 | Single store or multi-branch? | Single store/location for v1; schema includes a `stores` table and `store_id` FKs from day one so multi-branch is additive, not a rewrite. | No stated multi-branch requirement; cheap to future-proof. |
| 2 | Currency | Single local currency, configurable via config table. No multi-currency conversion. | Not mentioned in spec. |
| 3 | Tax authority | No authority named. A **pluggable tax-adapter interface** is designed (see Section J); no authority-specific payload/signing is assumed. Given likely deployment region (East Africa, TZS context from prior conversations), TRA's EFD/VFD model is the most probable future target, but nothing here hardcodes that — confirm official docs before building the adapter. | Spec explicitly forbids assuming undocumented tax rules. |
| 4 | User roles | Admin, Manager, Cashier, Inventory Clerk, Auditor (read-only). Configurable via `roles`/`permissions` tables, not hardcoded enums. | Standard grocery ERP role set; RBAC table design absorbs any relabeling. |
| 5 | Returns/refunds policy | Both full and partial returns supported, tied to the original sale, requiring Manager/Admin approval above a configurable threshold. | Needed for accounting integrity; conservative default (approval gate) is safest. |
| 6 | Weighed items (produce, deli) | Supported via "embedded-weight" barcodes (common retail convention: prefix digit + PLU/SKU + weight or price encoded in the barcode) in addition to standard EAN-13/UPC-A. Scale hardware integration is out of scope for v1, flagged as a future milestone. | Grocery stores near-universally need this; deferring the scale integration (a hardware/serial-protocol project) keeps v1 scoped. |
| 7 | Split tender payments | Supported: one sale can have multiple `payments` rows (cash + card, etc.). | Common POS requirement; cheap to model up front, expensive to retrofit. |
| 8 | Offline operation | Out of scope for v1 (online-only, single DB). Documented as a risk (Section N) given "$20/month" hosting may have availability limits. | Spec doesn't ask for offline; offline-first would multiply complexity (conflict resolution, local queues). |
| 9 | Scale (terminals, users, SKUs) | Small-to-medium grocery: 1–5 POS terminals, <20 concurrent users, up to ~50,000 SKUs, thousands of sales/day. Sized for a single small VPS. | Matches "$20/month" deployment target. |
| 10 | Numeric precision | Money: `NUMERIC(12,2)` for stored monetary amounts, `NUMERIC(14,6)` intermediate cost/quantity fields where weighted items need sub-unit precision (e.g., kg to 3 decimals, unit cost to 4–6 decimals to avoid WAC rounding drift), rounded to currency precision only at the final displayed/posted amount. | Prevents cumulative rounding errors in WAC while keeping receipts human-correct. |
| 11 | Discounts | Supported at line-item and sale (cart) level, expressed as either fixed amount or percentage, always resolved to a stored fixed amount at sale time (never a live formula) for auditability. | Spec requires historical accuracy; storing resolved amounts avoids retroactive drift if a discount rule later changes. |
| 12 | Negative stock | Disallowed by default (hard block at sale time) but configurable per store/product to "allow with warning" for edge cases (e.g., backorder-friendly categories). Default is the safe option. | Spec explicitly calls out negative inventory as an edge case to define, not to silently permit. |

Everything else below builds on these assumptions plus the specification's explicit requirements. Explicit spec requirements are labeled **[SPEC]**; anything added by engineering judgment is labeled **[ENG]**.

---

## A. Requirements Analysis

### A.1 Functional Requirements

**Product & Inventory**
- FR-1 **[SPEC]** Manage products: SKU, barcode(s), name, description, category, unit of measure, price, cost, tax class, active/inactive status.
- FR-2 **[SPEC]** Track stock quantity per product (per store, per assumption #1).
- FR-3 **[SPEC]** Record all stock movements (purchase receipt, sale, return, adjustment, transfer) with a running audit trail.
- FR-4 **[ENG]** Support multiple barcodes per product (case pack barcode, unit barcode, weighed-item barcode).
- FR-5 **[ENG]** Support low-stock thresholds and reorder point reporting.

**POS / Sales**
- FR-6 **[SPEC]** Barcode-driven checkout: scan → lookup → add to cart → adjust quantity → discount → tax → pay → finalize → receipt.
- FR-7 **[SPEC]** Generate receipts (printable/PDF) per sale.
- FR-8 **[ENG]** Support split-tender payments (assumption #7).
- FR-9 **[ENG]** Support voids (pre-finalization) and returns/refunds (post-finalization) with manager approval gating.
- FR-10 **[SPEC]** Every sale must atomically update inventory — no partial commits.

**Suppliers & Purchasing**
- FR-11 **[SPEC]** Maintain supplier records.
- FR-12 **[SPEC]** Create purchase orders against suppliers.
- FR-13 **[SPEC]** Record goods receiving against purchase orders, including partial deliveries.
- FR-14 **[SPEC]** Recalculate Weighted Average Cost on every goods receipt.

**Accounting / Reporting**
- FR-15 **[SPEC]** Profit & Loss reporting (gross sales, COGS, gross profit).
- FR-16 **[ENG]** Daily/period sales summaries, inventory valuation, stock movement reports, supplier purchase history.
- FR-17 **[SPEC]** Tax calculation on sales, structured for future tax-authority API submission.

**Users & Security**
- FR-18 **[SPEC]** Authentication and role-based authorization.
- FR-19 **[SPEC]** Audit logging of sensitive actions (price changes, voids, refunds, stock adjustments, user management).

### A.2 Non-Functional Requirements
- NFR-1 **[SPEC]** Responsive UI usable on desktop, tablet, and mobile (POS primarily tablet/desktop with a scanner).
- NFR-2 **[SPEC]** Deployable on low-cost cloud infra (~$20/month).
- NFR-3 **[ENG]** POS scan-to-cart round trip < 300ms server-side under normal load.
- NFR-4 **[ENG]** 99%+ uptime target on a single small VPS (documented as best-effort, not HA, given budget).
- NFR-5 **[SPEC]** Data integrity under concurrent sales (no stock corruption).
- NFR-6 **[ENG]** Automated nightly encrypted backups with tested restore procedure.
- NFR-7 **[ENG]** Horizontal extensibility: new stores, new payment types, new tax authority without core rewrite.

### A.3 Business Rules
- BR-1 **[SPEC]** Money is never computed client-side for authoritative totals; server recomputes and validates every total before persisting.
- BR-2 **[SPEC]** Historical selling price and cost must be preserved on every sale/purchase line, independent of later catalog price changes.
- BR-3 **[SPEC]** Inventory changes are transactional: an entire sale succeeds or the whole thing rolls back.
- BR-4 **[ENG]** Cost basis is Weighted Average Cost (WAC), recalculated on receipt, never on sale.
- BR-5 **[ENG]** A sale's tax and discount are computed once, at finalization, and frozen into the record (BR-2 extended to tax/discount).
- BR-6 **[SPEC]** Financially significant rows (sales, purchases, payments, movements) are never hard-deleted; corrections are new offsetting records (voids/returns/credit notes), preserving the audit trail.
- BR-7 **[ENG]** Negative stock is blocked by default; any allowed exception is explicit, logged, and configurable per product/store.

### A.4 Data Requirements
- Every monetary and quantity field typed `NUMERIC`, never floating point **[SPEC]**.
- Every transactional table carries `created_at`, `created_by`, and where mutable, `updated_at` **[ENG]**.
- Referential integrity via FKs; uniqueness on barcodes, SKUs, usernames **[SPEC]**.
- Full before/after audit trail for privileged mutations **[SPEC]**.

### A.5 Security Requirements
- Password hashing (bcrypt/argon2), session/JWT-based auth, RBAC enforced server-side on every endpoint, input validation, parameterized queries only, rate limiting on auth endpoints, HTTPS everywhere, secrets outside source control **[SPEC + ENG, detailed in Section H]**.

### A.6 Reporting Requirements
- P&L, daily sales, gross margin %, COGS, tax collected, inventory valuation, stock movement history, supplier purchase totals — all derived from transaction tables, never hand-edited **[SPEC + ENG, Section G]**.

### A.7 Tax Requirements
- Tax computed server-side per sale line using a `tax_rates` table; schema designed so a future fiscal-invoice/tax-authority integration is an additive service, not a core rewrite **[SPEC]**.

### A.8 Deployment Requirements
- Docker Compose stack (app, API, Postgres, Nginx), Let's Encrypt HTTPS, deployable on a ~$20/month VPS **[SPEC]**.

---

## B. System Architecture

```
                                   ┌─────────────────────────────┐
                                   │        End Users            │
                                   │ (Cashier / Manager / Admin) │
                                   └──────────────┬───────────────┘
                                                  │ HTTPS
                                   ┌──────────────▼───────────────┐
                                   │   Frontend (React + Tailwind)│
                                   │   SPA served as static build │
                                   └──────────────┬───────────────┘
                                                  │ REST/JSON (HTTPS, JWT)
                                   ┌──────────────▼───────────────┐
                                   │        Nginx (reverse proxy) │
                                   │   TLS termination, static +  │
                                   │   API routing, rate limiting │
                                   └──────────────┬───────────────┘
                                                  │
                                   ┌──────────────▼───────────────┐
                                   │     Backend API (FastAPI)    │
                                   │  ┌─────────────────────────┐ │
                                   │  │ Auth & RBAC middleware   │ │
                                   │  ├─────────────────────────┤ │
                                   │  │ Inventory Service        │ │
                                   │  │ Sales/POS Service        │ │
                                   │  │ Purchasing Service       │ │
                                   │  │ Accounting/Reporting Svc │ │
                                   │  │ Tax Integration Service  │ │
                                   │  │ Audit Logging (cross-cut)│ │
                                   │  └─────────────────────────┘ │
                                   └──────────────┬───────────────┘
                                                  │ SQL (transactions)
                                   ┌──────────────▼───────────────┐
                                   │        PostgreSQL             │
                                   │  ACID transactions, row locks,│
                                   │  NUMERIC types, constraints   │
                                   └──────────────┬───────────────┘
                                                  │
                                   ┌──────────────▼───────────────┐
                                   │   Background Jobs (worker)    │
                                   │  nightly backups, report      │
                                   │  pre-aggregation, tax retry   │
                                   │  queue, low-stock alerts      │
                                   └────────────────────────────────┘
```

### Authentication
JWT access tokens (short-lived, ~15 min) + refresh tokens (httpOnly secure cookie, rotated on use, revocable via a `refresh_tokens` table). Login validates against `users.password_hash` (argon2id). **[ENG choice, standard for SPA+API]**

### Authorization
RBAC: `users` → `user_roles` → `roles` → `role_permissions` → `permissions`. Every API route declares a required permission; middleware checks it against the authenticated user's resolved permission set before the handler executes. No client-side-only gating.

### Inventory Service
Owns `products`, `inventory_movements`, WAC recalculation. Every mutation to on-hand quantity or cost goes through this service inside a DB transaction with row-level locking (`SELECT ... FOR UPDATE` on the product's inventory row) to serialize concurrent updates (NFR-5).

### Sales Service
Owns cart → sale finalization. Talks to Inventory Service (deduct stock, get current WAC for COGS) and Accounting/Reporting (post the sale's financial facts) inside one DB transaction.

### Purchasing Service
Owns `purchases`, `purchase_items`, goods receiving. On receipt, calls Inventory Service to increase stock and recompute WAC.

### Accounting/Reporting Service
Read-mostly service that aggregates from `sales`, `sale_items`, `purchases`, `inventory_movements`, `payments`. Never stores editable derived totals as source of truth — only cached/materialized for performance (Section G).

### Tax Integration Service
Isolated module behind an interface (`TaxProvider`), so core POS calls `tax_provider.calculate(sale)` and, later, `tax_provider.submit(invoice)` without knowing which authority is behind it (Section J).

### Audit Logging
Cross-cutting: a DB trigger-independent application-layer logger (`audit_logs` table) records actor, action, entity, before/after diff (JSON), timestamp, IP, for every privileged mutation (price edits, voids, refunds, stock adjustments, user/role changes, purchase edits).

### Background Jobs
A lightweight worker (same codebase, run via cron inside the container or a `arq`/`celery`-style queue backed by Postgres or Redis) handles: nightly `pg_dump` backups, tax-submission retry queue, low-stock notifications, materialized report refresh.

### Backup Strategy
Nightly `pg_dump` (custom format) to encrypted off-box storage (e.g., S3-compatible bucket), 14-day retention minimum, weekly restore test in staging (Section L).

---

## C. Database Design

Naming: snake_case, singular concept/plural table names, `id BIGSERIAL PRIMARY KEY` unless noted, `created_at TIMESTAMPTZ NOT NULL DEFAULT now()` on all tables, `updated_at TIMESTAMPTZ` where rows are mutable.

### C.1 Identity & Access

**`stores`**
- Purpose: physical location (assumption #1 future-proofing).
- Columns: `id`, `name`, `address`, `timezone`, `is_active`, `created_at`.
- PK: `id`.

**`users`**
- Purpose: system accounts.
- Columns: `id`, `store_id FK→stores`, `username` (unique), `email` (unique), `password_hash`, `full_name`, `is_active`, `created_at`, `updated_at`, `last_login_at`.
- Unique: `username`, `email`.
- Indexes: `store_id`.

**`roles`**
- Columns: `id`, `name` (unique, e.g. Admin/Manager/Cashier/Inventory Clerk/Auditor), `description`.

**`permissions`**
- Columns: `id`, `code` (unique, e.g. `sales.void`, `products.write`), `description`.

**`role_permissions`**
- Columns: `role_id FK→roles`, `permission_id FK→permissions`. PK: composite `(role_id, permission_id)`.

**`user_roles`**
- Columns: `user_id FK→users`, `role_id FK→roles`. PK: composite `(user_id, role_id)`.

**`refresh_tokens`**
- Columns: `id`, `user_id FK→users`, `token_hash`, `expires_at`, `revoked_at`, `created_at`, `user_agent`, `ip_address`.
- Index: `user_id`, `token_hash` unique.

### C.2 Catalog & Inventory

**`product_categories`**
- Columns: `id`, `name`, `parent_id FK→product_categories NULL` (self-referencing tree), `is_active`.
- Unique: `(parent_id, name)`.

**`suppliers`**
- Columns: `id`, `name`, `contact_name`, `phone`, `email`, `address`, `tax_id`, `is_active`, `created_at`.

**`products`**
- Purpose: catalog master record. Current price/cost are *reference* values; historical values live in transaction lines (BR-2).
- Columns: `id`, `store_id FK→stores`, `sku` (unique per store), `name`, `description`, `category_id FK→product_categories`, `default_supplier_id FK→suppliers NULL`, `unit_of_measure` (enum: `each`, `kg`, `g`, `l`, `ml`), `is_weighed BOOLEAN`, `current_price NUMERIC(12,2)`, `current_cost NUMERIC(14,6)` (WAC, maintained by Inventory Service, never edited directly), `tax_class_id FK→tax_rates`, `reorder_point NUMERIC(14,3) NULL`, `is_active`, `created_at`, `updated_at`.
- Unique: `(store_id, sku)`.
- Indexes: `category_id`, `default_supplier_id`.

**`product_barcodes`**
- Purpose: one-to-many barcodes per product (case pack, unit, weighed-item prefix — assumption #6).
- Columns: `id`, `product_id FK→products`, `barcode` (unique), `barcode_type` (enum: `EAN13`, `UPC_A`, `CODE128`, `WEIGHT_EMBEDDED`, `INTERNAL`), `pack_quantity NUMERIC(14,3) DEFAULT 1` (e.g., case of 24), `is_primary BOOLEAN`.
- Unique: `barcode`.
- Index: `product_id`.

**`inventory_movements`**
- Purpose: single source of truth ledger for every stock change. Append-only.
- Columns: `id`, `store_id FK→stores`, `product_id FK→products`, `movement_type` (enum: `PURCHASE_RECEIPT`, `SALE`, `SALE_RETURN`, `PURCHASE_RETURN`, `ADJUSTMENT_IN`, `ADJUSTMENT_OUT`, `TRANSFER_IN`, `TRANSFER_OUT`), `quantity_delta NUMERIC(14,3)` (signed: + increases stock, - decreases), `unit_cost_at_movement NUMERIC(14,6)` (WAC at time of movement, frozen), `resulting_quantity_on_hand NUMERIC(14,3)` (snapshot after applying, for fast audit/debug), `reference_type` (enum: `sale`, `purchase`, `adjustment`, `transfer`), `reference_id BIGINT` (polymorphic pointer to `sales.id`/`purchases.id`/`stock_adjustments.id`), `reason` TEXT NULL, `created_by FK→users`, `created_at`.
- Indexes: `(product_id, created_at)`, `(reference_type, reference_id)`.
- Never updated or deleted (BR-6); corrections are new offsetting rows.

**`stock_adjustments`**
- Purpose: manual corrections (shrinkage, damage, stocktake) — the human-facing record that generates an `inventory_movements` row.
- Columns: `id`, `store_id FK→stores`, `product_id FK→products`, `quantity_delta NUMERIC(14,3)`, `reason_code` (enum: `DAMAGE`, `THEFT`, `EXPIRY`, `STOCKTAKE_CORRECTION`, `OTHER`), `notes`, `created_by FK→users`, `approved_by FK→users NULL`, `created_at`.

### C.3 Purchasing

**`purchases`**
- Purpose: purchase order + its receiving lifecycle in one header (status-driven).
- Columns: `id`, `store_id FK→stores`, `supplier_id FK→suppliers`, `purchase_number` (unique, human-readable), `status` (enum: `DRAFT`, `ORDERED`, `PARTIALLY_RECEIVED`, `RECEIVED`, `CANCELLED`), `order_date`, `expected_date NULL`, `notes`, `created_by FK→users`, `created_at`, `updated_at`.
- Unique: `purchase_number`.

**`purchase_items`**
- Columns: `id`, `purchase_id FK→purchases`, `product_id FK→products`, `quantity_ordered NUMERIC(14,3)`, `quantity_received NUMERIC(14,3) DEFAULT 0`, `unit_cost NUMERIC(14,6)` (cost at time of order — frozen, BR-2), `line_total NUMERIC(14,2)` (generated: `quantity_ordered * unit_cost`, informational).
- Index: `purchase_id`.

**`goods_receipts`**
- Purpose: one row per physical receiving event against a purchase (supports partial deliveries, BR from spec item F).
- Columns: `id`, `purchase_id FK→purchases`, `received_date`, `received_by FK→users`, `notes`, `created_at`.

**`goods_receipt_items`**
- Columns: `id`, `goods_receipt_id FK→goods_receipts`, `purchase_item_id FK→purchase_items`, `quantity_received NUMERIC(14,3)`, `unit_cost NUMERIC(14,6)` (actual invoiced cost for this receipt — may differ from PO cost; this is the value WAC recalculation uses), `condition_notes` (e.g., damaged, short-shipped).
- Index: `goods_receipt_id`, `purchase_item_id`.
- Each row here triggers one `inventory_movements` row (`PURCHASE_RECEIPT`) and a WAC recompute (Section D).

**`purchase_returns`** / **`purchase_return_items`**
- Purpose: returning goods to a supplier (damaged/wrong item).
- `purchase_returns`: `id`, `purchase_id FK→purchases`, `store_id`, `return_date`, `reason`, `created_by`, `created_at`.
- `purchase_return_items`: `id`, `purchase_return_id FK→purchase_returns`, `product_id FK→products`, `quantity NUMERIC(14,3)`, `unit_cost NUMERIC(14,6)` (cost at which it was received, for correct WAC reversal).
- Generates `inventory_movements` (`PURCHASE_RETURN`, negative delta).

### C.4 Sales / POS

**`sales`**
- Purpose: sale (receipt) header. Immutable once `status = COMPLETED` except for status transitions to `VOIDED`/`RETURNED`.
- Columns: `id`, `store_id FK→stores`, `sale_number` (unique, human-readable/receipt number), `cashier_id FK→users`, `status` (enum: `OPEN`, `COMPLETED`, `VOIDED`, `PARTIALLY_RETURNED`, `RETURNED`), `subtotal NUMERIC(12,2)`, `discount_total NUMERIC(12,2) DEFAULT 0`, `tax_total NUMERIC(12,2) DEFAULT 0`, `grand_total NUMERIC(12,2)`, `amount_tendered NUMERIC(12,2) NULL`, `change_due NUMERIC(12,2) NULL`, `voided_by FK→users NULL`, `voided_reason TEXT NULL`, `completed_at TIMESTAMPTZ NULL`, `created_at`.
- Unique: `sale_number`.
- Indexes: `(store_id, created_at)`, `cashier_id`.
- `grand_total` is a stored, computed-once value (BR-1/BR-5) — recomputed server-side and validated against the sum of `sale_items` + tax − discount before commit, never trusted from the client.

**`sale_items`**
- Purpose: line items — the historical record of what was actually sold, at what price/cost/tax (BR-2).
- Columns: `id`, `sale_id FK→sales`, `product_id FK→products`, `quantity NUMERIC(14,3)`, `unit_price_at_sale NUMERIC(12,2)` (frozen), `unit_cost_at_sale NUMERIC(14,6)` (WAC snapshot, frozen — this is what COGS/gross-profit reporting uses), `discount_amount NUMERIC(12,2) DEFAULT 0` (resolved amount, assumption #11), `tax_rate_id FK→tax_rates`, `tax_amount NUMERIC(12,2) DEFAULT 0`, `line_total NUMERIC(12,2)` (`quantity*unit_price_at_sale - discount_amount + tax_amount`), `created_at`.
- Index: `sale_id`, `product_id`.

**`payments`**
- Purpose: supports split tender (assumption #7).
- Columns: `id`, `sale_id FK→sales`, `payment_method` (enum: `CASH`, `CARD`, `MOBILE_MONEY`, `OTHER`), `amount NUMERIC(12,2)`, `reference` (e.g., card auth code/mobile money txn id), `created_at`.
- Index: `sale_id`.

**`sale_returns`**
- Purpose: customer return/refund against a completed sale (BR-6: never edit the original sale).
- Columns: `id`, `sale_id FK→sales`, `store_id`, `return_number` (unique), `reason`, `refund_method` (enum matches `payment_method`), `refund_amount NUMERIC(12,2)`, `processed_by FK→users`, `approved_by FK→users NULL` (assumption #5 approval gate), `created_at`.

**`sale_return_items`**
- Columns: `id`, `sale_return_id FK→sale_returns`, `sale_item_id FK→sale_items`, `quantity NUMERIC(14,3)`, `unit_price_refunded NUMERIC(12,2)`, `restock BOOLEAN DEFAULT true` (damaged returns may not go back to sellable stock).
- Generates `inventory_movements` (`SALE_RETURN`, positive delta) only when `restock = true`.

### C.5 Tax

**`tax_rates`**
- Purpose: versioned tax rules (rate changes over time must not corrupt historical sales — BR-2).
- Columns: `id`, `name` (e.g., "Standard VAT", "Zero-rated"), `rate_percent NUMERIC(6,3)`, `is_active`, `effective_from DATE`, `effective_to DATE NULL`.
- Index: `(is_active, effective_from)`.
- `sale_items.tax_rate_id` points to the specific version applied at sale time — never recalculated retroactively.

**`tax_submissions`** *(future integration, Section J)*
- Columns: `id`, `sale_id FK→sales`, `provider_code`, `status` (enum: `PENDING`, `SUBMITTED`, `ACKNOWLEDGED`, `FAILED`), `request_payload JSONB`, `response_payload JSONB`, `fiscal_reference` NULL, `attempt_count`, `last_attempted_at`, `created_at`.
- Index: `sale_id`, `status`.

### C.6 Audit

**`audit_logs`**
- Columns: `id`, `user_id FK→users NULL` (nullable for system/background actions), `action` (e.g., `SALE_VOID`, `PRICE_CHANGE`, `USER_ROLE_CHANGE`), `entity_type`, `entity_id`, `before_state JSONB NULL`, `after_state JSONB NULL`, `ip_address`, `user_agent`, `created_at`.
- Index: `(entity_type, entity_id)`, `created_at`, `user_id`.
- Append-only, no update/delete permission at the application layer.

### C.7 Config

**`store_settings`**
- Key-value per store (currency code, receipt footer text, negative-stock policy, return-approval threshold) — `id`, `store_id FK→stores`, `key`, `value JSONB`. Unique `(store_id, key)`.

### C.8 Relationship Summary
```
stores 1─* users, products, sales, purchases
users *─* roles (via user_roles) ; roles *─* permissions (via role_permissions)
product_categories 1─* products (self-referencing tree)
suppliers 1─* purchases
products 1─* product_barcodes, inventory_movements, sale_items, purchase_items
purchases 1─* purchase_items, goods_receipts
goods_receipts 1─* goods_receipt_items
sales 1─* sale_items, payments; sales 1─* sale_returns
sale_returns 1─* sale_return_items
tax_rates 1─* sale_items, sales(via items) ; sales 1─(0..1)─* tax_submissions
every mutation-worthy table → audit_logs (polymorphic, not FK-enforced by design)
```

---

## D. Inventory Accounting Logic (Weighted Average Cost)

**Formula:**
```
New WAC = (Existing Qty * Existing WAC + Received Qty * Received Unit Cost)
          ───────────────────────────────────────────────────────────────
                        Existing Qty + Received Qty
```
Applied **only on receipt of goods** (purchases, positive adjustments, restocked returns) — never recalculated on sale. Sales consume stock at the *current* WAC and freeze that value into `sale_items.unit_cost_at_sale`.

### Worked Examples

**Opening stock:** Product has 0 on hand. First receipt: 100 units @ 10.00.
`WAC = (0*0 + 100*10.00) / 100 = 10.00`. On-hand: 100.

**Second purchase at a different cost:** Receive 50 units @ 12.00.
`WAC = (100*10.00 + 50*12.00) / 150 = (1000+600)/150 = 10.6667`. On-hand: 150.

**Sale:** Sell 20 units. `unit_cost_at_sale = 10.6667` (frozen). On-hand: 130. WAC unchanged (sales don't alter WAC) — 10.6667.

**Sales return (restocked):** Customer returns 5 units from that sale. Inventory movement `SALE_RETURN +5` **does not re-average** — the cost of the returned stock is the cost it left at (10.6667), since it's the same physical goods coming back, not a new purchase. On-hand: 135, WAC stays 10.6667.

**Stock adjustment (found extra stock / shrinkage):**
- Shrinkage (write-off, `ADJUSTMENT_OUT` -3): on-hand 132, WAC unchanged (removing units at existing WAC doesn't change the average of what remains).
- Found stock with no cost basis (`ADJUSTMENT_IN` +2, cost = current WAC by convention, since no purchase cost exists): on-hand 134, WAC unchanged (adding at the existing average doesn't move it).

**Purchase return (goods sent back to supplier):** Return 10 units that were received at 12.00 (the specific receipt lot). `PURCHASE_RETURN -10` at unit_cost 12.00. New WAC recomputed as a *reversal*:
```
New WAC = (Existing Qty*Existing WAC − Returned Qty*Returned Unit Cost)
          ─────────────────────────────────────────────────────────────
                        Existing Qty − Returned Qty
```
This requires knowing which cost lot is being reversed — see Edge Cases below on why WAC (not FIFO) makes this approximate.

### COGS & Gross Profit
- **COGS for a sale** = `Σ (sale_items.quantity * sale_items.unit_cost_at_sale)` for the period. Because cost is frozen per line, COGS is a pure sum — no recomputation risk.
- **Gross Profit** = `Σ sale_items.line_total (net of tax, net of discount)` − `COGS`.
- **Gross Margin %** = `Gross Profit / Net Sales * 100`.

### Edge Cases
1. **Selling at zero stock**: blocked by default (BR-7). API returns `409 Conflict` with `INSUFFICIENT_STOCK`. If a store enables the "allow negative" override, the sale proceeds, `unit_cost_at_sale` uses the last known WAC, and the resulting movement drives quantity negative — flagged in a daily exception report.
2. **Negative inventory**: allowed only per the override in #1; always visible in a "negative stock" report; WAC math still works (average of a negative and a positive quantity is mathematically valid but must be treated with a warning banner, since a WAC computed against negative stock does not represent a physical inventory reality).
3. **Multiple purchases at different costs**: handled natively by the weighted-average formula (see examples above) — this is WAC's whole purpose, unlike FIFO/LIFO which would need cost-lot tracking.
4. **Partial receiving**: each `goods_receipt` is its own event; `purchase_items.quantity_received` accumulates; `purchases.status` moves `ORDERED → PARTIALLY_RECEIVED → RECEIVED` when `quantity_received == quantity_ordered` for all lines (or is manually closed short).
5. **Cancelled purchases**: a `purchase` can move to `CANCELLED` only while `status IN (DRAFT, ORDERED)` and no `goods_receipts` exist against it — no inventory impact, no reversal needed since nothing was posted yet. Once any receipt exists, the PO cannot be cancelled; use a purchase return instead.
6. **Returned goods (purchase return) needing a specific cost lot**: pure WAC has no lot memory, so the return uses the unit cost **recorded on the goods_receipt_item being reversed** (not the current WAC) to correctly back out that value, per the reversal formula above. This is the standard, defensible approach for a WAC system (documented limitation vs. FIFO — acceptable per spec's "Weighted Average Cost" requirement).
7. **Concurrent sales on the same product**: prevented from corrupting quantity via a DB-level row lock. The Inventory Service acquires `SELECT ... FOR UPDATE` on the product's inventory row (or uses an atomic `UPDATE products SET qty = qty - :n WHERE id=:id AND qty >= :n RETURNING qty` pattern) inside the sale transaction; a failed conditional update surfaces as `INSUFFICIENT_STOCK` and the whole sale transaction rolls back (never partial). Postgres's transaction isolation (READ COMMITTED + row locking, or SERIALIZABLE for the hot path if contention proves an issue) guarantees no lost updates.

---

## E. Sales / POS Workflow

1. **Scanner input** — USB/Bluetooth scanners emit keystrokes + Enter (HID keyboard emulation, per spec). Frontend keeps a hidden, always-focused input capturing rapid keystroke bursts terminated by Enter, distinguishing scans from manual typing by inter-keystroke timing (<30–50ms typical for scanners) as a UX nicety only — **correctness never depends on this heuristic**, since the same code path is used for manual SKU entry.
2. **Barcode lookup** — `GET /api/v1/products/barcode/{code}` resolves via `product_barcodes`, returns product + current price + tax class + on-hand qty (informational only).
3. **Product retrieval** — server returns canonical price/tax/name; frontend never sources these values from a local cache it can't trust for the final transaction.
4. **Cart creation** — client-side cart state (line items, quantities) is provisional; nothing is persisted server-side until finalization (an `OPEN` sale row may optionally be created early for crash-recovery, but is not required for MVP).
5. **Quantity changes** — client-side; for weighed items, quantity may come from the embedded-weight barcode or manual entry.
6. **Discounts** — cashier/manager applies a discount (permission-gated for manager-level discounts); resolved to a fixed amount client-side for display, but recomputed and re-validated server-side at finalization (BR-1).
7. **Tax calculation** — server-side only, using each line's `product.tax_class_id` → active `tax_rates` row as of sale date. Never trust a client-submitted tax amount.
8. **Payment** — one or more `payments` rows collected (split tender); sum must equal `grand_total` (server-validated) before the sale can complete.
9. **Sale finalization** — single DB transaction: (a) lock and validate stock for every line, (b) insert `sales` + `sale_items` + `payments`, (c) insert `inventory_movements` (`SALE`, negative delta) per line, freezing `unit_cost_at_sale` from current product WAC, (d) write `audit_logs` entry, (e) commit. Any failure at any step → full rollback, client receives an explicit error, cart is preserved client-side for retry (no silent partial sale — BR-3/FR-10).
10. **Inventory transaction** — covered in step 9(c); this is not a separate step from finalization, by design (atomicity).
11. **Receipt generation** — server renders a receipt payload (JSON) from the persisted `sales`/`sale_items`/`payments`; frontend renders/prints it. Receipt numbers come from `sales.sale_number`, generated inside the same transaction (e.g., a per-store sequence) to guarantee no gaps/duplicates under concurrency.
12. **Audit logging** — automatic on finalize, void, return, discount above threshold, and any price override at the register.

**Failure handling per step**: any failure before step 9 only affects unsaved client cart state (nothing to roll back). Failures inside step 9's transaction roll back atomically — the customer sees "payment failed, please retry," never a receipt without matching stock deduction or vice versa. A payment-processor timeout after the DB commit (money captured, but confirmation lost) is handled by making the payment `reference` idempotency-keyed so a retry recognizes the already-completed sale rather than double-charging.

---

## F. Purchasing Workflow

```
Supplier ─▶ Purchase (DRAFT) ─▶ Purchase (ORDERED)
                                     │
                                     ▼
                          Goods Receipt #1 (partial)
                                     │  → goods_receipt_items
                                     │  → inventory_movements (PURCHASE_RECEIPT)
                                     │  → WAC recompute per product
                                     ▼
                     purchase_items.quantity_received updated
                                     │
                         (if quantity_received < ordered)
                                     ▼
                     purchases.status = PARTIALLY_RECEIVED
                                     │
                          Goods Receipt #2 ... N
                                     ▼
                     purchases.status = RECEIVED (fully matched)
                                     │
                                     ▼
                        Accounting impact: inventory value ↑,
                        (optionally) accounts payable ↑ if
                        supplier-balance tracking is enabled
```

**Discrepancies**: if `goods_receipt_items.quantity_received` ≠ what was ordered for that line, the purchase simply reflects a partial/over receipt (over-receipt allowed, flagged); a `condition_notes` field captures damage/shortage narrative. Price discrepancies (invoiced cost ≠ PO cost) are captured because `goods_receipt_items.unit_cost` is independently recorded and is the value actually used for WAC — the PO's `unit_cost` remains as the original expectation for variance reporting.

**Accounting impact**: each `goods_receipt_item` posts one `inventory_movements` row and updates `products.current_cost` (WAC) and on-hand qty. Supplier balance/AP is out of spec's stated scope; the schema leaves room for a future `supplier_invoices`/`supplier_payments` pair without touching existing tables.

---

## G. Accounting and Reporting

All figures below are **computed from transaction history at query time (or materialized/cached for performance), never stored as manually editable totals** — this is a hard rule per BR-1/BR-2.

| Metric | Formula | Source tables |
|---|---|---|
| Gross Sales | `Σ sale_items.quantity * unit_price_at_sale` for `sales.status='COMPLETED'` in period | sale_items, sales |
| Discounts | `Σ sale_items.discount_amount` | sale_items |
| Tax Collected | `Σ sale_items.tax_amount` | sale_items |
| Net Sales | `Gross Sales − Discounts` | derived |
| COGS | `Σ sale_items.quantity * unit_cost_at_sale` | sale_items |
| Gross Profit | `Net Sales − COGS` | derived |
| Gross Margin % | `Gross Profit / Net Sales * 100` | derived |
| Purchases (period) | `Σ goods_receipt_items.quantity_received * unit_cost` | goods_receipt_items |
| Inventory Value | `Σ products.current_qty_on_hand * products.current_cost` (WAC) at a point in time | products |
| Stock Movement | Filtered listing/summary of `inventory_movements` by type/date/product | inventory_movements |
| Daily Sales | Gross/Net/Tax/Discount grouped by `date_trunc('day', sales.completed_at)` | sales, sale_items |
| Supplier totals | `Σ goods_receipt_items` joined to `purchases.supplier_id` | goods_receipt_items, purchases |

Reports that need to be **fast** at scale (daily P&L dashboard) can use a materialized view or a nightly rollup table (`daily_sales_summary`) populated by the background worker — but the rollup is always reproducible by re-running the aggregation query, never hand-editable, preserving auditability.

`products.current_qty_on_hand` itself is a maintained running total (updated transactionally alongside each `inventory_movements` insert) rather than summed from the ledger on every read, for performance — but it must always be reconcilable to `SUM(inventory_movements.quantity_delta)` for that product, and a periodic reconciliation job should assert this invariant.

---

## H. Security

- **Password hashing**: argon2id (preferred) or bcrypt, cost tuned to ~250ms on target hardware.
- **Sessions**: short-lived JWT access token (claims: user id, store id, resolved permissions or a permission-version stamp) + rotating httpOnly refresh cookie; refresh tokens revocable server-side (`refresh_tokens.revoked_at`) for logout/compromise response.
- **RBAC**: enforced in API middleware per route, not just hidden in the UI; every write endpoint re-checks permission server-side regardless of what the UI shows.
- **API authorization**: `store_id` scoping enforced on every query (a user can only touch their store's data) even before multi-store is exposed in the UI.
- **Input validation**: Pydantic (FastAPI) schemas validate shape/types/ranges on every request; reject unknown fields.
- **SQL injection prevention**: ORM/parameterized queries only (SQLAlchemy or equivalent) — no string-built SQL.
- **CSRF**: SPA using Bearer JWT in an `Authorization` header is not CSRF-vulnerable for state-changing calls; if refresh tokens use cookies, mark them `SameSite=Strict`/`Lax` + httpOnly + `Secure`, and don't rely on cookies for the access token itself.
- **Rate limiting**: Nginx or app-level limiter on `/auth/login` and `/auth/refresh` (e.g., 5/min/IP) to blunt credential stuffing.
- **Audit logs**: append-only, no update/delete permission granted to any application role at the DB level (revoke UPDATE/DELETE on `audit_logs` from the app's DB user, or enforce via a `BEFORE UPDATE/DELETE` trigger that raises an exception).
- **Secrets management**: `.env` file outside git (`.gitignore`'d), injected via Docker Compose env vars or a secrets manager; DB credentials, JWT signing key, and future tax-API keys never committed.
- **HTTPS**: Let's Encrypt via Certbot, auto-renewal, Nginx redirects all HTTP→HTTPS.
- **Backup security**: backups encrypted at rest (e.g., `gpg` or bucket-side encryption), access-restricted credentials, tested restore.

---

## I. API Design (representative — full OpenAPI spec generated from FastAPI at implementation time)

Base path `/api/v1`. All endpoints except `/auth/login` require `Authorization: Bearer <JWT>`.

| Method | Path | Auth/Permission | Purpose | Key failure cases |
|---|---|---|---|---|
| POST | `/auth/login` | none | Issue access+refresh tokens | 401 invalid credentials; 429 rate-limited |
| POST | `/auth/refresh` | valid refresh cookie | Rotate access token | 401 expired/revoked token |
| POST | `/auth/logout` | authenticated | Revoke refresh token | — |
| GET | `/products` | `products.read` | List/search products (paginated, filter by category/active) | — |
| POST | `/products` | `products.write` | Create product | 400 validation; 409 duplicate SKU/barcode |
| GET | `/products/{id}` | `products.read` | Get product detail incl. barcodes | 404 |
| PUT | `/products/{id}` | `products.write` | Update catalog fields (never cost — cost is system-managed) | 400; 404; 409 |
| GET | `/products/barcode/{code}` | `pos.use` | POS lookup by barcode | 404 unknown barcode |
| POST | `/products/{id}/barcodes` | `products.write` | Attach another barcode | 409 duplicate |
| GET | `/inventory/movements` | `inventory.read` | Filterable movement ledger | — |
| POST | `/inventory/adjustments` | `inventory.adjust` | Create manual stock adjustment | 400; 409 negative-stock policy |
| GET | `/inventory/valuation` | `reports.read` | Current inventory value snapshot | — |
| POST | `/sales` | `pos.use` | Finalize a sale (cart, payments) — the core POS write | 409 `INSUFFICIENT_STOCK`; 400 total mismatch; 422 validation |
| GET | `/sales` | `sales.read` | List/search sales history | — |
| GET | `/sales/{id}` | `sales.read` | Sale detail incl. items/payments | 404 |
| POST | `/sales/{id}/void` | `sales.void` | Void an unsettled/just-completed sale | 409 already voided/returned |
| POST | `/sales/{id}/returns` | `sales.return` | Create a return against a sale | 400 quantity exceeds sold; 403 needs approval |
| GET | `/sale-items` | `sales.read` | Query line items across sales (for reporting) | — |
| GET | `/suppliers` | `suppliers.read` | List suppliers | — |
| POST | `/suppliers` | `suppliers.write` | Create supplier | 400 |
| POST | `/purchases` | `purchases.write` | Create PO (draft/ordered) | 400 |
| GET | `/purchases/{id}` | `purchases.read` | PO detail incl. items, receipts | 404 |
| POST | `/purchases/{id}/receive` | `purchases.receive` | Record a goods receipt (partial or full) | 400 over-receipt policy; 409 PO cancelled |
| POST | `/purchases/{id}/cancel` | `purchases.write` | Cancel a PO | 409 already has receipts |
| POST | `/purchases/{id}/returns` | `purchases.write` | Create a purchase return | 400 exceeds received qty |
| GET | `/reports/pnl` | `reports.read` | P&L for a date range | 400 invalid range |
| GET | `/reports/daily-sales` | `reports.read` | Daily sales summary | — |
| GET | `/reports/stock-movement` | `reports.read` | Movement report | — |
| GET | `/users` | `users.manage` | List users | — |
| POST | `/users` | `users.manage` | Create user | 400; 409 duplicate username/email |
| PUT | `/users/{id}/roles` | `users.manage` | Assign roles | 404 |
| GET | `/audit-logs` | `audit.read` | Query audit trail | — |
| POST | `/tax/submit/{sale_id}` | `tax.manage` (system/background) | Submit sale to tax provider (future) | 502 provider unavailable → queued retry |
| GET | `/tax/submissions/{sale_id}` | `tax.manage` | Check submission status | 404 |

Every write endpoint returns the persisted server-computed totals (price, tax, grand total) in its response so the frontend reconciles to the authoritative values rather than trusting its own math (BR-1).

---

## J. Tax Integration (Future — Provider-Agnostic Design)

Core POS depends only on an internal interface, not a vendor SDK:

```
TaxProvider (interface)
  calculate(sale_draft) -> { line_taxes[], total_tax }      # can run locally using tax_rates table
  build_fiscal_invoice(sale) -> payload                      # provider-specific shape
  submit(payload) -> { status, fiscal_reference, raw_response }
  verify_signature(response) -> bool                          # if provider signs responses
```

- **Tax calculation** for day-to-day POS use is done locally from `tax_rates` (Section C.5) — this does not require the external authority to be reachable in real time.
- **Fiscal invoice creation / payload generation**: deferred until an authority is selected; the `tax_submissions` table stores whatever payload/response shape that authority requires as opaque JSONB, so no schema migration is needed to swap providers.
- **API authentication & signing**: authority-specific (e.g., mutual TLS, API keys, or device-based signing certificates) — **must be confirmed from that authority's official integration documentation before implementation**; nothing here assumes a mechanism.
- **Webhooks**: if the authority pushes async acknowledgements, an inbound `/tax/webhook` endpoint updates `tax_submissions.status`; must verify the webhook's authenticity (signature/HMAC) per that provider's docs.
- **Retry mechanism**: background worker polls `tax_submissions WHERE status IN ('PENDING','FAILED')`, exponential backoff, capped attempts, alerting after N failures.
- **Idempotency**: submissions keyed by `sale_id` (unique constraint) so a retry never double-submits; the provider call itself should pass an idempotency key if the authority supports one.
- **Offline/temporary API failure**: sale finalization is **never blocked** waiting on the tax authority (that would violate POS speed/availability) — the sale completes locally with locally-computed tax, and fiscal submission happens asynchronously via the retry queue; `tax_submissions.status` surfaces unsent invoices for operator visibility.
- **Storage of responses**: full raw `response_payload JSONB` retained for audit/dispute purposes, plus the extracted `fiscal_reference` for quick lookup/printing on the receipt once available (receipts may need a later reprint/addendum with the fiscal number — a UX detail to confirm against the eventual authority's requirements).

---

## K. Frontend Screens

- **Login** — username/password, "remember me" (refresh token), lockout messaging after rate-limit.
- **POS** — the money screen (Section K.1 below).
- **Products** — searchable/filterable table, create/edit modal, barcode management sub-view.
- **Inventory** — on-hand levels, movement ledger, manual adjustment form (reason required), low-stock list.
- **Suppliers** — CRUD list.
- **Purchases** — PO list by status, PO detail/edit (draft), create PO from supplier + product picker.
- **Goods Receiving** — pick an `ORDERED`/`PARTIALLY_RECEIVED` PO, enter received quantities/costs per line, submit.
- **Sales History** — searchable list, drill into a sale, initiate void/return from here.
- **Reports** — date-range pickers driving P&L, daily sales, stock movement, inventory valuation views (tables + simple charts).
- **Profit & Loss** — dedicated period-over-period view of the Section G metrics.
- **Users/Settings** — user list, role assignment, store settings (currency, negative-stock policy, return approval threshold).
- **Audit Log** — filterable, read-only, by user/entity/date.

### K.1 POS Screen — Speed & Scanner Optimization
- A single always-focused hidden text input captures scanner keystrokes anywhere on the screen (no need to click into a field first); Enter submits the scanned code to the barcode-lookup endpoint.
- Large, touch-friendly cart list; quantity +/- steppers for manual adjustment (with a numeric override for weighed items).
- Keyboard shortcuts for common actions (F-keys or similar) for power cashiers: void line, apply discount, tender cash, tender card.
- No page navigation during a transaction — POS is a single-route SPA view; network calls are limited to barcode lookups and the final `POST /sales`, keeping perceived latency low.
- Clear, large error toasts for `INSUFFICIENT_STOCK`/unknown barcode that don't block the rest of the cart.
- Receipt preview/print triggered immediately on the `201` response from `POST /sales`.

---

## L. Deployment (Target: ~$20/month)

- **Server**: single VPS, 2 vCPU / 4GB RAM (e.g., a $20/mo tier from DigitalOcean/Hetzner/Linode-class providers) — sufficient for the scale in assumption #9.
- **Docker setup**: `docker-compose.yml` with services `nginx`, `api` (FastAPI/Uvicorn), `db` (Postgres, named volume for data), `worker` (background jobs). Single `docker compose up -d` deployment.
- **PostgreSQL**: run in its own container with a persistent volume; connection pooling via `pgbouncer` if concurrent connections become a bottleneck (unlikely at target scale).
- **Reverse proxy**: Nginx terminates TLS, serves the built React static assets, proxies `/api/*` to the FastAPI container.
- **HTTPS**: Certbot (Let's Encrypt) with a cron/systemd-timer renewal, mounted into the Nginx container.
- **Domain**: any registrar; A record → VPS IP.
- **Environment variables**: `.env` (git-ignored) holding `DATABASE_URL`, `JWT_SECRET`, `REFRESH_TOKEN_SECRET`, future tax-provider credentials — loaded by Docker Compose, never baked into images.
- **Database backups**: nightly `pg_dump` from the `worker` container to an off-box encrypted bucket; retain 14+ days; documented restore runbook, tested monthly.
- **Monitoring**: lightweight — container health checks in Compose, Nginx/Postgres logs shipped to disk with rotation (`logrotate`), optional free-tier uptime pinger (e.g., a simple external HTTP check) for availability alerts.
- **Logs**: structured JSON application logs (request id, user id, endpoint, latency) to stdout, captured by Docker's logging driver, rotated.
- **Updates**: pull new image/build, `docker compose up -d --build`, run any pending Alembic migrations as a one-off command before restarting the API.
- **Disaster recovery**: documented runbook — spin up a fresh VPS, restore latest `pg_dump`, redeploy via Compose, repoint DNS; target RTO measured in hours (acceptable for this budget tier, and should be stated to the business owner as a known tradeoff).

---

## M. Development Roadmap

Each milestone is independently testable and shippable in sequence.

**M0 — Foundation**
- DB: initial migration framework (Alembic), `stores`, `users`, `roles`, `permissions`, `role_permissions`, `user_roles`.
- Backend: FastAPI skeleton, auth endpoints (login/refresh/logout), RBAC middleware.
- Frontend: login screen, protected routing shell.
- Tests: auth flow (valid/invalid login, token refresh, expired token rejection), RBAC denies unauthorized routes.
- Acceptance: a seeded admin user can log in and reach an empty authenticated shell; a non-permitted route returns 403.
- Dependencies: none.

**M1 — Product Catalog**
- DB: `product_categories`, `suppliers`, `products`, `product_barcodes`.
- Backend: CRUD endpoints for products/categories/suppliers, barcode lookup endpoint.
- Frontend: Products screen (list/create/edit), barcode management.
- Tests: unique SKU/barcode constraint violations return 409; barcode lookup returns correct product.
- Acceptance: an admin can create a product with a barcode and retrieve it by scanning (barcode lookup API) it.
- Dependencies: M0.

**M2 — Inventory Core**
- DB: `inventory_movements`, `stock_adjustments`, running `current_qty_on_hand`/`current_cost` columns on `products`.
- Backend: Inventory Service (adjustment endpoint, movement ledger query), row-locking logic.
- Frontend: Inventory screen (on-hand, ledger, adjustment form).
- Tests: concurrent adjustment requests on the same product don't corrupt quantity (load test with parallel requests); negative-stock policy enforced/overridable.
- Acceptance: manual adjustments correctly change on-hand and are fully traceable in the ledger.
- Dependencies: M1.

**M3 — Purchasing & Goods Receiving (incl. WAC)**
- DB: `purchases`, `purchase_items`, `goods_receipts`, `goods_receipt_items`, `purchase_returns`, `purchase_return_items`.
- Backend: Purchasing Service, WAC recompute logic, partial-receipt status transitions.
- Frontend: Purchases + Goods Receiving screens.
- Tests: WAC formula unit tests (matching Section D's worked examples exactly); partial receipt updates status correctly; cancelled-PO-with-receipt is rejected.
- Acceptance: receiving two batches at different costs produces the exact WAC from Section D's example.
- Dependencies: M2.

**M4 — POS / Sales Core**
- DB: `sales`, `sale_items`, `payments`, `tax_rates`.
- Backend: Sales Service — atomic finalize transaction, server-side price/tax/total recomputation, `INSUFFICIENT_STOCK` handling.
- Frontend: POS screen (scanner input, cart, checkout, receipt).
- Tests: a sale with client-tampered totals is rejected/recomputed; concurrent sales against the same low-stock item never oversell (load test); a mid-transaction DB failure leaves zero partial state (no orphaned inventory movement without a sale, or vice versa).
- Acceptance: a full scan-to-receipt flow works end-to-end with correct inventory deduction and frozen cost/price on the line item.
- Dependencies: M3 (needs WAC to exist for cost freezing).

**M5 — Returns, Voids & Audit Logging**
- DB: `sale_returns`, `sale_return_items`, `audit_logs`.
- Backend: void/return endpoints with approval gating, audit logger wired into all privileged mutations from M0–M4.
- Frontend: void/return actions on Sales History; Audit Log screen.
- Tests: return quantity can't exceed sold quantity; restocked vs. non-restocked returns affect inventory correctly; every privileged action produces an audit row.
- Acceptance: a return correctly reverses inventory and appears in the audit trail with before/after state.
- Dependencies: M4.

**M6 — Reporting & P&L**
- DB: optional `daily_sales_summary` rollup table/materialized view.
- Backend: Accounting/Reporting Service endpoints (Section G formulas).
- Frontend: Reports + Profit & Loss screens.
- Tests: report totals reconcile exactly against manually summed fixture data for a known date range.
- Acceptance: P&L for a test dataset matches hand-calculated expected values.
- Dependencies: M5 (needs returns factored into net sales).

**M7 — Tax Integration Scaffold**
- DB: `tax_submissions`.
- Backend: `TaxProvider` interface + a no-op/mock implementation, retry-queue worker.
- Frontend: submission status indicator on Sales History (optional for v1).
- Tests: mock provider round-trip; retry queue processes a forced failure correctly.
- Acceptance: architecture is demonstrably swappable (mock provider replaced with a stub "second provider" in a test without touching Sales Service code).
- Dependencies: M6 (not functionally, but logically last since it's the most speculative area pending real authority docs).

**M8 — Deployment Hardening**
- Docker Compose production stack, Nginx+TLS, backup job, log rotation, restore-runbook drill.
- Tests: restore drill from a backup succeeds against a fresh empty DB.
- Acceptance: the full stack deploys from scratch on a clean VPS following the written runbook.
- Dependencies: all prior milestones functionally complete.

---

## N. Risk Register

| # | Risk | Probability | Impact | Mitigation |
|---|---|---|---|---|
| 1 | Concurrent sales corrupt stock quantity | Medium | High | Row-level locking / atomic conditional updates in Inventory Service (Section D edge case 7); load tests in M2/M4. |
| 2 | Floating-point rounding errors in money/cost | Medium | High | `NUMERIC` types everywhere, no floats in code paths touching money; explicit rounding rules at display boundaries only. |
| 3 | Client-submitted totals trusted at checkout | Medium | High | Server always recomputes and validates totals before commit (BR-1), enforced by tests in M4. |
| 4 | Single VPS = single point of failure | High | Medium | Documented DR runbook, tested backups, acceptable per stated budget; escalate if uptime SLA tightens later. |
| 5 | Tax authority requirements unknown/change | High | Medium | Provider-agnostic `TaxProvider` interface (Section J); no core rewrite needed when authority is chosen. |
| 6 | Weighed-item barcode format assumptions wrong for chosen hardware | Medium | Medium | Confirm actual scale/label-printer barcode format during M1 before hardcoding a parser; keep `barcode_type` extensible. |
| 7 | WAC purchase-return math is approximate (no true lot tracking) | Low | Low | Documented limitation (Section D); acceptable per spec's explicit WAC requirement, not FIFO. |
| 8 | Negative stock silently masks real shrinkage/theft | Medium | Medium | Default-blocked; override is explicit, logged, and reportable (BR-7, N/A report in M2). |
| 9 | Backup restore untested until a real disaster | Medium | High | Scheduled monthly restore drills (M8 acceptance criteria) rather than "set and forget" backups. |
| 10 | Audit log tampering by a privileged DB user | Low | High | Revoke UPDATE/DELETE on `audit_logs` at the DB grant level, not just app logic. |
| 11 | Barcode scanner input misdetected as manual typing (or vice versa) causing wrong lookups | Low | Medium | Same code path handles both (Section E.1) — heuristic is UX-only, not correctness-critical. |
| 12 | Discount/permission abuse by cashiers (manual price overrides) | Medium | Medium | Permission-gated manager approval above a threshold; every override audit-logged. |
| 13 | Partial receiving / cost discrepancies create WAC drift over time | Low | Medium | `goods_receipt_items.unit_cost` always reflects actual invoiced cost, independent of PO estimate; periodic reconciliation report. |
| 14 | Report totals silently diverge from source ledger over time (bug in rollup table) | Medium | Medium | Reports are always re-derivable from raw tables (Section G rule); reconciliation job compares rollup vs. live aggregate periodically. |
| 15 | Scope creep before MVP ships (multi-store, offline mode, loyalty, etc.) | High | Medium | Roadmap (Section M) explicitly sequences MVP first; schema leaves room (e.g., `store_id` everywhere) without building those features now. |
| 16 | JWT/refresh-token compromise | Low | High | Short-lived access tokens, rotating refresh tokens, server-side revocation list, HTTPS-only. |
| 17 | Uncontrolled inflation of `product_barcodes`/duplicate barcodes across stores | Low | Medium | Global uniqueness constraint on `barcode`; conflict surfaces at creation time (409), not at checkout. |
| 18 | Migration/downtime risk on schema changes as the system grows | Medium | Medium | Alembic-managed migrations, tested against a staging copy of production data before each deploy. |
| 19 | Currency/locale formatting inconsistency between server and client | Low | Low | Server is the source of truth for all monetary values; client only formats for display, never recalculates. |
| 20 | Underestimated hardware needs at target scale (assumption #9 wrong) | Low | Medium | Docker Compose stack is trivially portable to a larger VPS if load testing (M8) shows the $20/mo tier insufficient. |

---

## O. Final Recommendation

**Stack**: PostgreSQL 16 · FastAPI (Python 3.12) + SQLAlchemy 2.0 + Alembic · React 18 + Tailwind CSS + Vite · Nginx reverse proxy · Docker Compose · Let's Encrypt/Certbot for TLS.

**Why FastAPI over Node/Express**: native Pydantic validation gives strong request/response typing "for free" (directly supports BR-1's server-side-truth requirement and the input-validation security requirement), auto-generated OpenAPI docs accelerate the API-design deliverable into a living contract, and async support handles the barcode-lookup-heavy POS traffic pattern well without added complexity.

**Why not a heavier stack (microservices, Kafka, NoSQL, etc.)**: the specification's own scale (single store, ~$20/month) and integrity requirements (ACID transactions, strong relational constraints, WAC math) are a textbook fit for a single well-modeled PostgreSQL database and a modular monolith — internally separated into the services described in Section B, but deployed as one process for now. This keeps operational cost and complexity proportional to the actual requirement, with clean seams (Sales/Inventory/Purchasing/Accounting/Tax as internal service modules) to split out later only if real scale ever demands it.

**Architecture in one line**: a modular-monolith FastAPI backend over PostgreSQL, fronted by a React SPA and Nginx, with all money/inventory logic enforced transactionally server-side and a provider-agnostic tax layer left as a clean extension point.

This blueprint is the baseline for implementation. Proceeding to code should follow the milestone order in Section M, starting with M0.
