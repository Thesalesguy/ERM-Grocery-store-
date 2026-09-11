"""Stable semantic identifiers for the seeded Chart of Accounts.

Application code (posting logic, reports) refers to accounts by these
code constants — never by a hardcoded integer `accounts.id` — so the
account rows can be reseeded/renumbered without touching business logic.
See docs/M4_ACCOUNTING_CORE.md Section 3 for why each of these eleven
accounts is genuinely required by the current business model (and why
the blueprint's other suggested examples — Accounts Receivable, a
populated Owner Equity/Retained Earnings — are NOT created: nothing in
M4 produces a transaction that would ever post to them).
"""

from app.modules.ap.models import SUPPLIER_PAYMENT_METHODS
from app.modules.sales.models import PAYMENT_METHODS

# --- ASSET ---------------------------------------------------------------
ACCOUNT_CASH_ON_HAND = "1000"
ACCOUNT_CARD_CLEARING = "1010"
ACCOUNT_MOBILE_MONEY_CLEARING = "1020"
ACCOUNT_BANK_TRANSFER_CLEARING = "1030"
ACCOUNT_OTHER_PAYMENT_CLEARING = "1040"
ACCOUNT_INVENTORY = "1500"
# A real operating bank account (docs/M6_AP_VENDOR_ACCOUNTING.md "Supplier
# payment accounting") — deliberately DISTINCT from the sales-side
# BANK_TRANSFER/CARD/MOBILE_MONEY "Clearing" accounts above, which model
# money customers paid that hasn't settled into the business's bank yet
# (a receivable-in-transit). A supplier payment is the opposite direction
# entirely — money actually leaving a real bank account — so it must not
# post through a customer-settlement clearing account; that would mix two
# unrelated economic flows into one balance and make it unauditable.
ACCOUNT_BANK_ACCOUNT = "1050"

# --- LIABILITY -------------------------------------------------------------
# "Purchase Clearing" is a genuine liability for goods received but not yet
# invoiced/matched against a supplier document — the M3/M4-era placeholder
# name persists (docs/M4_ACCOUNTING_CORE.md Section 9) even though M6 now
# gives it a real downstream: once a PurchaseInvoice is posted, the matched
# portion of this balance clears into ACCOUNT_ACCOUNTS_PAYABLE below. It is
# NOT renamed or repurposed as "the" AP account — see
# docs/M6_AP_VENDOR_ACCOUNTING.md "Purchase Clearing lifecycle" for why
# collapsing the two would corrupt the received-vs-invoiced distinction.
ACCOUNT_PURCHASE_CLEARING = "2000"
# The real Accounts Payable control account (docs/M6_AP_VENDOR_ACCOUNTING.md
# "AP liability"): established only once a PurchaseInvoice is POSTED
# (matched against what was actually received), reduced only by a recorded
# SupplierPayment. One shared control account for all suppliers — the
# supplier dimension lives on PurchaseInvoice/SupplierPayment rows (an AP
# subledger), not on a per-supplier GL account, mirroring how the store
# dimension lives on JournalEntry.store_id rather than on Account.
ACCOUNT_ACCOUNTS_PAYABLE = "2010"
ACCOUNT_TAX_PAYABLE = "2100"

# --- REVENUE ---------------------------------------------------------------
ACCOUNT_SALES_REVENUE = "4000"
# Contra-revenue: normal_balance is DEBIT even though the account_type is
# REVENUE (docs/M4_ACCOUNTING_CORE.md Section 3 "Contra accounts").
ACCOUNT_SALES_DISCOUNTS = "4100"
ACCOUNT_INVENTORY_ADJUSTMENT_GAIN = "4900"

