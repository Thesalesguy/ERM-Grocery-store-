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
    ],
    CASHIER: [
        PRODUCTS_READ,
        POS_USE,
        SALES_READ,
    ],
    INVENTORY_CLERK: [
        PRODUCTS_READ,
        PRODUCTS_WRITE,
        INVENTORY_READ,
        INVENTORY_ADJUST,
        PURCHASING_READ,
        PURCHASING_WRITE,
        PURCHASING_RECEIVE,
    ],
    AUDITOR: [
        PRODUCTS_READ,
        INVENTORY_READ,
        PURCHASING_READ,
        SALES_READ,
        REPORTS_READ,
        AUDIT_READ,
        ACCOUNTING_READ,
    ],
}
