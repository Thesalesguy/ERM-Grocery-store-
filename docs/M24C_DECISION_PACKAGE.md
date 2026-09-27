# M24C: Inter-Store Transfer Policy Decision Package

**Status:** Documentation only. No production code, migration, seed data,
test, frontend, API behavior, permission, transfer state, inventory
movement, or GL behavior changed. Starting HEAD: `e273d8e` (M24B commit).

**Purpose:** M24B correctly identified seven business-policy decisions that
cannot safely be inferred from the repository. This document repackages
those seven decisions into a form a business stakeholder can read and
answer without inspecting source code. It does not resolve any of them. It
does not reopen anything M24A/M24B already resolved.

**How to use this document:** read each decision section once; for each,
you (the stakeholder) fill in the Decision Form near the end of that
section, or leave it blank if you are not ready to decide. Nothing here is
implemented until an answer is recorded. A blank field is not an approval
of anything, including the status quo.

---

## Resolved Technical Constraints — Not Stakeholder Decisions

These are fixed facts about the current system, established by M24A/M24B
with direct code or design-document evidence. They are listed here so the
seven decisions below can be read without re-litigating them. **None of
these is a question for you to answer.**

- Over-receipt (receiving more than was shipped) is prohibited today, with
  no tolerance band and no override permission anywhere in the system.
- Any accounting correction discovered after the fact posts using the
  current date, never the original transaction date.
- Every accounting period, once closed for a store, blocks any new posting
  into that closed date range — no exception exists anywhere in the system,
  and none is proposed here.
- Stores remain the only explicit accounting/reporting scope in this
  system. There is no centralized "company" or "organization" ledger above
  the store level.
- Introducing such a centralized entity is not being considered as part of
  this decision package.
- The existing mechanics for shipping and receiving a transfer (how
  inventory moves, how the in-transit holding account is debited and
  credited) are unchanged and are not part of this decision package.
- A transfer that has been shipped cannot currently be cancelled. Only a
  transfer that has not yet shipped can be cancelled.
- If an "aging" or "timeout" alert is ever built for transfers stuck in
  transit, it cannot, by itself, create an accounting entry or silently
  change a transfer's status — every accounting entry and every status
  change in this system today happens only as the direct result of an
  authenticated person's explicit action, never automatically.
- The existing report that compares the accounting balance for "goods in
  transit" against the underlying transfer records already exists and
  already works. Running it today does not create a record of who ran it
  or when.
- The company's profit-and-loss statement already automatically includes
  any new expense-type account that is created, with no additional
  configuration — so creating a new expense account for transfer losses (if
  Decision 5 leads to that) would not require any change to how profit and
  loss is calculated.

---

## Decision 1 — Economic Loss Ownership

**1. Decision ID:** D1

**2. Business question:** When goods shipped from one store to another are
lost or destroyed after they leave the source store but before the
destination store receives them, which store's profit-and-loss statement
absorbs the cost?

**3. Why the decision matters:** Someone's financial results will show this
loss as an expense. Which store's manager, budget, and performance
evaluation absorbs a shipment lost in transit — through no fault of either
store's day-to-day operations — is a real management decision with ongoing
financial consequences, not a bookkeeping technicality.

**4. Current system behavior:** No loss can currently be recorded at all —
the feature does not exist. If it is built, the software will be able to
record the loss only against the source store's books (see #5 below); it
cannot, as currently designed, split a single loss across two stores'
books in one entry.

**5. What the repository establishes:** A firm mechanical fact: when a
shipment leaves the source store, the value is recorded in a holding
account ("Inventory In Transit") against the **source store's** ledger. It
only moves to the destination store's ledger at the moment of an actual
receipt. So for any quantity never received, the only ledger that currently
holds the value — and therefore the only ledger a future write-off could
credit — is the source store's.

**6. What the repository does NOT establish:** Whether the source store
*should* be the one whose expenses absorb the loss. This system has no
concept of "shipping terms" (who bears risk while goods are in transit) and
no document anywhere states an intended policy. The mechanical fact in #5
is about where the accounting entry lives today, not about who should bear
the cost.

