# M24A: Inter-Store Transfer Policy Resolution

**Status:** Policy/design only. No production code, migration, seeded
account, API behavior, transfer state, inventory movement, GL behavior,
test, or frontend file changed. Starting HEAD: `b7df88e` (M24 discovery
commit).

## 1. Executive policy summary

| # | Policy area | Resolution |
|---|---|---|
| 1 | Transfer creation authorization (F7) | **RESOLVED** — current one-sided creation, with either side's cancellation right as an ex-post check, is the intended design (M8 Design Decision 7's "no approval workflow" read consistently). Not a defect. |
| 2 | Ownership of loss | **Descriptive fact resolved** (the un-cleared value sits on the source store's books until receipted); **economic/financial responsibility for a loss is UNRESOLVED BUSINESS DECISION.** |
| 3 | Post-shipment cancellation | Current: **NO**, not permitted. Whether to add it, and whether it should be a distinct concept from loss/write-off, is **UNRESOLVED BUSINESS DECISION.** |
| 4 | Loss/write-off | **UNRESOLVED BUSINESS DECISION** — whether to build it at all, and every mechanic if so. |
| 5 | Discrepancies | Exact and short receipts: **implemented, working as indefinite partial receipt** (descriptive fact). Over-receipt: **implemented, strictly prohibited, no tolerance/override exists** (resolved). Damage/shortage recording as a distinct event: **UNRESOLVED.** |
| 6 | In-transit timeout | **UNRESOLVED BUSINESS DECISION** — no duration, no policy basis anywhere. |
| 7 | Mandatory reconciliation | Current: **no requirement exists; a period can close with In-Transit unreconciled** (descriptive fact). Whether reconciliation should become mandatory is **UNRESOLVED.** |
| 8 | GL accounting for loss | **UNRESOLVED BUSINESS DECISION** for the account and store attribution. **Resolved by definition**: any loss recognized as an expense necessarily affects P&L (unlike the pure balance-sheet reclassification ship/receive use today). |
| 9 | Accounting date for transfer corrections | **RESOLVED** — `date.today()`, extending the established M23A/M23B compensating-entry convention. Normal shipment/receipt dates are unaffected and unchanged. |
| 10 | Period-close interaction | **RESOLVED** for what the architecture already guarantees (corrections post today, never the original date, period-open is always enforced); **UNRESOLVED** for whether a new blocking check should be added. |
| 11 | Multi-store accounting scope | **RESOLVED** — no centralized/company account or entity; In-Transit attribution and consolidated reporting remain exactly as already built. |
| 12 | Terminal state model | Contingent on Policy Areas 2/3/4 — classified as far as possible, explicitly left open where those areas are unresolved. |

## 2. Evidence reviewed

- `docs/M24_DISCOVERY.md` in full (this policy phase's direct predecessor).
- `docs/M8_ADVANCED_INVENTORY_DESIGN.md` Design Decisions 6-11 and the
  "Deferred / known limitations" section, re-read in full for this phase.
- `docs/M21_DISCOVERY.md` Finding F7's exact wording and severity framing.
- `docs/M22_DISCOVERY.md` / `docs/M23_DISCOVERY.md` / `docs/M23A_POLICY.md`
  Section 3.5/3.6 (the established compensating-entry date convention and
  the per-store-entity precedent, both directly reused below).
- `backend/app/modules/transfers/{models,service,schemas}.py`,
  `backend/app/api/v1/endpoints/transfers.py`,
  `backend/app/modules/auth/permissions.py` (re-confirmed: no override,
  tolerance, or loss-related permission exists anywhere in the matrix).
- `backend/app/modules/accounting/service.py::close_accounting_period`
  (re-confirmed: takes no dependency on transfer or Inventory-In-Transit
  state — a period can be closed regardless).
- `docs/TECHNICAL_BLUEPRINT.md` — re-checked for any transfer-risk/
  commercial-terms language; none exists (transfers postdate the original
  blueprint entirely, introduced in M8).

## 3. Policy decisions

### 3.1 Transfer creation authorization (Policy Area 1 / Finding F7)

**Documented policy** (`docs/M8_ADVANCED_INVENTORY_DESIGN.md` Design
Decision 7): "creating a transfer already requires `inventory.transfer.write`
at *both* the source and destination store's operators' discretion (a
transfer is visible to and cancellable by either store before shipment)."

**Current implementation**: `_create_transfer_inner` accepts a caller whose
`caller_store_id` equals **either** `from_store_id` or `to_store_id` (not
both) — a Store-B operator can name Store A as counterparty without any
Store-A operator's own action.

**Resolution: these are the same policy, not a contradiction.** Read
together with the same Design Decision's own title — "REQUESTED and
APPROVED are deliberately NOT separate states" — "at both... store's
operators' discretion" is describing that operators at **either** end
*generically hold the capability* to act unilaterally, not that a specific
transfer requires **both** to jointly consent before it can exist. The
parenthetical that immediately follows — "visible to and cancellable by
**either** store before shipment" — is the actual check-and-balance the
design relies on: the design never intended a consent gate at creation; it
relies on the counterparty's unilateral **cancellation right** (also
one-sided, also already implemented, `cancel_transfer`'s
`caller_store_id in (from_store_id, to_store_id)`) as the ex-post remedy if
one side objects to a transfer the other side drafted. This is a coherent,
already-fully-implemented design, not an asymmetric gap.

- Who may create: an authenticated holder of `inventory.transfer.write`
  scoped to **either** the source or destination store (Manager or
  Inventory Clerk today), or an unrestricted (`caller_store_id is None`)
  caller such as Admin.
- Source-store authorization required? **No**, not exclusively — either
  side suffices.
- Destination-store authorization required? **No**, not exclusively —
  either side suffices.
- Both stores must authorize? **No** — this was M21's F7 question, and the
  answer, read against the full design decision, is no.
- Central/global role may create? **Yes** — an unrestricted caller
  (`caller_store_id is None`, e.g., Admin) always could, consistent with
  every other module's pattern.
- Approval required? **No**, by explicit design (Decision 7's title).
- May the creator also approve? **N/A** — no approval concept exists.
- Is DRAFT → SHIPPED intentionally approval-free? **Yes, explicitly**
  ("shipping is itself the commitment point... not a separate approval
  step").

**Proposed policy going forward:** none needed — current implementation
already matches documented intent once read completely. **This closes
Finding F7** as resolved-not-a-defect, superseding its MEDIUM
severity-with-uncertainty framing in M21/M22/M23's carried-forward notes.
No M25 change is required for this policy area.

### 3.2 Ownership of loss (Policy Area 2)

Two distinct questions must not be conflated (the brief's own instruction:
do not select a party without authoritative evidence):

**(a) Where does the un-cleared value sit today, as a matter of existing GL
structure?** This is a verifiable fact, not a policy choice.
`post_transfer_shipment_journal` posts `Dr Inventory In Transit / Cr
Inventory` **against the source store** (`JournalEntry.store_id =
transfer.from_store_id`) and this entry is never re-attributed to the
destination until a matching receipt posts `Dr Inventory / Cr Inventory In
Transit` **against the destination store**. For any quantity never
receipted, the un-cleared debit permanently remains on the **source**
store's own books. This is resolved and factual, directly traceable to M8
Design Decision 9's existing, unchanged posting logic.

**(b) Who is economically/financially responsible for a loss — i.e., whose
expense is it?** This is a genuine commercial-terms question (analogous to
FOB-shipping-point vs. FOB-destination risk-of-loss terms in real logistics)
that no document in this repository addresses. `docs/TECHNICAL_BLUEPRINT.md`
predates transfers entirely and has no risk-in-transit language; no M8-M24
document states whether the shipping store or the receiving store bears a
loss economically. **Marked UNRESOLVED BUSINESS DECISION.** Leaving the
expense on the source store's books (matching where the GL balance already
sits, per (a)) is the smallest-change option, but is explicitly **not**
confirmed here as the intended economic policy — it is offered only as the
path of least structural disruption if the business has no stated
preference.

- When does ownership transfer? Per (a), the value moves from the source's
  books to the destination's books at the moment of a receipt event —
  never at shipment alone, and never partially except to the extent
  actual receipts have occurred.
- Is loss a source-store or destination-store expense? **UNRESOLVED** (b).
- Must the system preserve both source and destination attribution? **Yes,
  already true** — every `InterStoreTransferLine` and both journal legs
  permanently record both `from_store_id`/`to_store_id`
  (`source_product_id`/`destination_product_id`), so whichever attribution
  the business eventually chooses can be implemented without losing either
  store's identity.

### 3.3 Post-shipment cancellation (Policy Area 3)

**Current policy: NO.** `_CANCELLABLE_TRANSFER_STATUSES = ("DRAFT",)`;
`cancel_transfer` raises `INVALID_TRANSFER_STATE` for a `SHIPPED` transfer.
`docs/M8_ADVANCED_INVENTORY_DESIGN.md`'s own "Deferred" section states this
explicitly and by name: "Cancelling or reversing a SHIPPED (in-transit)
transfer — deferred... the safe path (a real reverse-transfer) is a future
milestone's concern." **Permitted terminal outcomes today**: fully received,
or permanently `SHIPPED` with no further transition (the M21/M24 finding).

**Whether to add it, and what it would mean, is UNRESOLVED**, with one
evidence-based observation offered, not a decision: "cancelling" a transfer
whose goods have already physically left the source cannot mean the same
thing "cancelling" a DRAFT does (nothing has moved yet). It can only
sensibly mean one of the brief's options A ("goods are physically returned
to source" — a real reverse-logistics operation) or B ("goods are
administratively cancelled/lost" — which is economically indistinguishable
from Policy Area 4's loss/write-off event). Per the brief's own instruction
not to collapse cancellation and loss "unless policy explicitly says they
are the same event," and since no such statement exists anywhere, **this
document does not collapse them** — but it flags, as a design-economy
observation for whoever eventually resolves Policy Area 4, that a
future "post-shipment cancellation (option B)" and "loss/write-off" may
turn out to be the identical mechanism wearing two different reason labels,
and building two separate code paths for the same accounting effect would
be worth avoiding once both are actually decided.

- Who can cancel (if built)? **UNRESOLVED.**
- Required authorization? **UNRESOLVED.**
- Inventory effects? **UNRESOLVED**, though if option A (physical return)
  is ever chosen, the natural mechanism is a **new** transfer in the
  reverse direction — mirroring M8 Design Decision 8's own precedent that
  a transfer needing a second physical movement is a new transfer, not a
  mutation of the first — never decided here as the answer, only noted as
  the consistent pattern if option A is chosen.
- GL effects / Inventory In Transit cleared / terminal / reversible / audit
  requirements? **All UNRESOLVED**, contingent on which option (A/B/C/D) is
  eventually chosen and on Policy Area 4's resolution.

### 3.4 Loss / write-off (Policy Area 4)

**UNRESOLVED BUSINESS DECISION in its entirety** — whether this ERP must
support a formal loss/write-off event at all is not answered anywhere in
the repository. `docs/M21_DISCOVERY.md` rated the absence of any such path
HIGH severity (a real, growing risk for a grocery operation that does
physically lose or damage stock in transit), which is evidence that the
need is *plausible*, not evidence that the business has *decided* to build
it. Per the brief's explicit instruction, no account, state, or mechanic is
created here. Every sub-question — event/state name, who may declare it,
required authorization, source/destination attribution, inventory effect,
GL effect, expense account/category, treatment of Inventory In Transit,
accounting date (see 3.9 for the one exception), period-close behavior,
audit requirements, reason codes, supporting documentation, reversibility,
partial-loss support — **remains UNRESOLVED** until a human stakeholder
confirms the feature is wanted at all.

**If NO (the business decides not to build this): the intended alternative
for permanently shipped/unreceived goods is the status quo already in
place** — the transfer remains `SHIPPED` indefinitely, `Inventory In
Transit` remains permanently non-zero for that quantity, and the existing
reconciliation/drill-down reports (M24 discovery Section 6/13) are the
business's only tool for surfacing and manually tracking such cases outside
the accounting system (e.g., in a separate write-off process entirely
outside this ERP). This is not a good outcome for the GL, but it is a
legitimate, already-functioning fallback, not an unhandled crash state.

### 3.5 Discrepancies (Policy Area 5)

| Scenario | Current behavior | Classification |
|---|---|---|
| Shipped 10 / received 10 | Fully received (derived); no gap | Resolved, working |
| Shipped 10 / received 8 | Line stays partially received forever unless a later receipt event completes it; system cannot distinguish "the rest is still coming" from "the rest is short and gone" | Descriptive fact; whether a distinct **shortage-declaration** event is needed is **UNRESOLVED** |
| Shipped 10 / received 12 | `OVER_RECEIPT`, hard-rejected, zero mutation; no tolerance band, no override permission exists anywhere in the permission matrix (re-confirmed by grep) | **Resolved — strictly prohibited today.** Whether to ever add a tolerance or an authorized-override path is **UNRESOLVED**; no evidence supports either. |
| Duplicate receipt | `client_transaction_id` idempotency; second call returns the original, no double-posting | Resolved, working |
| Damaged quantity (received but flagged damaged) | No such concept exists — a receiver can only either receipt a quantity (at full frozen cost, no distinction for condition) or not receipt it at all (leaving it in the "shipped 10 / received 8"-shaped gap above) | **UNRESOLVED** |
| Missing quantity | Same shape as "shipped 10 / received 8" | **UNRESOLVED**, same as above |
| Receipt after a declared loss | N/A today (no loss event exists); if Policy Area 4 is ever resolved to build one, whether a subsequent real-world "it turned up after all" receipt should be permitted is a new question | **UNRESOLVED**, deferred to whenever Policy Area 4 is resolved |

**Is partial receipt supported?** Yes, fully, today — this is core,
working, tested functionality (M8 Design Decision 8), not a discrepancy at
all in the ordinary case; it only becomes an open question when partial
receipt *never completes* (the shortage/damage/loss questions above).
**Over-receipt:** prohibited, no tolerance, no authorization override —
resolved as the current, sole policy; changing it is a distinct,
unaddressed proposal with zero supporting evidence.

### 3.6 In-transit timeout (Policy Area 6)

**UNRESOLVED BUSINESS DECISION**, entirely. No document, constant, or
configuration field anywhere in this repository establishes a duration,
threshold, or operational policy for "how long is too long" for a
`SHIPPED` transfer to remain unreceived. No duration is invented here. If a
timeout is ever wanted, every one of the brief's sub-questions (automatic
state change vs. alert-only, recipient of the alert, whether it can create
accounting entries, whether manual investigation is mandatory, whether
transfers may remain indefinitely unresolved) needs its own answer from the
business — none is guessed at.

**What is already true, factually:** a transfer can and today does remain
`SHIPPED` indefinitely with no system-enforced consequence — this is the
current, real behavior, not a hypothetical.

### 3.7 Mandatory reconciliation (Policy Area 7)

**Current, verified fact:** `close_accounting_period` (M22) takes no
dependency whatsoever on transfer state or the `Inventory In Transit`
balance — a store's accounting period can be closed while transfers remain
outstanding, with no check, warning, or block of any kind. The existing
`inventory_in_transit_reconciliation`/`inventory_in_transit` reports are
available on demand, at any time, to anyone holding `inventory.read`, but
nothing requires anyone to run them on any cadence.

**Whether reconciliation should become mandatory is UNRESOLVED** — no
document establishes a frequency, a responsible role, an acceptable
unresolved-balance threshold, an escalation path, an aging requirement, or
whether it should be evaluated per-store or company-wide (the existing
report is already company-wide only, per M8 Design Decision 9's own
reasoning that the account represents value *between* stores). No alerts or
scheduled jobs are proposed or built in this phase, per the brief's
explicit instruction.

**If the business ultimately decides NO** (manual reporting remains
sufficient): the existing, already-shipped reconciliation and drill-down
reports are adequate for that policy — no further work would be required
for this specific question.

### 3.8 GL accounting for loss (Policy Area 8)

Per Policy Area 4, no loss/write-off mechanism is being designed here. The
sub-questions the brief still asks for are addressed to the extent
evidence-based answers exist:

- **Debit account, credit account:** **UNRESOLVED** — no account is
  created or selected here, per the brief's explicit instruction. A
  reasoned *candidate*, not a decision, is noted for whoever resolves this
  later: `ACCOUNT_INVENTORY_SHRINKAGE_EXPENSE` (already seeded, already
  used for ordinary stock-count shrinkage — the closest existing semantic
  match) would be the parsimonious, "reuse before inventing" choice this
  codebase has consistently made elsewhere (M4's original chart of
  accounts, M22's P&L fix reusing existing accounts rather than adding new
  ones). This is offered as evidence-adjacent context, not a resolution —
  the brief explicitly lists "loss GL account" among the items that must
  stay marked unresolved absent authoritative evidence, and reuse-by-
  convention is suggestive, not authoritative.
- **Is Inventory In Transit credited?** If a loss/write-off is ever built,
  **yes, necessarily** — this is not a policy choice but an accounting
  identity: the only way to permanently zero an outstanding In-Transit
  balance is to credit it; there is no other account currently holding the
  matching debit.
- **Is a dedicated transfer-loss expense account required, or do existing
  accounts suffice?** **UNRESOLVED** as a final decision (see the candidate
  above).
- **Does the loss affect P&L?** **Yes, resolved by definition** — the
  moment any EXPENSE-type account is debited (which any write-off,
  by definition, must do), the entry affects P&L. This is structurally
  different from the ship/receive legs, which are pure ASSET-to-ASSET
  reclassifications with no P&L effect at all (Design Decision 9) — a loss
  is definitionally not that.
- **Source or destination store attribution?** **UNRESOLVED** — see Policy
  Area 2(b). Descriptively, if the loss is posted against whichever store
  currently holds the un-cleared balance, that is the source store for any
  never-received quantity (Policy Area 2(a)'s factual finding).
- **One journal or multiple linked journals?** This is an implementation
  mechanic, not a business policy: the established, universal pattern in
  this codebase is **one balanced `JournalEntry` per economic event**
  (every existing `post_*_journal` function, without exception). Following
  that same pattern for a future loss entry requires no new business
  decision — it is architectural consistency, not policy.

### 3.9 Accounting date for transfer corrections (Policy Area 9)

**RESOLVED**, by direct extension of the established, repository-wide
convention:

- **Shipment:** unchanged — server-set at the moment of shipment
  (`transfer.shipped_at`), never backdatable. Not a "correction," so this
  policy area does not apply to it.
- **Normal receipt / partial receipt:** unchanged — a genuine, caller-
  supplied, backdatable real-world date (`received_date`), exactly like a
  goods receipt or a sale return, per M24 discovery Section 6's traced
  distinction between forward-posting events (their own real date) and
  compensating events (today).
- **Loss/write-off (if ever built):** **`date.today()`** — this is a
  compensating/corrective event discovered after the fact, in the same
  category as every other correction this codebase has ever built
  (`post_supplier_payment_reversal_journal`,
  `post_supplier_credit_note_reversal_journal`,
  `post_payroll_reversal_journal`, and now, since M23B,
  `post_purchase_invoice_void_journal`). This is not a new date policy —
  it is the same policy `docs/M23A_POLICY.md` Section 3.5 already
  established, applied to a new event type that fits the identical
  category (an after-the-fact correction, not an independent economic
  event with its own real date).
- **Post-shipment cancellation (if ever built):** **`date.today()`**, for
  the identical reason, whichever of options A-D it turns out to mean —
  even option A (a physical return) would be modeled as a *new* transfer
  (per 3.3's observation), whose own shipment/receipt legs would then use
  the ordinary forward-dating rules, not this policy area at all; only the
  *administrative* act of recording that the original transfer will not
  complete as planned is the corrective event this date policy governs.
- **Discrepancy adjustment (if ever built):** **`date.today()`**, same
  reasoning, if and when Policy Area 5's open questions are resolved into
  an actual posting mechanism.

### 3.10 Period-close interaction (Policy Area 10)

- **A transfer shipped in a closed period, loss discovered today:** the
  loss posts **today** (3.9), which must itself be in an **open** period;
  the original (closed) shipment period is never touched, never reopened,
  and never receives a backdated posting. **RESOLVED**, by direct
  architectural consequence of 3.9 plus M22's unconditional
  `_enforce_period_open` gate (no bypass exists anywhere in this
  codebase).
- **Does loss/cancellation post today?** **Yes** (3.9), resolved.
- **Is original-period correction ever allowed?** **No, never** — this
  matches every single correction mechanism in the entire system without
  exception; there is no precedent anywhere for posting a correction into
  a historical date, and M23B's own fix was specifically about *removing*
  the one place that used to do this. **RESOLVED.**
- **Can period close occur while Inventory In Transit remains
  unresolved?** **Yes, today, as a matter of fact** (3.7) — `close_
  accounting_period` has no such check. **RESOLVED as a descriptive fact.**
- **Does an unresolved transfer block period close?** **No, currently.**
  Whether it *should* is the same open question as 3.7's mandatory-
  reconciliation decision — **UNRESOLVED.**
- **May a closed period contain an outstanding shipped transfer?** **Yes**
  — already happens today, and is architecturally harmless: the shipment's
  own `JournalEntry` was already posted (and is immutable, per M4's
  `UPDATE`/`DELETE` revocation) before the period closed; closing the
  period afterward does not retroactively invalidate or need to touch that
  already-posted entry. **RESOLVED as a descriptive, safe fact.**

This is deliberately kept separate from fiscal-year logic, per the brief's
instruction — `docs/M23A_POLICY.md` Section 3.4 already established that no
fiscal-year concept is required beyond M22's ordinary period locking, and
nothing here revisits that.

### 3.11 Multi-store accounting scope (Policy Area 11)

- **Source-store ownership / destination-store ownership:** per Policy Area
  2(a) — the un-cleared value's ledger location is a resolved, factual
  matter (source, until receipted); *economic* ownership remains
  unresolved (2(b)).
- **Loss attribution / expense attribution:** **UNRESOLVED**, same as
  2(b)/3.8.
- **Inventory In Transit attribution:** **RESOLVED, unchanged** — always
  posted against `from_store_id` at ship, always cleared against
  `to_store_id` at receipt; this is the existing, correct, already-tested
  design (M8 Design Decision 9) and nothing here proposes changing it.
- **Reporting attribution:** **RESOLVED, unchanged** — the reconciliation
  report is deliberately company-wide only (no `store_ids` filter), per
  M8's own explicit reasoning that the account represents value *between*
  stores; the per-line drill-down (`inventory_in_transit`) *does* carry
  both stores' identities and *can* be filtered by `store_ids`, so
  per-store visibility already exists at that granularity too.
- **Is inter-store transfer accounting always explicit between stores?**
  **Yes, resolved** — verified in M24 discovery Section 13: nothing is
  ever netted, consolidated, or eliminated; every line and every journal
  permanently carries both store identities.
- **Is any centralized company-level account required?** **No, resolved**
  — consistent with `docs/M23A_POLICY.md` Section 3.6's decision that no
  company/organization entity exists or is needed. `Inventory In Transit`
  is an ordinary chart-of-accounts row like any other; its "company-wide"
  reconciliation view is a reporting-time aggregation across per-store
  `JournalEntry` rows, not a separate store-less ledger of its own. No new
  entity, account, or model is proposed.

### 3.12 Terminal state model (Policy Area 12)

Classified only as far as Policy Areas 2-4 currently allow — several
entries are explicitly left open pending those resolutions, per the
brief's own sequencing ("only after the business policies above are
resolved, determine the conceptual state machine"):

| Candidate | Classification | Basis |
|---|---|---|
| DRAFT | Persisted state | Existing, unchanged |
| SHIPPED | Persisted state | Existing, unchanged |
| RECEIVED | Derived state | Existing, unchanged (never stored, computed from line quantities) |
| PARTIALLY_RECEIVED | Derived state | Existing, unchanged (same derivation) |
| CANCELLED | Persisted state | Existing, unchanged (DRAFT-only) |
| LOST | **Not yet classifiable** | Contingent entirely on Policy Area 4's resolution; if built, most likely a persisted state (a new `TRANSFER_STATUSES` value) or a domain event that produces one — cannot be decided before Policy Area 4 is |
| WRITTEN_OFF | **Not yet classifiable** | Same as LOST; per 3.3's observation, may turn out to be the *same* state/event as LOST rather than a separate one, but that itself is unresolved |
| RETURNED_TO_SOURCE | **Not yet classifiable, but a design pattern is available if built** | Per 3.3, best modeled as a **domain event that creates a new transfer** (the reverse leg), not a state on the original transfer — mirrors M8 Design Decision 8's "a transfer needing a second physical movement is a new transfer" precedent. This is offered as the consistent pattern *if* the feature is ever built, not a decision that it should be. |

No state is assumed to require persistence merely because it appears on
this list — `RECEIVED`/`PARTIALLY_RECEIVED` are the existing, deliberate
proof that a real business concept does not always need its own stored
column.

## 4. Policy matrix

| Policy | Decision | Evidence | Current behavior | M25 requirement | Accounting consequence | Inventory consequence | Authorization consequence |
|---|---|---|---|---|---|---|---|
| Transfer creation | Resolved — either side, no approval | M8 Decision 7, read in full | Implemented, matches intent | None | None | None | None |
| Approval | Resolved — does not exist, by design | M8 Decision 7 title | N/A | None | N/A | N/A | N/A |
| Shipment | Resolved — unchanged | M8 Decision 7/9 | Implemented, correct | None | Dr In-Transit/Cr Inventory, source store | TRANSFER_OUT | `inventory.transfer.ship`, source store |
| Receipt | Resolved — unchanged | M8 Decision 7/9 | Implemented, correct | None | Dr Inventory/Cr In-Transit, destination store | TRANSFER_IN | `inventory.transfer.receive`, destination store |
| Partial receipt | Resolved — supported | M8 Decision 8 | Implemented, correct | None | Same as receipt, per event | Same as receipt, per event | Same as receipt |
| Over-receipt | Resolved — prohibited, no tolerance | Service code, permission matrix | `OVER_RECEIPT`, hard block | None (unless business adds tolerance — unresolved if so) | None (rejected) | None (rejected) | None |
| Post-shipment cancellation | **Unresolved** | M8 "Deferred" | Not permitted (`INVALID_TRANSFER_STATE`) | Unresolved — depends on 3.3/3.4 | Unresolved | Unresolved | Unresolved |
| Loss | **Unresolved** | No repo evidence | Does not exist | Unresolved | Unresolved | None expected (Section 4 of M24 discovery: nothing to move) | Unresolved |
| Damage | **Unresolved** | No repo evidence | Does not exist | Unresolved | Unresolved | Unresolved | Unresolved |
| Write-off | **Unresolved** | No repo evidence | Does not exist | Unresolved | Unresolved | None expected | Unresolved |
| Return to source | **Unresolved** | No repo evidence | Does not exist | Unresolved | Would be a new transfer's normal ship/receive accounting, if built | New TRANSFER_OUT/IN pair, if built | Same as ordinary transfer creation/ship/receive, if built |
| Discrepancy handling | **Unresolved** (beyond over-receipt, which is resolved) | Service code | Indefinite partial receipt; no shortage/damage event | Unresolved | Unresolved | Unresolved | Unresolved |
| Loss ownership | **Unresolved** (economic); resolved (ledger-location fact) | GL structure (M8 Decision 9) | Value sits on source's books until receipted | Unresolved | Unresolved | N/A | N/A |
| Write-off authority | **Unresolved** | No repo evidence | N/A | Unresolved | N/A | N/A | Unresolved |
| Timeout | **Unresolved** | No repo evidence | None exists; transfers may stay SHIPPED forever | Unresolved | Unresolved | N/A | N/A |
| Reconciliation (mandatory) | **Unresolved** (requirement); resolved (current non-requirement is a fact) | `close_accounting_period` code | On-demand only, never required | Unresolved | N/A | N/A | N/A |
| Loss GL account | **Unresolved** | No repo evidence (candidate noted, not decided) | N/A | Unresolved | N/A | N/A | N/A |
| Accounting date (corrections) | **Resolved** — `date.today()` | M23A Section 3.5, extended | N/A yet (no correction path exists to date) | If any correction is built, it follows this | Ensures period-lock consistency | N/A | N/A |
| Closed-period behavior | **Resolved** for what already holds; unresolved for whether to add a new block | M22 `_enforce_period_open`, `close_accounting_period` | Period close never checks transfer state | Unresolved (see reconciliation) | N/A | N/A | N/A |
| Store attribution | **Resolved** — always explicit, never netted | M8 Decision 9, verified in M24 discovery | Implemented, correct | None | None | None | None |
| Transfer terminal states | **Partially resolved** — existing states confirmed; new ones contingent | Sections 3.12 | `DRAFT`/`SHIPPED`/`CANCELLED` only | Contingent on 2/3/4 | Contingent | Contingent | Contingent |

## 5. Transfer lifecycle/state-machine policy

No change to the existing state machine is proposed by this document (see
Section 3.12 — every new candidate state is contingent on unresolved
business decisions). The current `DRAFT → SHIPPED → (derived RECEIVED)` /
`DRAFT → CANCELLED` machine is confirmed correct and sufficient for every
scenario this policy phase was able to resolve.

## 6. Inventory policy

- No new `InventoryMovement` type is proposed (M24 discovery Section 4:
  a lost/written-off shipment has no inventory-side correction to make —
  both stores' stock ledgers are already correct).
- Frozen-cost valuation (`unit_cost_at_shipment`) remains the only
  evidence-backed valuation basis for any future loss/write-off amount,
  should one ever be built (M24 discovery Section 7, unchanged).
- Partial receipt, over-receipt rejection, and idempotency all remain
  exactly as implemented — none is touched by this policy phase.

## 7. Accounting policy

- No account is created, seeded, or selected in this phase.
- The one settled principle: **any future transfer-correction event posts
  at `date.today()`, never a historical date**, and always passes through
  the existing, unconditional `_enforce_period_open` gate — this is not a
  new rule invented for transfers, it is the same rule M23A/M23B already
  established for every other correction mechanism, applied consistently.
- Whether a loss/write-off ever exists, which account it uses, and which
  store it is attributed to are all open (Sections 3.4/3.8).

## 8. Authorization policy

- Transfer creation, shipment, receipt, and DRAFT-cancellation
  authorization are all confirmed correct and unchanged (Section 3.1).
- No new permission is proposed in this phase — every write-off/loss/
  post-shipment-cancellation authorization question is unresolved pending
  Sections 3.3/3.4, and inventing a permission ahead of the feature it
  would gate is exactly the kind of premature design this phase must not
  do.

## 9. Period-close policy

Fully addressed in Section 3.10: the existing architecture already
guarantees a future correction cannot backdate into a closed period and
cannot touch an already-posted historical entry. Whether an unresolved
in-transit balance should ever **block** a period close is a distinct,
unresolved policy question (tied to Section 3.7), not decided here.

## 10. Multi-store policy

Fully addressed in Section 3.11: no centralized entity or account, explicit
per-transfer store attribution preserved everywhere, reporting scope
unchanged. The one open question (economic loss attribution) is a
consequence of Policy Area 2, not a separate multi-store architecture
question.

## 11. M25 implementation boundary

**Because every mechanic-level question (Sections 3.2(b)-3.8, 3.6, portions
of 3.7/3.10) remains unresolved, M25 cannot yet implement a loss/write-off
or post-shipment-cancellation feature.** The only implementation-boundary
items this phase can specify with confidence are the ones that were
resolved:

**What M25 may safely do, once undertaken, requiring no further policy
input:**
- Nothing structural — every resolved item in this document describes
  *existing, already-correct* behavior (creation authorization, dates,
  period-close safety, store attribution). There is no code change these
  resolutions call for, mirroring M23A's own finding that most of its
  resolved policy areas required no M23B-equivalent work either.
- If it is ever useful housekeeping: updating `docs/M21_DISCOVERY.md`'s/
  `docs/M22_DISCOVERY.md`'s/`docs/M23_DISCOVERY.md`'s carried-forward
  "Finding F7 unresolved" notes to reflect this phase's resolution — a
  documentation-only change, not covered by "implementation," and not
  performed in this document per its own no-code/no-doc-elsewhere scope.

**What M25 must NOT do until the remaining policy questions are answered:**
- Must not add any `LOST`/`WRITTEN_OFF`/`RETURNED_TO_SOURCE` state, column,
  or `TRANSFER_STATUSES` value.
- Must not add any loss/write-off/post-shipment-cancellation service
  function, endpoint, or permission.
- Must not create or seed any new account for a transfer-loss expense.
- Must not add a discrepancy/shortage/damage-recording mechanism.
- Must not add an in-transit timeout, alert, or scheduled job.
- Must not add a period-close block tied to unreconciled transfers.
- Must not change `Inventory In Transit`'s attribution, reporting scope,
  or company-wide-only reconciliation design.
- Must not touch the existing DRAFT/SHIPPED/CANCELLED/creation/ship/
  receive/cancel code paths, all of which this phase confirms need no
  change.

**Database/model, migration, seed/account, service, API, permission,
frontend, inventory-movement, GL, audit, reporting, background-job,
idempotency, locking, and cross-store-isolation changes: all NONE for this
phase's resolved items, and all UNDEFINED-PENDING-POLICY for its unresolved
items** — there is nothing to specify concretely for a feature whose basic
shape (does it exist, who triggers it, what account, what date-of-record
semantics beyond the general `date.today()` rule) is not yet decided.

## 12. M25 testing-session specification

Per the brief, sessions are designed now for the eventual implementation —
none is implemented or run in this phase. Sessions covering **already-
resolved, already-implemented** behavior are regression confirmations (they
should already pass today, unchanged); sessions covering **unresolved**
areas are explicitly marked as blocked until their policy question is
answered, mirroring M24 discovery's own treatment of its conditional
Sessions E/F.

| # | Session | Objective | Invariant | Setup | Operation | Expected result | Level | Failure mode prevented |
|---|---|---|---|---|---|---|---|---|
| A | Transfer creation authorization | Either side of a transfer may create it unilaterally; a third store may not | Section 3.1's resolved policy | Three stores | Store-B user creates a transfer naming Store A as source | Succeeds; audit-logged; Store-A user can see and cancel it | Service + adversarial | Regression on the now-resolved F7 policy |
| B | Shipment | Unchanged from M24 discovery Session B | Cost frozen at ship, both legs posted correctly | DRAFT transfer | `ship_transfer` | Unchanged, already passing | Service | Regression |
| C | Normal receipt | Unchanged from M24 discovery Session C | Frozen cost used, WAC rolled correctly | SHIPPED transfer | `receive_transfer` | Unchanged, already passing | Service | Regression |
| D | Cancellation before shipment | Unchanged from M24 discovery Session D | No inventory/GL effect | DRAFT transfer | `cancel_transfer` | Unchanged, already passing | Service | Regression |
| E | Post-shipment cancellation | **BLOCKED** — do not implement until Section 3.3 is resolved | N/A | N/A | N/A | N/A | N/A | Implementing a guessed policy |
| F | Loss/write-off | **BLOCKED** — do not implement until Sections 3.2(b)/3.4/3.8 are resolved | N/A | N/A | N/A | N/A | N/A | Implementing a guessed policy |
| G | Partial receipt | Unchanged from M24 discovery Session G | Running totals correct across events | SHIPPED transfer, 2+ receipts | sequential `receive_transfer` | Unchanged, already passing | Service | Regression |
| H | Over-receipt | Unchanged from M24 discovery Session H | Strictly rejected, no tolerance | SHIPPED transfer | over-quantity `receive_transfer` | `OVER_RECEIPT`, zero mutation | Service | Regression; also proves no tolerance was silently added |
| I | Discrepancy handling | **BLOCKED** — do not implement until Section 3.5's open items (shortage/damage recording) are resolved; the "indefinite partial receipt" *fact* itself is already covered by Session G | N/A for the unresolved portion | N/A | N/A | N/A | N/A | Implementing a guessed shortage/damage mechanism |
| J | Duplicate receipt | Unchanged from M24 discovery Session I | Idempotent by `client_transaction_id` | SHIPPED transfer | same receipt request twice | Exactly one receipt/journal | Service | Regression |
| K | Duplicate shipment | Unchanged from M24 discovery Session J | Idempotent by `ship_client_transaction_id` | DRAFT transfer | same ship request twice | Exactly one shipment/journal | Service | Regression |
| L | Concurrent shipment | Unchanged from M24 discovery Session K | Header lock serializes | DRAFT transfer, two threads | simultaneous `ship_transfer` | Exactly one succeeds | Concurrency | Regression |
| M | Concurrent receipt | Unchanged from M24 discovery Session L | Header lock + per-line remaining-qty check | SHIPPED transfer, two threads | simultaneous `receive_transfer` | Never jointly over-receive | Concurrency | Regression |
| N | Shipment vs. cancellation | Unchanged from M24 discovery Session M | Header lock makes them mutually exclusive | DRAFT transfer, two threads | simultaneous ship/cancel | Exactly one succeeds | Concurrency | Regression |
| O | Shipment vs. loss/write-off | **BLOCKED** — same reason as F | N/A | N/A | N/A | N/A | N/A | Implementing a guessed policy |
| P | Store isolation | Unchanged from M24 discovery Session N, extended to cover the now-resolved creation policy explicitly | `caller_store_id` checks hold for every operation, including creation (Session A above) | Three stores | third-store user attempts every operation | Rejected/404 on every attempt | Service + adversarial | Regression |
| Q | GL reconciliation | Unchanged from M24 discovery Session O | `Σdebit == Σcredit`; GL matches subledger | Several transfers at various stages | run `trial_balance`, reconciliation | Zero discrepancy | Service | Regression |
| R | Inventory In Transit reconciliation | Unchanged from M24 discovery Session P | `gl_balance == outstanding_total` for genuinely outstanding lines | Multiple transfers at mixed stages | run the reconciliation | Exact match | Service | Regression |
| S | WAC/cost integrity | Unchanged from M24 discovery Session Q | Frozen cost never contaminated by later WAC changes | Source WAC changes between ship and receive | ship, change WAC, receive | Destination uses the frozen cost | Service | Regression |
| T | Accounting-period enforcement (extended) | Any future correction (if built) posts today and respects `_enforce_period_open`; **no correction path exists yet to test beyond this general assertion** | Section 3.9/3.10's resolved date policy | N/A until a correction mechanism exists | N/A | Assertion recorded as a requirement on whatever is eventually built, not testable today | Service | A future correction reverting to a historical-date pattern (the exact M23B-fixed mistake) |
| U | Mutation testing | Prove every resolved invariant above is actually enforced, not merely untested | See Section 13 | Baseline passing suite | apply one mutation, run the relevant session, revert, diff-verify | RED for the correct reason each time | Mutation | A passing suite that doesn't test what it claims |

18 sessions in this specification (A-U with E/F/I/O explicitly blocked
pending policy), on top of the 21 already designed in `docs/M24_DISCOVERY.md`
(which this document does not replace — both are the eventual M25 test
plan's two halves: discovery's sessions for the mechanics M24 traced,
this phase's sessions for what M24A actually resolved or left open).

## 13. Mutation-testing specification

Concrete future mutations, mapped to the session each must fail:

| Mutation | Target (once it exists) | Session that must catch it |
|---|---|---|
| Bypass transfer authorization (creation) | `_create_transfer_inner`'s store check | A, P |
| Remove source-store check | `ship_transfer`'s `_enforce_store_access` | P |
| Remove destination-store check | `receive_transfer`'s `_enforce_store_access` | P |
| Remove transfer state guard | `ship_transfer`/`receive_transfer`/`cancel_transfer` status checks | B, C, D, N |
| Allow duplicate shipment | `ship_transfer`'s idempotency check | K |
| Allow duplicate receipt | `receive_transfer`'s idempotency check | J |
| Remove row locking | transfer header / product locks | L, M, N |
| Alter transferred quantity | shipment/receipt line processing | G, Q, R |
| Alter frozen transfer cost | `unit_cost_at_shipment` capture/reuse | S |
| Omit Inventory In Transit journal leg | `post_transfer_shipment_journal`/`post_transfer_receipt_journal` | Q, R |
| Reverse Inventory In Transit debit/credit | same two functions | Q, R |
| Omit loss expense (once built) | future loss-posting function | F |
| Post loss to wrong store (once built) | future loss-posting function | F |
| Use original shipment date after period close (once built) | future loss-posting function | T |
| Bypass period-open enforcement | `_enforce_period_open` (shared, M22) | T |
| Permit receipt after loss (once built) | `receive_transfer`'s future status guard | F, O |
| Permit cancellation after terminal state | `cancel_transfer`'s state guard | D, N |
| Permit cross-store loss/write-off (once built) | future loss-posting function's store check | F, P |

18 mutation targets, matching or exceeding the brief's minimum list. None
executed in this phase.

## 14. Unresolved decisions

Per the brief's explicit list, marked `UNRESOLVED BUSINESS DECISION`:

- Loss ownership (economic responsibility — Section 3.2(b)).
- Write-off authority (Section 3.4).
- Post-shipment cancellation semantics (Section 3.3).
- Discrepancy policy (shortage/damage recording — Section 3.5).
- Transfer creation authorization — **NOT unresolved**; this document
  resolves it (Section 3.1). Listed here only to state explicitly that it
  is no longer open, since the brief names it among the items to check.
- Loss GL account (Section 3.8).
- Loss accounting date — **NOT unresolved**; resolved as `date.today()`
  (Section 3.9), listed here for the same explicit-check reason.
- Timeout policy (Section 3.6).
- Reconciliation requirement (mandatory vs. on-demand — Section 3.7).
- Period-close treatment — **partially resolved**: what the architecture
  already guarantees is settled (Section 3.10); whether to add a new
  blocking check is unresolved, tied to the reconciliation-requirement
  question above.

## 15. Risks and assumptions

- **Assumption:** the M8 design doc's own prose, read in full and in
  context, is sufficient authority to resolve Finding F7 without a fresh
  stakeholder conversation. Risk: if the original M8 author's intent was
  actually the stricter "both sides must consent" reading, this document's
  resolution would be wrong. Mitigation: the resolution is stated with its
  full reasoning and the alternate reading is named explicitly, so it can
  be revisited if a stakeholder disagrees.
- **Assumption:** reusing the established `date.today()` compensating-entry
  convention for any future transfer correction is safe to resolve now,
  without waiting for the correction mechanism itself to be designed.
  Risk: none identified — this policy is orthogonal to *whether* a
  correction mechanism is ever built, only to *when it would post* if one
  is.
- **Risk of leaving so much unresolved:** M25 cannot build a loss/write-off
  or post-shipment-cancellation feature from this document alone. This is
  the correct, evidence-based outcome given the repository's actual state,
  not a shortfall of this phase's effort — mirroring `docs/M23A_POLICY.md`'s
  own experience, where most areas resolved to "no code needed" and the one
  genuine implementation candidate was narrow and mechanical.
- **Assumption:** the reasoned (not decided) candidate of reusing
  `ACCOUNT_INVENTORY_SHRINKAGE_EXPENSE` for a future loss entry is
  presented only as context to speed up a future decision, not as a
  default that M25 may implement without explicit confirmation.

## 16. Explicit implementation stop conditions

M25 must stop and obtain an explicit policy decision before implementing
any of the following (all UNRESOLVED per Section 14):

- Loss ownership.
- Write-off authority.
- Post-shipment cancellation semantics.
- Discrepancy/shortage/damage recording policy.
- Loss GL account and its store attribution.
- In-transit timeout policy.
- Mandatory-reconciliation requirement and any period-close blocking tied
  to it.

M25 may proceed without further stops on: transfer creation authorization
(resolved, no change needed), the ordinary ship/receive/partial-receipt/
over-receipt/duplicate-request paths (all confirmed correct, no change
needed), the accounting-date convention for any future correction
(resolved as `date.today()`), and the multi-store/store-attribution model
(resolved, no change needed) — none of these require any M25 code change
either, since each was already correct.
