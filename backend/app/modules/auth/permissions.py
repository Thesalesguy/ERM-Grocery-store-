"""The RBAC permission matrix — the single source of truth for both the
seed-data migration (`alembic/versions/..._m2_auth_and_rbac.py`) and the
`require_permission` dependency used on protected routes.

Defining it once here means the migration's seed data and the routes that
check these codes can never drift out of sync silently.

See docs/M2_AUTH_AND_POS.md for the documented rationale behind this
specific matrix.
"""

# --- Permission codes -------------------------------------------------
PRODUCTS_READ = "products.read"
PRODUCTS_WRITE = "products.write"
INVENTORY_READ = "inventory.read"
INVENTORY_ADJUST = "inventory.adjust"
PURCHASING_READ = "purchasing.read"
PURCHASING_WRITE = "purchasing.write"
PURCHASING_RECEIVE = "purchasing.receive"
POS_USE = "pos.use"
SALES_READ = "sales.read"
REPORTS_READ = "reports.read"
USERS_MANAGE = "users.manage"
AUDIT_READ = "audit.read"
ACCOUNTING_READ = "accounting.read"
# Reserved for a future manual-journal-posting endpoint (M4 task Section
# 27 explicitly asks manual posting endpoints to exist "only if users
# genuinely need" them — they don't yet: every M4 journal entry is posted
# automatically, inline, by the operational transaction it represents).
# Defined now so the permission code is stable when that endpoint is
# built, rather than invented later and back-seeded.
ACCOUNTING_POST = "accounting.post"
ACCOUNTING_REVERSE = "accounting.reverse"
ACCOUNTING_ADMIN = "accounting.admin"
# M5 (docs/M5_RETURNS_VOIDS_REFUNDS.md "RBAC"): a real POS cashier
# routinely processes simple merchandise returns, so return.read/write
# are granted to Cashier alongside pos.use/sales.read. Voiding a WHOLE
# transaction is a heavier action (it's economically a 100%-quantity
# return of every line, done in one call) and is deliberately held to
# Manager/Admin only, mirroring how many real POS systems require a
# manager override to void a completed sale — a documented business
# rule, not an arbitrary restriction.
SALES_RETURN_READ = "sales.return.read"
SALES_RETURN_WRITE = "sales.return.write"
SALES_VOID = "sales.void"
# M6 (docs/M6_AP_VENDOR_ACCOUNTING.md "RBAC"): four minimal AP permissions,
# not a reuse of accounting.reverse/accounting.post — mirrors the M5
# read/write/(heavier action) split. ap.write covers creating a DRAFT
# invoice and recording matching (no financial commitment yet); ap.post
# and ap.pay are the two irreversible-in-effect financial actions
# (establishing AP, moving cash/bank) and are deliberately separate so a
# role could plausibly have one without the other, though M6's own matrix
# below always grants them together.
AP_READ = "ap.read"
AP_WRITE = "ap.write"
AP_POST = "ap.post"
AP_PAY = "ap.pay"
# M7 (docs/M7_ADVANCED_AP_SETTLEMENT.md "RBAC"): creating a supplier
# credit note is a financial-commitment action (it posts a journal
# immediately, single-step) exactly like ap.post/ap.pay — kept as its own
# code rather than folded into ap.post so a role could plausibly have one
# without the other, though M7's own matrix always grants them together.
AP_CREDIT = "ap.credit"
# M8 (docs/M8_ADVANCED_INVENTORY_DESIGN.md "Design Decision 2"): stock
# counting is split into three tiers exactly like AP's write/post/pay
# split — data entry (count.write covers create/open/count-entry/recount/
# cancel) is separate from the review checkpoint (count.review) which is
# separate from the financial-commitment posting step (count.post), so a
# role can plausibly have one without the others (Inventory Clerk gets
# only the first below — a deliberate separation-of-duties choice: the
# person entering counts is never the same tier that commits them to the
# GL, mirroring real stocktake controls).
INVENTORY_COUNT_WRITE = "inventory.count.write"
INVENTORY_COUNT_REVIEW = "inventory.count.review"
INVENTORY_COUNT_POST = "inventory.count.post"
# Transfer permissions split by the three real operational actions
# (docs/M8_ADVANCED_INVENTORY_DESIGN.md "Design Decision 7"): drafting a
# transfer is not itself a financial commitment (no inventory moves yet),
# but shipping and receiving each move real stock and are kept as their
# own codes so a store's own staff can be granted receive without ship
# (or vice versa) if a future role needs that split.
INVENTORY_TRANSFER_WRITE = "inventory.transfer.write"
INVENTORY_TRANSFER_SHIP = "inventory.transfer.ship"
INVENTORY_TRANSFER_RECEIVE = "inventory.transfer.receive"

