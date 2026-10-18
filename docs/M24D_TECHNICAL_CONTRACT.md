# M24D: Inter-Store Transfer Technical Implementation Contract

**Status:** Documentation and architectural design only. No production code,
migration, seed data, test, or frontend file changed. Starting HEAD:
`f12e38d` (M24C commit).

**Purpose:** D1–D7 now carry authoritative, specific stakeholder business
requirements (reproduced in full in Section 1). This document is the
complete, implementation-ready technical contract that translates those
requirements into a concrete architecture: domain model, state machines,
accounting flows, and a phased M25 implementation plan. Where the current
architecture lacks a capability the stakeholder requires, this document
designs the necessary extension rather than downgrading the requirement.
Nothing in this document is implemented yet.

---

## 1. D1–D7 Confirmed Requirements

Reproduced verbatim from the authoritative business requirements supplied
for this phase, for traceability. Every design decision below cites back to
one of these seven.

**D1 — Economic Loss Ownership.** Single/small-store: vendor-caused
shortage/damage belongs to the Source Vendor; other loss is store shrinkage
expense. Multi-store chain: loss defaults to Source Store/Warehouse;
Destination Store explicitly accepts custody via digital signature; after
acceptance, subsequent loss belongs to the Destination Store per configured
rules. The system must support both a **corporate chain** model (stores are
divisions of one business; intercompany clearing/accounting treatment) and
a **financially independent/franchise** model (stores as independent
accounting entities; transfers can create AP/AR obligations between
entities). Neither model is reduced to the other.

**D2 — Write-Off Authority.** Authorized by Central Management or a
designated Regional/Store Manager. Self-approval is prohibited. Configurable
multi-tier approval (e.g., low-value = automated/Store-Manager approval;
higher-value escalates to Central Management/Corporate Loss Prevention).
The `$50` example is illustrative and must not be hard-coded. Reversal/
correction authority: Central Management or System Administrators. The
system must distinguish initiator from approver. Every approval is
auditable.

**D3 — Post-Shipment Reversal.** Physical return is supported. Destination
Store may initiate a return request for unrequested, excess, or unusable
stock. Authorization: Central Management or the Source Store Manager. A
return is a genuine physical inventory movement, never a status change, and
preserves inventory quantities, WAC, Inventory In Transit, GL, store
attribution, authorization, audit history, and idempotency.

**D4 — Shortage/Damage/Partial Receipt.** Receiving staff record the actual
physical result immediately (e.g., expected 100 → good 90, damaged 2, short
8). Good inventory is available to the destination immediately.
Discrepancies enter an investigation workflow supporting: shortage, damage,
partial receipt, investigation, approval, denial, final resolution,
write-off where appropriate, vendor responsibility where appropriate, and
return where appropriate. No quantity or money may be left in an undefined
state.

**D5 — Loss GL Account.** Reuse the existing global Expense/Shrinkage
account, with sub-ledger/store-cost-center attribution. Internal-transit
damage → Source Store cost center; damage after destination acceptance →
Destination Store cost center. Vendor-caused losses follow D1's vendor
attribution. The existing dynamic P&L logic must continue to include the
resulting expense correctly.

**D6 — In-Transit Timeout.** Aging/timeout monitoring: chilled/perishable
= 48 hours; ambient/dry grocery = 7 days; calculated from Expected Arrival
Date/Time. Alerts: Source Store Manager, Destination Store Manager, Central
Loss Prevention Dashboard. Timeout is alerting only — it must never change
transfer state, write inventory, create a loss, post a journal, or write
off goods. Alerts must be idempotent (no duplicate notifications on
repeated processing).

**D7 — Mandatory Reconciliation/Period Close.** Transfer reconciliation is
mandatory at financial period close. Responsible authority: Corporate
Controller/Head of Accounting. Open unresolved in-transit transfers block
period close. Resolution options: force reconciliation, write-off, or roll
into a pending-suspense ledger for the next period. No unresolved transfer
may silently disappear merely because a period closes.

---

## 2. Architecture Overview

The design adds five new architectural layers on top of the existing,
unchanged transfer/inventory/accounting core (`InterStoreTransfer`,
`InterStoreTransferLine`, `InterStoreTransferReceipt`,
`InterStoreTransferReceiptItem`, `post_transfer_shipment_journal`,
`post_transfer_receipt_journal`, `_post_journal`/`_enforce_period_open`,
`inventory_in_transit_reconciliation`) — none of which is modified in
shape, only extended at specific, named seams:

1. **Organizational layer** (D1): a new `AccountingEntity` concept that
   `Store` rows belong to, determining whether a transfer between two
   stores is an intra-entity reclassification (today's existing mechanics,
   unchanged) or an inter-entity transaction (new AP/AR treatment).
2. **Custody/receiving layer** (D1, D4): an expanded receipt shape that
   captures good/damaged/short quantities in one clerk action, plus an
   explicit, immutable custody-acceptance record that formalizes the
   existing loss-attribution boundary this system already has (the moment
   a receipt posts) rather than inventing a new boundary.
3. **Discrepancy/investigation layer** (D4, and vendor-caused loss for D1/
   D5): a generalized `ReceivingDiscrepancy` state machine usable by both
   inter-store transfer receiving and ordinary supplier goods receiving,
   since both need the identical set of outcomes (vendor responsibility,
   store responsibility, return, denial).
4. **Approval layer** (D2): a configurable, table-driven multi-tier
   approval mechanism with a hard, structural (not merely permission-based)
   self-approval prohibition.
5. **Reconciliation/period-close layer** (D6, D7): a computed aging
   surface, and an extension to `close_accounting_period` that detects
   unresolved transfer positions and requires one of the three named
   resolutions before a store's period can close.

No parallel accounting or inventory system is created. Every new flow
terminates in the same `_post_journal`/`JournalEntry`/`JournalLine`
machinery, the same `record_movement`/`InventoryMovement` machinery, and
the same `audit_service.log_event` machinery every existing module already
uses.

---

## 3. Domain Model

### 3.1 New entities

