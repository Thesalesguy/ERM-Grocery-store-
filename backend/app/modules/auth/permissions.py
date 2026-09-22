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
# M14 (docs/M14_DESIGN.md): a distinct permission from sales.void —
# sales.void gates INITIATING a void (already Manager/Admin-only);
# sales.return.approve gates being the SECOND user who authorizes another
# user's return/void once its amount reaches the store's configured
# approval threshold. Kept separate (mirrors the read/write/heavier-
# action split used everywhere else in this matrix — count.review vs.
# count.post, supply_chain.approve vs. .execute) so a role could
# plausibly hold one without the other, and so a Manager voiding a sale
# above threshold still needs a second Manager or an Admin to approve it
# — the same self-approval boundary this permission's holder is subject
# to is enforced in app.modules.sales.service, not by this grant alone.
SALES_RETURN_APPROVE = "sales.return.approve"
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
# M9 (docs/M9_SUPPLY_CHAIN_DESIGN.md "design answer #19"): four tiers
# mirroring the count/transfer split — read is separate from plan
# (generating recommendations is inventory-adjacent data work, not a
# financial commitment) which is separate from approve (a purchasing
# decision) which is separate from execute (the action that actually
# creates a real PO/transfer). Inventory Clerk gets read+plan only —
# approving/executing purchasing on the business's behalf is withheld
# from the same role that already lacks ap.post/ap.pay and
# inventory.count.review/post, for the identical separation-of-duties
# reason.
SUPPLY_CHAIN_READ = "supply_chain.read"
SUPPLY_CHAIN_PLAN = "supply_chain.plan"
SUPPLY_CHAIN_APPROVE = "supply_chain.approve"
SUPPLY_CHAIN_EXECUTE = "supply_chain.execute"
# M10 (docs/M10_DESIGN.md "RBAC"): hr.write covers Employee master data,
# EmploymentStatusPeriod, and EmploymentAssignment (personnel
# administration) — deliberately NOT CompensationPeriod. Setting an
# employee's pay rate is kept as its own code, hr.compensation.write,
# because it is meaningfully more sensitive than the rest of personnel
# admin (it directly determines payroll amounts) and because the
# approved HR Clerk scope (M10 design decision #3: "employee master
# data, employment history, attendance, payroll preparation/read access
# where appropriate") never mentions compensation — the two-code split
# is what lets HR Clerk get hr.write without also getting the ability to
# change anyone's pay.
HR_READ = "hr.read"
HR_WRITE = "hr.write"
HR_COMPENSATION_WRITE = "hr.compensation.write"
ATTENDANCE_READ = "attendance.read"
ATTENDANCE_WRITE = "attendance.write"
# Five tiers mirroring the AP/count/supply-chain write-then-progressively-
# heavier-action pattern: read is plain viewing; calculate is the
# mechanical, non-committing "preparation" step (M10 design decision #3's
# own word) that produces a DRAFT PayrollEmployeeResult from
# attendance+compensation inputs — no GL liability yet, same tier as
# AP_WRITE/INVENTORY_COUNT_WRITE; approve is a supervisory checkpoint
# confirming a store's calculated numbers are correct, still no GL entry;
# post is the one action that creates a real, binding GL liability
# (mirrors AP_POST/INVENTORY_COUNT_POST/SUPPLY_CHAIN_EXECUTE); reverse
# undoes a posted period with a compensating entry (mirrors
# ACCOUNTING_REVERSE).
#
# Manager RBAC decision (M10 design decision #2 required this be
# justified, not assumed): Manager gets payroll.read/calculate/approve
# but NOT payroll.post, unlike the AP/inventory-count/supply-chain
# tables above where Manager holds the full write-through-commit chain.
# The difference is conflict of interest, not workflow depth: an AP
# invoice or a stock count is about a vendor or a shelf, something the
# posting manager has no personal financial stake in. A payroll period
# is compensation for the manager's own team — and, in a single-manager
# store, plausibly the manager's own pay. Letting the same person
# calculate, approve, AND commit their team's payroll to a real GL
# liability collapses a segregation-of-duties boundary that exists in
# every other module specifically because that boundary matters more,
# not less, when the approver has a direct personal interest in the
# amounts. Manager keeps payroll.approve (an operational check that a
# store's numbers look right — it still requires a separate posting
# authority to act on) and is the same role already trusted with
# ACCOUNTING_REVERSE/AP_POST/AP_PAY elsewhere, so this is a targeted
# withholding for payroll specifically, not a general distrust of the
# role. payroll.post and payroll.reverse are Admin-only in this
# milestone (decision #2: "Admin gets highest payroll authority").
# RBAC boundary note (found during the Phase 11 adversarial review):
# payroll.read grants visibility into a PayrollPeriod's totals and every
# PayrollEmployeeResult's computed gross_pay/net_pay/pay_rate — the same
# class of information hr.compensation.write otherwise gates. HR Clerk
# holds payroll.read (per M10 design decision #3's "payroll preparation/
# read access") but not hr.compensation.write, so it can see a period's
# CALCULATED results without being able to see or change an employee's
# ongoing CompensationPeriod.rate configuration. This is deliberate, not
# an oversight: rate-setting is a standing, editable master-data
# authority with effect on every future period, while a calculated
# period's results are the specific, already-computed output HR Clerk
# needs to verify before requesting approval — the same "preparation"
# task the role exists for. If a future milestone decides payroll
# results need finer-grained masking (e.g. hiding net_pay from a
# preparer while still showing hours), that is a new, narrower
# permission to add — not a reason to withhold payroll.read from HR
# Clerk today.
PAYROLL_READ = "payroll.read"
PAYROLL_CALCULATE = "payroll.calculate"
PAYROLL_APPROVE = "payroll.approve"
PAYROLL_POST = "payroll.post"
PAYROLL_REVERSE = "payroll.reverse"
# M15 (docs/M15_DESIGN.md "Permissions"): shift.manage covers a cashier's
# own OPEN/close/cash-movement actions on THEIR OWN shift -- granted
# alongside pos.use to every role that can operate the POS at all (mirrors
# how sales.return.write sits alongside pos.use for Cashier). shift.read
# is separate (view-only, for a Manager/Admin/Auditor reviewing shift
# history/reconciliation) mirroring the read/write split used everywhere
# else in this matrix. shift.override is its own, narrower permission --
# NOT folded into shift.manage -- gating specifically the ability to close
# a DIFFERENT cashier's shift, mirroring sales.return.approve's own
# tier-separation reasoning: a role could plausibly have shift.manage
# (run your own till) without shift.override (close someone else's), and
# this milestone's Cashier role does exactly that.
SHIFT_MANAGE = "shift.manage"
SHIFT_READ = "shift.read"
SHIFT_OVERRIDE = "shift.override"

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
    SALES_RETURN_APPROVE: (
        "Approve another user's return/void once its amount reaches the store's "
        "configured approval threshold"
    ),
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
    SUPPLY_CHAIN_READ: "View replenishment plans, supplier catalog, and supply-chain metrics",
    SUPPLY_CHAIN_PLAN: "Generate replenishment recommendations and cancel a plan",
    SUPPLY_CHAIN_APPROVE: "Approve a replenishment recommendation",
    SUPPLY_CHAIN_EXECUTE: "Execute an approved plan into a real purchase order or transfer",
    HR_READ: "View employee records, employment history, and assignments",
    HR_WRITE: "Create/edit employees, employment status, and store/role assignments",
    HR_COMPENSATION_WRITE: "Set or change an employee's compensation (pay rate, pay type)",
    ATTENDANCE_READ: "View attendance records",
    ATTENDANCE_WRITE: "Record and correct attendance (clock in/out, corrections)",
    PAYROLL_READ: "View payroll periods and calculated results",
    PAYROLL_CALCULATE: "Run/re-run payroll calculation for a period (no GL impact)",
    PAYROLL_APPROVE: "Approve a calculated payroll period before posting",
    PAYROLL_POST: "Post an approved payroll period, creating a real GL liability",
    PAYROLL_REVERSE: "Reverse a posted payroll period with a compensating entry",
    SHIFT_MANAGE: "Open, record cash movements against, and close one's own cashier shift",
    SHIFT_READ: "View cashier shift history and reconciliation detail",
    SHIFT_OVERRIDE: "Close another cashier's shift on their behalf",
}