**7. Available policy choices:**
- **Source store** bears the loss (matches where the value already sits;
  requires no change to which store's ledger records the loss)
- **Destination store** bears the loss (would require moving the loss
  entry to a store's ledger that never held the goods)
- **Shared responsibility** (split between source and destination by some
  formula)
- **Another explicitly named business entity** (e.g., a central company
  office absorbs all transfer losses) — note: this system currently has no
  such entity to post to; adopting this option would require building one.

**8. Operational consequence of each choice:**
- *Source*: no new operational process needed; matches the physical
  reality that the source store had custody until receipt.
- *Destination*: the destination store's staff would be expensing a loss
  for goods they never physically held, which may complicate their
  understanding of their own store's performance.
- *Shared*: requires defining a split formula (50/50? by product value?)
  that does not exist anywhere in this system today.
- *Central entity*: requires building a new kind of ledger scope that does
  not exist in this system at all.

**9. Accounting consequence of each choice:**
- *Source*: one journal entry, posted to the source store, matching every
  other single-store financial event in this system.
- *Destination*: would require either (a) a cross-store entry (a shape this
  system has never used for any transaction) or (b) first moving the
  in-transit value to the destination's books through some new mechanism
  before expensing it there.
- *Shared*: would require two linked entries, a shape this system does not
  currently use anywhere.
- *Central entity*: would require a new ledger scope this system does not
  have.

**10. Inventory consequence of each choice:** None differ — no physical
inventory movement is created under any of these choices, because there is
nothing to move (the goods are gone, not misplaced on a shelf).

**11. Multi-store/reporting consequence:** Under every choice, both the
source and destination store identities remain permanently recorded on the
underlying transfer record and can be shown together in any report,
regardless of which store's ledger absorbs the actual expense.

**12. Security/authorization consequence:** Whichever store's ledger
records the loss most plausibly determines which store's staff need
authorization to declare it (see Decision 2) — but this is not decided
here.

**13. Implementation consequence:** *Source* requires no change to how the
system currently attributes the in-transit balance. *Destination*, *shared*,
and *central entity* each require new mechanisms this system does not
currently have, in increasing order of complexity.

**14. Exact stakeholder decision required:** Confirm whether the stores in
this business are financially independent of one another (in which case
this is a real cost-allocation decision with consequences) or are simply
internal reporting divisions of one business (in which case which store's
books show the expense has no real economic stakes, only a bookkeeping
convention). Then choose one of the four options in #7, or state a
different allocation rule.

### Decision Form — D1

```
Decision: Economic loss ownership
Chosen policy:
Authorization:
Threshold:
Accounting treatment:
Inventory treatment:
Store attribution:
Period-close treatment:
Audit requirement:
Additional notes:
```

---

## Decision 2 — Write-Off Authority

**1. Decision ID:** D2

**2. Business question:** If the business decides to build a way to
formally declare a shipment lost and write off its value, who is allowed to
do that, and under what conditions?

**3. Why the decision matters:** A write-off permanently reduces the
recorded value of company assets and creates a real expense. Getting the
authorization wrong risks either (a) making it too hard to ever clean up a
genuine loss, or (b) letting someone who mishandled or misplaced stock
quietly reclassify their own mistake as an unavoidable "lost in transit"
loss with no oversight.

**4. Current system behavior:** No write-off mechanism exists. No
permission for it exists. Nothing can be inferred by looking at what is
already permitted, because nothing like it has been built yet.

**5. What the repository establishes:** Two narrow, related facts:
- Every irreversible financial correction this system has ever built
  requires the person performing it to type a mandatory explanation
  ("reason"). A write-off, being irreversible and financial, would fit that
  same pattern.
- If a write-off is ever reversed (e.g., the goods turn up after all), the
  system's established approach is to post a brand-new, separate entry
  undoing it — never to edit or delete the original entry. Financial
  entries in this system are never edited or deleted once posted.

**6. What the repository does NOT establish:** Which role(s) may perform a
write-off; whether the acting person needs authorization tied to the
source store, the destination store, or either; whether both stores must
agree; whether a manager may write off their own store's loss without a
second person's sign-off; whether there is a dollar or quantity ceiling
above which a higher authority must approve; and whether reversing a
write-off needs extra sign-off beyond what created it.

**7. Available policy choices:**
- **Role:** any existing role that already manages inventory day-to-day
  (e.g., Manager, Inventory Clerk), vs. a new, more restricted role created
  specifically for this action, vs. an existing "admin"-level role only.
- **Store scope:** source-store authorization only, destination-store
  authorization only, either store, or both stores' sign-off required.
- **Self-approval:** permitted, vs. prohibited (someone other than the
  person who shipped/handled the loss must authorize it).
- **Thresholds:** no threshold (any amount may be written off by an
  authorized person), vs. a monetary or quantity ceiling above which
  additional approval is required.
- **Reversal:** write-offs may never be reversed, vs. may be reversed by
  the same authority, vs. may be reversed only by a higher authority.

**8. Operational consequence of each choice:** A narrower authorization
(new dedicated role, both-store sign-off, self-approval prohibited)
increases oversight but slows down legitimate write-offs and may require
staff coordination across two stores. A broader authorization (existing
inventory roles, either store, self-approval allowed) is faster to operate
but concentrates more trust in day-to-day staff.

**9. Accounting consequence of each choice:** None of these choices change
the shape of the resulting journal entry (see Decision 5) — authorization
only governs who may trigger it, not what it produces.

**10. Inventory consequence of each choice:** None — no inventory movement
is created by a write-off under any authorization model (see Decision 5,
#10 there).

**11. Multi-store/reporting consequence:** If both-store sign-off is
required, this would be the first feature in this system requiring two
different stores to jointly approve a single action; every existing
two-party transfer interaction today lets each side act unilaterally on
its own portion.

**12. Security/authorization consequence:** This decision directly
determines what new permission (if any) must be created and which existing
roles, if any, would hold it. No permission is created in this document.

**13. Implementation consequence:** A new, narrower permission is the most
likely shape needed if any restriction beyond existing inventory
permissions is wanted, but this is not decided or built here.

**14. Exact stakeholder decision required:** Name the role(s) authorized to
declare a write-off; state whether source-store, destination-store, or
both-store authorization is required; state whether self-approval is
permitted; state whether any monetary/quantity threshold applies and if so
what it is; state whether reversal requires the same or a higher authority.

### Decision Form — D2

```
Decision: Write-off authority
Chosen policy:
Authorization:
Threshold:
Accounting treatment:
Inventory treatment:
Store attribution:
Period-close treatment:
Audit requirement:
Additional notes:
```

---

## Decision 3 — Post-Shipment Reversal

**1. Decision ID:** D3

**2. Business question:** Once a transfer has been shipped, should there
ever be a way to bring it back — i.e., is a "recall" or "return before
arrival" a real operational need for this business?

**3. Why the decision matters:** Today, once a transfer ships, it can never
be cancelled — only fully received, or left permanently in transit. If the
real-world business sometimes needs to pull back a shipment before it
arrives (e.g., wrong destination, wrong order), the system currently offers
no way to represent that, and staff would have to work around it manually.

**4. Current system behavior:** Cancellation is only possible before a
transfer ships. After shipment, any attempt to cancel is rejected outright.

**5. What the repository establishes:** This prohibition is a deliberate,
documented design choice, not an oversight. It was explicitly deferred as a
"future milestone's concern" when the transfer system was originally built.
Separately, this codebase has an existing, working pattern for a related
real-world situation — "goods that were already received are physically
sent back" — which is handled by creating an entirely new document that
references the original, rather than editing the original. If a
"post-shipment return" feature is ever built here, that same shape (a new
document, not an edit) is the only pattern this codebase has ever used for
anything resembling it.

**6. What the repository does NOT establish:** Whether this business's
actual shipping operations ever encounter a situation where a shipment
needs to be recalled before arrival, as opposed to the practical reality
always being "the goods are gone (lost, delivered, or consumed) — account
for it going forward" rather than "bring them physically back."

**7. Available policy choices — two fundamentally different concepts:**

- **A. Shipped is final.** A transfer that has shipped can never be
  cancelled or reversed as a matter of policy. Any unresolved goods (lost,
  damaged, or simply undelivered) are handled exclusively through the
  receiving/loss processes covered by Decisions 1, 2, and 4 — never through
  anything resembling "cancellation."
- **B. Physical return is supported.** A shipped transfer's goods may be
  physically sent back to the source through an explicit return workflow.
  **This is not the same as changing the transfer's status from "shipped"
  back to "cancelled."** It is a materially different, separate
  undertaking: a brand-new document representing the reverse physical
  movement and its own accounting entries, layered on top of the original
  transfer (which itself remains permanently in its shipped state,
  unchanged).

**8. Operational consequence of each choice:**
- *A*: simpler; matches current behavior; staff never expect a "recall"
  button to exist.
- *B*: requires staff to initiate and process an entirely new kind of
  transaction (a reverse shipment/receipt) whenever goods are physically
  sent back — real new operational steps, not a status flip.

**9. Accounting consequence of each choice:**
- *A*: no new accounting mechanism related to reversal; any financial
  effect of an unresolved shipment comes only from Decision 1/5's loss
  mechanism, if built.
- *B*: an entirely new pair of accounting entries (a normal shipment-style
  entry and a normal receipt-style entry, in the reverse direction),
  reusing existing accounting mechanics — no loss is recognized, since the
  goods physically came back.

**10. Inventory consequence of each choice:**
- *A*: none.
- *B*: an ordinary pair of inventory movements identical in shape to any
  other shipment and receipt, just in the reverse direction — no new kind
  of inventory movement is required.

**11. Multi-store/reporting consequence:** Under B, both the original
transfer and the new return document remain visible, each carrying both
stores' identities, so nothing is lost from either store's records; the
two documents are linked by reference but each is its own complete record.

**12. Security/authorization consequence:** Under B, who may initiate a
return is a new, currently undecided question (not addressed by this
document); it would need its own authorization decision, likely similar in
shape to who may create an ordinary transfer.

**13. Implementation consequence:** *A* requires no new code. *B* requires
a new document type and workflow, though it can reuse this system's
existing shipment/receipt mechanics rather than inventing new ones.

**14. Exact stakeholder decision required:** State whether this business's
actual inter-store logistics operation ever needs to physically recall a
shipment before it arrives. If yes, choose option B and identify who
should be authorized to initiate a return. If no, option A applies and no
further action is needed on this decision.

### Decision Form — D3

```
Decision: Post-shipment reversal (physical return)
Chosen policy: [ A - shipped is final  |  B - physical return supported ]
Authorization:
Threshold:
Accounting treatment:
Inventory treatment:
Store attribution:
Period-close treatment:
Audit requirement:
Additional notes:
```

---

## Decision 4 — Shortage / Damage / Partial Receipt

**1. Decision ID:** D4

**2. Business question:** When the quantity a destination store actually
receives doesn't match what was shipped — because of shortage, damage, or
an incomplete delivery — how should that mismatch be recorded, and does it
require investigation before it is treated as a real financial loss?

**3. Why the decision matters:** Today, a partial receipt (receiving less
than shipped) leaves the remainder in permanent limbo — indistinguishable
from "the rest is still on its way." Without a decision here, damaged or
short deliveries have no formal path to resolution, and staff have no
guidance on when to treat a shortfall as an actual loss versus waiting
longer.

**4. Current system behavior — by scenario:**

| Scenario | What happens today |
|---|---|
| Shipped 10 / received 8 | The line stays "partially received" indefinitely. The system cannot tell "the other 2 are still coming" from "the other 2 are gone for good." |
| Shipped 10 / received 8, with 2 later found or lost | No mechanism exists to declare the missing 2 as found or as permanently lost — it simply stays in the same indefinite state above. |
| Shipped 10 / damaged before receipt | The receiving staff can only either receive the full quantity (as if undamaged) or not receive it — there is no way to receive a quantity while flagging it as damaged. Not receiving it falls into the same indefinite state as the shortage scenario. |
| Shipped 10 / partial receipt followed by an unresolved remainder | Same indefinite state as above — receiving a partial amount never marks anything as "final." |
| Damage discovered after receipt | **This already has a complete, working solution today** — once goods are formally received, an ordinary stock adjustment (the same tool used for any other post-receipt inventory discrepancy, e.g., a shelf count) fully handles it. No transfer-specific change is needed for this case; it is not part of this decision. |

**5. What the repository establishes:** The over-receipt case (receiving
*more* than shipped) is already fully resolved and prohibited outright,
with no tolerance or override — this is settled and is not part of this
decision. Damage discovered *after* a normal receipt is already fully
handled by the ordinary inventory-adjustment tool this system already has
for any stock discrepancy — also settled, and not part of this decision.

**6. What the repository does NOT establish:** Whether a shortage or
damage discovered *before or during* receipt should ever be automatically
treated as a financial loss, or whether it must first go through some kind
of investigation or confirmation step before any financial effect is
recorded. No such investigation workflow exists anywhere in this system
today for any purpose.

**7. Available policy choices, for each open scenario (shortage/damage
discovered at or before receipt, and an indefinitely partial remainder):**
- **Accept the discrepancy immediately** and create an explicit loss/damage
  recording workflow tied to Decisions 1/2/5 (no investigation required
  before financial recognition).
- **Require investigation first** — the shortfall is flagged but not
  financially recognized until someone confirms it is a genuine,
  unrecoverable loss (this system has no workflow/task-assignment concept
  today to enforce such an investigation step).
- **Another defined process** you specify.

**8. Operational consequence of each choice:** Immediate acceptance is
faster for staff but risks premature or inaccurate loss recognition
(e.g., recording a loss for goods that later turn up). Requiring
investigation is more accurate but requires building a process this system
does not have any equivalent of today, and slows down closing out the
transfer.

**9. Accounting consequence of each choice:** Immediate acceptance ties
directly into Decision 5's loss-posting mechanism as soon as the shortfall
is confirmed. Requiring investigation means no accounting entry is made
until that investigation concludes — the in-transit balance would remain
open in the meantime, exactly as it does today.

**10. Inventory consequence of each choice:** Neither choice requires a new
kind of inventory movement — a loss, once confirmed, has no physical
inventory-side effect to record (the goods were never on the destination's
shelf and are already correctly off the source's shelf).

**11. Multi-store/reporting consequence:** Under either choice, the
shortfall/damage record would carry both stores' identities, matching how
every other transfer-related record already works.

**12. Security/authorization consequence:** Whoever can confirm a
shortage/damage as a final, financially-recognized loss is governed by
Decision 2's authorization answer — this decision does not introduce a
separate authorization question, only the trigger for when Decision 2's
authority is invoked.

**13. Implementation consequence:** "Accept immediately" is a
straightforward extension of the future loss-recording mechanism (Decision
5). "Require investigation" would require building an entirely new
holding/flagging concept this system has never had for any purpose.

**14. Exact stakeholder decision required:** Describe how receiving staff
actually handle a short or damaged delivery today, outside this system
(is a shortfall usually obvious and immediate, or does it typically require
follow-up with the source store or a carrier before anyone is confident
it's a real loss?). Then choose whether the system should record such a
shortfall as an immediate loss candidate or require a confirmation step
first.

### Decision Form — D4

```
Decision: Shortage / damage / partial-receipt handling
Chosen policy:
Authorization:
Threshold:
Accounting treatment:
Inventory treatment:
Store attribution:
Period-close treatment:
Audit requirement:
Additional notes:
```

---

## Decision 5 — Loss GL Account and Attribution

**1. Decision ID:** D5

**2. Business question:** When a transfer loss is financially recognized,
what kind of expense is it, which store's financial statements show it,
and should it use a brand-new expense category or an existing one?

**3. Why the decision matters:** This decision directly shapes the
company's expense reporting going forward — a new expense category is
permanent bookkeeping structure, and misclassifying transfer losses (e.g.,
blending them into an unrelated existing expense category) would make it
harder to see this specific cost clearly in the future.

**4. This decision depends on Decision 1.** The store attribution question
in this decision cannot be finally settled independent of Decision 1's
answer (who economically bears the loss).

**5. What the repository establishes:** A confirmed loss, once
financially recognized, requires two things to happen together: (a) the
"goods in transit" holding-account balance must be cleared to zero for the
lost quantity, and (b) a corresponding financial expense must be
recognized somewhere. There is no way to do (a) without also doing (b) —
the system does not allow silently zeroing out an asset without recording
where the value went.

**6. What the repository does NOT establish:** Which specific expense
account should be used, and which store's books it lands on economically
(this second question is Decision 1, not repeated here).

**7. Available policy choices:**
- **Whether it is a P&L (profit-and-loss) expense:** yes, necessarily —
  this is not actually a choice; any loss recognized this way is
  structurally an expense the moment it is recorded, the same way every
  other expense in this system works.
- **Economic category:** an operating expense representing inventory value
  permanently lost — comparable in kind to the two existing categories this
  system already uses for other kinds of discrepancies (inventory
  shrinkage from stock counts, and cash-drawer variances).
- **Store attribution:** follows whatever Decision 1 resolves.
- **Whether a dedicated transfer-loss account is required, or an existing
  expense category is acceptable:** this system's established pattern, when
  it previously faced an economically similar but distinct kind of
  discrepancy (cash-drawer variance vs. stock-count shrinkage), was to
  create a brand-new, dedicated account for the new discrepancy source
  rather than reuse the existing one — even though both were, in the
  abstract, "an expensed discrepancy." A transfer loss is again a distinct
  source (an in-transit logistics failure) from either of those two. This
  pattern is offered as evidence of how this system has consistently
  handled this exact kind of question before; it does not itself decide
  the question for you.

**8. Operational consequence of each choice:** A dedicated account gives
management a clear, isolated view of exactly how much money is lost to
transfer failures specifically. Reusing an existing account is simpler to
set up but blends transfer losses into a category that already means
something else, making it harder to track this specific risk over time.

**9. Accounting consequence of each choice:** A dedicated account requires
creating one new line in the chart of accounts (a one-time setup task, not
a policy question in itself). Reusing an existing account requires no new
setup but permanently mixes this cost into that account's existing meaning.
Either way, this expense automatically flows into the company's existing
profit-and-loss report with no other changes needed.

**10. Inventory consequence of each choice:** None — no inventory movement
is created under either choice; there is nothing physical to move (the
loss already happened; the physical stock records are already correct).

**11. Multi-store/reporting consequence:** Whichever store's ledger records
the expense (per Decision 1), both stores' identities remain visible in the
underlying transfer record for reporting purposes regardless of which
store's profit-and-loss statement absorbs it.

**12. Security/authorization consequence:** None beyond Decision 2's
answer — creating the journal entry is governed by who is authorized to
declare the write-off, not by which account is used.

**13. Implementation consequence:** Creating a new dedicated expense
account is a small, mechanical setup task once this decision and Decision 1
are both resolved — it does not require any change to how the
profit-and-loss report itself is calculated.

**14. Exact stakeholder decision required:** Decide whether transfer losses
should get their own dedicated expense category or be folded into an
existing one, and (pending Decision 1) which store's financial statements
should show it. Do not decide the specific account name/number here — that
is a routine bookkeeping detail to finalize once these policy questions are
settled, not a business decision.

### Decision Form — D5

```
Decision: Loss GL account and attribution
Chosen policy:
Authorization:
Threshold:
Accounting treatment:
Inventory treatment:
Store attribution:
Period-close treatment:
Audit requirement:
Additional notes:
```

---

## Decision 6 — In-Transit Timeout

**1. Decision ID:** D6

**2. Business question:** Should the system flag a shipment that has been
"in transit" for an unusually long time without being received, and if so,
how long is too long?

**3. Why the decision matters:** Without any aging awareness, a shipment
can sit unreceived forever with nobody prompted to look into it. Right now
finding these requires someone to proactively run a report and think to
check — nothing surfaces them automatically.

**4. Current system behavior:** A shipment can remain "in transit"
indefinitely with no system-driven consequence of any kind. Detection is
possible today only if someone manually runs the existing "goods in
transit" reconciliation report and notices an old-looking line.

**5. What the repository establishes:** If any aging/timeout mechanism is
ever built, it must be limited to alerting/reporting only — **it cannot, by
itself, automatically change the transfer's status or automatically create
an accounting entry.** This is not a design choice up for debate here; it
follows from the fact that absolutely nothing in this system's entire
history has ever posted a financial entry or changed a record's status
without a specific person's direct, in-the-moment action.

**6. What the repository does NOT establish:** Whether a timeout concept
is needed at all for this business, and if so, any specific duration.

**7. Available policy choices:**
- **No timeout concept** — rely entirely on manual, on-demand reporting, as
  today.
- **An aging threshold with an alert**, of a duration you specify.

If a threshold is wanted, sub-choices include:
- Duration (e.g., number of days).
- Calendar days vs. business days.
- Whether the threshold should differ by route (which store pairs) or be
  uniform.
- Who receives the alert (this system has no existing concept of an
  alert recipient for an inventory/financial exception to reuse).
- What happens after the alert (does it merely inform, or does it require
  someone to formally investigate?).
- Whether a transfer may remain unresolved indefinitely even after
  repeated alerts.

**8. Operational consequence of each choice:** No timeout means staff must
remember to check manually. A timeout with alerts adds visibility but
requires deciding who is responsible for acting on the alert, and building
a notification mechanism this system does not currently have for this
purpose.

**9. Accounting consequence of each choice:** None, under either choice —
an alert, however it is built, cannot itself create or change any
accounting entry (see #5). Any actual financial effect still requires a
human to act under Decision 2's authority.

**10. Inventory consequence of each choice:** None.

**11. Multi-store/reporting consequence:** Whatever threshold and scope are
chosen, both stores on a stale transfer remain visible in the existing
drill-down report regardless of any alerting mechanism.

**12. Security/authorization consequence:** None directly — an alert
recipient is not the same as someone authorized to act on it; acting still
requires whatever Decision 2 resolves.

**13. Implementation consequence:** **An alert-only implementation and an
implementation that changes accounting or transfer state are materially
different scopes.** The former is a reporting/notification feature with no
dependency on Decisions 1, 2, or 5. The latter would require those
decisions to be resolved first and is out of scope for this decision alone.

**14. Exact stakeholder decision required:** Provide typical transit-time
expectations for shipments between this business's stores, so a meaningful
threshold (if any) can be set. State whether a timeout concept is wanted at
all, and if so, the duration and who should be alerted.

### Decision Form — D6

```
Decision: In-transit timeout
Chosen policy:
Authorization:
Threshold:
Accounting treatment:
Inventory treatment:
Store attribution:
Period-close treatment:
Audit requirement:
Additional notes:
```

---

## Decision 7 — Mandatory Reconciliation / Period Close

**1. Decision ID:** D7

**2. Business question:** Should regularly reconciling the "goods in
transit" balance be a required, tracked control — and if an unresolved
balance exists, should that ever prevent closing the books for an
accounting period?

**3. Why the decision matters:** Right now, nothing requires anyone to
check whether the "goods in transit" balance makes sense, and nothing stops
an accounting period from closing regardless of how large or old that
balance is. If the business wants stronger assurance that in-transit
shipments are being actively tracked, this needs to become a defined,
required control rather than an optional report someone might run.

**4. Present the existing facts:**
- The comparison report ("Inventory In Transit reconciliation") that checks
  whether the accounting balance matches the underlying transfer records
  already exists and works today.
- It is currently read-only — running it changes nothing.
- It does not itself create any audit record — running it today leaves no
  trace of who ran it or when.
- Closing an accounting period currently does not depend on transfer state
  in any way — a period can be closed with any amount of unresolved
  "goods in transit" balance outstanding, with no warning.
- Nothing here (M24B or this document) has established that reconciliation
  must ever block closing a period — that remains a live, open question.

**5. What the repository does NOT establish:** Whether reconciliation
should be mandatory at all; how often it should occur; who is responsible
for performing it; whether an unresolved balance should trigger escalation;
whether a formal audit record of each reconciliation should be required;
and whether an unresolved balance should ever be allowed to block period
close.

**6. Available policy choices:**
- **Mandatory or not:** reconciliation stays optional/on-demand (as today),
  vs. becomes a required, scheduled activity.
- **Frequency**, if mandatory (e.g., monthly, per period-close cycle).
- **Responsible role** for performing it.
- **Escalation:** whether an unresolved balance requires notifying someone
  further up, and if so whom.
- **Audit record:** whether performing reconciliation should leave a formal
  record (it does not today).
- **Scope:** store-level or centralized (the underlying report today is
  centralized across all stores only, with a store-level drill-down view
  available for those who want a narrower look).
- **Period-close blocking:** whether an unresolved "goods in transit"
  balance may ever block closing a period, and if so, whether that
  applies to *any* outstanding balance or only to balances that are aged
  or specifically disputed.

**7. Operational consequence of each choice:** Making reconciliation
mandatory adds a real recurring task someone must own and be accountable
for. Allowing it to block period close adds a hard stop that could delay
routine month-end closing if a transfer investigation (Decision 4) is still
unresolved.

**8. Accounting consequence of each choice:** If period-close blocking is
adopted, this would be the first time closing a period for a store depends
on anything outside that store's own accounting records — today, closing a
period only checks the date range and that store's own history, nothing
from any other part of the system.

**9. Inventory consequence of each choice:** None directly — this decision
concerns process and control, not inventory movement.

**10. Multi-store/reporting consequence:** If reconciliation becomes
mandatory and centralized (matching the report's current scope), it would
be evaluated across all stores together rather than one store at a time,
unless you specify a store-level requirement instead.

**11. Security/authorization consequence:** Whoever is designated
responsible for performing reconciliation is a new role assignment this
document does not decide.

**12. Implementation consequence:** Making reconciliation "mandatory" as a
policy is primarily a process/procedure matter and does not, by itself,
require a code change (the report already exists). Adding a period-close
block, by contrast, would be a new kind of software dependency this system
has never had — today, closing a period for one area of the business checks
nothing outside itself.

**13-14. Exact stakeholder decision required:** State whether the business
wants "goods in transit" treated as a routine report available on request
(today's behavior) or as a formal, enforced control. If the latter, specify
frequency, the responsible role, whether escalation and a formal audit
record are required, and — separately and explicitly — whether an
unresolved balance should ever be allowed to block period close, and for
which balances (all outstanding balances, or only aged/disputed ones).

### Decision Form — D7

```
Decision: Mandatory reconciliation / period-close effect
Chosen policy:
Authorization:
Threshold:
Accounting treatment:
Inventory treatment:
Store attribution:
Period-close treatment:
Audit requirement:
Additional notes:
```

---

## Cross-Decision Dependencies

```
D1 (loss ownership) ─────────────► D5 (loss GL account & attribution)
     │
     └── together with D2 (write-off authority) ─► the loss/write-off
                                                     workflow as a whole

D3 (post-shipment reversal) ─────► scope of any physical-return
                                    implementation, if built

D4 (shortage/damage/partial) ────► scope of any receiving/discrepancy
                                    workflow, if built

D6 (in-transit timeout) ─────────► scope of any aging/alert infrastructure,
                                    if built

D7 (mandatory reconciliation) ───► reconciliation controls, and possibly
                                    a period-close dependency, if built
```

| Dependency | Description |
|---|---|
| D1 → D5 | The loss GL account's store attribution cannot be finalized until D1 (who economically bears the loss) is answered. The account's existence and category can be discussed independent of D1, but final attribution cannot. |
| D1 + D2 → loss/write-off workflow | The entire loss/write-off feature (Decisions 1, 2, and 5 together) cannot be built until both who bears the loss (D1) and who may declare it (D2) are answered. |
| D3 → physical-return scope | Whether any post-shipment reversal work is built at all depends entirely on D3; if D3 selects option A ("shipped is final"), no implementation work follows from this decision at all. |
| D4 → discrepancy/receiving workflow | The shape of any shortage/damage recording mechanism depends on D4's choice between immediate acceptance and a required investigation step. |
| D6 → aging/alert infrastructure | Whether any alerting mechanism is built, and its shape, depends entirely on D6. |
| D7 → reconciliation controls + period-close | Whether reconciliation becomes a tracked, mandatory process, and separately whether it ever blocks period close, both depend on D7 alone. |

**Decisions that cannot be finalized independently:** D5 cannot be fully
finalized without D1 (store attribution). The overall loss/write-off
feature cannot be built without both D1 and D2 resolved together, and D4's
outcome (if it produces confirmed losses) feeds into that same feature via
D1/D2/D5. D3, D6, and D7 are each independently decidable and do not block
or depend on any other decision in this package.

---

## Implementation Consequence Matrix

This table lets the eventual M25 implementation scope be generated directly
from your answers. It does not rank any option.

| Decision | If selected policy A | If selected policy B | Code impact | Migration impact | Test impact |
|---|---|---|---|---|---|
| D1 — Loss ownership | Source store bears loss | Destination store (or shared/central entity) bears loss | A: no change to existing in-transit posting logic. B: new cross-store or value-transfer mechanism needed before expensing. | A: none. B: potentially new table/column to support a value-transfer step. | A: straightforward single-store journal test. B: new tests for a cross-store or transfer-before-expense mechanism. |
| D2 — Write-off authority | Existing inventory roles, either-store, self-approval allowed | New dedicated role, both-store sign-off, self-approval prohibited | A: reuse existing permission-check patterns. B: new permission definition, plus a two-party-approval mechanism not used anywhere else today. | A: new permission constant (data migration for permission tables). B: same, plus schema for a pending-approval state. | A: standard authorization tests, mirroring existing patterns. B: new tests for dual-approval and self-approval rejection, a first-of-its-kind scenario. |
| D3 — Post-shipment reversal | Shipped is final (no new mechanism) | Physical return supported | A: no new code. B: new document/workflow reusing existing ship/receive mechanics. | A: none. B: new table for the return document (or reuse of the existing transfer table with a linking reference). | A: none needed beyond existing regression coverage. B: full new session set for the return lifecycle. |
| D4 — Shortage/damage/partial | Accept discrepancy immediately | Require investigation before recognition | A: extends the future loss-recording mechanism (D1/D2/D5) directly. B: requires an entirely new holding/flagging concept this system has never had. | A: minimal, tied to whatever D1/D2/D5 already require. B: new table(s) for tracking an investigation state. | A: reuses D1/D2/D5's test coverage. B: new tests for the investigation workflow, including how/when it resolves to a final loss. |
| D5 — Loss GL account | New dedicated expense account | Reuse an existing expense account | A: create one new chart-of-accounts row (mechanical, not a formula change). B: no new account, but blends this cost into an existing category's meaning. | A: one new seed/account-creation migration. B: none. | A: test the new account is correctly picked up by profit-and-loss with no formula change. B: test the existing account correctly reflects the added cost. |
| D6 — In-transit timeout | No timeout concept | Aging threshold with alert | A: no new code. B: new alert/reporting feature (a scheduled check), explicitly alert-only — never state- or accounting-mutating. | A: none. B: possibly a new table to track alert state/history. | A: none needed. B: new tests proving the alert never mutates state or posts accounting entries. |
| D7 — Mandatory reconciliation / period close | Reconciliation stays informational, no period-close effect | Reconciliation becomes mandatory and/or blocks period close | A: no code change — the existing report already satisfies this. B: new dependency added to period-close logic, the first time closing a period depends on another module's state — a materially larger change than a single check might suggest. | A: none. B: possibly a new table to track reconciliation completion/audit records. | A: none needed. B: new tests for the period-close block, including edge cases (aged vs. fresh balances, if scoped that way). |

---

## M25 Stop Conditions

M25 must not implement any of the following until the corresponding policy
above is explicitly resolved by a stakeholder:

- Transfer-loss states (any `LOST`/`WRITTEN_OFF`-shaped status or record) —
  requires D1, D2, and D4 resolved.
- Write-off permissions — requires D2 resolved.
- Transfer-loss GL accounts — requires D1 and D5 resolved.
- Shortage accounting (any mechanism recognizing a shortfall as a loss) —
  requires D4 resolved.
- Post-shipment return workflow — requires D3 resolved.
- Timeout thresholds (any aging/alert mechanism) — requires D6 resolved.
- Reconciliation period-close blocking — requires D7 resolved.

---

## Stakeholder Answer Format

Fill in each field below directly, or leave blank if not ready to decide.
**A blank field means "not yet decided," never "approved as-is."**

```
D1 — Economic loss owner:
[ ]

D2 — Write-off authority:
[ ]

D3 — Post-shipment physical return required?
[ ]

D4 — Shortage/damage handling:
[ ]

D5 — Transfer-loss accounting category:
[ ]

D6 — In-transit timeout:
[ ]

D7 — Mandatory reconciliation / period-close effect:
[ ]
```