| Entity | Purpose | Key fields |
|---|---|---|
| `AccountingEntity` | D1: distinguishes corporate-division stores from financially independent stores | `id`, `name`, `entity_type` (`CORPORATE_DIVISION` \| `INDEPENDENT`), `is_default` |
| `Store.accounting_entity_id` (new FK column on existing table) | Assigns every store to exactly one entity | `NOT NULL`, backfilled to one default `CORPORATE_DIVISION` entity for every existing store |
| `Product.perishability_class` (new column on existing table) | D6: drives the 48h/7d aging threshold | `CHILLED` \| `AMBIENT`, default `AMBIENT` |
| `InterStoreTransferReceiptItem` (extended, existing table) | D4: captures the physical result in one action | new columns `quantity_damaged: Decimal`, `quantity_declared_short: Decimal` (both default 0); existing `quantity_received` becomes explicitly "good quantity made available to the destination" |
| `TransferCustodyAcceptance` | D1: the immutable, digital custody-acceptance record | `id`, `transfer_id`, `transfer_receipt_id`, `store_id`, `accepted_by`, `accepted_at`, `is_digital_acknowledgment` |
| `ReceivingDiscrepancy` | D4: generalized shortage/damage investigation, reusable by transfer receiving and supplier goods receiving | `id`, `source_type` (`TRANSFER_RECEIPT` \| `GOODS_RECEIPT`), `source_id`, `product_id`, `store_id`, `counterparty_store_id`/`supplier_id`, `discrepancy_type` (`SHORTAGE` \| `DAMAGE` \| `BOTH`), `quantity_short`, `quantity_damaged`, `unit_cost`, `status`, `raised_by`, `raised_at`, `resolved_by`, `resolved_at`, `resolution_type`, `resolution_notes` |
| `ApprovalPolicyRule` | D2: configurable multi-tier approval, admin-managed, never hard-coded | `id`, `action_type` (`TRANSFER_WRITE_OFF`), `scope_store_id` (nullable = all stores), `min_amount`, `max_amount` (nullable = unbounded), `required_role_id`, `escalation_role_id` (nullable) |
| `TransferWriteOff` | D2, D4, D5: the loss write-off record | `id`, `transfer_id`, `transfer_line_id`, `discrepancy_id` (nullable), `quantity`, `unit_cost`, `amount`, `store_id`, `reason`, `initiated_by`, `initiated_at`, `status` (`PENDING_APPROVAL` \| `APPROVED` \| `DENIED` \| `POSTED` \| `REVERSED`), `journal_entry_id`, `reversal_journal_entry_id`, `client_transaction_id` |
| `WriteOffApproval` | D2: initiator/approver distinction, self-approval prohibition | `id`, `write_off_id`, `required_role_id`, `initiated_by`, `approved_by`, `decision` (`APPROVED` \| `DENIED`), `decided_at`, `decision_notes` — `CHECK (initiated_by <> approved_by)` |
| `SupplierCreditNoteRequest` | D1/D4/D5: wraps the existing `SupplierCreditNote` lifecycle for a vendor-responsibility discrepancy outcome | `id`, `discrepancy_id`, `status` (`REQUESTED` \| `SUBMITTED_TO_VENDOR` \| `APPROVED` \| `DENIED`), `resulting_credit_note_id` (nullable) |
| `InterStoreTransfer.origin_transfer_id` (new FK column on existing table) | D3: marks a transfer as a physical return of another transfer, never a status flip | nullable, `NULL` for every ordinary transfer |
| `IntercompanyReceivable` / `IntercompanyPayable` postings | D1: AP/AR between independent entities | modeled as ordinary `JournalEntry`/`JournalLine` rows against two new accounts (Section 6), not a new ledger table — see Section 8 |
| `TransferAgingAlert` | D6: idempotent aging/timeout alert record | `id`, `transfer_line_id`, `expected_arrival_at`, `threshold_hours` (frozen at ship time from the product's classification), `fired_at`, `acknowledged_by`, `acknowledged_at` — unique on `transfer_line_id` so re-detection is naturally idempotent |
| `TransferSuspenseRecord` | D7: tracks a balance carried forward past a period close | `id`, `transfer_line_id`, `discrepancy_id` (nullable), `amount`, `store_id`, `accounting_period_id`, `created_by`, `created_at`, `resolved_at` (nullable), `resolution_type` (nullable: `LATE_RECEIPT` \| `WRITTEN_OFF`) |

### 3.2 Entities explicitly reused, not duplicated

- `InterStoreTransfer`/`InterStoreTransferLine`/`InterStoreTransferReceipt` —
  unchanged in shape (only the receipt item extension above).
- The physical return (D3) is **not** a new document type — it is an
  ordinary `InterStoreTransfer` row with `from_store_id`/`to_store_id`
  reversed and `origin_transfer_id` set, going through the exact same
  `ship_transfer`/`receive_transfer` pipeline every other transfer uses.
  This directly follows the `create_purchase_return`-precedent reasoning
  M24B already established, and is the mechanism that guarantees D3's
  "preserve inventory quantities; WAC; Inventory In Transit; GL; store
  attribution; authorization; audit history; idempotency" requirement for
  free — none of those properties needs re-implementing.
- Vendor-caused loss (D1, D4) reuses the existing `SupplierCreditNote`
  mechanism (which already reduces `ACCOUNT_ACCOUNTS_PAYABLE` directly) —
  no new receivable account is needed for a vendor claim, since the
  business already owes the vendor money in the ordinary course of
  business and a credit note nets against that existing obligation.
- `_post_journal`, `_enforce_period_open`, `record_movement`,
  `lock_product_for_update`, `compute_new_wac`, `audit_service.log_event`
  — every new flow terminates in these, unchanged.

---

## 4. Organizational/Accounting Model (D1)

Every `Store` belongs to exactly one `AccountingEntity`. On migration, a
single default entity (`entity_type = CORPORATE_DIVISION`) is created and
every existing store is backfilled onto it — **zero behavior change for any
deployment that never creates a second entity.** This is the load-bearing
backward-compatibility guarantee for the entire contract.

**Determining which model applies to a given transfer:** compare
`from_store.accounting_entity_id` and `to_store.accounting_entity_id`.

- **Same entity (including every store in the default, single-entity
  configuration):** the transfer is an **intra-entity reclassification**.
  Existing mechanics apply completely unchanged: `Dr Inventory In Transit /
  Cr Inventory` at ship, `Dr Inventory / Cr Inventory In Transit` at
  receipt, both pure balance-sheet moves, no P&L impact, no AP/AR. This is
  the "corporate chain" model and is **already fully implemented** — D1
  requires no new mechanism for it.
- **Different entities:** the transfer is an **inter-entity transaction**.
  It is economically a sale from the source entity to the destination
  entity, valued at the frozen `unit_cost_at_shipment` (no markup — no
  transfer-pricing policy was specified, so goods move at cost, consistent
  with this system's existing "never invent an unstated value" discipline).
  See Section 8 for the exact posting.

**Store/warehouse/counterparty/cost-center relationship:** a `Store` is
unchanged as the operational and inventory scope. `AccountingEntity` is a
strictly coarser grouping above `Store` (many stores → one entity), used
only to decide which of the two postings above applies and to scope
entity-level reports (a new, optional reporting view — Section 19,
Phase 7 — that aggregates `JournalEntry` rows by `Store.accounting_entity_id`
exactly the way the existing company-wide reconciliation aggregates by no
grouping at all). No new "warehouse" concept is introduced — a warehouse
is modeled as an ordinary `Store` today and remains so.

---

## 5. Inventory Model

For a transfer line, the full quantity ledger is:

```
requested_quantity                              (existing, unchanged)
shipped_quantity                                 (existing, unchanged)
  = quantity_good (across all receipt events)     (existing column, redefined as "good")
  + quantity_damaged (across all receipt events)  (new)
  + quantity_declared_short (across all events)   (new)
  + quantity_returned (via a return-transfer)      (new, derived: Σ receipt-good on the
                                                     linked return transfer)
  + quantity_written_off                           (new, derived: Σ TransferWriteOff.quantity
                                                     for this line)
  + remaining_in_transit                           (derived: shipped_quantity minus every
                                                     term above)
```

**Invariant (new, required by this design):**
`shipped_quantity == quantity_good + quantity_damaged +
quantity_declared_short + quantity_returned + quantity_written_off +
remaining_in_transit`, at all times, for every line. This is the concrete
form of "the inventory ledger must remain mathematically consistent" and
"do not leave quantities or money in an undefined state." Every new mutating
operation (receipt, discrepancy resolution, write-off, return) is validated
against this identity before it commits — a `CHECK`-equivalent invariant
enforced in the service layer at every write, since it spans multiple
tables and cannot be a single-table DB constraint (matching how
`InterStoreTransferLine.received_bounds` today is a single-table proxy for
the same idea, extended in Section 22).

**Good quantity availability (D4):** `quantity_good` is recorded via the
existing `record_movement(... movement_type="TRANSFER_IN" ...)` call,
unchanged — it becomes on-hand, sellable stock the instant the receipt
posts, exactly as today. Damaged and declared-short quantities create
**no** inventory movement at receipt time (there is nothing to move — the
goods are damaged-but-physically-present or simply never arrived); a
`ReceivingDiscrepancy` row is what tracks them until resolved.

**No negative inventory:** unaffected — every new increasing movement
(`TRANSFER_IN` for `quantity_good`, and for a return's own receipt leg) is
strictly additive; nothing in this design ever attempts a movement that
could drive `current_qty_on_hand` negative.

---

## 6. Accounting Model

### 6.1 New accounts required

Per instruction #9, no specific GL account code is asserted here — exact
codes are an M25 mechanical detail, assigned at implementation time
following the existing numbering blocks.

| New account (name only) | Type / normal balance | Purpose | Required by |
|---|---|---|---|
| Transfer Suspense | ASSET / DEBIT | Holds a balance reclassified out of Inventory In Transit at a forced period close, pending later resolution | D7 |
| Intercompany Receivable | ASSET / DEBIT | Source entity's claim against a different destination entity for goods shipped at cost | D1 (independent-entity model) |
| Intercompany Payable | LIABILITY / CREDIT | Destination entity's obligation to a different source entity for goods received at cost | D1 (independent-entity model) |

**No new expense account is created.** Per D5's explicit instruction, every
store-responsibility write-off debits the existing
`ACCOUNT_INVENTORY_SHRINKAGE_EXPENSE` (5900) — this supersedes M24B's
earlier, non-binding lean toward a dedicated account; the stakeholder's
explicit answer controls. **No new vendor-claim account is created** —
vendor-responsibility resolutions reduce `ACCOUNT_ACCOUNTS_PAYABLE` via the
existing `SupplierCreditNote` mechanism, unchanged.

### 6.2 Store-cost-center attribution (D5)

D5 asks for "sub-ledger/store-cost-center attribution." This system's
existing, universal mechanism for exactly this — every other automated
posting already uses it, without exception — is `JournalEntry.store_id`
(which store) plus `JournalEntry.source_type`/`source_id` (which specific
sub-ledger event). A new `source_type = "TRANSFER_WRITE_OFF"` is added to
`AUTOMATED_SOURCE_TYPES`, with `source_id = TransferWriteOff.id`. This is
the cost-center dimension: any report can already group/filter the trial
balance by `source_type = "TRANSFER_WRITE_OFF"` the same way it can for
`STOCK_ADJUSTMENT` or `PAYROLL_POSTING` today, cleanly separating
transfer-caused shrinkage from stock-count shrinkage on the *same* GL
account without a new column. No new "cost center" table or field is
introduced — the existing dimension already satisfies the requirement.

**Store attribution rule (D1 + D5, the dynamic-attribution mechanism):**

- Before custody acceptance (no receipt event yet covers the lost
  quantity): the write-off's `store_id` = the transfer's `from_store_id`
  (source store bears it) — mechanically identical to M24B's original
  finding, now confirmed as the intended *default*, not merely an artifact
  of ledger location.
- After custody acceptance for that specific quantity (i.e., the loss is
  damage discovered on stock **already** in a posted receipt): this is
  Scenario F from M24B/M24 discovery — **already fully solved, zero new
  mechanism** — the existing `create_stock_adjustment` /
  `STOCK_ADJUSTMENT_OUT` / `ACCOUNT_INVENTORY_SHRINKAGE_EXPENSE` path
  handles it, correctly attributed to the destination store (the store
  whose product row the adjustment targets) with zero new code.
- Vendor-caused (either configuration): no `TransferWriteOff`/store
  expense is posted at all — the discrepancy resolves through
  `SupplierCreditNoteRequest` → `SupplierCreditNote`, which reduces
  Accounts Payable at whichever store holds the original purchase
  relationship, unchanged from existing AP mechanics.

### 6.3 P&L inclusion

Unchanged and re-verified: `profit_and_loss()`'s `operating_expenses` sums
every `EXPENSE`-type account except `ACCOUNT_COGS`, dynamically, from the
trial balance. Since every store-responsibility write-off posts to the
already-`EXPENSE`-typed `ACCOUNT_INVENTORY_SHRINKAGE_EXPENSE`, **it is
automatically included with zero formula change** — the exact outcome M22's
formula was already built to guarantee, now exercised by a second,
distinct `source_type` sharing the same account.

---

## 7. WAC Behavior

| Event | WAC treatment |
|---|---|
| Shipment (unchanged) | `unit_cost_at_shipment` frozen from the source product's `current_cost` at ship time, under row lock. |
| Receipt — good quantity (unchanged) | Frozen `unit_cost_at_shipment` rolled into the destination's WAC via `compute_new_wac`, exactly as today. |
| Receipt — damaged/short quantity | **No WAC effect at either store** — nothing is added to the destination's on-hand stock, and the source's WAC was already correctly adjusted at ship time (the goods left its books then). The frozen `unit_cost_at_shipment` is preserved on the discrepancy record purely as the valuation basis for whatever resolution eventually follows (write-off amount, vendor claim amount) — never re-derived from either store's current WAC. |
| Write-off | Valued at the same frozen `unit_cost_at_shipment` × written-off quantity — the only value with any evidentiary basis, unchanged from M24B's finding. Posting the write-off has **no WAC effect** at either store (nothing on-hand changes). |
| Physical return (D3) | The return-transfer's own shipment leg captures a **new** `unit_cost_at_shipment`, from the **destination-of-the-original-transfer's** `current_cost` at the moment the return ships — not forced to match the original transfer's frozen cost. This is not a defect: it is the same rule every other movement of that stock already follows (WAC reflects the holding store's cost at the moment of movement, never retroactively pinned to an earlier event). |
| Vendor claim / credit note | Uses the existing `SupplierCreditNote` cost basis rules, unchanged — this design adds no new vendor-side WAC rule. |

---

## 8. AP/AR Treatment (Independent Entities)

Applies only when `from_store.accounting_entity_id !=
to_store.accounting_entity_id`. For the default, single-entity
configuration this section never executes.

**At shipment** (source entity ships to a different entity):
```
Dr Intercompany Receivable   shipped_value   (source store's books)
    Cr Inventory                 shipped_value
```
Posted with `source_type = "INTER_ENTITY_TRANSFER_SHIP"`,
`store_id = transfer.from_store_id`, `posting_date =
transfer.shipped_at.date()` (a genuine new economic event — the source
entity's own sale — using its own real date, not a compensating-entry
date). **No P&L impact** — this is a transfer at cost, not a sale at a
margin; `Inventory` is relieved and `Intercompany Receivable` recognized
at the identical value, matching this system's stated policy that no
markup is invented.

**At receipt** (destination entity accepts custody — the same event as
custody acceptance, Section 9):
```
Dr Inventory                  received_value   (destination store's books)
    Cr Intercompany Payable       received_value
```
`source_type = "INTER_ENTITY_TRANSFER_RECEIVE"`, `store_id =
transfer_receipt.store_id`, `posting_date = received_date` (caller-supplied,
same rule as today's ordinary receipt).

**Originating/receiving entity:** derived directly from
`transfer.from_store_id`/`to_store_id`'s `accounting_entity_id` — no new
field is needed on the transfer itself.

**Invoice/reference:** the `InterStoreTransfer`/`InterStoreTransferReceipt`
rows themselves are the reference (`source_id` on each journal entry) —
no separate "intercompany invoice" document is created, consistent with
"use existing entities wherever appropriate."

**Settlement:** a new, minimal `IntercompanySettlement` record (mirrors
`SupplierPayment`'s shape exactly: `id`, `from_entity_id`, `to_entity_id`,
`amount`, `settled_at`, `method`, `client_transaction_id`) posts, when
actual payment occurs between the two businesses:
```
Dr Intercompany Payable   amount   (paying/destination entity's books)
    Cr Bank Account            amount
Dr Bank Account            amount   (receiving/source entity's books)
    Cr Intercompany Receivable amount
```
— two independent, single-entity-scoped journal entries (never one
cross-entity entry, matching the codebase's universal one-entry-per-store
rule), each following the exact posting shape `SupplierPayment` already
uses today.

**Reversal:** a new, dedicated
`post_intercompany_settlement_reversal_journal` function, following the
identical established pattern as `post_supplier_payment_reversal_journal`
(a new compensating entry at `date.today()`, never an edit of the original).

**Reconciliation:** the existing `inventory_in_transit_reconciliation`
report is extended (not replaced) with an entity-scoped variant that
compares each entity pair's `Intercompany Receivable` balance against the
counterpart `Intercompany Payable` balance on the other entity's books —
they must always net to zero across the pair, the AP/AR analogue of the
existing In-Transit check.

**Interaction with the physical custody/in-transit layer:** unchanged —
whether same-entity or cross-entity, the physical goods are still "in
transit" between the shipment and receipt events in exactly the same
real-world sense; only the *financial recognition* differs (a pure
reclassification for same-entity, a sale/purchase for cross-entity). No
separate "cross-entity in-transit" account is needed because, unlike the
same-entity case, a cross-entity shipment recognizes revenue/relief
immediately at ship (Intercompany Receivable), so there is no unresolved
asset sitting in limbo the way `Inventory In Transit` does — the
receivable itself **is** the outstanding position, and it is already
trackable via ordinary AP/AR-style aging, without inventing a new account.

---

## 9. Vendor Loss Handling

Applies when a `ReceivingDiscrepancy`'s investigation concludes
`VENDOR_RESPONSIBILITY` **and** the discrepancy can be traced to an
identifiable supplier/purchase-order origin. Traceability requirement,
stated explicitly: a discrepancy on a **transfer** receipt can only be
attributed to a vendor if the specific stock lot's original acquisition
(the source store's own prior `GoodsReceipt`) is identifiable — this
system has no per-lot tracking (M8's own limitation, unchanged), so vendor
attribution for a transfer-sourced discrepancy is only possible when the
source store's inventory for that product came from a single, identifiable
recent purchase order with no intervening sales/other movements diluting
the lot; where this cannot be established, `VENDOR_RESPONSIBILITY` is not
offered as an investigation outcome for that discrepancy, and it must
resolve as `STORE_RESPONSIBILITY` or `RETURN_REQUIRED` instead. For a
discrepancy on an ordinary supplier `GoodsReceipt` (not a transfer),
traceability is direct and immediate — the receipt already carries its own
`purchase_order_id`/`supplier_id`.

**Complete lifecycle** (`SupplierCreditNoteRequest`):

```
REQUESTED  →  SUBMITTED_TO_VENDOR  →  APPROVED  →  (existing SupplierCreditNote created)
                                    ↘  DENIED  →  (falls back to STORE_RESPONSIBILITY,
                                                    re-opens the ReceivingDiscrepancy)
```

- `REQUESTED`: created automatically the moment a discrepancy's
  investigation concludes `VENDOR_RESPONSIBILITY` with a traceable origin.
- `SUBMITTED_TO_VENDOR`: a manual transition recording that the claim was
  actually communicated to the supplier (this system has no
  vendor-communication channel — this is a status a staff member sets after
  doing so outside the system, mirroring how this ERP already has no
  outbound-email mechanism for purchase orders either).
- `APPROVED`: the vendor accepted the claim. The **existing**
  `SupplierCreditNote` creation service (unchanged, M7) is invoked with
  `reason="GOODS_RETURN"` or the closest existing matching reason, crediting
  Accounts Payable for the claimed value at the discrepancy's frozen unit
  cost. This is the only point at which money moves.
- `DENIED`: the vendor rejected the claim. The originating
  `ReceivingDiscrepancy` is reopened into `INVESTIGATING`, forcing a new
  resolution decision (typically `STORE_RESPONSIBILITY` at that point) —
  the loss does not silently vanish, satisfying D4's "do not leave money in
  an undefined state."

**No new financial account is required** — the resulting credit note uses
the existing `SupplierCreditNote` → `ACCOUNT_ACCOUNTS_PAYABLE` reduction
mechanism verbatim.

---

## 10. Approval Model (D2)

### 10.1 Configurable multi-tier rule table

`ApprovalPolicyRule` rows are admin-managed data, never code constants. For
a `TransferWriteOff` of a given `amount` at a given `store_id`, the
applicable rule is the row whose `[min_amount, max_amount)` range contains
`amount`, scoped first to that specific `store_id` if a row exists, else
the store-agnostic (`scope_store_id IS NULL`) row for that range. **If no
rule matches at all** (an unconfigured gap), the system fails closed: the
write-off requires the single highest-authority role in the system (the
Admin-equivalent role) rather than silently permitting an unauthorized
approval — this is a new, explicit invariant (Section 22) preventing a
configuration gap from ever becoming a security hole.

Illustrative (not hard-coded) example, matching the stakeholder's own
example: a rule `min_amount=0, max_amount=50, required_role=Store Manager`
and a second rule `min_amount=50, max_amount=NULL,
required_role=Regional Manager`. The `$50` boundary exists only as data an
administrator enters; nothing in the code refers to `50`.

### 10.2 Initiator/approver distinction

`TransferWriteOff.initiated_by` is set at creation (whoever raised it —
typically whoever investigated the discrepancy). `WriteOffApproval` is a
**separate** row, created only once someone with the required role reviews
it. `CHECK (initiated_by <> approved_by)` is a **database-level**
constraint on `WriteOffApproval` — self-approval is prevented structurally,
not merely by an application-layer permission check that a future code
change could accidentally weaken. This directly implements D2's
unconditional "self-approval is prohibited" (no threshold or exception is
attached to this rule, matching the stakeholder's own absolute phrasing).

### 10.3 Roles

Mapped onto this system's existing role vocabulary wherever a fit exists,
extended only where the stakeholder named a function this codebase has no
equivalent for:

| Stakeholder term | Mapping |
|---|---|
| Store Manager | **Existing** `Manager` role (already store-scoped) — gains new write-off-initiate permission |
| Regional Manager / Central Management (approval tier) | **New role**, `Regional Manager` — a *cross-store-scoped* user (`caller_store_id IS NULL` on their user row, reusing the exact existing "unrestricted caller" mechanism Admin already uses — no new "region" grouping concept is invented) holding the write-off-approve permission but not full Admin |
| Corporate Loss Prevention (D6 alert recipient) | **New role**, `Loss Prevention Officer` — cross-store-scoped, read-only, receives the aging dashboard view; no write permission |
| Corporate Controller / Head of Accounting (D7) | **Reuses** the existing `accounting.admin` permission (today Admin-only); this satisfies "the highest financial authority in the system" without inventing a redundant role. A future, separate `Controller` role narrower than full Admin is flagged as an optional refinement (Section 19, out of scope for M25) |
| System Administrators (write-off reversal) | **Existing** `Admin` role |

### 10.4 Auditability

Every `WriteOffApproval` decision (`APPROVED` or `DENIED`) is logged via
the existing `audit_service.log_event`, `entity_type="write_off_approval"`,
recording `initiated_by`, `approved_by`, `decision`, and `decision_notes` —
the same pattern every other financial action in this system already uses.

---

## 11. Custody Acceptance (D1)

**Design decision:** custody acceptance is **the same event** as a transfer
receipt, not a separate, earlier physical hand-off step. No evidence
exists in this repository (or in the stakeholder's answers) of a real
operational need for "sign now, count later" — the stakeholder's own
example (expected 100 → good 90/damaged 2/short 8) describes counting and
accepting happening together. Splitting them into two real steps is noted
as a possible future extension (Section 19) if the business later confirms
a genuine "sign at the dock before counting" physical process, but is not
built now, per instruction #9's prohibition on inventing an unstated
workflow.

**What is being accepted:** the specific quantities recorded on that
receipt event (good, damaged, declared-short) — not the whole transfer, so
a partially-received transfer has one `TransferCustodyAcceptance` row per
receipt event, matching the existing multi-event receipt model exactly.

**Who can accept:** whoever is already authorized to receive that transfer
— the existing `inventory.transfer.receive` permission, destination-store
scoped. No new permission is introduced for custody acceptance itself; it
is folded into the existing receiving action, consistent with instruction
#9's prohibition on inventing new permissions where an existing one
already covers the act.

**Record shape:** `TransferCustodyAcceptance(transfer_id,
transfer_receipt_id, store_id, accepted_by, accepted_at,
is_digital_acknowledgment=True)`, created in the **same transaction** as
the `InterStoreTransferReceipt` it corresponds to — never a separate,
racy write. **Immutable**: `UPDATE`/`DELETE` revoked from the application
runtime role on this table, the identical DB-privilege pattern already
used for `journal_entries`/`journal_lines`/`audit_logs`/
`inventory_movements`.

**Not a legal digital signature:** per instruction #11, this is an
internal ERP custody acknowledgment only — the authenticated user's
identity, action, and timestamp, nothing more. No claim of legal
e-signature compliance is made anywhere in this design or its eventual
implementation.

**Effect on loss attribution:** this record is the operative boundary for
D1's dynamic rule (Section 6.2) — any quantity **not yet** covered by any
`TransferCustodyAcceptance` row remains a source-store position; any
quantity **covered** by one is, from that moment, a destination-store
position, using the existing, unmodified stock-adjustment mechanism for
any loss discovered from that point forward (Scenario F, already solved).

---

## 12. Discrepancy Investigation Workflow (D4)

### 12.1 State machine

```
                    ┌────────────────────────────────────────┐
                    │                                          │
   RECORDED ──► INVESTIGATING ──► VENDOR_RESPONSIBILITY ──► RESOLVED
                    │       │                     │
                    │       │                     └──(vendor denies)──► INVESTIGATING (reopened)
                    │       │
                    │       ├──► STORE_RESPONSIBILITY ──► (TransferWriteOff created,
                    │       │                              PENDING_APPROVAL → APPROVED
                    │       │                              → POSTED) ──► RESOLVED
                    │       │                                    │
                    │       │                                    └──(DENIED by approver)──►
                    │       │                                        INVESTIGATING (reopened)
                    │       │
                    │       ├──► RETURN_REQUIRED ──► (TransferReturn lifecycle,
                    │       │                          Section 13) ──► RESOLVED
                    │       │
                    │       └──► DENIED ──► RESOLVED (no financial effect; the recorded
                    │                        quantities themselves are corrected via an
                    │                        amending receipt-detail entry, not a new
                    │                        movement)
                    │
                    └── (RECORDED alone, never investigated) — not a valid terminal state;
                        a discrepancy cannot be closed out of RECORDED directly
```

- `RECORDED`: created automatically the instant a receipt event posts
  nonzero `quantity_damaged` or `quantity_declared_short` for a line
  (Section 5). Not user-created directly.
- `INVESTIGATING`: a manual transition by a holder of the new
  `inventory.transfer.discrepancy.investigate` permission (Section 10.3's
  role table extended: granted to `Manager`/`Inventory Clerk`, the same
  tier that already holds `inventory.transfer.write`, following this
  codebase's own established "investigation/correction sits at the same or
  a slightly heavier tier than the routine write permission" pattern).
- The three substantive outcomes (`VENDOR_RESPONSIBILITY`,
  `STORE_RESPONSIBILITY`, `RETURN_REQUIRED`) and the no-op outcome
  (`DENIED`) are each a distinct transition requiring a mandatory `reason`
  (matching the established mandatory-reason pattern for every
  irreversible financial correction in this codebase — Section 22).
- `RESOLVED` is reached only via one of the three substantive workflows
  fully completing, or via `DENIED`'s immediate close — **never** directly
  from `INVESTIGATING`.

### 12.2 Applicability to `GOODS_RECEIPT`

The identical state machine and `ReceivingDiscrepancy` table serve a
supplier goods-receipt discrepancy (the "single/small-store" case in D1,
where there is no second store to transfer between at all). The only
difference is `source_type = "GOODS_RECEIPT"` and the `RETURN_REQUIRED`
outcome routes to the **existing** `create_purchase_return` function
(unchanged, M19) instead of the new transfer-return lifecycle (Section 13)
— reusing the existing, working mechanism rather than duplicating it.

---

## 13. Physical Return Workflow (D3)

### 13.1 Lifecycle

```
REQUEST (destination store) ──► AUTHORIZE (Central Management OR the
                                            ORIGINAL transfer's source-store
                                            Manager) ──► SHIP (ordinary
                                            ship_transfer, on the new
                                            reverse-direction transfer) ──►
                                            RECEIVE (ordinary receive_transfer,
                                            at the original source store) ──►
                                            RESOLVED
```

- **Request:** a holder of the new `inventory.transfer.return.initiate`
  permission, scoped to the **destination** store of the original transfer
  (per D3: "Destination Store may initiate"), calls
  `initiate_transfer_return(original_transfer_id, lines, reason)`. This
  creates a **new** `InterStoreTransfer` row with `from_store_id =
  original.to_store_id`, `to_store_id = original.from_store_id`,
  `origin_transfer_id = original.id`, `status = "DRAFT"` — using the
  existing `_create_transfer_inner` machinery verbatim, with one added
  validation: the requested return quantity for each line may not exceed
  that line's net good-quantity-received-and-still-on-hand at the
  destination (a new check, since ordinary transfer creation has no such
  upstream-quantity ceiling).
- **Authorize:** a holder of the new
  `inventory.transfer.return.authorize` permission — granted to the
  **Regional Manager**/Admin tier (Central Management) **or** the
  **original transfer's source-store Manager** (an explicit,
  transfer-specific check: `caller_store_id == original.from_store_id`,
  not merely "any Manager anywhere") — transitions the new transfer from
  `DRAFT` to authorized. This is implemented as the existing `DRAFT` state
  simply being eligible for `ship_transfer` only once this authorization
  is recorded; no new persisted transfer status is introduced (avoiding
  duplicating `TRANSFER_STATUSES`) — instead, a new `authorized_by`/
  `authorized_at` pair of columns on `InterStoreTransfer` (nullable, only
  ever set on a return-shaped transfer) gates `ship_transfer` for any
  transfer with a non-null `origin_transfer_id`: such a transfer cannot
  ship until `authorized_by` is set.
- **Ship/Receive:** the **existing, unmodified** `ship_transfer`/
  `receive_transfer` functions, called against the new transfer exactly as
  for any other transfer — this is what guarantees D3's "genuine physical
  inventory movement," "preserve inventory quantities," "WAC" (Section 7),
  "Inventory In Transit," "GL," "store attribution," "audit history," and
  "idempotency" requirements automatically, since it is the same,
  already-fully-tested code path.
- **Resolved:** the moment the return transfer's own receipt fully
  completes (the existing derived-RECEIVED fact), the **original**
  discrepancy record (if the return arose from one — `RETURN_REQUIRED`
  outcome) transitions to `RESOLVED`, referencing the return transfer's id.

### 13.2 Cancellation/error handling

The return transfer is an ordinary `InterStoreTransfer` and inherits the
existing `cancel_transfer` behavior unchanged (`DRAFT`-only cancellation).
A return that has already shipped follows the same rules as any other
shipped transfer (Section 12/13's own mechanisms apply recursively — a
return can itself be lost, damaged, or short-received, and is investigated
exactly the same way, since it is not a structurally different kind of
transfer).

---

## 14. Aging/Alerts (D6)

**Perishability classification:** `Product.perishability_class` (new
column, `CHILLED` \| `AMBIENT`, default `AMBIENT`) is captured onto the
transfer line at ship time (frozen, alongside `unit_cost_at_shipment`, in a
new `TransferAgingAlert.threshold_hours` value: `48` for `CHILLED`, `168`
(7 days) for `AMBIENT`) — so a later reclassification of the product never
retroactively changes an in-flight transfer's threshold.

**Expected arrival:** computed once at ship time as `shipped_at +
threshold_hours`, stored on the `TransferAgingAlert` row (created
proactively at ship time, in `PENDING` conceptual state — i.e., `fired_at
IS NULL`), one row per transfer line.

**Detection mechanism — an explicit scoping decision:** this repository
has no job-scheduler/background-worker infrastructure for anything
resembling a per-minute or per-hour autonomous check (`TECHNICAL_BLUEPRINT.md`'s
background-worker scope names only nightly backups, a tax-retry queue, and
report refresh — nothing matching this shape). Rather than inventing an
entirely new scheduling subsystem, aging detection is **computed lazily**:
any read of the existing `inventory_in_transit` drill-down report, or a new
dedicated "Central Loss Prevention Dashboard" report endpoint, evaluates
`now() > expected_arrival_at` for every outstanding line and, on first
observing `True` for a given line, **creates** the `TransferAgingAlert`
row's `fired_at` value at that moment (a single `UPDATE ... WHERE fired_at
IS NULL`, naturally idempotent — a second concurrent read racing the same
check either wins the update or observes it already set, never creating a
duplicate). If this codebase later gains real scheduled-job infrastructure,
the identical detection query can instead run periodically — the
idempotency guarantee (a unique row per line, updated once) is identical
either way and does not depend on which trigger mechanism drives it. This
scoping decision is called out explicitly rather than silently narrowed:
**detection latency under the lazy model is bounded by how often someone
views the dashboard**, the same accepted limitation M24 discovery already
documented for the existing reconciliation report.

**Alert recipients — a second explicit scoping decision:** this repository
has no outbound notification channel (email/SMS/push) for anything, for
any module. "Alert: Source Store Manager; Destination Store Manager;
Central Loss Prevention Dashboard" is satisfied as three **read-scoped
views** of the same underlying `TransferAgingAlert` data: the source
store's manager sees their store's fired alerts on their own store's
version of the report (existing store-scoped read authorization,
unchanged), the destination store's manager likewise, and the
`Loss Prevention Officer` role sees the unfiltered, company-wide version —
**not** a push/email dispatch, since building a new outbound-notification
subsystem is out of scope for this contract unless the business
subsequently confirms it is required (flagged for Section 19 as a future
extension, not silently dropped).

**Absolute constraint (re-stated, unconditional):** nothing in this
mechanism ever calls `ship_transfer`, `receive_transfer`,
`cancel_transfer`, `TransferWriteOff` creation, or `_post_journal`. An
aging alert is purely an additional row plus a read-time computation —
this is enforced by the mechanism having **no** write access to any of
those functions in its own code path, not merely by convention.

**Acknowledgment/audit:** `acknowledged_by`/`acknowledged_at`, settable by
anyone who can view the alert, purely informational (does not resolve the
underlying transfer) — logged via the existing `audit_service.log_event`.

---

## 15. Period Close Controls (D7)

### 15.1 Extending `close_accounting_period`

Before creating the `AccountingPeriod` row for `store_id` covering
`[period_start, period_end]`, a new check runs (in addition to the
existing date-range/overlap validation, which is unchanged): does this
store have any of the following **open** positions, dated on or before
`period_end`?

1. An `InterStoreTransferLine` where this store is `from_store_id` and
   `remaining_in_transit > 0` (Section 5's identity) — an unresolved
   outbound in-transit position on this store's own books.
2. A `ReceivingDiscrepancy` with `store_id` = this store and `status` not
   in `{RESOLVED}`.
3. A `TransferWriteOff` with `store_id` = this store and `status =
   "PENDING_APPROVAL"`.

If any exist, `close_accounting_period` raises a new `ConflictError`
(`error_code="UNRESOLVED_TRANSFER_POSITIONS"`) listing every blocking row,
**unless** the caller (who must hold `accounting.admin`, unchanged) also
supplies an explicit resolution instruction per blocking item — one of:

- **Force reconciliation:** not a database action at all — the caller is
  told to go complete the real underlying workflow (receive the
  transfer, finish the investigation) and retry the close. No new code
  path; this is the "do nothing new, go finish the real thing" option.
- **Write-off:** the caller triggers the existing `TransferWriteOff`
  creation flow (Section 10) for the item **now** — this does **not**
  bypass D2's approval requirement; if approval cannot be obtained before
  the period must close, the caller must use the suspense option instead.
  This is an explicit, stated invariant (Section 22): **period-close
  pressure never authorizes an unapproved write-off.**
- **Suspense carry-forward:** a new
  `carry_forward_to_suspense(transfer_line_id_or_discrepancy_id,
  accounting_period_id)` service call, requiring `accounting.admin`
  (mapped to the Controller role, Section 10.3), posts:
  ```
  Dr Transfer Suspense        outstanding_amount   (this store's books)
      Cr Inventory In Transit     outstanding_amount
  ```
  dated `date.today()` (a compensating entry, following the established
  convention), and creates a `TransferSuspenseRecord` row linking the
  suspended amount back to the original transfer line/discrepancy and to
  the `AccountingPeriod` being closed. **The underlying transfer's own
  `shipped_quantity`/`received_quantity` bookkeeping is untouched** — this
  is purely a GL reclassification for period-close presentation, not a
  resolution of the real-world discrepancy.

Only once every blocking item for that store has an explicit resolution
(force-reconciled to zero, written off, or suspended) does the
`AccountingPeriod` row get created.

### 15.2 Resolving a suspended item later

When the real-world transfer eventually resolves (a late receipt, or a
subsequent write-off), the resolving function (`receive_transfer` or
`TransferWriteOff` posting) checks for an **open** `TransferSuspenseRecord`
covering that line first:

- If one exists, the credit side of the resolving journal targets
  **Transfer Suspense** for `min(resolving_quantity,
  suspended_remaining_amount)`, and **Inventory In Transit** for any
  remainder beyond the suspended amount (covering the case where only part
  of a line's outstanding balance was ever suspended). The
  `TransferSuspenseRecord` is marked `resolved_at`/`resolution_type =
  "LATE_RECEIPT"` or `"WRITTEN_OFF"` once its suspended amount is fully
  cleared.
- If none exists, the resolving journal targets **Inventory In Transit**
  exactly as it does today — no change for a line that was never
  suspended.

This is the concrete mechanism that satisfies "no unresolved transfer may
silently disappear" — a suspended item remains fully visible (its own
row, its own report, its own required eventual resolution) and the money
always lands in an account that is later provably clearable to zero, never
an unexplained balance.

### 15.3 Reopening/correction rules

Unchanged from the existing `AccountingPeriod` design: **no reopening.**
Once a period closes (with or without a suspense carry-forward), it stays
closed; any later resolution posts into whatever period is open *then*,
never into the closed one — identical to every other correction mechanism
in this system.

---

## 16. Security/Store Isolation

Every new mutating function follows the identical, existing pattern:

| Function | Store-access check |
|---|---|
| Discrepancy investigation transition | `caller_store_id` must equal the discrepancy's `store_id` (destination) or be unrestricted |
| `TransferWriteOff` initiation | `caller_store_id` must equal the write-off's `store_id` or be unrestricted |
| `WriteOffApproval` creation | the approver's role must hold the required approval permission for that tier — **no** store-scope requirement beyond the role check itself, since Regional-Manager-tier approval is explicitly cross-store by design (Section 10.3) |
| `initiate_transfer_return` | `caller_store_id` must equal the **destination** store of the original transfer (D3: only the destination may request) |
| Return authorization | `caller_store_id` must equal the original transfer's `from_store_id`, **or** the caller holds the Regional-Manager/Admin tier |
| Custody acceptance | inherits `receive_transfer`'s existing check unchanged (destination store only) |
| `carry_forward_to_suspense` | requires `accounting.admin` (already unrestricted-only in practice — no store-scoped user holds it today) |
| Cross-entity postings | no new isolation rule — the existing per-store scoping already prevents any cross-store data access; entity grouping is a read-side aggregation only, never a new authorization boundary |

**No new cross-store or cross-entity bypass is introduced anywhere** — every
new table carries `store_id` and is queried/written through the same
`_enforce_store_access` helper pattern the transfers module already uses.

---

## 17. Concurrency/Idempotency

| Operation | Idempotency key | Concurrency guard |
|---|---|---|
| Extended receipt (good/damaged/short) | Existing `client_transaction_id` on `InterStoreTransferReceipt`, unchanged | Existing transfer-header lock + destination-product locks, unchanged |
| `ReceivingDiscrepancy` creation | Implicit — created at most once per receipt-line-item (a `UNIQUE` constraint on `(source_type, source_id)` where `source_id` = the receipt item's id) | Created inside the same transaction as the receipt it originates from |
| Discrepancy state transition | New `client_transaction_id` field on the transition call, same fast-path-then-lock-then-recheck shape as `ship_transfer`/`receive_transfer` | Lock the `ReceivingDiscrepancy` row `FOR UPDATE` before checking/advancing `status` |
| `TransferWriteOff` creation | New `client_transaction_id` field, unique | Lock the transfer line row `FOR UPDATE` before validating remaining-quantity bounds, mirroring `receive_transfer`'s own lock order |
| `WriteOffApproval` creation | `UNIQUE(write_off_id)` — a write-off can only ever be decided once (approved or denied); a second decision attempt on an already-decided write-off is a no-op returning the existing decision, matching `cancel_transfer`'s idempotent-no-op pattern | Lock the `TransferWriteOff` row `FOR UPDATE` |
| `initiate_transfer_return` | Reuses `create_transfer`'s existing behavior (two identical requests legitimately create two DRAFT transfers, unchanged) | N/A — a DRAFT has no side effect to duplicate, same as today |
| Return authorization | New `client_transaction_id`-equivalent (`authorized_by` being non-null is itself the idempotency guard — a second authorization attempt is a no-op if already authorized) | Lock the transfer header row `FOR UPDATE` |
| Aging alert creation | `UNIQUE(transfer_line_id)` on `TransferAgingAlert`, `fired_at` set via a single conditional `UPDATE ... WHERE fired_at IS NULL` | No row lock needed — the conditional update is itself atomic |
| `carry_forward_to_suspense` | New `client_transaction_id` field, unique | Lock the transfer line/discrepancy row `FOR UPDATE`, mirroring existing correction-posting patterns |
| Intercompany settlement | `client_transaction_id`, unique, mirroring `SupplierPayment` exactly | Lock both entities' relevant balances are not locked as a pair (no cross-entity lock ordering exists) — each entity's own journal entry is independently atomic per the existing single-store-scoped `_post_journal` transaction boundary; no new deadlock surface is introduced because no function ever locks rows belonging to two different stores/entities in a fixed but reversible order (Section 21 revisits this explicitly for the concurrent-settlement case) |

**General rule preserved:** every new mutating function locks its primary
row(s) *before* reading or checking status/quantity fields, exactly as
`ship_transfer`/`receive_transfer`/`cancel_transfer` already do — no new
function relies on a status check performed before acquiring the relevant
lock.

---

## 18. Failure Handling

| Failure scenario | Behavior |
|---|---|
| Duplicate receipt (with damage/short fields) | Existing idempotency fast path returns the original receipt, including its already-created discrepancy row(s) — unchanged behavior, extended fields included |
| Duplicate return request | Two DRAFT return-transfers legitimately created (matches ordinary transfer-creation semantics) — the second is simply cancelled or ignored by the requester; no data corruption results |
| Duplicate write-off | `client_transaction_id` fast path returns the existing `TransferWriteOff`, never posts twice |
| Duplicate approval | `UNIQUE(write_off_id)` on `WriteOffApproval` — second attempt is a no-op returning the existing decision |
| Concurrent receipt | Existing transfer-header lock serializes; unchanged |
| Concurrent write-off attempts on the same line | Transfer-line row lock (new, on `TransferWriteOff` creation) serializes; the loser sees the updated `remaining_in_transit` under the lock and is rejected if it would over-write-off |
| Concurrent return request + write-off on the same remaining quantity | Both acquire the transfer-line lock in the same order (line id ascending, matching `ship_transfer`'s existing lock-ordering discipline) — whichever commits first reduces `remaining_in_transit`; the second re-checks under its own lock and is rejected if insufficient quantity remains |
| Concurrent approval by two different approvers | `UNIQUE(write_off_id)` on `WriteOffApproval` — the loser's `IntegrityError` is caught and it returns the winner's decision, the identical recovery shape `ship_transfer`/`receive_transfer` already use for their own unique-constraint races |
| Retry after database failure mid-transaction | No partial state is ever visible — every new operation follows the existing "no `commit()` inside the service function itself; the route handler commits once" discipline, so a crash mid-operation rolls back everything, exactly as Section 11 of `M24_DISCOVERY.md` already verified for the pre-existing functions |
| Retry after external notification failure | Not applicable — no outbound notification is ever sent synchronously as part of a financial operation (Section 14's scoping decision); an alert's `fired_at` write is itself the complete, atomic unit of work |
| Partially completed transaction (e.g., discrepancy created, write-off approval failed) | Cannot occur as a partial state — discrepancy creation and any single write-off/approval step are each their own complete, committed unit; a `TransferWriteOff` sitting in `PENDING_APPROVAL` is not a "partial failure," it is a valid, expected, fully-committed intermediate state |
| Stale client request (e.g., approving a write-off already reversed) | The `WriteOffApproval`/`TransferWriteOff` status is re-checked under lock before any state change; a stale approval attempt against an already-`REVERSED` write-off is rejected with a clear conflict error, not silently applied |
| Cross-store ID manipulation (e.g., submitting another store's transfer-line id to a write-off call) | Rejected by the existing `_enforce_store_access` pattern (Section 16), extended to every new function |
| Unauthorized entity access (submitting a cross-entity settlement without the entity-level authority) | Rejected — settlement requires `accounting.admin`, unrestricted only |
| Period closing while a transfer is being actively resolved | The close attempt's blocking-item scan (Section 15.1) and any concurrent resolution (receipt, approval, suspense) both operate under row-level locks on the same underlying rows (`InterStoreTransferLine`, `ReceivingDiscrepancy`, `TransferWriteOff`) — whichever transaction commits first is authoritative; the other re-reads under its own lock and either finds the item already resolved (close proceeds) or still open (close is blocked, or the resolution is rejected if the period closed first) |
| Resolution attempted after period closure | Any resolving journal (late receipt, write-off, suspense clearing) still passes through the unconditional `_enforce_period_open` gate — if the *current* date's period is closed, the resolution is refused exactly like every other correction in this system; it must wait for the next open period, exactly as Section 15.3 states |

---

## 19. M25 Implementation Boundary

### A. Must implement in M25

- `AccountingEntity` model + migration + `Store.accounting_entity_id`
  backfill (Section 4).
- Extended `InterStoreTransferReceiptItem` (`quantity_damaged`,
  `quantity_declared_short`) + migration.
- `TransferCustodyAcceptance` model + migration, wired into
  `receive_transfer`.
- `ReceivingDiscrepancy` model + migration + state machine service
  functions, wired to both `TRANSFER_RECEIPT` and `GOODS_RECEIPT` origins.
- `ApprovalPolicyRule`, `TransferWriteOff`, `WriteOffApproval` models +
  migrations + service functions + the new GL posting (Section 6.1/6.2)
  and new `source_type = "TRANSFER_WRITE_OFF"`/reversal source type.
- `SupplierCreditNoteRequest` model + migration + service functions
  wrapping the existing `SupplierCreditNote` creation.
- `InterStoreTransfer.origin_transfer_id`,
  `authorized_by`/`authorized_at` columns + migration;
  `initiate_transfer_return`/return-authorization service functions.
- New permissions: `inventory.transfer.discrepancy.investigate`,
  `inventory.transfer.writeoff.initiate`,
  `inventory.transfer.writeoff.approve`,
  `inventory.transfer.writeoff.reverse`,
  `inventory.transfer.return.initiate`,
  `inventory.transfer.return.authorize`,
  `accounting.reconciliation.force_close`; new roles `Regional Manager`,
  `Loss Prevention Officer`.
- `Product.perishability_class` + migration; `TransferAgingAlert` model +
  migration + lazy-detection service function + dashboard report endpoint.
- `close_accounting_period` extension (Section 15.1);
  `TransferSuspenseRecord` model + migration;
  `carry_forward_to_suspense` service function; suspense-aware receipt/
  write-off crediting logic (Section 15.2).
- Three new GL accounts (Transfer Suspense, Intercompany Receivable,
  Intercompany Payable) — seeded by migration, following existing
  `SYSTEM_ACCOUNTS` conventions.
- `IntercompanySettlement` model + migration + service functions +
  reversal function (Section 8).
- Frontend surfacing for every workflow above (Phase 10, Section 20).

### B. Already implemented and requires no change

- Ordinary DRAFT→SHIPPED→RECEIVED/CANCELLED transfer lifecycle.
- `Inventory In Transit` shipment/receipt mechanics for same-entity
  transfers.
- WAC freezing/rolling mechanics.
- Damage discovered **after** a normal receipt (existing
  `create_stock_adjustment` path — zero transfer-specific work, confirmed
  again in Section 6.2/11).
- Over-receipt prohibition.
- `_enforce_period_open`/closed-period protection.
- P&L's dynamic `operating_expenses` inclusion of any correctly-typed
  EXPENSE account.
- `create_purchase_return` (reused for `GOODS_RECEIPT`-origin returns,
  Section 12.2).

### C. Requires a future milestone (not M25)

- A dedicated `Controller` role narrower than full Admin (Section 10.3) —
  `accounting.admin` remains the interim mapping.
- A real outbound notification channel (email/SMS/push) for aging alerts
  (Section 14) — the lazy, read-time, dashboard-only mechanism is the M25
  scope.
- A genuine job-scheduler/background-worker subsystem, if the business
  later requires sub-dashboard-refresh detection latency for aging alerts.
- A two-step "sign at the dock, count later" custody model, if the
  business later confirms this reflects real operations (Section 11).
- Transfer-pricing/markup policy for cross-entity transfers (currently
  always at cost — Section 4/8), if the business later wants inter-entity
  transfers priced above cost.
- Per-lot cost tracking, which would allow vendor-responsibility
  attribution (Section 9) in cases this design cannot currently trace.

### D. Explicitly out of scope

- Any legally-compliant e-signature mechanism (Section 11).
- Reducing the corporate-chain and independent-entity models to one
  another, or merging their accounting treatment.
- Any automatic (non-human-triggered) write-off, state change, or journal
  posting driven by the aging mechanism (D6's absolute constraint).
- Serialized/lot tracking beyond what Section 9 already notes as a
  limitation.
- Any change to sales, payroll, HR, or fiscal (M20) modules.

---

## 20. Critical Accounting Trace (17 Scenarios)

Each scenario: Physical inventory → Inventory In Transit → WAC → GL →
P&L → AP/AR → store/entity attribution → cost center → approval → audit
event → period status. "—" means no effect for that dimension.

**1. Normal transfer (unchanged, same entity).**
Physical: source −10 at ship, destination +10 at receipt. In-Transit: Dr
1000 at ship, Cr 1000 at receipt (net zero). WAC: frozen at ship, rolled at
receipt. GL: two balanced entries, both pure reclass. P&L: none. AP/AR:
none (same entity). Attribution: both stores, throughout. Cost center: —.
Approval: none needed. Audit: `TRANSFER_SHIPPED`/`TRANSFER_RECEIPT_COMPLETED`.
Period: each leg's own date, must be open for that store.

**2. Short receipt (shipped 10, good 8, short 2 declared).**
Physical: source −10; destination +8 only. In-Transit: Dr 1000 at ship, Cr
800 at receipt for the good portion — remaining Dr 200 stays open, now
tracked by a `RECORDED` `ReceivingDiscrepancy` for the 2-unit shortage.
WAC: 8 units rolled normally; the 2 short units carry no WAC effect. GL:
receipt journal posts only for the 800-value good portion; no journal yet
for the 2 short units (nothing to post until the discrepancy resolves).
P&L: none yet. AP/AR: none (same entity example). Attribution: discrepancy
`store_id` = destination (it is discovered there), pending investigation
outcome. Cost center: pending. Approval: pending investigation. Audit:
`TRANSFER_RECEIPT_COMPLETED` + `DISCREPANCY_RECORDED`. Period: receipt
posts into destination's open period; the discrepancy itself posts nothing
yet.

**3. Damaged receipt (shipped 10, good 8, damaged 2).**
Identical shape to Scenario 2 except `quantity_damaged=2` instead of
`quantity_declared_short`. In-Transit: same partial-clear pattern. The
`ReceivingDiscrepancy.discrepancy_type = "DAMAGE"`.

**4. Short + damaged receipt (shipped 10, good 6, damaged 2, short 2).**
Same mechanism, `discrepancy_type = "BOTH"`, two separate quantity fields
on one `ReceivingDiscrepancy` row (or two rows — implementation detail
left to M25, either is consistent with this contract as long as the total
identity in Section 5 holds).

**5. Lost shipment (shipped 10, never received, before custody
acceptance).**
Physical: source −10 at ship; destination never +anything. In-Transit: Dr
1000 at ship, never credited by any receipt. Eventually (via aging alert
→ human investigation → `STORE_RESPONSIBILITY`) a `TransferWriteOff` for
10 units posts `Dr 5900 (Shrinkage) / Cr 1520 (In-Transit)`, `store_id =
from_store_id` (Section 6.2, pre-custody rule). WAC: no effect. P&L: yes,
via the existing dynamic formula. AP/AR: none (same entity). Cost center:
`source_type="TRANSFER_WRITE_OFF"`. Approval: per the configured tier for
the amount. Audit: full trail (initiation, approval, posting). Period:
posts at `date.today()`, must be open for the source store.

**6. Loss before custody acceptance (identical to Scenario 5, restated for
explicitness).** Same as Scenario 5 — "before custody acceptance" and "no
receipt event has ever covered this quantity" are the same condition in
this design.

**7. Loss after custody acceptance (damage discovered on already-received
goods).**
Physical: destination's stock, already on-hand, found damaged. In-Transit:
already fully cleared (unaffected — this quantity was never outstanding).
WAC: unaffected. GL: the **existing**, unmodified
`create_stock_adjustment`/`STOCK_ADJUSTMENT_OUT` path posts `Dr 5900 / Cr
1500 (Inventory)`, `store_id = destination` (Section 6.2, post-custody
rule — already solved, zero new code). P&L: yes, already included today.
AP/AR: none. Cost center: `source_type="STOCK_ADJUSTMENT"` (distinct from
Scenario 5's `TRANSFER_WRITE_OFF`, correctly separating the two loss
sources on the same account, per Section 6.2). Approval: existing stock-
adjustment authorization, unchanged. Audit: existing stock-adjustment
audit event. Period: destination's open period, today's date.

**8. Vendor-caused damage.**
Physical/In-Transit: as Scenario 3/4 (a receiving discrepancy). Investigation
concludes `VENDOR_RESPONSIBILITY` (traceable origin confirmed). GL: **no**
`TransferWriteOff`/shrinkage posting at all — instead, once
`SupplierCreditNoteRequest` reaches `APPROVED`, the existing
`SupplierCreditNote` posts `Dr 2010 (Accounts Payable) / Cr 1500
(Inventory)` (or the existing credit-note reason's account, unchanged AP
mechanics), reducing what the store owes the vendor. P&L: none directly
(a balance-sheet-only AP reduction, matching existing credit-note
treatment). AP/AR: Accounts Payable reduced. Attribution: the purchasing
store (wherever the original vendor relationship sits). Cost center: N/A
(existing AP sub-ledger). Approval: existing credit-note authorization,
unchanged. Audit: existing credit-note audit trail. Period: the credit
note's own posting date, existing rules.

**9. Physical return (D3).**
Physical: a **new** reverse-direction transfer ships from the original
destination (−quantity there) and receives at the original source
(+quantity there). In-Transit: the new transfer's own Dr/Cr pair, fully
symmetric with Scenario 1 — the *original* transfer's own In-Transit entry
is untouched (it already cleared when the goods were first received). WAC:
new `unit_cost_at_shipment` captured from the returning store's current
cost (Section 7 — a legitimate, expected divergence from the original
cost). GL: two new, ordinary balanced entries — no loss recognized. P&L:
none. AP/AR: if the original transfer was cross-entity, the return, being
its own transfer in the reverse direction between the same two entities,
posts its own Intercompany Receivable/Payable pair, independently netting
against (but not literally reversing) the original — the two entities'
mutual balance simply nets closer to zero as both legs exist. Attribution:
both stores, via the return's own record; `origin_transfer_id` links it to
the original for reporting. Cost center: N/A. Approval: return
authorization (Section 13). Audit: full new-transfer audit trail, plus a
note on the original discrepancy (if any) marking it resolved via this
return's id. Period: the return's own ship/receive dates, ordinary rules.

**10. Approved write-off.**
As Scenario 5, carried through to `APPROVED` → `POSTED`. Physical: none
(nothing to move). In-Transit: credited to zero for the written-off
quantity. GL: `Dr 5900 / Cr 1520`, balanced. P&L: expense recognized.
AP/AR: none. Attribution: per Section 6.2's rule for the case (pre- or
post-custody). Cost center: `TRANSFER_WRITE_OFF`. Approval: a distinct
`WriteOffApproval` row, `decision="APPROVED"`, `approved_by != initiated_by`
(DB-enforced). Audit: initiation + approval + posting, three linked
events. Period: today's date, open.

**11. Rejected write-off.**
`WriteOffApproval.decision="DENIED"`. Physical: unaffected. In-Transit:
unaffected — still outstanding. GL: **nothing posted** — a denied write-off
creates no journal entry at all (there is nothing to "undo" because
nothing financial happened). P&L: unaffected. Attribution: N/A. Cost
center: N/A. Approval: the denial itself, fully audited
(`decision_notes` mandatory, matching the mandatory-reason convention).
Audit: initiation + denial. Period: N/A — no posting occurred, so no
period-open check was ever needed for this attempt. The originating
`ReceivingDiscrepancy` (if any) reopens into `INVESTIGATING` — the loss
does not vanish (Section 12.1).

**12. Write-off reversal.**
A previously `POSTED` write-off is reversed by Central Management/Admin
(Section 10.3/D2's reversal authority). GL: a **new** compensating entry,
`Dr 1520 (In-Transit) / Cr 5900 (Shrinkage)` — the exact mirror image,
posted `date.today()`, never editing the original — following the
established "new entry, never a mutation" rule (M4's immutability, reused
verbatim by every reversal function in this system). P&L: the original
expense is offset in the current period (not retroactively erased from the
period it was originally recognized in — matching every other reversal in
this codebase). In-Transit: restored to outstanding — the underlying real-
world question ("were the goods actually found?") must then be resolved
through the ordinary receipt or a fresh investigation, exactly as if the
write-off had never posted. Attribution: same store as the original.
Approval: reversal itself requires the reversal-tier authority (Admin);
the *original* approval is untouched (history is never rewritten). Audit:
a new, linked reversal event. Period: today's date, must be open.

**13. Corporate inter-store transfer (same entity — restates Scenario 1
under the explicit corporate-chain label).** Identical to Scenario 1;
D1's "corporate chain" requirement is exactly today's existing, unchanged
mechanics, confirmed to require no new code.

**14. Independent-entity inter-store transfer.**
Physical: source −10 at ship, destination +10 at receipt (unchanged).
In-Transit: **not used** for the cross-entity case (Section 8) — replaced
by Intercompany Receivable/Payable. GL at ship:
`Dr Intercompany Receivable / Cr Inventory` (source entity's books). GL at
receipt: `Dr Inventory / Cr Intercompany Payable` (destination entity's
books). WAC: unchanged mechanics (frozen at ship, rolled at receipt). P&L:
none (at-cost transfer, no margin). AP/AR: a live receivable on one
entity's books and a live payable on the other's, until settled. Cost
center: N/A. Attribution: each entity's own separate books — never
combined into one journal entry (Section 8's explicit rule). Approval:
none beyond ordinary ship/receive authorization. Audit: existing
`TRANSFER_SHIPPED`/`TRANSFER_RECEIPT_COMPLETED`, plus the new
`source_type` values on the journal entries themselves. Period: each
entity's own store's own open-period check, independently.

**15. Period-end unresolved transfer.**
A store attempts to close a period with an outstanding
`remaining_in_transit > 0` line. `close_accounting_period` raises
`UNRESOLVED_TRANSFER_POSITIONS` (Section 15.1) — the period is **not**
created, GL and inventory are completely unaffected, until one of the
three resolutions is explicitly chosen.

**16. Suspense carry-forward.**
Controller chooses suspense for the Scenario 15 item. GL: `Dr Transfer
Suspense / Cr Inventory In Transit`, today's date, the source store's
books. In-Transit: relieved by exactly the suspended amount. Suspense:
recorded, open, linked to the specific line and the closing period. P&L:
none (still a balance-sheet-only reclassification). Attribution:
unchanged store. Approval: `accounting.admin` only. Audit: a dedicated
`SUSPENSE_CARRY_FORWARD` event. Period: **the period now closes
successfully** for that store, with the item's value provably sitting in
Transfer Suspense, never silently dropped.

**17. Forced reconciliation.**
Controller instead chooses to resolve the Scenario 15 item directly before
closing — e.g., the destination finally submits the overdue receipt.
Physical/In-Transit/WAC/GL: exactly Scenario 1 or 2's ordinary receipt
mechanics, dated today or the caller-supplied real date (whichever the
receiving workflow legitimately uses — unchanged). Once the item is no
longer outstanding, `close_accounting_period` is retried and succeeds with
no suspense entry ever created. No unexplained balance results in any of
the 17 scenarios.

---

## 21. Contradiction Check

Every pairwise interaction among D1–D7, and between D1–D7 and the existing
invariants, was traced. **No unresolvable contradiction was found.** Two
apparent tensions were identified and are resolved by this document's own
architecture (not by silently reinterpreting stakeholder intent):

1. **Apparent tension:** D1's "the only ledger holding the un-cleared
   in-transit value is the source store's, until an actual receipt posts"
   (an existing, mechanical fact) vs. D1's new "Destination Store
   explicitly accepts custody... loss belongs to Destination" (a
   stakeholder requirement). **Resolution:** custody acceptance is defined
   (Section 11) to be the same event as the receipt that already performs
   this exact ledger transfer — the two statements describe the identical
   moment, not competing ones. No conflict.
2. **Apparent tension:** D4's "good inventory becomes available to the
   destination immediately" vs. D1's requirement that a cross-entity
   receipt also recognize an Intercompany Payable. **Resolution:** the
   inventory movement and the AP/AR journal post in the same atomic
   transaction (Section 8/18), so there is never a window where stock is
   sellable but financially unrecognized. No conflict.

**No contradiction was found between any stakeholder answer and an
existing invariant** (double-entry balance, closed-period protection,
store isolation, no over-receipt, WAC integrity, immutability) — every new
flow terminates in the same enforcement points those invariants already
live in (`_post_journal`, `_enforce_period_open`, `_enforce_store_access`,
`record_movement`'s bounds checks), rather than introducing a parallel path
that could bypass them.

**Supersession, not contradiction:** D5's explicit "reuse the existing
global Expense/Shrinkage account" supersedes M24B's earlier, non-binding
analytical lean toward a dedicated new account. This is not a
contradiction requiring escalation — M24B's own language explicitly framed
that lean as unresolved pending stakeholder input, and D5 is exactly that
input.

---

## 22. Explicit Invariants

Preserved, unchanged:

- Double-entry accounting; every journal entry's debits equal its credits
  (DB-enforced trigger, untouched).
- No negative inventory.
- No over-receipt (unchanged; the new `quantity_good` bound is the same
  ceiling `quantity_received` already enforced).
- WAC/frozen-cost integrity (Section 7).
- Inventory In Transit reconciliation (extended, not replaced, by the
  entity-scoped AP/AR reconciliation in Section 8).
- Closed-period protection — no bypass, ever, including under
  period-close pressure (Section 15.1's explicit "write-off never skips
  approval" rule).
- Store isolation (Section 16).
- Auditability — every new state transition, approval, and posting logs
  via the existing `audit_service.log_event`.
- Idempotency and concurrency safety (Section 17).
- Historical immutability — every correction is a new entry, never an edit
  (Scenario 12).
- Deterministic store/entity attribution — Section 6.2's rule is a pure
  function of custody-acceptance state and vendor-traceability, never
  ambiguous or caller-chosen.

New invariants this design requires:

- **Quantity identity** (Section 5): `shipped_quantity == good + damaged +
  declared_short + returned + written_off + remaining_in_transit`, checked
  on every write.
- **Self-approval is structurally impossible**, not merely
  permission-gated (`CHECK (initiated_by <> approved_by)`, Section 10.2).
- **Fail-closed approval routing**: an unconfigured `ApprovalPolicyRule`
  gap requires the highest-authority role, never silently permits a lower
  one (Section 10.1).
- **Period-close pressure never authorizes an unapproved write-off**
  (Section 15.1) — suspense, not a shortcut approval, is the only
  pressure-relief valve.
- **A suspended balance is always provably clearable to zero** — every
  `TransferSuspenseRecord` has a defined resolution path (Section 15.2),
  never an open-ended write-off-by-default.
- **The aging mechanism has no write access** to any transfer-mutating,
  inventory-mutating, or journal-posting function (Section 14) — enforced
  by code-path isolation, not merely by documentation.
- **No cross-entity journal entry** — every posting remains scoped to
  exactly one store/entity, even for intercompany transactions (two
  independent entries, never one, Section 8/17).

---

## 23. Migration Strategy

All migrations are additive, nullable-or-defaulted, and backward
compatible with zero behavior change for any store that never uses a new
feature:

1. `AccountingEntity` table + `Store.accounting_entity_id` (`NOT NULL`
   after backfilling one default `CORPORATE_DIVISION` row for all existing
   stores, in the same migration).
2. `Product.perishability_class` (`NOT NULL DEFAULT 'AMBIENT'`).
3. `InterStoreTransferReceiptItem.quantity_damaged`,
   `.quantity_declared_short` (`NOT NULL DEFAULT 0`).
4. `TransferCustodyAcceptance` table (new, `UPDATE`/`DELETE` revoked from
   the application role, matching the `journal_entries`/`audit_logs`
   pattern).
5. `ReceivingDiscrepancy` table (new).
6. `ApprovalPolicyRule`, `TransferWriteOff`, `WriteOffApproval` tables
   (new); new `AUTOMATED_SOURCE_TYPES` values
   (`"TRANSFER_WRITE_OFF"`, `"TRANSFER_WRITE_OFF_REVERSAL"`) added to the
   existing `CHECK` constraint on `journal_entries.source_type`.
7. `SupplierCreditNoteRequest` table (new).
8. `InterStoreTransfer.origin_transfer_id`, `.authorized_by`,
   `.authorized_at` (all nullable).
9. New permission rows (`ROLE_PERMISSIONS` seed data, not schema) and new
   `Role` rows (`Regional Manager`, `Loss Prevention Officer`).
10. `TransferAgingAlert` table (new).
11. `TransferSuspenseRecord` table (new); three new `Account` rows
    (Transfer Suspense, Intercompany Receivable, Intercompany Payable),
    seeded following the existing `SYSTEM_ACCOUNTS` list pattern; new
    `AUTOMATED_SOURCE_TYPES` values for the intercompany postings and the
    suspense carry-forward posting.
12. `IntercompanySettlement` table (new).

Every migration follows this codebase's existing migration-safety
discipline (additive-only, reversible `downgrade()`, no destructive
`ALTER` on populated columns) — re-verified against
`docs/M18_DESIGN.md`'s migration-safety audit criteria at implementation
time, not re-litigated here.

---

## 24. API/Frontend Impact

**New endpoints (M25):** discrepancy list/detail/investigate/resolve;
write-off create/approve/deny/reverse; `ApprovalPolicyRule` CRUD (Admin
only); transfer-return request/authorize; aging dashboard (company-wide
and store-scoped views); suspense record list; `close_accounting_period`'s
existing endpoint gains new required fields for blocking-item resolution
when blockers exist (backward compatible — a store with zero blocking
items closes exactly as it does today, with no new required field).

**Changed endpoints:** `receive_transfer`'s request schema gains optional
`quantity_damaged`/`quantity_declared_short` per line (defaulting to 0 —
existing callers sending only `quantity_received` behave identically to
today).

**Frontend (Phase 10 only, after every backend invariant is established):**
a receiving-discrepancy entry form (good/damaged/short per line); an
investigation queue view; a write-off approval inbox; a return-request
form; a custody-acceptance confirmation step folded into the existing
receive screen; the aging/loss-prevention dashboard; a period-close
blocking-item resolution screen. None of this is built before Phase 4
(Section 19/20 below) establishes the underlying accounting/inventory
invariants in the backend and its tests.

---

## 25. M25 Implementation Phasing

Ordered so every accounting/inventory invariant is established and tested
before any UI is exposed, and so no single change spans more than one
domain.

| Phase | Objective | Files/components | Migration | API | Frontend | Acceptance criteria |
|---|---|---|---|---|---|---|
| 1 | `AccountingEntity` foundation | `accounting_entities` module (new), `Store` model | Yes (additive, backfilled) | None yet | None | Full existing regression suite green with zero behavioral diff; new entity table exists and is queryable |
| 2 | Extended receipt capture + custody acceptance | `transfers/models.py`, `transfers/service.py::receive_transfer` | Yes | `receive_transfer` request schema extended (backward compatible) | None yet | Happy path (damaged=short=0) byte-identical to today; new fields correctly persisted; `TransferCustodyAcceptance` row created 1:1 with each receipt |
| 3 | `ReceivingDiscrepancy` + investigation state machine | New `discrepancies` module, wired from both transfers and purchasing receiving | Yes | Discrepancy list/detail/investigate endpoints | None yet | State machine transitions correctly gated by permission and store scope; `GOODS_RECEIPT` and `TRANSFER_RECEIPT` origins both exercised |
| 4 | Approval-gated write-off | `ApprovalPolicyRule`, `TransferWriteOff`, `WriteOffApproval`, GL posting | Yes | Write-off create/approve/deny/reverse endpoints | None yet | Self-approval structurally impossible (DB test); fail-closed routing proven; full accounting trace (Scenarios 5/10/11/12) passes |
| 5 | Vendor claim linkage | `SupplierCreditNoteRequest`, wiring to existing `SupplierCreditNote` | Yes | Vendor-claim lifecycle endpoints | None yet | Scenario 8 passes end to end; denial correctly reopens the discrepancy |
| 6 | Physical return workflow | `origin_transfer_id`/`authorized_by`, `initiate_transfer_return` | Yes | Return request/authorize endpoints | None yet | Scenario 9 passes; return cannot exceed net-on-hand-received; D3's dual authorization path both work |
| 7 | Cross-entity AP/AR | Intercompany posting logic, `IntercompanySettlement` | Yes | Settlement create/reverse endpoints | None yet | Scenario 14 passes; entity-pair AP/AR always nets to zero across both books |
| 8 | Aging/timeout | `Product.perishability_class`, `TransferAgingAlert`, lazy-detection query | Yes | Dashboard endpoint(s) | None yet | Idempotent alert creation proven under concurrent reads; zero write access to any mutating function proven by code-path audit |
| 9 | Period-close reconciliation gate | `close_accounting_period` extension, `TransferSuspenseRecord` | Yes | Extended close endpoint | None yet | Scenarios 15/16/17 all pass; a period never closes with a silently-dropped balance |
| 10 | Frontend | All screens named in Section 24 | None | None (consumes existing) | Full UI | Manual browser smoke test of every new workflow, plus existing frontend regression suite green |

Each phase ends with its own full backend regression run (existing suite +
that phase's new tests) before the next phase begins — no phase proceeds
on a red suite.

---

## 26. M25 Testing Sessions

Every new business rule below has the nine required test categories;
sessions are grouped by the phase that introduces them. Sessions reuse the
established mutation-testing methodology (apply, run, confirm RED for the
stated reason, revert, diff-verify byte-identical) verbatim from M19–M24B.

| # | Phase | Session | Objective | Categories covered | Mutation target |
|---|---|---|---|---|---|
| A | 1 | Entity backfill | Every existing store lands on exactly one default entity; zero behavior change | Happy-path, migration-safety | Remove the backfill step — proves the migration test would catch a store left without an entity |
| B | 2 | Extended receipt, zero discrepancy | Byte-identical to today's existing receipt tests when damaged/short are both 0 | Happy-path, regression | Force `quantity_damaged` to a nonzero default — proves the regression suite catches an unintended behavior change |
| C | 2 | Custody acceptance creation | One `TransferCustodyAcceptance` row per receipt event, immutable | Happy-path, idempotency, concurrency | Attempt an `UPDATE` on the table at the DB layer — proves the privilege revoke holds |
| D | 3 | Discrepancy auto-creation | Nonzero damaged/short always produces exactly one discrepancy row | Happy-path, adversarial (zero damaged+short, boundary at exactly 0) | Skip discrepancy creation on a nonzero value — proves detection |
| E | 3 | Investigation authorization | Only a holder of the investigate permission, scoped to the destination store, may transition | Authorization, cross-store isolation | Remove the store-scope check — proves cross-store denial is real |
| F | 3 | Investigation outcome — vendor untraceable | `VENDOR_RESPONSIBILITY` is refused when no traceable origin exists | Adversarial | Bypass the traceability check — proves it is enforced |
| G | 4 | Write-off happy path | A store-responsibility write-off posts a balanced entry to the correct account/store | Happy-path, accounting reconciliation | Omit the credit leg — proves the balance-trigger catches it |
| H | 4 | Fail-closed routing | An amount with no matching `ApprovalPolicyRule` requires the highest-authority role | Adversarial, authorization | Remove the fail-closed fallback — proves an unconfigured gap would otherwise under-authorize |
| I | 4 | Self-approval structurally blocked | `initiated_by == approved_by` is rejected at the DB layer, not just the API layer | Authorization, adversarial | Attempt the insert directly via a raw session bypassing the service function — proves the DB constraint, not just app code, blocks it |
| J | 4 | Duplicate approval | Two concurrent approval attempts on the same write-off never both succeed | Idempotency, concurrency | Remove `UNIQUE(write_off_id)` — proves the race is otherwise exploitable |
| K | 4 | Write-off reversal | A posted write-off reverses via a new entry, never an edit | Happy-path, historical immutability | Attempt to `UPDATE` the original journal — proves the DB revoke holds |
| L | 4 | Period-closed write-off refusal | A write-off dated into a closed period is refused | Period-close | Bypass `_enforce_period_open` for the new source type — proves the shared gate still covers it |
| M | 5 | Vendor claim approved | Approved claim posts the existing credit-note reduction correctly | Happy-path, accounting reconciliation | Post to the wrong account — proves the reconciliation test catches misattribution |
| N | 5 | Vendor claim denied reopens | A denied claim reopens the discrepancy, never silently closes it | Adversarial | Silently resolve on denial — proves the reopen path is required |
| O | 6 | Return quantity ceiling | A return cannot exceed net-received-and-on-hand | Adversarial, inventory reconciliation | Remove the ceiling check — proves over-return is otherwise possible |
| P | 6 | Return dual authorization | Either Central Management or the original source-store Manager (and no one else) may authorize | Authorization, cross-store isolation | Widen the check to any Manager anywhere — proves the transfer-specific scoping is load-bearing |
| Q | 6 | Return full cycle | Physical return reuses ship/receive verbatim, preserving WAC/In-Transit/GL/audit | Happy-path, accounting + inventory reconciliation | Skip the return's own In-Transit posting — proves the reconciliation report catches an orphaned return |
| R | 7 | Cross-entity ship/receive | Correct Intercompany Receivable/Payable posted on the correct entity's books | Happy-path, accounting reconciliation | Post both legs to the same store — proves the entity-separation check is enforced |
| S | 7 | Entity-pair reconciliation | Receivable and Payable across an entity pair always net to zero | Accounting reconciliation | Break one leg's amount — proves the entity-scoped reconciliation report catches the divergence |
| T | 7 | Settlement idempotency | Duplicate settlement requests never double-post | Idempotency, concurrency | Remove `client_transaction_id` uniqueness — proves double-settlement is otherwise possible |
| U | 8 | Aging alert idempotency | Concurrent dashboard reads never create duplicate alerts for the same line | Idempotency, concurrency | Remove `UNIQUE(transfer_line_id)` — proves duplicate alert creation is otherwise possible |
| V | 8 | Aging alert write-access isolation | The detection code path calls no mutating function | Adversarial (static/code-path audit) | Introduce a call to `TransferWriteOff` creation from the detection path — proves a review/test would catch the violation |
| W | 8 | Threshold freezing | A later product reclassification does not change an in-flight transfer's threshold | Adversarial | Re-derive the threshold live instead of using the frozen value — proves the freeze is load-bearing |
| X | 9 | Period-close blocked | A period cannot close with any unresolved position | Period-close, adversarial | Remove the blocking check — proves an unresolved balance could otherwise silently close |
| Y | 9 | Suspense carry-forward | Suspense posting balances and later resolves correctly (both late-receipt and write-off paths) | Happy-path, accounting reconciliation, period-close | Omit the suspense-aware credit-account check on later resolution — proves the account-selection logic (Section 15.2) is load-bearing |
| Z | 9 | Suspense never bypasses approval | A period-close write-off resolution still requires full `WriteOffApproval` | Adversarial, authorization | Auto-approve under close pressure — proves the "never authorizes an unapproved write-off" invariant (Section 22) holds |
| AA | Cross-phase | Quantity identity | The Section 5 identity holds after every combination of good/damaged/short/return/write-off | Inventory reconciliation, adversarial | Break one term of the identity in a new code path — proves the invariant check is actually exercised, not just asserted in documentation |
| AB | Cross-phase | Full 17-scenario regression | Every scenario in Section 20 reproduces its stated trace exactly | Happy-path, accounting + inventory reconciliation | N/A (integration-level confirmation, not a single mutation target) |

28 sessions, covering every new business rule with the nine required test
categories (happy-path, authorization, cross-store isolation, idempotency,
concurrency, period-close, accounting reconciliation, adversarial/
malformed-input, and a mutation target proving the corresponding control
is actually exercised) plus two cross-phase integration sessions.

---

## 27. Acceptance Criteria

M25 is acceptance-complete only when, for every phase in Section 25:

1. That phase's own testing sessions (Section 26) all pass.
2. The full pre-existing regression suite (every module from M1 through
   M24B) still passes unchanged.
3. Every mutation target in Section 26 produces the stated RED failure,
   confirmed live and reverted byte-identical, per this codebase's
   established mutation-testing discipline.
4. The Section 5 quantity identity and Section 22's invariant list hold
   under every scenario in Section 20.
5. `git diff`/migration review confirms no unrelated file was touched in
   that phase (Section 25's "no phase implements multiple unrelated
   domains" rule).
6. For Phase 10 specifically: a live browser smoke test of every new
   workflow, per this project's standing frontend-verification practice.

---

## 28. Hostile Design Review (Self-Review)

Each listed attack was traced against this design; the finding and, where
a flaw was found, the fix already folded into the sections above (never
left undocumented) are recorded here.

| Attack | Finding |
|---|---|
| Accounting imbalance | Every new posting is a single call into the existing `_post_journal`, which enforces `SUM(debit)=SUM(credit)` via the existing DB trigger — no new code path constructs a `JournalEntry` any other way. No flaw found. |
| Inventory imbalance | The Section 5 identity is the explicit invariant designed to catch this; Session AA specifically targets it. No flaw found, contingent on that check actually being implemented as specified. |
| Duplicate posting | Every new financial action carries a `client_transaction_id` (write-off, settlement) or an equivalent DB-uniqueness guard (discrepancy-per-receipt-item, alert-per-line, approval-per-write-off). No flaw found. |
| Unauthorized approval | Fail-closed routing (Section 10.1) was specifically added **because** the initial design, without it, would have let an unconfigured amount range fall through with no required role — this was found and fixed during this review, not merely assumed safe. |
| Self-approval | DB-level `CHECK` constraint, not just an application check — specifically hardened during this review after considering that a future code change to the service layer could otherwise silently drop an app-level-only check. |
| Cross-store access | Every new table carries `store_id`; every new function reuses `_enforce_store_access`. No flaw found. |
| Cross-entity access | Considered explicitly: could a same-entity read accidentally aggregate a different entity's journal rows? The entity-scoped reconciliation (Section 8) is an **additive** report, never a change to the existing per-store queries — no existing query implicitly assumes single-entity scope in a way that would leak across entities, since `JournalEntry` was already store-scoped, and `AccountingEntity` is a strict grouping on top, never a wider default. No flaw found. |
| Period-close bypass | Considered whether the suspense option could become a de facto bypass of the write-off approval requirement under time pressure — this was the exact risk that produced Section 22's explicit invariant and Session Z's mutation target. Found and fixed during this review. |
| Negative inventory | No new decreasing movement type is introduced; every new flow either creates no movement (write-off, discrepancy) or an ordinary bounded increasing movement (return receipt). No flaw found. |
| WAC corruption | Considered whether a return's new-cost-at-shipment rule (Section 7) could be mistaken for a bug and "fixed" by a future implementer to force-match the original frozen cost, silently reintroducing a circular-cost risk analogous to what M8 Design Decision 9 already guards against for ordinary transfers — flagged explicitly in Section 7's own text as the correct, intended behavior, specifically to prevent that future mistake. |
| Orphaned Inventory In Transit | The suspense mechanism (Section 15) exists specifically so a period close can never leave an orphaned, unexplained In-Transit balance with no forward resolution path — this was the central risk D7 was written to prevent, and Session Y directly targets it. |
| Orphaned discrepancy | A `DENIED` discrepancy still reaches `RESOLVED` (Section 12.1) — considered whether a discrepancy could be left in `INVESTIGATING` forever with no forcing function; unlike the transfer-aging case, no timeout forces discrepancy resolution. **Genuine residual gap, flagged, not concealed:** this design does not force-resolve a stalled `INVESTIGATING` discrepancy the way D6 forces awareness of a stalled transfer. Mitigation: any discrepancy still open at period close is caught by `close_accounting_period`'s new blocking check (Section 15.1, blocking condition 2), which forces a decision at the latest by the next period close — so it cannot persist indefinitely across a close boundary, but could persist within an open period. Recorded as a known, accepted limitation, not silently hidden. |
| Orphaned return | A `RETURN_REQUIRED` discrepancy whose return transfer is created but never shipped: caught by the same period-close blocking check (the original discrepancy remains open until the return's receipt resolves it) — same mitigation as above. |
| Orphaned AP/AR | The entity-pair reconciliation (Section 8) surfaces a non-zero net balance the same way the existing In-Transit reconciliation surfaces a discrepancy — detection exists; no automatic forcing function beyond period-close review of the underlying store's own books (Intercompany Receivable/Payable are ordinary balance-sheet accounts, so the destination/source store's own trial balance already surfaces them to anyone reviewing it, without needing a dedicated block). |
| Vendor-credit mismatch | The credit note amount is always sourced from the discrepancy's frozen `unit_cost`, never independently re-entered — considered whether a future implementer might allow a manually-typed amount at `SUBMITTED_TO_VENDOR`/`APPROVED` time, which would break this guarantee; explicitly noted in Section 9 that the value must come from the discrepancy record, not a free-text field, to prevent this. |
| Duplicate timeout alert | `UNIQUE(transfer_line_id)` + a single conditional `UPDATE ... WHERE fired_at IS NULL` — Session U targets this directly. No flaw found. |
| Concurrent resolution | Every resolution path (receipt, write-off, suspense-clearing) locks the same underlying row before checking state (Section 17/18) — considered the specific case of a write-off and a late receipt racing for the same remaining quantity; both attempt to lock the transfer line row first, so they serialize, and the loser re-validates under its own lock and is rejected if the quantity is no longer available. No flaw found. |
| Retry after partial failure | No service function commits internally; the existing "no `commit()` inside the service layer" discipline is preserved for every new function (Section 18). No flaw found. |

One genuine, disclosed residual gap was found (stalled `INVESTIGATING`
discrepancies within an open period) and its bounded mitigation is
recorded rather than concealed, per instruction: "do not conceal
unresolved technical defects."

---

## 29. Controls (performed before commit)

- `git status --short` / `git diff` — confirmed only
  `docs/M24D_TECHNICAL_CONTRACT.md` is new (Section 30).
- Zero production, migration, seed, test, or frontend files changed.
- All seven decisions (D1–D7) represented in Section 1, with every
  downstream section traceable back to them.
- Every accounting flow in Sections 6, 8, 9, 15, and 20 verified to
  balance (each named posting is an explicit Dr/Cr pair; Section 21/28
  cross-check for imbalance risk).
- Every inventory flow verified against the Section 5 quantity identity.
- M25 has 28 explicit testing sessions (Section 26) plus two cross-phase
  integration sessions, each with the nine required test categories where
  applicable.
