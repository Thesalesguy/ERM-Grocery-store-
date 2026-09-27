# M24B: Remaining Inter-Store Transfer Business-Policy Resolution

**Status:** Policy only. No production code, migration, seeded account, API
behavior, permission, transfer state, inventory movement, GL behavior,
test, or frontend file changed. Starting HEAD: `5ab77cb` (M24A commit).

This phase resolves only the seven areas `docs/M24A_POLICY.md` left open. No
area M24A already resolved is reopened; each is treated as authoritative
and reused without re-derivation.

## 1. Executive policy summary

| # | Policy area | Resolution |
|---|---|---|
| 1 | Loss ownership | **GL-posting attribution: RESOLVED** (mechanically, the source store — the only ledger holding the un-cleared value). **Economic/management responsibility: UNRESOLVED BUSINESS DECISION.** |
| 2 | Write-off authority | **UNRESOLVED BUSINESS DECISION** for role/threshold/dual-approval. **RESOLVED** for reason-mandatory (yes, by direct pattern consistency) and for reversal mechanism-if-ever-permitted (a new compensating entry, never a mutation). |
| 3 | Post-shipment cancellation | Current: **prohibited, unchanged, confirmed.** Mechanism-if-a-physical-return-is-ever-built: **RESOLVED by strong analogy** (a new document, mirroring `create_purchase_return`). Whether to build any post-shipment terminal mechanism at all, and which of the four options: **UNRESOLVED.** |
| 4 | Discrepancy/shortage/damage | Scenarios A (exact), C (over-receipt), and G (duplicate) are **already fully resolved, unchanged.** Scenario F (damage discovered *after* receipt) is **RESOLVED — already fully solved by the existing stock-adjustment mechanism, no new work needed.** Scenarios B/D/E (pre-receipt shortage/damage recording) remain **UNRESOLVED.** |
| 5 | Loss GL account + attribution | **RESOLVED**: a *new, dedicated* EXPENSE account is required (not a reuse of `ACCOUNT_INVENTORY_SHRINKAGE_EXPENSE`), by direct precedent; it necessarily affects P&L, automatically, under M22's existing formula, with zero P&L-formula change ever needed; GL posting is mechanically against the source store; one journal, not a cross-store entry. **UNRESOLVED**: the account's exact code/name (a deferred implementation detail, not a policy blocker) and the *economic* attribution question (tied to Policy 1). |
| 6 | In-transit timeout | **UNRESOLVED BUSINESS DECISION** for whether the concept is needed at all and any threshold. **RESOLVED**: *if* ever built, it must be alert-only, never an automatic state change or automatic accounting entry — by total absence of any autonomous-posting precedent anywhere in this system. |
| 7 | Mandatory reconciliation | **UNRESOLVED BUSINESS DECISION** for whether it becomes mandatory, its frequency, and whether it blocks period close. **RESOLVED/factual**: per-store vs. cross-store scope is unchanged from M24A; the "required report" already exists; running it today creates no audit trail. |

## 2. Evidence reviewed

- `docs/M24_DISCOVERY.md` and `docs/M24A_POLICY.md` in full (this phase's
  direct predecessors — nothing in either is reopened without new,
  explicitly-cited evidence).
- `docs/M8_ADVANCED_INVENTORY_DESIGN.md` Design Decisions 6-11 and
  "Deferred / known limitations," re-read for this phase.
- `docs/M3_PURCHASING_RECEIVING_WAC.md` / `backend/app/modules/purchasing/
  service.py::create_purchase_return` — the direct, existing precedent for
  "goods already received get physically sent back are modeled as a new
  document, never a mutation of the original receipt."
- `backend/app/modules/accounting/constants.py` / the M4, M10, M15
  migrations — the full EXPENSE-account history, specifically confirming
  `ACCOUNT_CASH_OVER_SHORT` (5910, M15) was created as a **new, dedicated**
  account rather than reusing `ACCOUNT_INVENTORY_SHRINKAGE_EXPENSE` (5900,
  M4) for a different discrepancy source, despite both being "an expensed
  discrepancy" in the abstract.
- `docs/M22_DISCOVERY.md` / `docs/M22_TESTING_SESSIONS.md` — the exact,
  already-implemented `profit_and_loss()` formula (every `EXPENSE`
  account except `ACCOUNT_COGS`, dynamically summed).
