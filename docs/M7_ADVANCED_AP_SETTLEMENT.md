# M7 — Advanced AP Settlement, Supplier Credits & AP Hardening

## Phase 0 — Design decisions (resolved before any code was written)

M6 built a *correct but simplified* AP model: one invoice ↔ exactly one PO
(header FK), one payment ↔ exactly one invoice (header FK), FIFO receipt-lot
matching computed on the fly (never persisted), and no supplier-credit
concept at all. Every one of those simplifications was a genuine, documented
M6 scope decision — not a bug — but they are exactly the assumptions that
break once a real business runs multiple invoices per PO, pays several
invoices with one cheque, or gets a supplier credit note. M7 removes those
specific assumptions. It does **not** touch anything else in M0–M6 that
still works: goods receipt accounting, WAC, COGS, the sales side, and the
core `_post_journal` engine are unchanged.

Answers to the 18 required questions:

1. **Can one invoice reference multiple POs?** Yes. `purchase_invoices.purchase_order_id`
   becomes **nullable** and is now only a display/filtering convenience
   ("primary PO"), never a validation boundary. Each `PurchaseInvoiceLine`
   still points at one `purchase_order_item_id`, and different lines on the
   same invoice may belong to different purchase orders — validated
   per-line (the line's PO must share the invoice's `store_id` and
   `supplier_id`), never by requiring one shared header PO.
2. **Can one invoice reference multiple receipts?** Yes — already true in
   M6 (a PO item can be received across several `GoodsReceipt`s and one
   invoice line can draw from more than one of those lots via FIFO). M7
   makes this **explicit and persisted** instead of recomputed: a new
   `purchase_invoice_receipt_matches` table records, per invoice line, the
   exact `goods_receipt_item_id`, `matched_quantity`, `matched_unit_cost`,
   and `variance_amount` consumed from each lot.
3. **Can one PO have multiple invoices?** Yes — already true in M6 (no
   exclusivity constraint). Unchanged, now exercised explicitly by tests.
4. **Can one receipt be split across invoices?** Yes — the FIFO
   lot-consumption algorithm (`already_invoiced_qty` skip-ahead) already
   allowed this; matches are now persisted per lot so it is provable, not
   just algorithmically true.
5. **How is invoice quantity matched?** Unchanged FIFO-by-receipt-order
   algorithm, ceiling = that PO item's `quantity_received` (never
   `quantity_ordered`). The only change is that the walk's per-lot results
   are now written to `purchase_invoice_receipt_matches` instead of only
   being summed into a total.
6. **How are invoice price variances represented?** Unchanged: `(invoiced
   unit price − matched receipt-lot unit cost) × matched quantity`, summed
   and posted automatically to `Purchase Price Variance` (5100) — still a
   pure derived fact from data that already exists, not an estimate. Now
   also stored per-lot on the match row for full traceability (M7 Section
   1's "a matching record should preserve... variance").
7. **How are unmatched invoices represented?** A line whose PO item cannot
   supply the requested quantity from receipt data raises
   `MATCH_DATA_INCONSISTENT` (a defensive, should-be-unreachable guard). A
   line that would push `quantity_invoiced` past `quantity_received` raises
   `OVER_INVOICING` — a **hard, synchronous, visible rejection** at post
   time, not a silently-accepted posting and not a queued approval
   workflow. This is the same M6 decision, reaffirmed: it satisfies "an
   exception must be visible to authorized users" (it blocks the action and
   returns a specific error code the UI surfaces immediately) without
   inventing a new stateful approval-queue entity for a condition that is
   deliberately never allowed to post in the first place. A full
   asynchronous exception-approval workflow is **deferred** (see
   "Deferred").