# --- Roles --------------------------------------------------------------
ADMIN = "Admin"
MANAGER = "Manager"
CASHIER = "Cashier"
INVENTORY_CLERK = "Inventory Clerk"
AUDITOR = "Auditor"
# M10 (docs/M10_DESIGN.md design decision #3): a minimal HR/workforce
# role, scoped to exactly what that decision names — employee master
# data, employment history, attendance, and payroll preparation/read —
# and explicitly never accounting administration, journal reversal,
# arbitrary store administration, or user administration. See
# ROLE_PERMISSIONS below for the exact grant.
HR_CLERK = "HR Clerk"

ALL_ROLES: dict[str, str] = {
    ADMIN: "Full system access, including user management",
    MANAGER: "Runs day-to-day store operations: catalog, inventory, purchasing, POS, reports",
    CASHIER: "Operates the POS and looks up products; no catalog/inventory write access",
    INVENTORY_CLERK: "Manages catalog, stock levels, and purchasing/receiving",
    AUDITOR: "Read-only access across the system, including the audit log",
    HR_CLERK: (
        "Manages employee records, employment history, and attendance; prepares payroll "
        "for approval but cannot approve, post, or reverse it"
    ),
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
        SALES_RETURN_APPROVE,
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
        SUPPLY_CHAIN_READ,
        SUPPLY_CHAIN_PLAN,
        SUPPLY_CHAIN_APPROVE,
        SUPPLY_CHAIN_EXECUTE,
        HR_READ,
        HR_WRITE,
        HR_COMPENSATION_WRITE,
        ATTENDANCE_READ,
        ATTENDANCE_WRITE,
        PAYROLL_READ,
        PAYROLL_CALCULATE,
        PAYROLL_APPROVE,
        # Deliberately NOT PAYROLL_POST or PAYROLL_REVERSE — see the
        # permission constants' docstring above for the full
        # conflict-of-interest justification (M10 design decision #2).
        SHIFT_MANAGE,
        SHIFT_READ,
        SHIFT_OVERRIDE,
    ],
    CASHIER: [
        PRODUCTS_READ,
        POS_USE,
        SALES_READ,
        SALES_RETURN_READ,
        SALES_RETURN_WRITE,
        # M15: a Cashier can run their OWN till (open/close/record a cash
        # movement, view its own history) but NOT shift.override —
        # closing another cashier's shift is Manager/Admin-only,
        # mirroring sales.return.approve's own separation. shift.read
        # alongside shift.manage mirrors how Cashier already holds
        # sales.read/sales.return.read alongside pos.use/sales.return.write
        # — operating something and viewing your own history of it go
        # together everywhere else in this matrix.
        SHIFT_MANAGE,
        SHIFT_READ,
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
        SUPPLY_CHAIN_READ,
        SUPPLY_CHAIN_PLAN,
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
        SUPPLY_CHAIN_READ,
        HR_READ,
        ATTENDANCE_READ,
        PAYROLL_READ,
        SHIFT_READ,
    ],
    # M10 design decision #3's exact scope: employee master data,
    # employment history, attendance, and payroll preparation/read.
    # Never accounting.*, *.reverse, users.manage, or any other module's
    # write/admin permission.
    HR_CLERK: [
        HR_READ,
        HR_WRITE,
        ATTENDANCE_READ,
        ATTENDANCE_WRITE,
        PAYROLL_READ,
        PAYROLL_CALCULATE,
    ],
}