# --- EXPENSE ---------------------------------------------------------------
ACCOUNT_COGS = "5000"
ACCOUNT_INVENTORY_SHRINKAGE_EXPENSE = "5900"
# Purchase-invoice variance/tax accounts (docs/M6_AP_VENDOR_ACCOUNTING.md
# "Invoice variances" / "Purchase invoice accounting"). Both are real,
# named accounts with an actual posting rule — never a silent absorption
# into Purchase Clearing, Inventory, or AP (M6 task Section 5/11: "No
# orphan clearing balances... do not hide it... without documenting why").
#
# ACCOUNT_PURCHASE_PRICE_VARIANCE: normal_balance DEBIT, but — like Sales
# Discounts' contra-revenue precedent (Section 3) — routinely carries a
# CREDIT balance for a run of favorable variances (invoiced cheaper than
# received). One column (normal_balance), not a separate mechanism.
#
# ACCOUNT_PURCHASE_TAX_EXPENSE: M6 deliberately does NOT implement
# recoverable input-tax accounting (no purchase tax RATE concept exists
# anywhere in this schema, and inventing an automatic purchase-tax
# calculation "because sales tax exists" was explicitly out of scope —
# docs/M6_AP_VENDOR_ACCOUNTING.md "Purchase invoice tax"). An invoice's
# stated tax_amount is treated conservatively as a non-recoverable cost of
# the purchase and expensed outright — never capitalized into Inventory
# (which would corrupt WAC for a value uninvolved in any inventory
# movement) and never claimed as a tax-authority-recoverable asset without
# the reconciliation workflow that would require.
ACCOUNT_PURCHASE_PRICE_VARIANCE = "5100"
ACCOUNT_PURCHASE_TAX_EXPENSE = "5200"
# Contra-cost account for a supplier-granted invoice discount (e.g. an
# early-payment or negotiated discount shown on the invoice itself) —
# the direct purchase-side mirror of ACCOUNT_SALES_DISCOUNTS. Distinct
# from ACCOUNT_PURCHASE_PRICE_VARIANCE: a discount is an explicit,
# stated reduction on the supplier's own document, not an unexplained
# difference between the invoiced unit price and the receipt cost.
ACCOUNT_PURCHASE_DISCOUNTS = "5150"

# A payment method on a Sale (app.modules.sales.models.PAYMENT_METHODS)
# always maps to exactly one clearing/cash account — never a generic
# "Cash" posting for every method (M4 task Section 18).
PAYMENT_METHOD_ACCOUNT_CODE: dict[str, str] = {
    "CASH": ACCOUNT_CASH_ON_HAND,
    "CARD": ACCOUNT_CARD_CLEARING,
    "MOBILE_MONEY": ACCOUNT_MOBILE_MONEY_CLEARING,
    "BANK_TRANSFER": ACCOUNT_BANK_TRANSFER_CLEARING,
    "OTHER": ACCOUNT_OTHER_PAYMENT_CLEARING,
}
assert set(PAYMENT_METHOD_ACCOUNT_CODE) == set(PAYMENT_METHODS)

# A supplier payment's method (app.modules.ap.models.SUPPLIER_PAYMENT_METHODS)
# maps to the real asset account it actually reduces (docs/M6_AP_VENDOR_ACCOUNTING.md
# "Supplier payment accounting"). Deliberately a SEPARATE mapping from
# PAYMENT_METHOD_ACCOUNT_CODE above, not a reuse of it — an outgoing
# vendor settlement and an incoming customer clearing position are
# different economic events even where the method name coincides (CASH).
SUPPLIER_PAYMENT_METHOD_ACCOUNT_CODE: dict[str, str] = {
    "CASH": ACCOUNT_CASH_ON_HAND,
    "BANK_TRANSFER": ACCOUNT_BANK_ACCOUNT,
    "CHEQUE": ACCOUNT_BANK_ACCOUNT,
    "OTHER": ACCOUNT_BANK_ACCOUNT,
}
assert set(SUPPLIER_PAYMENT_METHOD_ACCOUNT_CODE) == set(SUPPLIER_PAYMENT_METHODS)