- `docs/M10_DESIGN.md`'s payroll conflict-of-interest reasoning
  (`PAYROLL_POST`/`PAYROLL_REVERSE` withheld from Manager specifically
  because a manager should not self-approve their own team's payroll) —
  read as a possible analogy for write-off self-authorization, not as
  direct transfer-specific evidence.
- `backend/app/modules/accounting/schemas.py::JournalEntryReverseRequest`
  (`reason` mandatory, `min_length=1`) vs.
  `backend/app/modules/transfers/schemas.py::TransferCancelRequest`
  (`reason` optional) vs. `backend/app/modules/accounting/models.py::
  AccountingPeriod.reason` (mandatory, `NOT NULL`) — the exact pattern
  distinguishing reversible/low-stakes actions (optional reason) from
  irreversible financial corrections (mandatory reason).
- `docs/TECHNICAL_BLUEPRINT.md` Section F (background worker scope:
  "nightly `pg_dump` backups, tax-submission retry queue, low-stock
  notifications, materialized report refresh") — re-checked; confirms no
  automated/scheduled process anywhere in this system's design or
  implementation has ever posted a `JournalEntry` autonomously.
- `backend/app/modules/accounting/service.py::close_accounting_period` —
  re-confirmed to take zero dependency on transfer state (unchanged from
  M24A, not re-derived, cited again only for the cross-policy scenarios).
- `backend/app/api/v1/endpoints/transfers.py::inventory_in_transit_
  reconciliation` / `reports.py`'s equivalent — both pure `GET` reads with
  no `audit_service.log_event` call; running them today leaves no audit
  trail.

## 3. Seven policy decisions

### Policy 1 — Loss ownership

Two questions kept sharply separate, per the brief's own instruction not to
infer the answer from ledger location alone:

**(a) Which store's journal records the loss, mechanically?** This is not
inferred from ledger location as a convenience — it is a **structural
necessity of double-entry bookkeeping**: `Inventory In Transit` is posted
with `JournalEntry.store_id = transfer.from_store_id` at shipment (M8
Design Decision 9, unchanged) and is *never* re-attributed to the
destination until an actual receipt event moves it there. An asset cannot
be credited off a ledger it was never debited onto. Therefore, for any
quantity never receipted, **the only ledger on which a write-off can be
posted is the source store's** — this is a mechanical fact about the
existing GL architecture (Source discipline category 2: derived from
Design Decision 9's posting rule + basic double-entry mechanics), not an
inference from convenience and not itself a business decision about who is
at fault or who should bear the cost.

**(b) Who is economically/financially responsible for the loss?**
**UNRESOLVED BUSINESS DECISION.** This depends on a fact this repository
does not establish and flagged as an open informational gap as far back as
`docs/M23_DISCOVERY.md` Phase 4: whether this system's stores are branches
of one company (in which case "which store's P&L absorbs it" is a pure
management-accounting convention with no real economic stakes) or
financially/legally distinct entities (in which case risk-of-loss-in-transit
is a genuine commercial-terms question, analogous to FOB-shipping-point
vs. FOB-destination terms in real logistics, that no document here
addresses). **Missing stakeholder input required:** confirmation of the
legal/financial relationship between stores, and, separately, whether the
business wants risk-of-loss to follow physical possession (destination
bears it once received, source bears it until then — which is what (a)'s
mechanics already default to) or some other allocation (e.g., always
split, always centrally absorbed, or dependent on carrier/route terms this
repository has no concept of).

- Does ownership transfer at shipment or at receipt? Per (a)'s mechanics,
  the *ledger* location moves only at an actual receipt event — there is no
  partial/gradual transfer; a line is either "on the source's books" (not
  yet receipted) or "on the destination's books" (receipted), atomically,
  per unit. Whether this ledger fact should also govern *economic*
  ownership is part of (b)'s unresolved question.
- Does responsibility differ from accounting ownership? Potentially — see
  (a) vs. (b) above; this is exactly why they are kept separate.
- Can responsibility depend on the reason for loss (e.g., carrier fault
  vs. mishandling)? No evidence anywhere addresses carriers, insurance, or
  fault — **unresolved**, and nothing in this system currently records a
  "reason for loss" taxonomy to hang such a rule on.
- Must both stores be recorded on the loss event? **Resolved: yes, already
  guaranteed by existing structure**, not a new policy — any future loss
  event referencing a `transfer_id`/`transfer_line_id` automatically
  inherits both `from_store_id` and `to_store_id` for free, since the
  `InterStoreTransfer`/`InterStoreTransferLine` rows already carry both
  identities permanently (Source discipline category 3: existing
  implementation behavior).
- How does loss appear in store-level analytics? **Resolved for reporting
  completeness, independent of the GL-attribution question**: both stores'
  identities can and should appear in any drill-down report of the event,
  exactly as `reports/service.py::inventory_in_transit`'s existing
  `InTransitRow` already shows both `from_store_id` and `to_store_id` for
  every outstanding line today, regardless of which store's ledger any
  future write-off is posted against.

### Policy 2 — Write-off authority

- **Required role:** **UNRESOLVED.** No permission tier is reserved for
  this (re-confirmed, no `inventory.transfer.loss`-shaped or equivalent
  constant exists). *Pattern observation, not a decision* (category 2): this
  codebase consistently tiers a module's permissions from read → write →
  a narrower, heavier "commit/correct" action held by fewer roles (e.g.
  `ap.write` vs. `ap.reverse`; `inventory.count.write` vs.
  `.review`/`.post`) — a write-off, being the heaviest, most irreversible
  action in the transfer module, would by this pattern most plausibly sit
  above `inventory.transfer.write`, not alongside it. Which role(s)
  actually hold it is not decided here.
- **Required source/destination store access:** **UNRESOLVED**, tied
  directly to Policy 1(a)/(b) — if the loss is posted against the source
  store's ledger (mechanically necessary per 1(a)), the *acting* caller
  most plausibly needs source-store access at minimum, mirroring
  `ship_transfer`'s own source-only authorization check; but this is an
  inference from an analogous pattern, not a decision, and is marked
  unresolved.
- **Must both stores approve?** **UNRESOLVED**, but a catalogued
  observation, not a decision: no feature anywhere in this entire
  ERP (M2-M24) requires two different stores to jointly approve a single
  action — every dual-party interaction (ship/receive, create/cancel) is
  designed so each party unilaterally controls only their own side. This
  makes a dual-approval requirement here an architectural departure with
  no existing precedent, not something ruled in or out by evidence.
- **Can a manager self-authorize?** **UNRESOLVED.** *Analogy noted, not
  transfer-specific evidence*: `docs/M10_DESIGN.md`'s explicit conflict-
  of-interest reasoning (a Manager is withheld `PAYROLL_POST`/
  `PAYROLL_REVERSE` specifically because approving their own team's payroll
  is a documented self-interest risk) is a structurally similar situation
  — a Manager writing off their own store's inventory loss carries an
  analogous incentive risk (mishandled or missing stock recorded as
  "lost in transit" rather than as their own error) — but M10's reasoning
  was stated specifically for payroll, never extended to transfers by any
  document, so this is offered as a risk worth stakeholder attention, not
  a resolved requirement.
- **Separate accounting permission required?** **UNRESOLVED** — no
  evidence either way.
- **Threshold-based approval (monetary/quantity)?** **UNRESOLVED.** No
  threshold is invented, per the brief's explicit instruction.
- **Reason code mandatory?** **RESOLVED: yes.** Direct pattern consistency
  (category 2): every *irreversible financial correction* this codebase has
  ever built requires a mandatory reason (`AccountingPeriod.reason`
  `NOT NULL`; `JournalEntryReverseRequest.reason`, `min_length=1`), while
  *reversible or lower-stakes* actions leave it optional
  (`TransferCancelRequest.reason`, a DRAFT-only, no-financial-effect
  action). A write-off permanently zeroes a real asset and recognizes a
  real expense — squarely in the mandatory-reason category, not the
  optional one.
- **Supporting documentation mandatory?** **Not currently implementable as
  stated — resolved as a factual/architectural finding**: no file-
  attachment or document-upload subsystem exists anywhere in this ERP for
  any action. Requiring "supporting documentation" would mean building an
  entirely new capability, not merely adding a validation rule. Whether the
  business wants that capability at all is **unresolved**; if it does not,
  the free-text `reason` field (matching 100% of existing precedent) is the
  only currently-supported alternative.
- **Reversible?** **UNRESOLVED whether reversal should be permitted at
  all** (e.g., goods thought lost are later found). **RESOLVED for the
  mechanism if it ever is permitted**: this codebase never mutates or
  deletes a posted financial entry (M4's core immutability rule, enforced
  at the DB privilege level); any reversal would have to be a **new,
  separate compensating entry**, exactly like every other reversal in this
  system, never an undo of the original write-off row.

### Policy 3 — Post-shipment cancellation

**Current, unchanged policy: prohibited.** `_CANCELLABLE_TRANSFER_STATUSES
= ("DRAFT",)` remains exactly as M8 built it and M24A confirmed — this is
not reopened. The **permitted terminal outcomes today** are exactly two:
fully received, or permanently `SHIPPED` (the M21/M24 finding).

**Whether any additional terminal mechanism should ever be added for a
`SHIPPED` transfer, and which of the brief's four options it should be, is
UNRESOLVED.** One piece of evidence strengthens (without resolving) the
*mechanism*, should physical return ever be chosen:

- **If physical return is intended:** `backend/app/modules/purchasing/
  service.py::create_purchase_return` is a **direct, existing, in-this-
  exact-codebase precedent** for "goods already received are physically
  sent back" — modeled as an entirely **new document** (`PurchaseReturn`)
  that references but never mutates the original `GoodsReceipt`. This is
  materially stronger evidence than M24A had (which reasoned only from M8's
  own "a transfer needing a second physical movement is a new transfer"
  principle, stated in the abstract) — it is now doubly confirmed by an
  actual, shipped, tested sibling-module implementation of the identical
  shape. **Resolved, if this option is chosen:** the transfer does **not**
  remain the same transfer; a genuinely new transfer (or return-shaped
  document) is created in the reverse direction; its own ship/receive legs
  clear `Inventory In Transit` and restore source inventory through the
  **ordinary, unchanged** transfer mechanics (no new inventory-movement
  type needed); cost basis is the same frozen `unit_cost_at_shipment`
  the original transfer already captured (the only value with any
  evidentiary basis, per M24 discovery Section 7). **A separate
  transfer/return document is required** — never a mutation of the
  original.
- **If administrative loss declaration is intended:** this is Policy
  4/1/2's territory — not decided differently here, and not merged with
  cancellation as its own concept per the brief's explicit instruction.
- **If cancellation remains prohibited (the status quo) forever:** the
  documented alternative terminal mechanisms are exactly the two already
  named above (fully received, or permanently `SHIPPED` pending whatever
  Policy 1/2/4 eventually resolve).

**Missing stakeholder input required:** whether this business's actual
inter-store logistics ever involves a shipment being recalled/returned
before arrival (making option A a real operational need) versus the
practical reality always being "the goods are gone, account for it"
(making option A pure speculative complexity the brief itself warns
against building).

### Policy 4 — Discrepancy / shortage / damage

| Scenario | Resolution | Basis |
|---|---|---|
| A. shipped = received | **Resolved, unchanged** — fully supported, working today | Existing implementation |
| B. shipped > received | **Resolved as fact**: partial receipt, remainder stays indefinitely outstanding, no distinction between "still coming" and "permanently short." **Whether the remainder should ever be forced to a terminal loss state is UNRESOLVED**, tied to Policies 1/2/4-D/E. | Existing implementation + M24 discovery |
| C. shipped < received | **Resolved, unchanged, already prohibited entirely** — `OVER_RECEIPT`, hard block, no tolerance, no override permission anywhere (M24A, re-confirmed, not reopened) | Existing implementation |
| D. physically damaged goods (discovered *at* receipt, before recording) | **UNRESOLVED** — no distinct "receive but flag as damaged" concept exists; the receiver can only either receipt a quantity at face value or not receipt it (falling into scenario B's shape) | No repository evidence |
| E. missing goods (discovered at receipt) | **UNRESOLVED**, identical shape to D and B — no distinct shortage-declaration event exists | No repository evidence |
| F. goods discovered damaged **after** receipt | **RESOLVED — already fully solved, no new mechanism needed.** Once `receive_transfer` has posted `TRANSFER_IN` and rolled the destination's WAC, the goods are ordinary on-hand stock at the destination store. Damage discovered from that point forward is indistinguishable from — and already fully handled by — the existing, unrelated `create_stock_adjustment` / `STOCK_ADJUSTMENT_OUT` / `ACCOUNT_INVENTORY_SHRINKAGE_EXPENSE` workflow every other post-receipt stock discrepancy in this ERP already uses. **This is not a transfer-specific gap at all.** | Direct, existing implementation (`inventory/service.py::create_stock_adjustment`) |
| G. duplicate receipt attempt | **Resolved, unchanged** — `client_transaction_id` idempotency, exactly-once (M24 discovery, not reopened) | Existing implementation |

This materially narrows the real scope of "Policy 4": only pre-receipt
discrepancies (D and E, and the still-outstanding-remainder question in B)
are genuine, currently-unaddressed gaps. Post-receipt damage (F) requires
zero new transfer-specific work.

For D/E specifically, per-scenario answers where derivable:

- Is receipt allowed at all when damage/shortage is discovered? **Today,
  yes for whatever quantity the receiver chooses to receipt** — nothing
  prevents receipting fewer units than shipped and simply never receipting
  the rest (this is literally scenario B, already resolved as a fact).
  What is missing is a way to **record why** the rest will never come,
  which is exactly D/E's open question.
- Is partial receipt final or provisional? **Provisional, today, by
  default** — nothing marks a partially-received line as "this is as
  final as it will ever get" versus "more is still coming." This
  provisional-forever default is itself the root of the whole M21/M24
  finding.
- Does missing/damaged quantity become a loss automatically? **No,
  never, today** — and **whether it ever should** is exactly Policy 1/2/4's
  unresolved core.
- Who authorizes the adjustment, inventory effect, GL effect, cost basis,
  accounting date, audit requirements for D/E: all **UNRESOLVED**, deferred
  to whichever mechanism (if any) Policy 2/4 eventually specify — cost
  basis, if ever needed, has only one evidence-backed candidate (the same
  frozen `unit_cost_at_shipment`, per M24 discovery Section 7, unchanged),
  and accounting date, if ever needed, is already resolved as
  `date.today()` (M24A Section 3.9, reused here without re-derivation).

### Policy 5 — Loss GL account + store attribution

- **Does the loss hit P&L?** **Resolved: yes, necessarily**, the moment any
  `EXPENSE`-type account is debited for it — this is a structural fact
  about how `account_type` classification works in this system's chart of
  accounts, not a special case invented for transfers.
- **Exact conceptual expense category:** an operating expense representing
  inventory value permanently lost — the same *category* of expense
  `ACCOUNT_INVENTORY_SHRINKAGE_EXPENSE` and `ACCOUNT_CASH_OVER_SHORT`
  already represent for their own discrepancy sources.
- **Can an existing expense account be used? Is a dedicated account
  required?** **Resolved: a new, dedicated account is required, not a
  reuse of an existing one.** Direct, on-point precedent: when M15 needed
  to expense a *different* discrepancy source (cash-drawer variance) that
  was economically similar in kind to the M4-era inventory-shrinkage
  discrepancy, this codebase created a **new** account
  (`ACCOUNT_CASH_OVER_SHORT`, 5910) rather than reusing
  `ACCOUNT_INVENTORY_SHRINKAGE_EXPENSE` (5900) — establishing the pattern
  "each distinct discrepancy *source* gets its own account, even when the
  accounting *treatment* (an expense) is identical." A transfer loss is a
  distinct source (in-transit logistics failure) from both stock-count
  shrinkage and cash-drawer variance, so by this established, repeated
  pattern, it warrants its own account too. **No account is created or
  seeded in this phase** — this is documented as a firm M25
  implementation requirement (a new EXPENSE-type account), with the exact
  code/name left as an M25 mechanical detail, not a business-policy
  blocker.
- **Reconciliation with M22's P&L policy — the critical check the brief
  requires:** M22's `profit_and_loss()` computes `operating_expenses` as
  the sum of **every** `EXPENSE`-type account except `ACCOUNT_COGS`,
  dynamically, from the trial balance (`docs/M22_DISCOVERY.md` Phase 4,
  `docs/M22_TESTING_SESSIONS.md`, unchanged). **This means the moment a
  correctly-typed new transfer-loss EXPENSE account exists and is posted
  to, it is automatically included in `operating_expenses` — no P&L
  formula change, no special category, no code change to
  `profit_and_loss()` at all is ever required.** This is exactly what the
  brief demands be checked, and it resolves cleanly: **no special P&L
  category is created outside M22's existing rule; none is needed.**
- **Is Inventory In Transit credited?** **Resolved: yes, necessarily** — it
  is the only account currently holding the un-cleared debit; there is no
  other way to zero it.
- **Source or destination attribution (GL posting)?** **Resolved: source**,
  per Policy 1(a)'s mechanical necessity — restated here, not re-derived.
- **Do both stores receive reporting attribution?** **Resolved: yes**, per
  Policy 1's reporting-completeness conclusion — restated, not re-derived.
- **One journal, or cross-store accounting?** **Resolved: one, store-
  scoped `JournalEntry`**, exactly matching the universal one-entry-per-
  event pattern this entire codebase uses without exception (including the
  transfer module's own existing shipment and receipt legs, each its own
  separate entry, never combined into one cross-store posting).

### Policy 6 — In-transit timeout

- **Does the concept need to exist at all?** **UNRESOLVED.** No document,
  field, or configuration anywhere in this repository addresses typical
  transit duration between this business's stores, so there is no basis —
  direct or derived — to conclude either that a timeout is needed or that
  it is not.
- **Threshold (duration/basis):** **UNRESOLVED**, no duration invented, per
  the brief's explicit instruction.
- **If ever built, does it automatically change state, or only alert?**
  **Resolved: alert-only, never an automatic state change or automatic
  accounting entry.** Derived (category 2) from a total, verified absence
  across this entire system: `docs/TECHNICAL_BLUEPRINT.md`'s own
  background-worker scope (nightly backups, tax-submission retries,
  low-stock notifications, materialized-report refresh) names zero
  autonomous accounting or state-mutating job, and no milestone from M0
  through M24 has ever built one — every single `JournalEntry` and every
  transfer-status change in this system's entire history has been the
  direct, synchronous result of an authenticated human action within a
  request. Introducing the *first-ever* autonomous financial/state-mutating
  process for a timeout would be a significant architectural departure
  with zero supporting precedent.
- **Who receives the alert?** **UNRESOLVED** — no notification-recipient
  concept for an inventory/financial exception exists to extend.
- **Can timeout trigger mandatory investigation?** **UNRESOLVED** — "mandatory"
  implies an enforceable process this system has no mechanism to enforce
  today (no workflow/task-assignment concept exists anywhere in this ERP).
- **Can timeout trigger loss/write-off?** **No, not automatically**,
  consistent with the alert-only conclusion above; a *human*, authorized
  per Policy 2 (once resolved), would still have to act. Whether the
  alert should make that human action easier/pre-populated is an
  implementation nicety, not a policy blocker.
- **May a transfer remain SHIPPED indefinitely?** **Resolved: yes, as a
  fact** — this is the current, real, unbounded behavior today (M24
  discovery, restated), and remains true regardless of whether an alert
  mechanism is ever added (an alert does not itself resolve the transfer).

### Policy 7 — Mandatory reconciliation

- **Is reconciliation mandatory?** **UNRESOLVED.**
- **Frequency, responsible role, acceptable unresolved balance,
  escalation, aging breakdown:** **All UNRESOLVED** — no repository
  evidence establishes any of these, and none is invented.
- **Required report:** **Resolved/factual — already exists.** Whether or
  not running it is ever made *mandatory*, the report itself
  (`inventory_in_transit_reconciliation` + `inventory_in_transit`'s
  per-line drill-down) is already built, tested, and reachable — "what
  report would satisfy this control" is not an open question even though
  "must someone run it on a schedule" is.
- **Per-store or cross-store?** **Resolved, unchanged from M24A** — the
  aggregate reconciliation is company-wide only, by M8's own explicit
  design reasoning (the account represents value *between* stores); the
  per-line drill-down already supports store filtering for anyone who
  wants a narrower view. Not reopened.
- **Does reconciliation create audit evidence?** **Resolved/factual: no,
  not today.** Both reconciliation endpoints are pure `GET` reads with no
  `audit_service.log_event` call — running the report leaves no record
  that it was run, by whom, or when. Whether a formal control needs one
  (e.g., "reconciliation performed" log entries) is **unresolved**, but
  the *current absence* is a verified fact, not a guess.
- **Does an unresolved balance block period close?** **UNRESOLVED — the
  brief explicitly warns against defaulting to yes, and this document does
  not.** One architectural consequence is noted, not decided: adding such
  a block would be a genuinely new kind of precondition —
  `close_accounting_period` (M22) currently has zero dependency on any
  other module's state (only a valid date range and no overlap with an
  existing closed period for the *same* store); making it query the
  transfers module would be a new cross-module coupling this system has
  never had, a larger change than a single validation line might suggest.
  This is offered as scope information for whoever eventually decides the
  question, not as an argument against building it.
- **Informational or a financial control?** **UNRESOLVED** — this is the
  crux of the whole policy area and requires direct stakeholder input:
  does the business want Inventory In Transit reconciliation to be a
  routine management report (as it functions today) or a formal,
  enforced control gating something else (period close, financial
  sign-off, etc.)?

**Missing stakeholder input required (Policy 7 as a whole):** an
operational decision about how seriously this business wants to treat a
non-trivial or aging in-transit balance — nothing in the repository's
history addresses this, because the reconciliation report itself is new
enough (M8) that no operational cadence around it has ever been
documented or exercised.

## 4. Cross-policy consistency scenarios

Traced against everything resolved above; every "must never" condition
from the brief is checked explicitly at the end of each scenario.

**Scenario 1: DRAFT → SHIPPED → RECEIVED**
- Transfer state: DRAFT → SHIPPED (persisted) → fully RECEIVED (derived).
- Physical inventory: source decremented at ship; destination incremented
  at receipt.
- Inventory In Transit: debited at ship, fully credited to zero at receipt.
- GL effect: two balanced entries, no P&L impact (pure reclassification).
- P&L effect: none.
- Store attribution: both stores explicit throughout.
- Authorization: source ships, destination receives (M24A, unchanged).
- Audit: `TRANSFER_SHIPPED`, `TRANSFER_RECEIPT_COMPLETED`.
- Accounting date: shipment's own date; receipt's own caller-supplied date.
- Period-close interaction: each leg must be posted into an open period for
  its own store at its own time; unaffected by any later close.
- **Checks:** no overstated In-Transit (fully cleared); no unexplained
  disappearance; no double recognition; no misattributed loss (none
  occurred); no closed-period bypass; no historical-date correction (none
  needed); nothing terminal receives inventory improperly; no loss posted
  at all, let alone twice. **All hold.**

**Scenario 2: DRAFT → CANCELLED**
- Transfer state: DRAFT → CANCELLED (persisted, terminal).
- Physical inventory: untouched (nothing ever moved).
- Inventory In Transit: never touched.
- GL/P&L effect: none.
- Store attribution: both stores could have cancelled; audit records
  whichever did.
- Authorization: either side (M24A, unchanged).
- Audit: `TRANSFER_CANCELLED`.
- Accounting date: N/A, no posting.
- Period-close interaction: N/A.
- **Checks:** trivially satisfied — nothing financial ever occurred.

**Scenario 3: SHIPPED → physically returned → source restored (Policy 3,
IF this option is ever chosen and built)**
- Transfer state: original transfer remains permanently `SHIPPED`
  (unchanged, per 3.3's resolution that it is never mutated); a **new**
  return-transfer is created, itself following the ordinary DRAFT → SHIPPED
  → RECEIVED path in the reverse direction.
- Physical inventory: the *original* destination (now acting as the
  return's source) decrements nothing new (it never received the goods in
  this scenario) — this scenario only makes sense if construed as goods
  that were shipped but recalled *before* ever being formally received;
  the return-transfer's own shipment/receipt legs move inventory exactly
  as any ordinary transfer would, restoring the original source's on-hand
  quantity upon the return's own receipt.
- Inventory In Transit: the **original** transfer's In-Transit debit is
  cleared only once the **return**-transfer's own receipt posts (an
  ordinary receipt leg, crediting In-Transit and debiting Inventory back
  at the original source) — mechanically indistinguishable from an
  ordinary receipt, just with source and destination swapped.
- GL/P&L effect: none beyond the ordinary, no-P&L-impact transfer
  reclassification — no loss is recognized, since the goods physically
  came back.
- Store attribution: both original stores plus the return-transfer's own
  (identical) pair.
- Authorization: **UNRESOLVED** (Policy 3) — whoever is deemed authorized
  to initiate the return.
- Audit: the return-transfer's own `TRANSFER_CREATED`/`SHIPPED`/
  `RECEIPT_COMPLETED` events, plus (if built) a marker on the original
  noting a return was initiated against it.
- Accounting date: the return-transfer's own legs use ordinary
  forward-dating rules (not `date.today()`'s corrective-entry rule,
  since each leg is a genuine new economic event, per M24A Section 3.9's
  reasoning extended here).
- Period-close interaction: ordinary — each leg must fall in an open
  period for its own store at its own time.
- **Checks:** In-Transit is not overstated once the return completes (it
  is fully cleared, twice over, correctly); inventory is never created
  without a matching movement (the return's receipt is a real,
  ordinary receipt); no double-recognition (the original transfer's
  in-transit debit and the return's eventual credit net to exactly the
  original shipped value); no loss is posted (none occurred); nothing
  terminal (the original SHIPPED transfer) receives inventory itself —
  the *return*-transfer does, correctly, as its own document. **All hold,
  contingent on physical return ever being the chosen policy.**

**Scenario 4: SHIPPED → permanently lost → write-off (Policies 1/2/5, IF
ever built)**
- Transfer state: **UNRESOLVED** whether a new persisted state
  (`LOST`/`WRITTEN_OFF`) is used or the transfer remains `SHIPPED` with a
  separate write-off record pointing at it (M24A Section 3.12, not
  reopened — still contingent).
- Physical inventory: untouched at both stores (source already correctly
  decremented at ship; destination never incremented) — **no
  `InventoryMovement` is created**, per M24 discovery Section 4's finding
  that there is nothing to move.
- Inventory In Transit: credited to zero for the written-off quantity.
- GL effect: one journal, `Dr [new dedicated transfer-loss expense
  account] / Cr Inventory In Transit`, posted **against the source store**
  (Policy 1(a)/5, resolved).
- P&L effect: yes, automatically, via M22's existing dynamic
  `operating_expenses` formula (Policy 5, resolved) — no special handling.
- Store attribution: GL posting on the source; both stores' identities
  preserved on the record for reporting (Policy 1).
- Authorization: **UNRESOLVED** (Policy 2).
- Audit: a mandatory `reason` (Policy 2, resolved), plus the standard
  `audit_service.log_event` pattern every financial correction uses.
- Accounting date: `date.today()` (Policy 5/M24A Section 3.9, resolved),
  and today's period for the source store must be open.
- Period-close interaction: if today's period for the source store is
  closed, the write-off is refused exactly like every other correction in
  this system (the same sharp edge every existing compensating entry
  already accepts) — never posted to the original, possibly long-closed,
  shipment period.
- **Checks:** In-Transit is not overstated afterward (credited to exactly
  zero for the written-off quantity — the brief's first prohibited
  outcome is directly prevented); inventory does not disappear without an
  accounting explanation (the expense entry *is* the explanation); no
  inventory is created without a matching movement (none is created at
  all, correctly); no double recognition (a single journal, gated by the
  same idempotency/uniqueness discipline every other `post_*_journal`
  function uses); loss is attributed to the source store only, which is
  mechanically the *only* store whose books could ever hold it — never an
  "unauthorized" store, since no other store's ledger has anything to
  credit; no closed period is bypassed (refused, not silently posted); no
  historical-date correction (always today); a terminal write-off must not
  later receive inventory — **this requires an explicit guard once built**
  (a mutation target, Section 12); a loss must not be postable twice —
  **this requires the same idempotency discipline every other financial
  event in this system already has**, restated as an explicit M25
  requirement, not assumed for free. **All checks are satisfiable by the
  resolved mechanics above; the two flagged as "requires an explicit
  guard" are exactly why Section 12's mutation targets name them
  specifically.**

**Scenario 5: SHIPPED → partially received → remaining quantity
unresolved**
- Transfer state: remains `SHIPPED` (derived: partially received).
- Physical inventory: correctly reflects exactly what has actually moved
  so far at both stores.
- Inventory In Transit: correctly non-zero for the remaining quantity —
  this is the **existing, correct, working behavior**, not a defect by
  itself (M8 Design Decision 9's own stated design).
- GL/P&L effect: none beyond the receipts already posted.
- Store attribution: both stores, throughout.
- Authorization: unchanged, ordinary receive.
- Audit: each receipt event's own `TRANSFER_RECEIPT_COMPLETED`.
- Accounting date: each receipt's own caller-supplied date.
- Period-close interaction: none special — this is not itself a
  correction.
- **Checks:** this scenario, left exactly as-is, satisfies every
  prohibited-outcome check trivially, because nothing about it is wrong —
  it is the honest, correct representation of "not fully arrived yet." It
  only becomes a problem if it *never* resolves, which is Scenario 8's
  concern, not this one's.

**Scenario 6: SHIPPED → partial receipt → remaining quantity declared
lost (Policies 1/2/4/5 combined, IF ever built)**
- Identical to Scenario 4, except the write-off amount is
  `(shipped_quantity − received_quantity) × unit_cost_at_shipment` for the
  affected line(s) rather than the full shipped value — the same frozen
  cost basis, the same GL mechanics, the same source-store attribution,
  the same `date.today()` rule.
- **Checks:** identical to Scenario 4's, with the added requirement (an
  explicit M25 invariant, Section 11) that the write-off amount can never
  exceed the *actually still-outstanding* quantity at the moment it is
  declared — otherwise a later receipt attempt against an already-
  written-off remainder would either double-recognize inventory or exceed
  what was ever shipped. This is exactly why "permit receipt after loss"
  is named as a required guard (Section 12).

**Scenario 7: SHIPPED → damaged before receipt → write-off (Policies 2/4/5
combined, IF ever built)**
- Identical in every accounting respect to Scenario 4/6 — "damaged before
  receipt" and "lost before receipt" have the **same GL/inventory shape**
  under everything resolved in this document (no inventory movement,
  source-store-attributed expense, `date.today()`, credited In-Transit).
  The only difference is the **reason code** recorded (Policy 2's mandatory
  `reason`), not the accounting mechanics. This is a direct, useful
  simplification this policy phase surfaces: **"lost" and "damaged," for
  accounting purposes, are the same event with a different label** —
  Policy 4's still-unresolved D/E questions are really about **whether
  and how this reason is captured and whether a different disposition
  (e.g., partial salvage value) ever applies**, not about a different GL
  treatment. No different GL treatment is invented here for damage vs.
  loss; if the business wants one (e.g., salvage value reduces the
  write-off amount), that is a distinct, unresolved question this
  document does not answer.

**Scenario 8: SHIPPED → no receipt for an extended period →
timeout/reconciliation (Policies 6/7, mostly unresolved)**
- Transfer state: remains `SHIPPED` (derived: 0% or partially received)
  indefinitely — **resolved as the current, real, and (absent Policy 6/7
  resolution) permanent behavior.**
- Physical inventory: correct, as in Scenario 5.
- Inventory In Transit: correctly non-zero, indefinitely — this is
  precisely the HIGH-severity finding M21/M24 already identified, now
  traced through to its logical endpoint: **nothing in this document
  changes this outcome**, because Policies 6 and 7 (the only mechanisms
  that could ever surface or force a resolution) remain unresolved.
- GL/P&L effect: none, ever, unless Policy 6/7 are resolved and a
  human eventually acts under Policy 2's (still unresolved) authority to
  declare a loss.
- Store attribution: both stores, throughout, visible via the existing
  drill-down report at any time (M24 discovery Section 6/13, unchanged).
- Authorization: N/A — nothing happens automatically.
- Audit: none generated merely by the passage of time (Policy 6 resolved:
  no automatic accounting entry).
- Accounting date: N/A until a human acts.
- Period-close interaction: **resolved/factual** — every period for both
  stores can continue to close, indefinitely, with this balance
  outstanding, exactly as today (Policy 7, not itself changed by aging).
- **Checks:** In-Transit remains genuinely, honestly non-zero (not
  "overstated after a terminal loss," because no terminal loss has
  occurred — this is the correct distinction the whole discovery chain has
  been careful to preserve); nothing disappears without explanation
  (nothing has disappeared — it may simply still be exactly where the last
  known state says it is, or may not be, and the system cannot tell the
  difference, which is the entire point of Findings F3/HIGH); no closed
  period is bypassed. **This scenario's "failure mode," if the business
  considers indefinite unresolved balances unacceptable, is not an
  accounting-integrity bug — it is the direct, expected consequence of
  Policies 6 and 7 remaining open.** Resolving this outcome requires
  resolving those two policies, not a different accounting mechanism.

## 5. Complete policy matrix

| Policy | Decision | Evidence | Current behavior | M25 requirement | Inventory consequence | GL consequence | P&L consequence | Authorization |
|---|---|---|---|---|---|---|---|---|
| Loss ownership (ledger location) | RESOLVED — source store | Design Decision 9 + double-entry mechanics | In-Transit always posted against `from_store_id` | Any future loss journal must post against the source store | None (no movement) | Dr loss expense / Cr In-Transit, source store | Yes, via M22's dynamic formula | N/A |
| Loss ownership (economic) | UNRESOLVED BUSINESS DECISION | None | N/A | Blocked until resolved | N/A | N/A | N/A | N/A |
| Write-off authority (role/threshold) | UNRESOLVED BUSINESS DECISION | None (pattern observation only) | Does not exist | Blocked until resolved | N/A | N/A | N/A | Blocked |
| Write-off reason requirement | RESOLVED — mandatory | Reversal/period-close reason-mandatory pattern | N/A (feature doesn't exist) | Reason field, `NOT NULL`, if built | N/A | N/A | N/A | N/A |
| Write-off reversibility mechanism | RESOLVED — new compensating entry, never a mutation; whether permitted at all is unresolved | M4 immutability rule | N/A | If ever permitted, a new entry, not an undo | N/A | New offsetting entry | Yes | Same as write-off authority (unresolved) |
| Post-shipment cancellation (current) | RESOLVED — prohibited, unchanged | M8 Deferred section, M24A | `INVALID_TRANSFER_STATE` | None | None | None | None | N/A |
| Post-shipment cancellation (future mechanism, if physical return) | RESOLVED by analogy, IF chosen | `create_purchase_return` precedent | Does not exist | New reverse-direction transfer/document | Ordinary TRANSFER_OUT/IN pair on the new document | Ordinary transfer legs, no loss | None (no loss) | Same as ordinary transfer creation/ship/receive |
| Post-shipment cancellation (whether to build, which option) | UNRESOLVED BUSINESS DECISION | None | N/A | Blocked until resolved | N/A | N/A | N/A | N/A |
| Over-receipt | RESOLVED, unchanged — prohibited | M24A | `OVER_RECEIPT` | None | None | None | None | None |
| Damage discovered after receipt | RESOLVED — already solved by existing stock adjustment | `create_stock_adjustment` | Fully supported today | None (no transfer-specific work) | STOCK_ADJUSTMENT_OUT | Existing shrinkage-expense mechanics | Yes, already included in P&L today | Existing `inventory.adjust` |
| Damage/shortage discovered at receipt (before recording) | UNRESOLVED BUSINESS DECISION | None | Falls into ordinary partial-receipt shape | Blocked until resolved | N/A | N/A | N/A | N/A |
| Duplicate receipt | RESOLVED, unchanged | M24 discovery | Idempotent | None | None | None | None | None |
| Loss GL account (which account) | RESOLVED — new dedicated account required | `ACCOUNT_CASH_OVER_SHORT` precedent | Does not exist | Create one new EXPENSE account in M25 (not seeded now) | N/A | New account debited | Automatic, via M22's existing formula | N/A |
| Loss GL account (store attribution) | RESOLVED — source, mechanically; economic attribution unresolved | Policy 1 | N/A | Post against source | N/A | Source store | N/A | N/A |
| Timeout (concept) | UNRESOLVED BUSINESS DECISION | None | Does not exist; transfers may stay SHIPPED forever | Blocked until resolved | N/A | N/A | N/A | N/A |
| Timeout (mechanism, if built) | RESOLVED — alert-only, never automatic | Total absence of autonomous posting anywhere | N/A | Alert only; never auto-state-change or auto-posting | None | None | None | N/A |
| Mandatory reconciliation | UNRESOLVED BUSINESS DECISION | None | On-demand only | Blocked until resolved | N/A | N/A | N/A | N/A |
| Reconciliation blocking period close | UNRESOLVED BUSINESS DECISION | None; brief explicitly warns against defaulting to yes | Never blocks today | Blocked until resolved | N/A | N/A | N/A | N/A |
| Reconciliation scope (per-store/cross-store) | RESOLVED, unchanged | M8/M24A | Company-wide aggregate; per-line drill-down supports store filter | None | N/A | N/A | N/A | N/A |
| Reconciliation audit trail | RESOLVED/factual — none exists today | Endpoint code, no `log_event` call | No audit record created by running the report | Unresolved whether one should be added | N/A | N/A | N/A | N/A |
| Store/entity accounting scope | RESOLVED, unchanged | M23A/M24A | Per-store, explicit, no central entity | None | None | None | None | None |
| Transfer terminal states | Contingent on the above | M24A Section 3.12 | `DRAFT`/`SHIPPED`/`CANCELLED` only | Blocked until Policies 1-4 resolve | Contingent | Contingent | Contingent | Contingent |

No cell in this matrix uses "probably," "recommended," or "best practice"
as a stand-in for a decision — every UNRESOLVED cell states plainly that it
is unresolved, and every RESOLVED cell names its evidence.

## 6. Accounting treatment

Summarized from Policies 1/5 and the cross-policy scenarios: any future
transfer-loss/write-off entry is `Dr [new dedicated EXPENSE account,
M25-created, not named/seeded here] / Cr Inventory In Transit`, one
balanced `JournalEntry`, posted against the **source** store, dated
`date.today()`, subject to the same unconditional `_enforce_period_open`
gate every other posting already respects, automatically included in
`profit_and_loss()`'s `operating_expenses` under M22's existing dynamic
formula with no formula change required. No account is created and no
formula is touched in this phase.

## 7. Inventory treatment

No new `InventoryMovement` type is required for a loss/write-off (nothing
physical to move, per M24 discovery Section 4, reconfirmed). Damage
discovered *after* receipt requires zero new transfer-specific mechanism
(Policy 4, Scenario F) — the existing stock-adjustment path already
handles it completely. Pre-receipt damage/shortage recording (Policy 4
D/E) and any physical-return mechanism (Policy 3, if chosen) remain
open, with the return mechanism's *shape*, if built, resolved to be an
ordinary new transfer using unchanged, existing inventory-movement types.

## 8. Authorization model

No new permission is created in this phase. If write-off authority is
ever resolved, the established pattern this codebase would most plausibly
extend is a new, narrower permission above `inventory.transfer.write`
(pattern-observed, not decided) — the specific role(s) and any dual-
approval or self-authorization restriction remain fully open pending
direct stakeholder input, including the noted-but-unresolved payroll-style
conflict-of-interest question.

## 9. Period-close treatment

Fully resolved for what the existing architecture already guarantees:
any future correction posts today, into an open period, and never
touches a historical closed period (Policy 5/Scenario 4/6). Whether an
unresolved Inventory In Transit balance should ever become a *precondition*
for closing a period is explicitly left open (Policy 7), with the
scope-and-coupling consequence of building such a check noted for whoever
eventually decides it.

## 10. M25 implementation boundary

| Change | Mandatory / Optional / Future extension / Out of scope |
|---|---|
| A. New `LOST`/`WRITTEN_OFF`-shaped state or record | **Out of scope** until Policies 1(b)/2/3/4 are resolved |
| B. New inventory movement type for loss | **Out of scope, permanently** — none is ever needed (resolved, Section 7) |
| B. New inventory movement type for a physical return (if Policy 3 chooses option A) | **Future extension**, and even then reuses existing `TRANSFER_OUT`/`TRANSFER_IN` types on a new document — no new type needed |
| C. New dedicated transfer-loss EXPENSE account | **Mandatory, once Policy 1(b)/2/4 are resolved and the feature is built** — not created now |
| C. Any P&L formula change | **Out of scope, permanently** — resolved as unnecessary (Section 6) |
| D. New write-off/loss permission | **Out of scope** until Policy 2 is resolved |
| E. New loss/write-off/cancellation endpoint | **Out of scope** until Policies 2/3/4 are resolved |
| F. Frontend surfacing of any new mechanism | **Out of scope** until the underlying mechanism is resolved and built |
| G. Aging/"days outstanding" column on the existing drill-down report | **Optional future enhancement** — requires no policy resolution, pure reporting addition, not proposed for implementation now |
| G. Formal reconciliation-frequency reporting/dashboard | **Out of scope** until Policy 7 is resolved |
| H. Timeout alert / scheduled job | **Out of scope** until Policy 6 is resolved; if it is, resolved to be alert-only (Section 3, Policy 6) |
| H. Period-close blocking on unresolved in-transit balances | **Out of scope** until Policy 7 is resolved |
| I. Migrations for any of the above | **Out of scope** — none is created in this phase, and none should be until the corresponding feature is authorized |
| J. Seed/account changes | **Out of scope** — none created or seeded in this phase |
| K. Audit-trail additions (e.g., "reconciliation performed" events) | **Out of scope** until Policy 7 resolves whether a formal audit trail is wanted |

## 11. M25 testing sessions

Sessions A-D/G/K reuse M24/M24A's already-resolved, already-passing
coverage (restated for completeness, not re-designed); E/F/H/I/J/M/N/O/T/U
are genuinely new, all explicitly blocked pending their policy resolution
except where noted otherwise.

| # | Session | Objective | Invariant | Setup | Operation | Expected result | Level | Failure mode |
|---|---|---|---|---|---|---|---|---|
| A | Normal shipment/receipt | Unchanged from M24 discovery B/C | Cost frozen, both legs balance | DRAFT transfer | ship then receive | Passes, unchanged | Service | Regression |
| B | Cancellation before shipment | Unchanged from M24 discovery D | No inventory/GL effect | DRAFT transfer | `cancel_transfer` | Passes, unchanged | Service | Regression |
| C | Physical return (Policy 3, option A) | **BLOCKED** — do not implement until Policy 3 chooses this option | N/A | N/A | N/A | N/A | N/A | Implementing a guessed policy |
| D | Loss/write-off | **BLOCKED** — do not implement until Policies 1(b)/2/4/5's account are resolved | N/A | N/A | N/A | N/A | N/A | Implementing a guessed policy |
| E | Partial receipt | Unchanged from M24 discovery G | Running totals correct | SHIPPED transfer, 2+ receipts | sequential receives | Passes, unchanged | Service | Regression |
| F | Damage (at receipt, pre-recording) | **BLOCKED** — Policy 4 D unresolved | N/A | N/A | N/A | N/A | N/A | Implementing a guessed policy |
| F2 | Damage (after receipt) | Prove the existing stock-adjustment path already covers this, with no transfer-specific code | Existing shrinkage mechanics apply unchanged | Fully-received transfer line, destination stock | `create_stock_adjustment` at the destination, unrelated to the transfer | Correctly expensed via the existing mechanism; transfer itself untouched | Service | A future implementer wrongly builds a duplicate transfer-specific damage path |
| G | Shortage (pre-receipt, undeclared) | **BLOCKED** — Policy 4 E unresolved | N/A | N/A | N/A | N/A | N/A | Implementing a guessed policy |
| H | Duplicate receipt | Unchanged from M24 discovery I | Idempotent | SHIPPED transfer | same receipt twice | Passes, unchanged | Service | Regression |
| I | Duplicate loss declaration | **BLOCKED** — Policy 2/4 unresolved; specification only: once built, must be idempotent by the same `client_transaction_id`-style discipline every other financial event uses | N/A | N/A | N/A | N/A | N/A | Double-posting a loss |
| J | Concurrent receipt/loss race | **BLOCKED** — Policy 2/4 unresolved; specification only: a receipt and a loss declaration racing for the same line must be mutually exclusive via the existing transfer-header lock pattern, exactly like ship-vs-cancel today | N/A | N/A | N/A | N/A | N/A | A line ending up both received and written off |
| K | Concurrent cancellation/loss race | **BLOCKED** — same reasoning as J, for whichever cancellation mechanism (if any) Policy 3 resolves to | N/A | N/A | N/A | N/A | N/A | Inconsistent combined terminal state |
| L | Cross-store authorization for loss | **BLOCKED** — Policy 2 unresolved; specification only: whatever role/store-scope Policy 2 resolves to must be enforced exactly like every other transfer operation's `_enforce_store_access` | N/A | N/A | N/A | N/A | N/A | Cross-store loss declaration |
| M | Unauthorized write-off attempt | **BLOCKED** — same as L | N/A | N/A | N/A | N/A | N/A | Privilege escalation on a financial action |
| N | GL reconciliation (post-loss) | **BLOCKED** — depends on the loss mechanism existing | Once built: `Σdebit == Σcredit` on the loss entry; trial balance still balances afterward | Transfer with a declared loss | run `trial_balance` | Balanced | Service | Unbalanced correction entry |
| O | Inventory In Transit reconciliation (post-loss) | **BLOCKED** — same as N | Once built: `gl_in_transit_balance` correctly drops by exactly the written-off amount; `outstanding_in_transit_total` matches | Transfer with a declared loss | run the reconciliation | Zero discrepancy | Service | The brief's "In-Transit remains overstated after a terminal loss" prohibited outcome |
| P | P&L reconciliation (post-loss) | **BLOCKED** — same as N | Once built: `operating_expenses` includes the new loss amount automatically, with no formula change | Transfer with a declared loss, in a known date range | run `profit_and_loss` | The loss amount appears in `operating_expenses`, no special-casing needed | Service | A hidden or double-counted expense category |
| Q | WAC/cost preservation for a loss | **BLOCKED** — same as N | Once built: the write-off amount uses the frozen `unit_cost_at_shipment`, never a later WAC | Transfer with a source WAC change after shipment, then a declared loss | declare the loss | Amount matches the frozen cost, not the current source WAC | Service | Wrong-cost contamination |
| R | Closed-period enforcement for a loss | **BLOCKED** — same as N; specification only, extending M22's existing, unconditional gate | Once built: a loss dated today is refused if today's period (for the source store) is closed; never attempts the original shipment date | Closed period covering the original shipment date; today's period open | declare a loss | Succeeds, dated today, original period untouched | Service | The exact M23B-fixed mistake recurring in a new feature |
| S | Current-date correction | **BLOCKED** — same as N | Once built: the loss journal's `posting_date == date.today()`, never the original invoice/shipment date | Transfer shipped long ago | declare a loss today | `posting_date == date.today()` | Service | Reverting to historical-date posting |
| T | Audit trail for a loss | **BLOCKED** — same as N | Once built: mandatory `reason`, correct actor, correct entity references | Transfer with a declared loss | inspect the audit log | Complete, mandatory-reason record | Service | Untraceable financial correction |
| U | Idempotency for loss declaration | Same as Session I — listed separately per the brief's own explicit list | Exactly one loss entry regardless of retries | Same request sent twice | declare the same loss twice | One entry | Service | Double-posting |
| V | Terminal-state enforcement | **BLOCKED** — same as N; specification only: once a line/transfer is written off, no further receipt against it may ever succeed | Written-off line | attempt a receipt | Rejected | Service | Receiving inventory that was already expensed away |
| W | Timeout/aging behavior | **BLOCKED** — Policy 6 unresolved; if ever built, must only alert, never auto-post or auto-transition (resolved mechanism) | N/A | N/A | N/A | N/A | N/A | An autonomous process posting to the GL, the first ever in this system |
| X | Mandatory-reconciliation control | **BLOCKED** — Policy 7 unresolved | N/A | N/A | N/A | N/A | N/A | Implementing a guessed control |
| Y | Physical-return full cycle (Policy 3, option A) | **BLOCKED** — same as C, restated at the full-cycle level | N/A | N/A | N/A | N/A | N/A | Implementing a guessed policy |
| Z | Mutation testing | Prove every resolved invariant above is actually enforced | See Section 12 | Baseline passing suite | apply, run, revert, diff-verify | RED for the correct reason each time | Mutation | A passing suite that doesn't test what it claims |

26 sessions (A-Z), most explicitly blocked pending their governing policy,
none implemented or run in this phase — on top of the 21 (M24 discovery)
+ 18 (M24A) sessions already specified.

## 12. M25 mutation targets

| Mutation | Target (once it exists) | Session it must fail |
|---|---|---|
| Remove loss authorization | future loss-declaration function's permission/store check | L, M |
| Remove store-access check | same | L, M |
| Remove terminal-state guard | future loss-declaration + `receive_transfer`'s status check | V |
| Allow duplicate loss | future loss-declaration's idempotency check | I, U |
| Omit Inventory In Transit credit | future loss-posting journal function | O |
| Omit loss expense (debit) | same | N, P |
| Post loss to wrong store | same | N, O |
| Use original shipment date | same | R, S |
| Bypass period-open enforcement | `_enforce_period_open` (shared, M22, unchanged) | R |
| Alter loss quantity | future loss-declaration's quantity validation | N, O |
| Alter frozen cost | future loss-declaration's cost lookup | Q |
| Permit receipt after loss | `receive_transfer`'s future status guard | V |
| Permit cancellation after loss | future cancellation/loss interaction guard | K, V |
| Disable timeout/aging detection | **N/A unless Policy 6 resolves to require it** — no mutation target until then |
| Disable mandatory reconciliation | **N/A unless Policy 7 resolves to require it** — no mutation target until then |

13 concrete, currently-specifiable mutation targets; the two the brief
lists that depend on features not yet authorized are explicitly named as
not-yet-applicable rather than silently omitted.

## 13. Unresolved decisions

Restated from Section 1, each with its exact missing input:

1. **Loss ownership (economic).** Missing: confirmation of the legal/
   financial relationship between stores, and the business's intended
   risk-of-loss allocation.
2. **Write-off authority** (role, dual-approval, self-authorization,
   separate accounting permission, threshold). Missing: a direct
   stakeholder decision; no repository evidence addresses any of these for
   transfers specifically.
3. **Post-shipment cancellation — whether to build any mechanism, and
   which of the four options.** Missing: whether this business's real
   logistics ever involves a recallable shipment.
4. **Discrepancy/shortage/damage recording at or before receipt** (Policy
   4 D/E, and whether B's indefinite remainder should ever force a
   terminal state). Missing: an operational description of how receiving
   staff actually handle a short or damaged delivery today, outside this
   system.
5. **Loss GL account's exact code/name and its economic store
   attribution.** Missing: the same stakeholder input as #1 (attribution)
   plus a routine chart-of-accounts naming decision (not itself a policy
   blocker, purely mechanical, deferred to M25).
6. **In-transit timeout — whether the concept is needed, and any
   threshold.** Missing: typical transit-time data for this business's
   actual store network.
7. **Mandatory reconciliation — whether required, frequency, and whether
   it blocks period close.** Missing: whether this business wants
   Inventory In Transit treated as a routine report or a formal control.

## 14. Risks and assumptions

- **Assumption:** the `ACCOUNT_CASH_OVER_SHORT`-vs-`ACCOUNT_INVENTORY_
  SHRINKAGE_EXPENSE` precedent is strong enough evidence to resolve "new
  account required" without further stakeholder input. Risk: if the
  business actually wants to minimize the chart of accounts and is
  indifferent to blending discrepancy sources, this conclusion would be
  wrong — but reusing the existing account remains available as a fallback
  if a stakeholder overrides this reasoning later; no cost is locked in by
  documenting the alternative that fits the codebase's own pattern.
- **Assumption:** the `create_purchase_return` precedent is close enough
  in shape to justify resolving the physical-return *mechanism* (new
  document, not a mutation) even though *whether* physical return is
  ever wanted remains unresolved. Risk: minimal — this is a mechanism
  answer conditioned explicitly on a policy choice that has not been made,
  not a standalone commitment.
- **Assumption:** the total absence of any autonomous-posting precedent in
  this system is strong enough to resolve "timeout must be alert-only"
  even though "should a timeout exist at all" remains unresolved. Risk:
  minimal, for the same reason — this only constrains the *shape* of a
  feature that may never be built.
- **Risk of the scenario-7-style outcome (Section 4, Scenario 8):** this
  document does not close the door on transfers remaining unresolved
  indefinitely, because doing so would require exactly the two policy
  areas (6/7) that have zero repository evidence to decide from. This is
  the correct, honest outcome, not a shortfall — inventing a timeout or a
  mandatory-reconciliation rule here would be precisely the kind of
  invented policy the brief prohibits.

## 15. Explicit implementation stop conditions

M25 must stop and obtain explicit policy decisions before implementing any
of the following (all UNRESOLVED per Section 13):

- Loss ownership (economic/financial responsibility).
- Write-off authority (role, approval structure, thresholds).
- Post-shipment cancellation — whether to build it, and which mechanism.
- Discrepancy/shortage/damage recording at or before receipt.
- The loss GL account's exact identity and its economic store
  attribution.
- In-transit timeout policy (concept and threshold).
- Mandatory-reconciliation requirement and any period-close blocking tied
  to it.

M25 may proceed without further stops on: the loss journal's mechanical
shape *if and when* the above are resolved (source-store posting,
`date.today()`, one balanced entry, automatic P&L inclusion, no formula
change — all resolved here), the physical-return mechanism's shape *if*
that option is chosen (a new document, per the `PurchaseReturn` precedent),
post-receipt damage (already fully solved by the existing stock-adjustment
path, no new work ever needed), and every area M24A already closed
(creation authorization, over-receipt, accounting-date convention,
period-close safety guarantees, multi-store attribution) — none of which
this document reopens.