ALL_PERMISSIONS: dict[str, str] = {
    PRODUCTS_READ: "View products and barcodes",
    PRODUCTS_WRITE: "Create, edit, activate/deactivate products and barcodes",
    INVENTORY_READ: "View stock levels and the inventory movement ledger",
    INVENTORY_ADJUST: "Create manual stock adjustments",
    PURCHASING_READ: "View suppliers and purchase orders",
    PURCHASING_WRITE: "Create/edit purchase orders",
    PURCHASING_RECEIVE: "Record goods receipts against a purchase order",
    POS_USE: "Operate the point of sale (finalize sales)",
    SALES_READ: "View sales history and receipts",
    REPORTS_READ: "View reports (P&L, stock movement, ...)",
    USERS_MANAGE: "Create users and assign roles",
    AUDIT_READ: "View the audit log",
    ACCOUNTING_READ: "View the chart of accounts, journal entries, and financial reports",
    ACCOUNTING_POST: "Manually post a journal entry (reserved; no endpoint uses this yet)",
    ACCOUNTING_REVERSE: "Reverse a posted journal entry with a compensating entry",
    ACCOUNTING_ADMIN: (
        "Manage accounting configuration (reserved for future chart-of-accounts admin)"
    ),
    SALES_RETURN_READ: "View sale returns and return eligibility",
    SALES_RETURN_WRITE: "Process a merchandise return against a completed sale",
    SALES_VOID: "Void an entire completed sale (a full return of every line in one action)",
    AP_READ: "View supplier invoices, AP balances, aging, and Purchase Clearing reconciliation",
    AP_WRITE: "Create a draft supplier invoice",
    AP_POST: (
        "Post or void a supplier invoice, matching it against receipts and "
        "establishing/reversing AP"
    ),
    AP_PAY: "Record a supplier payment, settling Accounts Payable",
    AP_CREDIT: "Create a supplier credit note, reducing Accounts Payable",
    INVENTORY_COUNT_WRITE: "Create, open, count, recount, and cancel a stock count",
    INVENTORY_COUNT_REVIEW: "Review a counted stock count before posting",
    INVENTORY_COUNT_POST: "Post a reviewed stock count, committing its variance to the GL",
    INVENTORY_TRANSFER_WRITE: "Create and cancel a draft inter-store transfer",
    INVENTORY_TRANSFER_SHIP: "Ship an inter-store transfer from its source store",
    INVENTORY_TRANSFER_RECEIVE: "Receive an inter-store transfer at its destination store",
}

# --- Roles --------------------------------------------------------------
ADMIN = "Admin"
MANAGER = "Manager"
CASHIER = "Cashier"
INVENTORY_CLERK = "Inventory Clerk"
AUDITOR = "Auditor"

ALL_ROLES: dict[str, str] = {
    ADMIN: "Full system access, including user management",
    MANAGER: "Runs day-to-day store operations: catalog, inventory, purchasing, POS, reports",
    CASHIER: "Operates the POS and looks up products; no catalog/inventory write access",
    INVENTORY_CLERK: "Manages catalog, stock levels, and purchasing/receiving",
    AUDITOR: "Read-only access across the system, including the audit log",
}

# --- The matrix: role name -> permission codes it grants -----------------
# docs/M2_AUTH_AND_POS.md documents the reasoning behind each row.
ROLE_PERMISSIONS: dict[str, list[str]] = {
    ADMIN: list(ALL_PERMISSIONS),
    MANAGER: [
        PRODUCTS_READ,
        PRODUCTS_WRITE,
        INVENTORY_READ,
        INVENTORY_ADJUST,
        PURCHASING_READ,
        PURCHASING_WRITE,
        PURCHASING_RECEIVE,
        POS_USE,
        SALES_READ,
        REPORTS_READ,
        AUDIT_READ,
        ACCOUNTING_READ,
        ACCOUNTING_REVERSE,
        SALES_RETURN_READ,
        SALES_RETURN_WRITE,
        SALES_VOID,
        AP_READ,
        AP_WRITE,
        AP_POST,
        AP_PAY,
        AP_CREDIT,
        INVENTORY_COUNT_WRITE,
        INVENTORY_COUNT_REVIEW,
        INVENTORY_COUNT_POST,
        INVENTORY_TRANSFER_WRITE,
        INVENTORY_TRANSFER_SHIP,
        INVENTORY_TRANSFER_RECEIVE,
    ],
    CASHIER: [
        PRODUCTS_READ,
        POS_USE,
        SALES_READ,
        SALES_RETURN_READ,
        SALES_RETURN_WRITE,
    ],
    INVENTORY_CLERK: [
        PRODUCTS_READ,
        PRODUCTS_WRITE,
        INVENTORY_READ,
        INVENTORY_ADJUST,
        PURCHASING_READ,
        PURCHASING_WRITE,
        PURCHASING_RECEIVE,
        # Can draft/match an invoice against a receipt (purchasing-adjacent
        # data entry) but not commit it financially — posting and paying
        # are Manager/Admin-only (M6 task Section 14: heavier than
        # "record what the paperwork says").
        AP_READ,
        AP_WRITE,
        # Counting/entering data is a clerk-level task; reviewing and
        # posting the financial variance is not (see the permission
        # constants' own docstring above for the separation-of-duties
        # reasoning).
        INVENTORY_COUNT_WRITE,
        INVENTORY_TRANSFER_WRITE,
        INVENTORY_TRANSFER_SHIP,
        INVENTORY_TRANSFER_RECEIVE,
    ],
    AUDITOR: [
        PRODUCTS_READ,
        INVENTORY_READ,
        PURCHASING_READ,
        SALES_READ,
        REPORTS_READ,
        AUDIT_READ,
        ACCOUNTING_READ,
        SALES_RETURN_READ,
        AP_READ,
    ],
}
