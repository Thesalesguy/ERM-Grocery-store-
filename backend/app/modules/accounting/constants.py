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

from app.modules.sales.models import PAYMENT_METHODS

# --- ASSET ---------------------------------------------------------------
ACCOUNT_CASH_ON_HAND = "1000"
ACCOUNT_CARD_CLEARING = "1010"
ACCOUNT_MOBILE_MONEY_CLEARING = "1020"
ACCOUNT_BANK_TRANSFER_CLEARING = "1030"
ACCOUNT_OTHER_PAYMENT_CLEARING = "1040"
ACCOUNT_INVENTORY = "1500"

# --- LIABILITY -------------------------------------------------------------
# "Purchase Clearing" is a genuine liability (goods have been received and
# not yet paid for — someone owes the supplier), not a placeholder. It is
# named "clearing" only because there is no per-supplier open-balance
# subledger yet (M3 has no supplier_invoices/supplier_payments tables) —
# the aggregate obligation this account tracks is real, not fabricated.
# See docs/M4_ACCOUNTING_CORE.md Section 9.
ACCOUNT_PURCHASE_CLEARING = "2000"
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
        "Purchase Clearing (Accounts Payable)",
        "LIABILITY",
        "CREDIT",
        "Goods received from a supplier but not yet reconciled against a supplier "
        "payable/paid record (docs/M4_ACCOUNTING_CORE.md Section 9).",
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