8. **Can one supplier payment settle multiple invoices?** Yes — new.
   `SupplierPayment` becomes a payment header; a new
   `supplier_payment_allocations` table (one row per invoice it settles)
   replaces the old single `purchase_invoice_id` FK. One payment still
   posts exactly **one** accounting journal (Dr AP for the payment's total,
   Cr Cash/Bank) — the allocation rows are operational metadata, not
   separate journal entries (M7 Section 9's explicit requirement).
9. **Can one invoice receive multiple payments?** Yes — the M6
   one-payment-per-invoice limitation is removed by the same allocation
   table; nothing on `PurchaseInvoice` itself prevented this, it was purely
   the payment's own header FK that did.
10. **Can one payment partially settle an invoice?** Yes, at both levels:
    an allocation amount may be less than the invoice's outstanding
    balance, and a payment's allocations may leave some invoices still
    outstanding.
11. **How are supplier credits represented?** A new `supplier_credit_notes`
    header (supplier's own document reference, immutable once created —
    there is no DRAFT state; a credit note is a single-step operation,
    unlike an invoice, because it requires no three-way matching decision)
    with `supplier_credit_note_lines` (line detail) and
    `supplier_credit_allocations` (which invoice(s) it reduces).
12. **How are supplier debit notes represented?** **Deferred.** A debit
    note (a supplier-side document that *increases* what is owed, for a
    reason other than a fresh invoice) has no concrete business scenario
    in this system that isn't already covered by either a new
    `PurchaseInvoice` or `Purchase Price Variance` (an unfavorable
    variance already increases the effective cost automatically).
    Inventing a distinct debit-note entity with no real workflow behind it
    would be exactly the "incomplete workflow just to increase feature
    count" the task explicitly forbids.
13. **How is an unapplied supplier payment handled?** **Rejected
    explicitly, not modeled.** M7 does not implement a supplier-advance/
    unapplied-credit balance sheet concept (that would require its own GL
    account, its own aging/statement treatment, and its own reconciliation
    — a real, non-trivial feature, not a corner of this one). A payment's
    `allocations` must sum to **exactly** its `amount`
    (`ALLOCATION_MUST_EQUAL_PAYMENT_AMOUNT` if not) — the same explicit-
    rejection pattern M6 used for overpayment.
14. **How are supplier credits allocated?** Same rule as payments, for the
    same reason: a credit note's `allocations` must sum to exactly its
    `grand_total` at creation time. There is no unapplied-credit pool to
    race over later — allocation happens atomically with creation, in the
    same transaction, under the same invoice-row locks used for payments.
15. **How does AP aging work?** Unchanged bucket structure (current /
    1–30 / 31–60 / 61–90 / 90+, by `due_date`), but the per-invoice
    outstanding balance used for bucketing is now `grand_total -
    amount_paid - amount_credited` (previously just `- amount_paid`).
    There is no separate "disputed" invoice state in this system — an
    invoice either fails to post (over-invoicing) or posts as a normal
    financial fact; a posted invoice always ages normally at its true
    outstanding balance.
16. **How does a supplier statement work?** New `get_supplier_statement`:
    a chronological list of every invoice, credit note, and payment
    allocation for one supplier between two dates, each shown with its
    signed effect on the running balance, plus an opening balance (the net
    effect of everything before `date_from`) and a closing balance —
    verified in tests to equal `get_supplier_ap_summary`'s `total_owed`.
17. **How is AP subledger reconciled to GL?** Unchanged mechanism
    (`ap_reconciliation`: GL Accounts Payable balance vs. Σ outstanding
    invoice balances) — now naturally correct under credits too, since
    `amount_credited` already reduces both the invoice's own outstanding
    balance *and* (via the credit note's own journal) the GL AP balance by
    the identical amount.
18. **What remains deferred?** Multi-currency; recoverable/input purchase
    tax; freight/landed-cost allocation; supplier debit notes (#12);
    unapplied supplier payments/credits and supplier advances (#13/#14);
    draft-invoice editing; voiding a paid/partially-paid invoice; a
    stateful, queryable exception-approval workflow (#7) beyond the
    existing hard-block-at-post mechanism; per-supplier GL sub-accounts
    (the supplier dimension stays on the subledger, not the chart of
    accounts, exactly as M6 decided for the single AP control account).

## Architecture summary

```
PO(s) ──> Receipt(s) ──> Invoice(s) ──> AP ──> Credit/Debit adjustments
                                          │            │
                                          └──────┬─────┘
                                                 Payment allocation
                                                 │
                                    Supplier balance / AP aging /
                                    Supplier statement / GL reconciliation
```

The single most important rule carried over unchanged from M6 and applied
throughout M7: **never invent a balancing journal that doesn't correspond
to a real transaction.** Every new journal in M7 (credit notes) is built
from the same `_LineSpec`-pair-computed-once discipline as every M4/M6
journal — see `accounting/service.py`'s module docstring.

See `docs/M7_HARDENING_AUDIT.md` for the full test inventory, concurrency/
failure-injection/mutation results, the 19-question final auditor
self-review, and the verdict.