# (code, name, account_type, normal_balance, description) — seeded verbatim
# by the M4 migration. Single source of truth for both the migration and
# any test that needs to assert the full chart.
SYSTEM_ACCOUNTS: list[tuple[str, str, str, str, str]] = [
    (
        ACCOUNT_CASH_ON_HAND,
        "Cash on Hand",
        "ASSET",
        "DEBIT",
        "Physical cash held at the till; CASH-tendered sale payments and change given.",
    ),
    (
        ACCOUNT_CARD_CLEARING,
        "Card Clearing",
        "ASSET",
        "DEBIT",
        "CARD sale payments pending settlement from the card processor.",
    ),
    (
        ACCOUNT_MOBILE_MONEY_CLEARING,
        "Mobile Money Clearing",
        "ASSET",
        "DEBIT",
        "MOBILE_MONEY sale payments pending settlement.",
    ),
    (
        ACCOUNT_BANK_TRANSFER_CLEARING,
        "Bank Transfer Clearing",
        "ASSET",
        "DEBIT",
        "BANK_TRANSFER sale payments pending settlement.",
    ),
    (
        ACCOUNT_OTHER_PAYMENT_CLEARING,
        "Other Payment Clearing",
        "ASSET",
        "DEBIT",
        "Sale payments recorded under the OTHER payment method.",
    ),
    (
        ACCOUNT_INVENTORY,
        "Inventory",
        "ASSET",
        "DEBIT",
        "Weighted-average cost value of on-hand stock across all stores.",
    ),
    (
        ACCOUNT_PURCHASE_CLEARING,
        "Purchase Clearing",
        "LIABILITY",
        "CREDIT",
        "Goods received from a supplier but not yet matched against a posted supplier "
        "invoice (docs/M6_AP_VENDOR_ACCOUNTING.md 'Purchase Clearing lifecycle'). Renamed "
        "from 'Purchase Clearing (Accounts Payable)' in M6 now that a real, separate "
        "Accounts Payable account exists — this account was never itself the payable to "
        "the supplier, only the interim received-not-yet-invoiced position.",
    ),
    (
        ACCOUNT_ACCOUNTS_PAYABLE,
        "Accounts Payable",
        "LIABILITY",
        "CREDIT",
        "Amounts owed to suppliers for POSTED (matched) invoices, reduced only by a "
        "recorded SupplierPayment (docs/M6_AP_VENDOR_ACCOUNTING.md 'AP liability').",
    ),
    (
        ACCOUNT_BANK_ACCOUNT,
        "Bank Account (Operating)",
        "ASSET",
        "DEBIT",
        "The business's real operating bank account; reduced by BANK_TRANSFER/CHEQUE/OTHER "
        "supplier payments (docs/M6_AP_VENDOR_ACCOUNTING.md 'Supplier payment accounting').",
    ),
    (
        ACCOUNT_PURCHASE_PRICE_VARIANCE,
        "Purchase Price Variance",
        "EXPENSE",
        "DEBIT",
        "Difference between a posted invoice's unit price and the receipt cost it is "
        "matched against (docs/M6_AP_VENDOR_ACCOUNTING.md 'Invoice variances'); often "
        "carries a credit balance for favorable variances, same contra convention as "
        "Sales Discounts.",
    ),
    (
        ACCOUNT_PURCHASE_TAX_EXPENSE,
        "Purchase Tax Expense (Non-Recoverable)",
        "EXPENSE",
        "DEBIT",
        "Tax stated on a posted supplier invoice, treated conservatively as a "
        "non-recoverable cost of the purchase in the absence of an input-tax-recovery "
        "workflow (docs/M6_AP_VENDOR_ACCOUNTING.md 'Purchase invoice tax').",
    ),
    (
        ACCOUNT_PURCHASE_DISCOUNTS,
        "Purchase Discounts",
        "EXPENSE",
        "CREDIT",
        "Contra-cost: a supplier-granted discount stated on a posted invoice, reducing "
        "the net cost of the purchase (docs/M6_AP_VENDOR_ACCOUNTING.md 'Invoice discounts').",
    ),
    (
        ACCOUNT_TAX_PAYABLE,
        "Tax Payable",
        "LIABILITY",
        "CREDIT",
        "Sales tax collected from customers, owed to the tax authority.",
    ),
    (
        ACCOUNT_SALES_REVENUE,
        "Sales Revenue",
        "REVENUE",
        "CREDIT",
        "Gross sale revenue before discounts (net of tax).",
    ),
    (
        ACCOUNT_SALES_DISCOUNTS,
        "Sales Discounts",
        "REVENUE",
        "DEBIT",
        "Contra-revenue: discounts given on sales, reducing gross Sales Revenue.",
    ),
    (
        ACCOUNT_INVENTORY_ADJUSTMENT_GAIN,
        "Inventory Adjustment Gain",
        "REVENUE",
        "CREDIT",
        "Stock found in excess of the recorded on-hand quantity (positive adjustment).",
    ),
    (
        ACCOUNT_COGS,
        "Cost of Goods Sold",
        "EXPENSE",
        "DEBIT",
        "Weighted-average cost of inventory sold, frozen per sale line at sale time.",
    ),
    (
        ACCOUNT_INVENTORY_SHRINKAGE_EXPENSE,
        "Inventory Shrinkage Expense",
        "EXPENSE",
        "DEBIT",
        "Stock found missing relative to the recorded on-hand quantity (negative adjustment).",
    ),
]
