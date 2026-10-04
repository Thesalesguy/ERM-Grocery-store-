"""Automated upgrade/downgrade/upgrade migration-cycle verification.

Runs against a dedicated database (not the one other tests use) so a
migration bug can't corrupt state other tests depend on. Exercises the
full chain from scratch through every milestone and back down again,
checking the resulting table count at each step, not just that Alembic
didn't raise.
"""

import os
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, inspect

from alembic import command
from app.core.config import get_settings

BACKEND_DIR = Path(__file__).resolve().parent.parent
TEST_MIGRATIONS_DATABASE_URL = os.environ.get(
    "TEST_MIGRATIONS_DATABASE_URL",
    "postgresql+psycopg://erp_user:erp_password@localhost:5432/erp_test",
)
M0_REVISION = "7b22f673d866"
M1_HEAD_REVISION = "9163f992ddc1"
M2_RBAC_SEED_REVISION = "e6180fca2ee0"
M2_HARDENING_REVISION = "89a42dfbfaea"  # M2 hardening: sale idempotency key
M3_HEAD_REVISION = "c82162efb3af"  # M3: supplier code, goods receipt/return idempotency
M4_ACCOUNTING_CORE_REVISION = "8df037a45976"  # M4: chart of accounts, journal engine, permissions
M4_HEAD_REVISION = "581d2a07f38c"  # M4 hardening: allow MANUAL journal source type
M5_HEAD_REVISION = "35d411b947ec"  # M5: sale returns quantity tracking and idempotency
M6_HEAD_REVISION = "36173e29a9f0"  # M6: accounts payable, purchase invoices, supplier payments
M7_HEAD_REVISION = "a4f2c8e91b6d"  # M7: advanced AP settlement, credit notes, payment allocation
M8_HEAD_REVISION = "b7e3f1a29c5d"  # M8: stock counts, inter-store transfers, replenishment
M9_HEAD_REVISION = "36ec624cf083"  # M9: supplier product catalog, replenishment plans
M10_SCHEMA_REVISION = "be26de9d9459"  # M10: HR/workforce and payroll (base schema)
M10_CANCELLED_STATUS_REVISION = "2857faf007be"  # M10: + payroll_periods CANCELLED status
M10_HEAD_REVISION = "1e832b76969e"  # M10: + calculation-consistency CHECK
M11_HEAD_REVISION = "32e51bcda102"  # M11: + reports performance indexes
M12_PHASE8_REVISION = "4708fb75ace5"  # M12: + revoke erp_app on alembic_version
M12_HEAD_REVISION = "17fb9afe8d39"  # M12: + grant erp_app SELECT-only on alembic_version
M14_HEAD_REVISION = "e0d2359bb08a"  # M14: return/void approval threshold
M15_PRE_HARDENING_REVISION = "1a4bae98d246"  # pre-M15: stock adjustment idempotency
M15_HEAD_REVISION = "db482a11ee31"  # M15: cashier/till shift sessions
M16_HARDENING_REVISION = "e1a681c4aba3"  # M16: pre-implementation hardening
M16_HEAD_REVISION = "4a83c462dbff"  # M16: store settings permissions
M19_HEAD_REVISION = "3a0d50ccc909"  # M19: purchase order idempotency key
M20_HEAD_REVISION = "9c4c5a209aa9"  # M20: fiscal integration boundary + permissions
M22_HEAD_REVISION = "aa9ac6ad7476"  # M22: accounting_periods
M25_PHASE1_HEAD_REVISION = "75d685966c98"  # M25 Phase 1: accounting_entities foundation
M25_PHASE2_HEAD_REVISION = "b628fc058834"  # M25 Phase 2: extended receiving + custody acceptance


def _alembic_config() -> Config:
    return Config(str(BACKEND_DIR / "alembic.ini"))


@pytest.fixture
def migrations_db(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MIGRATIONS_DATABASE_URL", TEST_MIGRATIONS_DATABASE_URL)
    get_settings.cache_clear()
    yield TEST_MIGRATIONS_DATABASE_URL
    get_settings.cache_clear()


def _table_count(db_url: str) -> int:
    engine = create_engine(db_url)
    try:
        return len(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _current_revision(db_url: str) -> str:
    engine = create_engine(db_url)
    try:
        with engine.connect() as conn:
            return conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
    finally:
        engine.dispose()


def test_full_upgrade_downgrade_upgrade_cycle(migrations_db: str) -> None:
    cfg = _alembic_config()

    command.downgrade(cfg, "base")
    # Alembic keeps its own bookkeeping table around at "base" — it just
    # clears the row in it, rather than dropping itself.
    assert _table_count(migrations_db) == 1

    command.upgrade(cfg, M0_REVISION)
    # M0: stores, users, roles, permissions, role_permissions, user_roles,
    # audit_logs + alembic_version.
    assert _table_count(migrations_db) == 8

    command.upgrade(cfg, M1_HEAD_REVISION)
    # M1 adds 18 tables on top of M0's 7 (+ alembic_version).
    assert _table_count(migrations_db) == 26

    command.upgrade(cfg, M2_HARDENING_REVISION)
    # M2 adds one table (refresh_tokens); the RBAC seed migration adds
    # rows, not tables; the hardening migration adds a column
    # (sales.client_transaction_id), not a table.
    assert _table_count(migrations_db) == 27

    command.upgrade(cfg, M3_HEAD_REVISION)
    # M3 adds columns (suppliers.code, goods_receipts.store_id/
    # client_transaction_id, purchase_returns.client_transaction_id),
    # not tables — the purchasing tables already existed from M1.
    assert _table_count(migrations_db) == 27

    command.upgrade(cfg, M4_HEAD_REVISION)
    # M4 adds three tables: accounts, journal_entries, journal_lines.
    assert _table_count(migrations_db) == 30

    command.upgrade(cfg, M5_HEAD_REVISION)
    # M5 adds columns (sale_items.quantity_returned, sale_returns.
    # client_transaction_id, sale_return_items.discount_refunded/
    # tax_refunded/unit_cost_refunded), not tables.
    assert _table_count(migrations_db) == 30

    command.upgrade(cfg, M6_HEAD_REVISION)
    # M6 adds three tables: purchase_invoices, purchase_invoice_lines,
    # supplier_payments.
    assert _table_count(migrations_db) == 33

    command.upgrade(cfg, M7_HEAD_REVISION)
    # M7 adds five tables: purchase_invoice_receipt_matches,
    # supplier_payment_allocations, supplier_credit_notes,
    # supplier_credit_note_lines, supplier_credit_allocations.
    assert _table_count(migrations_db) == 38

    command.upgrade(cfg, M8_HEAD_REVISION)
    # M8 adds six tables: stock_counts, stock_count_lines,
    # inter_store_transfers, inter_store_transfer_lines,
    # inter_store_transfer_receipts, inter_store_transfer_receipt_items.
    assert _table_count(migrations_db) == 44

    command.upgrade(cfg, M9_HEAD_REVISION)
    # M9 adds two tables: supplier_products, replenishment_plans (the
    # products.target_stock_quantity/minimum_stock_quantity columns and
    # the purchase_orders/inter_store_transfers.replenishment_plan_id
    # back-links are columns, not tables).
    assert _table_count(migrations_db) == 46

    command.upgrade(cfg, M22_HEAD_REVISION)
    # This jump goes all the way to the M22 head, not just M10 (the
    # explicit M9_HEAD_REVISION step above was the last named checkpoint
    # before "head" is used for the rest of the chain). M10 adds fifteen
    # tables: departments, positions, employees, employment_status_periods,
    # employment_assignments, compensation_periods, overtime_policies,
    # attendance_records, deduction_types, deduction_rates, payroll_periods,
    # payroll_employee_results, payroll_earning_lines,
    # payroll_deduction_lines, payroll_reversals (stores.
    # attendance_day_boundary_hour and the accounts/journal_entries
    # widening are a column/rows/CHECK change, not tables) -- 46 + 15 = 61.
    # M11 adds two indexes, not tables; M12 only revokes/grants a
    # privilege; M14 adds two COLUMNS, not tables -- unchanged at 61
    # through M14. The pre-M15 hardening migration adds one COLUMN, not a
    # table. M15 itself adds exactly two new tables (cashier_shifts,
    # cash_movements): 61 + 2 = 63. The M16 pre-implementation hardening
    # migration adds two more (supplier_payment_reversals,
    # supplier_credit_note_reversals; sale_returns.return_date and the
    # sales/sale_returns shift_id indexes are a column/index change, not
    # tables): 63 + 2 = 65. M19 adds one COLUMN (purchase_orders.
    # client_transaction_id), not a table -- unchanged at 65. M20 adds
    # two new tables (fiscal_configs, fiscal_submissions; stores.
    # legal_name/tax_registration_number and the fiscal.* permission seed
    # are a column/rows change, not tables): 65 + 2 = 67. M22 adds one new
    # table (accounting_periods; the profit_and_loss() operating_expenses
    # fix is a code change, not a schema change): 67 + 1 = 68.
    assert _table_count(migrations_db) == 68
    assert _current_revision(migrations_db) == M22_HEAD_REVISION

    command.upgrade(cfg, M25_PHASE1_HEAD_REVISION)
    # M25 Phase 1 (docs/M24D_TECHNICAL_CONTRACT.md Section 23) adds one new
    # table (accounting_entities; stores.accounting_entity_id is a column,
    # not a table): 68 + 1 = 69.
    assert _table_count(migrations_db) == 69
    assert _current_revision(migrations_db) == M25_PHASE1_HEAD_REVISION

    command.upgrade(cfg, "head")
    # M25 Phase 2 (docs/M24D_TECHNICAL_CONTRACT.md Sections 3.1/5/11/23)
    # adds one new table (inter_store_transfer_custody_acceptances;
    # the damaged/declared-short quantity columns on
    # inter_store_transfer_lines/inter_store_transfer_receipt_items are
    # columns, not tables): 69 + 1 = 70.
    assert _table_count(migrations_db) == 70
    assert _current_revision(migrations_db) == M25_PHASE2_HEAD_REVISION

    command.downgrade(cfg, M0_REVISION)
    assert _table_count(migrations_db) == 8

    command.upgrade(cfg, "head")
    assert _table_count(migrations_db) == 70
    assert _current_revision(migrations_db) == M25_PHASE2_HEAD_REVISION


def test_rbac_seed_data_present_after_upgrade(migrations_db: str) -> None:
    cfg = _alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")

    engine = create_engine(migrations_db)
    try:
        with engine.connect() as conn:
            role_count = conn.exec_driver_sql("SELECT count(*) FROM roles").scalar_one()
            permission_count = conn.exec_driver_sql("SELECT count(*) FROM permissions").scalar_one()
    finally:
        engine.dispose()
    assert role_count == 6
    # M14 adds one new permission (sales.return.approve) on top of M12's
    # 44 = 45. M15 adds three more (shift.manage, shift.read,
    # shift.override) = 48. The pre-M15 hardening migration adds no
    # permissions (a column only). M16 adds one more (ap.reverse) = 49,
    # then two more (store.settings.read, store.settings.write) = 51.
    # M20 adds three more (fiscal.read, fiscal.config.write,
    # fiscal.retry) = 54.
    assert permission_count == 54


def test_m4_downgrade_refuses_when_journal_entries_exist(migrations_db: str) -> None:
    """M18 discovery finding: 8df037a45976 (M4 accounting core) was the
    one accounting-core downgrade in the whole chain with no guard before
    dropping journal_entries/journal_lines/accounts -- unlike every later
    accounting migration (581d2a07f38c, M6, M14), which all refuse rather
    than silently destroy real financial history. Fixed to match that
    same discipline; proven here against a real posted journal_entries
    row, not just an empty database (the same discipline that caught
    M6/M7's own downgrade-vs-populated-data gaps)."""
    from sqlalchemy.exc import DBAPIError, InternalError

    cfg = _alembic_config()
    command.downgrade(cfg, "base")
    # Upgrade to exactly the M4 core revision (not the M4 hardening head)
    # so the downgrade below exercises 8df037a45976's own guard in
    # isolation, as a single revision step.
    command.upgrade(cfg, M4_ACCOUNTING_CORE_REVISION)

    engine = create_engine(migrations_db)
    try:
        with engine.begin() as conn:
            store_id = conn.exec_driver_sql(
                "INSERT INTO stores (name, timezone, is_active, created_at) "
                "VALUES ('T', 'UTC', true, now()) RETURNING id"
            ).scalar_one()
            conn.exec_driver_sql(
                "INSERT INTO journal_entries "
                "(journal_number, store_id, posting_date, entry_type, source_type, created_at) "
                f"VALUES ('JE-GUARD-1', {store_id}, '2024-01-01', 'STANDARD', 'SALE', now())"
            )
    finally:
        engine.dispose()

    try:
        with pytest.raises((DBAPIError, InternalError)):
            command.downgrade(cfg, M3_HEAD_REVISION)

        # Postgres transactional DDL must roll the whole migration back on
        # failure, never leaving the database partially downgraded.
        assert _current_revision(migrations_db) == M4_ACCOUNTING_CORE_REVISION
        assert _table_count(migrations_db) == 30
    finally:
        # Clean up NO MATTER WHAT the assertions above did, so a failure
        # here can never poison the shared migrations test database for
        # every other test in this file's next run.
        engine = create_engine(migrations_db)
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    "DELETE FROM journal_entries WHERE journal_number = 'JE-GUARD-1'"
                )
        finally:
            engine.dispose()
        command.downgrade(cfg, "base")


def test_m6_downgrade_refuses_when_ap_journal_entries_exist(migrations_db: str) -> None:
    """M18 discovery finding: M6's downgrade guard code already existed
    (raises if journal_entries has a PURCHASE_INVOICE/PURCHASE_INVOICE_VOID/
    SUPPLIER_PAYMENT row, or journal_lines references an M6 AP account) but
    had no test proving it against real populated data -- only the empty-DB
    upgrade/downgrade cycle exercised this migration before. Proven here
    against a real PURCHASE_INVOICE journal entry (mirrors
    test_m7_downgrade_refuses_when_credit_note_data_exists's own
    discipline, applied to the one migration upstream of it)."""
    from sqlalchemy.exc import DBAPIError, InternalError

    cfg = _alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, M6_HEAD_REVISION)

    engine = create_engine(migrations_db)
    try:
        with engine.begin() as conn:
            store_id = conn.exec_driver_sql(
                "INSERT INTO stores (name, timezone, is_active, created_at) "
                "VALUES ('T', 'UTC', true, now()) RETURNING id"
            ).scalar_one()
            conn.exec_driver_sql(
                "INSERT INTO journal_entries "
                "(journal_number, store_id, posting_date, entry_type, source_type, created_at) "
                f"VALUES ('JE-GUARD-2', {store_id}, '2024-01-01', 'STANDARD', "
                "'PURCHASE_INVOICE', now())"
            )
    finally:
        engine.dispose()

    try:
        with pytest.raises((DBAPIError, InternalError)):
            command.downgrade(cfg, M5_HEAD_REVISION)

        assert _current_revision(migrations_db) == M6_HEAD_REVISION
        assert _table_count(migrations_db) == 33
    finally:
        engine = create_engine(migrations_db)
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    "DELETE FROM journal_entries WHERE journal_number = 'JE-GUARD-2'"
                )
        finally:
            engine.dispose()
        command.downgrade(cfg, "base")


def test_m7_downgrade_refuses_when_credit_note_data_exists(migrations_db: str) -> None:
    """M7 Section 33: the downgrade guard must fail LOUDLY, before any
    destructive step, when real M7-only data exists that the M6 schema
    cannot represent — proven here against a real populated database, not
    just an empty one (the exact discipline that caught M6's own
    downgrade-vs-populated-data bug)."""
    from sqlalchemy.exc import DBAPIError, InternalError

    cfg = _alembic_config()
    command.downgrade(cfg, "base")
    # Upgrade to exactly the M7 head (not "head") so the downgrade below is
    # a single revision step — later milestones batch multiple downgrade
    # steps into one transaction, which would roll back this step's own
    # guard failure along with any later milestone's already-successful
    # step, and this test only means to exercise M7's own guard in isolation.
    command.upgrade(cfg, M7_HEAD_REVISION)

    engine = create_engine(migrations_db)
    try:
        with engine.begin() as conn:
            store_id = conn.exec_driver_sql(
                "INSERT INTO stores (name, timezone, is_active, created_at) "
                "VALUES ('T', 'UTC', true, now()) RETURNING id"
            ).scalar_one()
            supplier_id = conn.exec_driver_sql(
                "INSERT INTO suppliers (name, is_active, created_at) "
                "VALUES ('S', true, now()) RETURNING id"
            ).scalar_one()
            conn.exec_driver_sql(
                "INSERT INTO supplier_credit_notes "
                "(store_id, supplier_id, credit_number, credit_date, reason, "
                " grand_total, amount_allocated, client_transaction_id, created_at) "
                f"VALUES ({store_id}, {supplier_id}, 'CN-1', '2024-01-01', "
                "'COMMERCIAL_DISCOUNT', 10.00, 0, 'ctxn-guard-test', now())"
            )
    finally:
        engine.dispose()

    try:
        with pytest.raises((DBAPIError, InternalError)):
            command.downgrade(cfg, M6_HEAD_REVISION)

        # The failed downgrade must not have left the database partially
        # migrated — Postgres transactional DDL rolls the whole migration
        # back.
        assert _current_revision(migrations_db) == M7_HEAD_REVISION
        assert _table_count(migrations_db) == 38
    finally:
        # Clean up the blocking row NO MATTER WHAT the assertions above
        # did, so a failure here can never poison the shared migrations
        # test database for every other test in this file's next run (the
        # exact failure mode this very fix was needed for during this
        # audit — a prior version of this test without the finally left
        # a real leftover row that broke every other migration test until
        # manually purged).
        engine = create_engine(migrations_db)
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    "DELETE FROM supplier_credit_notes WHERE credit_number = 'CN-1'"
                )
        finally:
            engine.dispose()
        command.downgrade(cfg, "base")


def test_m8_downgrade_refuses_when_transfer_movement_data_exists(migrations_db: str) -> None:
    """M8's downgrade guard must fail LOUDLY, before any destructive step,
    when a real TRANSFER_IN/TRANSFER_OUT inventory movement exists that the
    M7 schema's narrower CHECK constraint cannot represent — the ledger is
    append-only, so this guard can never be worked around, only refused
    (mirrors test_m7_downgrade_refuses_when_credit_note_data_exists)."""
    from sqlalchemy.exc import DBAPIError, InternalError

    cfg = _alembic_config()
    command.downgrade(cfg, "base")
    # Upgrade to exactly the M8 head (not "head") so the downgrade below
    # stays a single revision step — see
    # test_m7_downgrade_refuses_when_credit_note_data_exists's own comment
    # for why this matters once a later milestone exists.
    command.upgrade(cfg, M8_HEAD_REVISION)

    engine = create_engine(migrations_db)
    try:
        with engine.begin() as conn:
            store_id = conn.exec_driver_sql(
                "INSERT INTO stores (name, timezone, is_active, created_at) "
                "VALUES ('T', 'UTC', true, now()) RETURNING id"
            ).scalar_one()
            category_id = conn.exec_driver_sql(
                "INSERT INTO product_categories (name, is_active, created_at) "
                "VALUES ('C', true, now()) RETURNING id"
            ).scalar_one()
            product_id = conn.exec_driver_sql(
                "INSERT INTO products "
                "(store_id, category_id, sku, name, unit_of_measure, is_weighed, "
                " current_price, current_cost, current_qty_on_hand, "
                " allow_negative_stock, is_active, created_at) "
                f"VALUES ({store_id}, {category_id}, 'SKU-GUARD', 'P', 'each', false, "
                "1.00, 1.00, 5.000, false, true, now()) RETURNING id"
            ).scalar_one()
            conn.exec_driver_sql(
                "INSERT INTO inventory_movements "
                "(store_id, product_id, movement_type, quantity_delta, "
                " unit_cost_at_movement, resulting_quantity_on_hand, reference_type, "
                " reference_id, created_at) "
                f"VALUES ({store_id}, {product_id}, 'TRANSFER_OUT', -1, 1.00, 4.000, "
                "'inter_store_transfer', 999999, now())"
            )
    finally:
        engine.dispose()

    try:
        with pytest.raises((DBAPIError, InternalError)):
            command.downgrade(cfg, M7_HEAD_REVISION)

        # Postgres transactional DDL must roll the whole migration back on
        # failure, never leaving the database partially downgraded.
        assert _current_revision(migrations_db) == M8_HEAD_REVISION
        assert _table_count(migrations_db) == 44
    finally:
        # Clean up NO MATTER WHAT the assertions above did, so a failure
        # here can never poison the shared migrations test database for
        # every other test in this file's next run.
        engine = create_engine(migrations_db)
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    "DELETE FROM inventory_movements WHERE reference_id = 999999 "
                    "AND reference_type = 'inter_store_transfer'"
                )
        finally:
            engine.dispose()
        command.downgrade(cfg, "base")


def test_m9_downgrade_refuses_when_generated_po_exists(migrations_db: str) -> None:
    """M9's downgrade guard must fail LOUDLY, before any destructive step,
    when a real purchase_orders.replenishment_plan_id link exists that the
    M8 schema cannot represent (mirrors the M7/M8 precedents above)."""
    from sqlalchemy.exc import DBAPIError, InternalError

    cfg = _alembic_config()
    command.downgrade(cfg, "base")
    # Upgrade to exactly the M9 head (not "head") so the downgrade below is
    # a single revision step — M10 added a later revision, and a multi-step
    # downgrade batches into one transaction (a later milestone's own guard
    # failure would roll back an earlier, otherwise-successful step too),
    # so this test only means to exercise M9's own guard in isolation.
    command.upgrade(cfg, M9_HEAD_REVISION)

    engine = create_engine(migrations_db)
    try:
        with engine.begin() as conn:
            store_id = conn.exec_driver_sql(
                "INSERT INTO stores (name, timezone, is_active, created_at) "
                "VALUES ('T', 'UTC', true, now()) RETURNING id"
            ).scalar_one()
            supplier_id = conn.exec_driver_sql(
                "INSERT INTO suppliers (name, is_active, created_at) "
                "VALUES ('S', true, now()) RETURNING id"
            ).scalar_one()
            category_id = conn.exec_driver_sql(
                "INSERT INTO product_categories (name, is_active, created_at) "
                "VALUES ('C', true, now()) RETURNING id"
            ).scalar_one()
            product_id = conn.exec_driver_sql(
                "INSERT INTO products "
                "(store_id, category_id, sku, name, unit_of_measure, is_weighed, "
                " current_price, current_cost, current_qty_on_hand, "
                " allow_negative_stock, is_active, created_at) "
                f"VALUES ({store_id}, {category_id}, 'SKU-GUARD-M9', 'P', 'each', false, "
                "1.00, 1.00, 5.000, false, true, now()) RETURNING id"
            ).scalar_one()
            purchase_order_id = conn.exec_driver_sql(
                "INSERT INTO purchase_orders "
                "(store_id, supplier_id, purchase_number, status, order_date, created_at) "
                f"VALUES ({store_id}, {supplier_id}, 'PO-GUARD-M9', 'DRAFT', CURRENT_DATE, now()) "
                "RETURNING id"
            ).scalar_one()
            plan_id = conn.exec_driver_sql(
                "INSERT INTO replenishment_plans "
                "(generation_batch_id, destination_store_id, product_id, needed_quantity, "
                " suggested_quantity, source_type, supplier_id, urgency, reason, status, "
                " created_at) "
                f"VALUES ('33333333-3333-3333-3333-333333333333', {store_id}, {product_id}, 10, "
                f"10, 'SUPPLIER', {supplier_id}, 'NORMAL', 'guard test', 'RECOMMENDED', now()) "
                "RETURNING id"
            ).scalar_one()
            conn.exec_driver_sql(
                f"UPDATE purchase_orders SET replenishment_plan_id = {plan_id} "
                f"WHERE id = {purchase_order_id}"
            )
    finally:
        engine.dispose()

    try:
        with pytest.raises((DBAPIError, InternalError)):
            command.downgrade(cfg, M8_HEAD_REVISION)

        assert _current_revision(migrations_db) == M9_HEAD_REVISION
        assert _table_count(migrations_db) == 46
    finally:
        engine = create_engine(migrations_db)
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    "UPDATE purchase_orders SET replenishment_plan_id = NULL "
                    "WHERE replenishment_plan_id IN "
                    "(SELECT id FROM replenishment_plans WHERE generation_batch_id = "
                    "'33333333-3333-3333-3333-333333333333')"
                )
                conn.exec_driver_sql(
                    "DELETE FROM replenishment_plans WHERE generation_batch_id = "
                    "'33333333-3333-3333-3333-333333333333'"
                )
        finally:
            engine.dispose()
        command.downgrade(cfg, "base")


def _capture_m0_m8_integrity_snapshot(engine: Engine) -> dict[str, object]:
    """A deterministic snapshot of every M0-M8 BUSINESS/OPERATIONAL
    table's row count plus a handful of monetary/quantity aggregates —
    used to prove the M9 migration (upgrade, downgrade, and re-upgrade)
    never silently alters pre-existing operational/accounting data.
    Column additions (`replenishment_plan_id`, `target_stock_quantity`,
    `minimum_stock_quantity`) are all nullable, so they must never change
    any of these numbers.

    Deliberately EXCLUDES `permissions`/`role_permissions`/`roles`: this
    codebase's M2 RBAC-seed migration (e6180fca2ee0) imports the LIVE
    `app.modules.auth.permissions.ALL_PERMISSIONS`/`ROLE_PERMISSIONS`
    dicts rather than a frozen historical snapshot (see that file's own
    docstring — a deliberate M2-era design choice, not an M9 defect), and
    every milestone from M4 onward (M4, M5, M6, M7, M8, M9 — verified by
    grepping alembic/versions/*.py for this import) follows the same
    add-permissions-then-delete-them-on-downgrade pattern. The practical
    effect: in a from-scratch test build against TODAY's code, M2 already
    seeds ALL currently-defined permissions (including M9's
    supply_chain.* ones) at the M2 step, so "M8 head" and "M9 head" show
    IDENTICAL permission counts in this environment, and M9's own
    downgrade (which unconditionally deletes its 4 permission codes,
    mirroring M8/M7/M6/M5/M4's identical pattern) then removes 4 rows
    that a freshly-built "M8 head" would still have — a real, but
    SYSTEMIC AND PRE-EXISTING (not M9-specific) migration-testing
    limitation, documented in docs/M9_HARDENING_AUDIT.md rather than
    patched ad hoc in only one milestone's migration file. This function
    still captures those three tables' counts for visibility in the
    returned dict; the caller must not assert equality on them."""
    with engine.connect() as conn:
        counts = {}
        for table in (
            "stores",
            "product_categories",
            "products",
            "suppliers",
            "purchase_orders",
            "purchase_order_items",
            "inter_store_transfers",
            "inter_store_transfer_lines",
        ):
            counts[f"count:{table}"] = conn.exec_driver_sql(
                f"SELECT COUNT(*) FROM {table}"
            ).scalar_one()
        for informational_table in ("permissions", "roles", "role_permissions"):
            counts[f"informational_only:{informational_table}"] = conn.exec_driver_sql(
                f"SELECT COUNT(*) FROM {informational_table}"
            ).scalar_one()
        counts["sum:products.current_qty_on_hand"] = conn.exec_driver_sql(
            "SELECT COALESCE(SUM(current_qty_on_hand), 0) FROM products"
        ).scalar_one()
        counts["sum:products.current_cost"] = conn.exec_driver_sql(
            "SELECT COALESCE(SUM(current_cost), 0) FROM products"
        ).scalar_one()
        counts["sum:purchase_order_items.quantity_ordered"] = conn.exec_driver_sql(
            "SELECT COALESCE(SUM(quantity_ordered), 0) FROM purchase_order_items"
        ).scalar_one()
        counts["sum:purchase_order_items.quantity_received"] = conn.exec_driver_sql(
            "SELECT COALESCE(SUM(quantity_received), 0) FROM purchase_order_items"
        ).scalar_one()
        counts["sum:inter_store_transfer_lines.requested_quantity"] = conn.exec_driver_sql(
            "SELECT COALESCE(SUM(requested_quantity), 0) FROM inter_store_transfer_lines"
        ).scalar_one()
        counts["sum:inter_store_transfer_lines.shipped_quantity"] = conn.exec_driver_sql(
            "SELECT COALESCE(SUM(shipped_quantity), 0) FROM inter_store_transfer_lines"
        ).scalar_one()
        return counts


def test_m0_m8_data_integrity_survives_m9_upgrade_downgrade_reupgrade(
    migrations_db: str,
) -> None:
    """Phase 10 of the M9 hardening pass: populates real M0-M8 data (a
    store, category, product, supplier, a purchase order with a partially
    received line, and an inter-store transfer with a partially shipped
    line), captures a row-count-and-aggregate snapshot, then proves that
    snapshot is BIT-FOR-BIT identical after upgrading to M9 head, after
    downgrading back to M8 head, and after re-upgrading to M9 head again.
    A single divergence anywhere would mean the M9 migration silently
    altered pre-existing operational data — exactly what this test exists
    to catch."""
    cfg = _alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, M8_HEAD_REVISION)

    engine = create_engine(migrations_db)
    try:
        with engine.begin() as conn:
            store_id = conn.exec_driver_sql(
                "INSERT INTO stores (name, timezone, is_active, created_at) "
                "VALUES ('Integrity Store', 'UTC', true, now()) RETURNING id"
            ).scalar_one()
            category_id = conn.exec_driver_sql(
                "INSERT INTO product_categories (name, is_active, created_at) "
                "VALUES ('Integrity Category', true, now()) RETURNING id"
            ).scalar_one()
            supplier_id = conn.exec_driver_sql(
                "INSERT INTO suppliers (name, is_active, created_at) "
                "VALUES ('Integrity Supplier', true, now()) RETURNING id"
            ).scalar_one()
            product_id = conn.exec_driver_sql(
                "INSERT INTO products "
                "(store_id, category_id, sku, name, unit_of_measure, is_weighed, "
                " current_price, current_cost, current_qty_on_hand, "
                " allow_negative_stock, is_active, created_at) "
                f"VALUES ({store_id}, {category_id}, 'SKU-INTEGRITY', 'Integrity Product', "
                "'each', false, 9.99, 4.50, 37.500, false, true, now()) RETURNING id"
            ).scalar_one()
            store_b_id = conn.exec_driver_sql(
                "INSERT INTO stores (name, timezone, is_active, created_at) "
                "VALUES ('Integrity Store B', 'UTC', true, now()) RETURNING id"
            ).scalar_one()
            product_b_id = conn.exec_driver_sql(
                "INSERT INTO products "
                "(store_id, category_id, sku, name, unit_of_measure, is_weighed, "
                " current_price, current_cost, current_qty_on_hand, "
                " allow_negative_stock, is_active, created_at) "
                f"VALUES ({store_b_id}, {category_id}, 'SKU-INTEGRITY', 'Integrity Product B', "
                "'each', false, 9.99, 0, 3.000, false, true, now()) RETURNING id"
            ).scalar_one()
            purchase_order_id = conn.exec_driver_sql(
                "INSERT INTO purchase_orders "
                "(store_id, supplier_id, purchase_number, status, order_date, created_at) "
                f"VALUES ({store_id}, {supplier_id}, 'PO-INTEGRITY', 'PARTIALLY_RECEIVED', "
                "CURRENT_DATE, now()) RETURNING id"
            ).scalar_one()
            conn.exec_driver_sql(
                "INSERT INTO purchase_order_items "
                "(purchase_order_id, product_id, quantity_ordered, quantity_received, "
                " quantity_invoiced, unit_cost) "
                f"VALUES ({purchase_order_id}, {product_id}, 20.000, 7.000, 0, 4.50)"
            )
            transfer_id = conn.exec_driver_sql(
                "INSERT INTO inter_store_transfers "
                "(from_store_id, to_store_id, transfer_number, status, requested_date, "
                " created_at) "
                f"VALUES ({store_id}, {store_b_id}, 'TR-INTEGRITY', 'SHIPPED', CURRENT_DATE, "
                "now()) RETURNING id"
            ).scalar_one()
            conn.exec_driver_sql(
                "INSERT INTO inter_store_transfer_lines "
                "(transfer_id, source_product_id, destination_product_id, requested_quantity, "
                " shipped_quantity, received_quantity) "
                f"VALUES ({transfer_id}, {product_id}, {product_b_id}, 15.000, 15.000, 6.000)"
            )
    finally:
        engine.dispose()

    def _business_keys(snapshot: dict[str, object]) -> dict[str, object]:
        return {k: v for k, v in snapshot.items() if not k.startswith("informational_only:")}

    engine = create_engine(migrations_db)
    try:
        before_m9 = _capture_m0_m8_integrity_snapshot(engine)
    finally:
        engine.dispose()

    try:
        command.upgrade(cfg, "head")
        engine = create_engine(migrations_db)
        try:
            after_m9_upgrade = _capture_m0_m8_integrity_snapshot(engine)
        finally:
            engine.dispose()
        assert _business_keys(after_m9_upgrade) == _business_keys(before_m9), (
            "M9 upgrade altered pre-existing M0-M8 BUSINESS data: "
            f"{ {k: v for k, v in after_m9_upgrade.items() if v != before_m9[k]} }"
        )

        command.downgrade(cfg, M8_HEAD_REVISION)
        engine = create_engine(migrations_db)
        try:
            after_downgrade = _capture_m0_m8_integrity_snapshot(engine)
        finally:
            engine.dispose()
        assert _business_keys(after_downgrade) == _business_keys(before_m9), (
            "M9 downgrade altered pre-existing M0-M8 BUSINESS data: "
            f"{ {k: v for k, v in after_downgrade.items() if v != before_m9[k]} }"
        )
        # The permissions/role_permissions counts are captured for
        # visibility only (see _capture_m0_m8_integrity_snapshot's
        # docstring for why they are a known, pre-existing, cross-
        # milestone RBAC-seeding characteristic, not asserted equal).

        command.upgrade(cfg, "head")
        engine = create_engine(migrations_db)
        try:
            after_reupgrade = _capture_m0_m8_integrity_snapshot(engine)
        finally:
            engine.dispose()
        assert _business_keys(after_reupgrade) == _business_keys(before_m9), (
            "M9 re-upgrade altered pre-existing M0-M8 BUSINESS data: "
            f"{ {k: v for k, v in after_reupgrade.items() if v != before_m9[k]} }"
        )
    finally:
        command.downgrade(cfg, "base")


def test_m10_downgrade_refuses_when_employee_data_exists(migrations_db: str) -> None:
    """M10's downgrade guard must fail LOUDLY, before any destructive
    step, when a real employee row exists — there is no other
    representation of a person's employment record anywhere else in the
    schema (mirrors the M9 supplier_products precedent: unconditional,
    not status-gated, because master data has no "not yet real" state)."""
    from sqlalchemy.exc import DBAPIError, InternalError

    cfg = _alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, M10_HEAD_REVISION)

    engine = create_engine(migrations_db)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO employees (employee_number, legal_name, hire_date, created_at) "
                "VALUES ('EMP-GUARD-M10', 'Guard Test', '2024-01-01', now())"
            )
    finally:
        engine.dispose()

    try:
        with pytest.raises((DBAPIError, InternalError)):
            command.downgrade(cfg, M9_HEAD_REVISION)

        assert _current_revision(migrations_db) == M10_HEAD_REVISION
        assert _table_count(migrations_db) == 61
    finally:
        engine = create_engine(migrations_db)
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    "DELETE FROM employees WHERE employee_number = 'EMP-GUARD-M10'"
                )
        finally:
            engine.dispose()
        command.downgrade(cfg, "base")


def test_m10_downgrade_refuses_when_non_draft_payroll_period_exists(migrations_db: str) -> None:
    """A payroll_period past DRAFT (nothing calculated/decided yet) blocks
    the downgrade even with zero employees — mirrors M9's RECOMMENDED-
    status replenishment_plans exception in the opposite direction (here,
    only the untouched DRAFT status is safe to discard)."""
    from sqlalchemy.exc import DBAPIError, InternalError

    cfg = _alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, M10_HEAD_REVISION)

    engine = create_engine(migrations_db)
    try:
        with engine.begin() as conn:
            store_id = conn.exec_driver_sql(
                "INSERT INTO stores (name, timezone, is_active, created_at) "
                "VALUES ('T', 'UTC', true, now()) RETURNING id"
            ).scalar_one()
            conn.exec_driver_sql(
                "INSERT INTO payroll_periods "
                "(store_id, period_start, period_end, pay_date, status, created_at) "
                f"VALUES ({store_id}, '2024-01-01', '2024-01-15', '2024-01-20', 'OPEN', now())"
            )
    finally:
        engine.dispose()

    try:
        with pytest.raises((DBAPIError, InternalError)):
            command.downgrade(cfg, M9_HEAD_REVISION)

        assert _current_revision(migrations_db) == M10_HEAD_REVISION
    finally:
        engine = create_engine(migrations_db)
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    "DELETE FROM payroll_periods WHERE period_start = '2024-01-01' "
                    "AND period_end = '2024-01-15'"
                )
        finally:
            engine.dispose()
        command.downgrade(cfg, "base")


def test_m10_downgrade_succeeds_when_only_draft_payroll_period_exists(migrations_db: str) -> None:
    """The one exception: a payroll_period still in DRAFT (never opened,
    calculated, approved, or posted) is safe to discard, mirroring M9's
    RECOMMENDED-status replenishment_plans exception."""
    cfg = _alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, M10_HEAD_REVISION)

    engine = create_engine(migrations_db)
    try:
        with engine.begin() as conn:
            store_id = conn.exec_driver_sql(
                "INSERT INTO stores (name, timezone, is_active, created_at) "
                "VALUES ('T', 'UTC', true, now()) RETURNING id"
            ).scalar_one()
            conn.exec_driver_sql(
                "INSERT INTO payroll_periods "
                "(store_id, period_start, period_end, pay_date, status, created_at) "
                f"VALUES ({store_id}, '2024-01-01', '2024-01-15', '2024-01-20', 'DRAFT', now())"
            )
    finally:
        engine.dispose()

    command.downgrade(cfg, M9_HEAD_REVISION)
    assert _current_revision(migrations_db) == M9_HEAD_REVISION
    command.downgrade(cfg, "base")


def test_m10_cancelled_status_downgrade_refuses_when_cancelled_period_exists(
    migrations_db: str,
) -> None:
    """The follow-up migration adding CANCELLED to payroll_periods.status
    must itself refuse to downgrade past the point where that value is a
    valid CHECK-constraint member while a real CANCELLED row exists —
    mirrors every other status-widening guard in this file."""
    from sqlalchemy.exc import DBAPIError, InternalError

    cfg = _alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, M10_HEAD_REVISION)

    engine = create_engine(migrations_db)
    try:
        with engine.begin() as conn:
            store_id = conn.exec_driver_sql(
                "INSERT INTO stores (name, timezone, is_active, created_at) "
                "VALUES ('T', 'UTC', true, now()) RETURNING id"
            ).scalar_one()
            conn.exec_driver_sql(
                "INSERT INTO payroll_periods "
                "(store_id, period_start, period_end, pay_date, status, created_at) "
                f"VALUES ({store_id}, '2024-01-01', '2024-01-15', '2024-01-20', 'CANCELLED', now())"
            )
    finally:
        engine.dispose()

    try:
        with pytest.raises((DBAPIError, InternalError)):
            command.downgrade(cfg, M10_SCHEMA_REVISION)
        assert _current_revision(migrations_db) == M10_HEAD_REVISION
    finally:
        engine = create_engine(migrations_db)
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql("DELETE FROM payroll_periods WHERE status = 'CANCELLED'")
        finally:
            engine.dispose()
        command.downgrade(cfg, "base")


def test_m11_indexes_created_on_upgrade_and_removed_on_downgrade(migrations_db: str) -> None:
    """docs/M11_DESIGN.md Section 11/14: the two performance indexes
    this milestone adds must actually appear at M11 head, disappear on
    downgrade to the M10 head (a purely additive, non-destructive step —
    dropping an index can never lose data or relax an invariant, so no
    downgrade guard is needed here, unlike the M7/M8/M9/M10 guard tests
    above), and come back on re-upgrade. Proven against Alembic's own
    behavior directly (inspector.get_indexes), not just "the migration
    ran without raising"."""
    cfg = _alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, M10_HEAD_REVISION)

    def _index_names(table: str) -> set[str]:
        engine = create_engine(migrations_db)
        try:
            return {ix["name"] for ix in inspect(engine).get_indexes(table)}
        finally:
            engine.dispose()

    assert "ix_sales_store_completed" not in _index_names("sales")
    assert "ix_payroll_periods_store_status" not in _index_names("payroll_periods")

    command.upgrade(cfg, M11_HEAD_REVISION)
    assert "ix_sales_store_completed" in _index_names("sales")
    assert "ix_payroll_periods_store_status" in _index_names("payroll_periods")

    command.downgrade(cfg, M10_HEAD_REVISION)
    assert "ix_sales_store_completed" not in _index_names("sales")
    assert "ix_payroll_periods_store_status" not in _index_names("payroll_periods")

    command.upgrade(cfg, M11_HEAD_REVISION)
    assert "ix_sales_store_completed" in _index_names("sales")
    assert "ix_payroll_periods_store_status" in _index_names("payroll_periods")

    command.downgrade(cfg, "base")


def test_m11_upgrade_downgrade_reupgrade_preserves_populated_m10_business_data(
    migrations_db: str,
) -> None:
    """A purely additive index migration must never touch existing rows.
    Seeds real M0-M10 business data at the M10 head, cycles through the
    M11 upgrade/downgrade/re-upgrade, and proves every row survives
    byte-for-byte -- the same discipline as
    test_m0_m8_data_integrity_survives_m9_upgrade_downgrade_reupgrade
    above, applied to M11's own migration."""
    cfg = _alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, M10_HEAD_REVISION)

    engine = create_engine(migrations_db)
    try:
        with engine.begin() as conn:
            store_id = conn.exec_driver_sql(
                "INSERT INTO stores (name, timezone, is_active, created_at) "
                "VALUES ('M11 Migration Test Store', 'UTC', true, now()) RETURNING id"
            ).scalar_one()
            conn.exec_driver_sql(
                "INSERT INTO payroll_periods "
                "(store_id, period_start, period_end, pay_date, status, created_at) "
                f"VALUES ({store_id}, '2024-01-01', '2024-01-15', '2024-01-20', 'DRAFT', now())"
            )
        with engine.connect() as conn:
            before = conn.exec_driver_sql(
                "SELECT id, name FROM stores WHERE id = %s", (store_id,)
            ).one()
            before_period = conn.exec_driver_sql(
                "SELECT store_id, status FROM payroll_periods WHERE store_id = %s", (store_id,)
            ).one()
    finally:
        engine.dispose()

    command.upgrade(cfg, M11_HEAD_REVISION)
    command.downgrade(cfg, M10_HEAD_REVISION)
    command.upgrade(cfg, M11_HEAD_REVISION)

    engine = create_engine(migrations_db)
    try:
        with engine.connect() as conn:
            after = conn.exec_driver_sql(
                "SELECT id, name FROM stores WHERE id = %s", (store_id,)
            ).one()
            after_period = conn.exec_driver_sql(
                "SELECT store_id, status FROM payroll_periods WHERE store_id = %s", (store_id,)
            ).one()
    finally:
        engine.dispose()

    assert after == before
    assert after_period == before_period

    command.downgrade(cfg, "base")


def test_m14_downgrade_refuses_when_approval_data_exists(migrations_db: str) -> None:
    """M18 discovery finding: M14's downgrade guard code already existed
    (raises if any store has a configured return_approval_threshold_amount,
    or any sale_returns row was created under the approval gate) but had
    no test proving it against real populated data. Proven here against a
    real store configuration (mirrors test_m7_downgrade_refuses_when_
    credit_note_data_exists's own discipline, applied to the current
    migration head)."""
    from sqlalchemy.exc import DBAPIError, InternalError

    cfg = _alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, M14_HEAD_REVISION)

    engine = create_engine(migrations_db)
    try:
        with engine.begin() as conn:
            store_id = conn.exec_driver_sql(
                "INSERT INTO stores "
                "(name, timezone, is_active, return_approval_threshold_amount, created_at) "
                "VALUES ('T', 'UTC', true, 100.00, now()) RETURNING id"
            ).scalar_one()
    finally:
        engine.dispose()

    try:
        with pytest.raises((DBAPIError, InternalError)):
            command.downgrade(cfg, M12_HEAD_REVISION)

        assert _current_revision(migrations_db) == M14_HEAD_REVISION
    finally:
        engine = create_engine(migrations_db)
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql("DELETE FROM stores WHERE id = %s", (store_id,))
        finally:
            engine.dispose()
        command.downgrade(cfg, "base")


def test_m25_phase1_backfills_every_existing_store_onto_one_default_entity(
    migrations_db: str,
) -> None:
    """docs/M24D_TECHNICAL_CONTRACT.md Section 23: a store created BEFORE
    this migration runs must be backfilled onto exactly one, newly
    seeded, default CORPORATE_DIVISION entity -- proven against a real
    pre-existing store, not just a from-scratch database."""
    cfg = _alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, M22_HEAD_REVISION)

    engine = create_engine(migrations_db)
    try:
        with engine.begin() as conn:
            store_id = conn.exec_driver_sql(
                "INSERT INTO stores (name, timezone, is_active, created_at) "
                "VALUES ('Pre-Phase1 Store', 'UTC', true, now()) RETURNING id"
            ).scalar_one()
    finally:
        engine.dispose()

    command.upgrade(cfg, M25_PHASE1_HEAD_REVISION)

    engine = create_engine(migrations_db)
    try:
        with engine.connect() as conn:
            entity_count = conn.exec_driver_sql(
                "SELECT COUNT(*) FROM accounting_entities"
            ).scalar_one()
            default_count = conn.exec_driver_sql(
                "SELECT COUNT(*) FROM accounting_entities WHERE is_default = true"
            ).scalar_one()
            null_count = conn.exec_driver_sql(
                "SELECT COUNT(*) FROM stores WHERE accounting_entity_id IS NULL"
            ).scalar_one()
            store_entity_id, default_entity_id = conn.exec_driver_sql(
                "SELECT s.accounting_entity_id, e.id FROM stores s "
                "JOIN accounting_entities e ON e.is_default = true "
                "WHERE s.id = %s",
                (store_id,),
            ).one()
    finally:
        engine.dispose()

    assert entity_count == 1
    assert default_count == 1
    assert null_count == 0
    assert store_entity_id == default_entity_id

    command.downgrade(cfg, "base")


def test_m25_phase1_downgrade_refuses_when_a_second_entity_exists(migrations_db: str) -> None:
    """Mirrors the M7/M8/M9/M10 downgrade-guard precedents in this same
    file: this migration's own downgrade must fail LOUDLY, before any
    destructive step, when data exists that it cannot represent -- here,
    a second accounting_entities row (which could only have been created
    by a later, not-yet-downgraded M25 phase)."""
    from sqlalchemy.exc import DBAPIError, InternalError

    cfg = _alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, M25_PHASE1_HEAD_REVISION)

    engine = create_engine(migrations_db)
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO accounting_entities (name, entity_type, is_default, created_at) "
                "VALUES ('Guard Independent', 'INDEPENDENT', false, now())"
            )
    finally:
        engine.dispose()

    try:
        with pytest.raises((DBAPIError, InternalError)):
            command.downgrade(cfg, M22_HEAD_REVISION)

        assert _current_revision(migrations_db) == M25_PHASE1_HEAD_REVISION
        assert _table_count(migrations_db) == 69
    finally:
        engine = create_engine(migrations_db)
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    "DELETE FROM accounting_entities WHERE name = 'Guard Independent'"
                )
        finally:
            engine.dispose()
        command.downgrade(cfg, "base")


def test_m25_phase1_downgrade_refuses_when_a_store_was_reassigned(migrations_db: str) -> None:
    """Same guard, the other triggering condition: a store reassigned
    off the default entity onto a (necessarily later-phase-created)
    second entity."""
    from sqlalchemy.exc import DBAPIError, InternalError

    cfg = _alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, M25_PHASE1_HEAD_REVISION)

    engine = create_engine(migrations_db)
    try:
        with engine.begin() as conn:
            other_entity_id = conn.exec_driver_sql(
                "INSERT INTO accounting_entities (name, entity_type, is_default, created_at) "
                "VALUES ('Guard Reassign Target', 'INDEPENDENT', false, now()) RETURNING id"
            ).scalar_one()
            store_id = conn.exec_driver_sql(
                "INSERT INTO stores (name, timezone, is_active, accounting_entity_id, "
                "created_at) VALUES ('Guard Reassigned Store', 'UTC', true, %s, now()) "
                "RETURNING id",
                (other_entity_id,),
            ).scalar_one()
    finally:
        engine.dispose()

    try:
        with pytest.raises((DBAPIError, InternalError)):
            command.downgrade(cfg, M22_HEAD_REVISION)

        assert _current_revision(migrations_db) == M25_PHASE1_HEAD_REVISION
        assert _table_count(migrations_db) == 69
    finally:
        engine = create_engine(migrations_db)
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql("DELETE FROM stores WHERE id = %s", (store_id,))
                conn.exec_driver_sql(
                    "DELETE FROM accounting_entities WHERE id = %s", (other_entity_id,)
                )
        finally:
            engine.dispose()
        command.downgrade(cfg, "base")


def test_m25_phase1_upgrade_downgrade_reupgrade_preserves_populated_m22_business_data(
    migrations_db: str,
) -> None:
    """A purely additive foundation migration must never touch existing
    rows -- mirrors
    test_m11_upgrade_downgrade_reupgrade_preserves_populated_m10_business_data's
    own discipline, applied to this migration. Downgrading past this
    migration is safe here ONLY because no second entity/reassignment
    exists yet in this freshly-seeded scenario (see the two guard tests
    above for the case where it is not safe)."""
    cfg = _alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, M22_HEAD_REVISION)

    engine = create_engine(migrations_db)
    try:
        with engine.begin() as conn:
            store_id = conn.exec_driver_sql(
                "INSERT INTO stores (name, timezone, is_active, created_at) "
                "VALUES ('M25 Phase1 Migration Test Store', 'UTC', true, now()) RETURNING id"
            ).scalar_one()
        with engine.connect() as conn:
            before = conn.exec_driver_sql(
                "SELECT id, name, timezone FROM stores WHERE id = %s", (store_id,)
            ).one()
    finally:
        engine.dispose()

    command.upgrade(cfg, M25_PHASE1_HEAD_REVISION)
    command.downgrade(cfg, M22_HEAD_REVISION)
    command.upgrade(cfg, M25_PHASE1_HEAD_REVISION)

    engine = create_engine(migrations_db)
    try:
        with engine.connect() as conn:
            after = conn.exec_driver_sql(
                "SELECT id, name, timezone FROM stores WHERE id = %s", (store_id,)
            ).one()
    finally:
        engine.dispose()

    assert after == before
    command.downgrade(cfg, "base")


def test_m25_phase2_existing_receipt_items_backfill_to_zero_damaged_short(
    migrations_db: str,
) -> None:
    """docs/M24D_TECHNICAL_CONTRACT.md Section 23: an existing (pre-Phase-2)
    transfer line/receipt item must land on damaged=0/declared_short=0
    after this migration -- proven against real pre-existing data, not
    just a from-scratch database."""
    cfg = _alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, M25_PHASE1_HEAD_REVISION)

    engine = create_engine(migrations_db)
    try:
        with engine.begin() as conn:
            store_a = conn.exec_driver_sql(
                "INSERT INTO stores (name, timezone, is_active, accounting_entity_id, "
                "created_at) VALUES ('P2 Store A', 'UTC', true, "
                "(SELECT id FROM accounting_entities WHERE is_default = true), now()) "
                "RETURNING id"
            ).scalar_one()
            store_b = conn.exec_driver_sql(
                "INSERT INTO stores (name, timezone, is_active, accounting_entity_id, "
                "created_at) VALUES ('P2 Store B', 'UTC', true, "
                "(SELECT id FROM accounting_entities WHERE is_default = true), now()) "
                "RETURNING id"
            ).scalar_one()
            category_id = conn.exec_driver_sql(
                "INSERT INTO product_categories (name, is_active, created_at) "
                "VALUES ('P2 Cat', true, now()) RETURNING id"
            ).scalar_one()
            product_a = conn.exec_driver_sql(
                "INSERT INTO products (store_id, category_id, sku, name, unit_of_measure, "
                "is_weighed, current_price, current_cost, current_qty_on_hand, "
                "allow_negative_stock, is_active, created_at) VALUES "
                f"({store_a}, {category_id}, 'SKU-P2', 'P2 Product', 'each', false, "
                "9.99, 4.00, 10.000, false, true, now()) RETURNING id"
            ).scalar_one()
            product_b = conn.exec_driver_sql(
                "INSERT INTO products (store_id, category_id, sku, name, unit_of_measure, "
                "is_weighed, current_price, current_cost, current_qty_on_hand, "
                "allow_negative_stock, is_active, created_at) VALUES "
                f"({store_b}, {category_id}, 'SKU-P2', 'P2 Product B', 'each', false, "
                "9.99, 0, 0, false, true, now()) RETURNING id"
            ).scalar_one()
            transfer_id = conn.exec_driver_sql(
                "INSERT INTO inter_store_transfers (from_store_id, to_store_id, "
                "transfer_number, status, requested_date, created_at) VALUES "
                f"({store_a}, {store_b}, 'TR-P2-BACKFILL', 'SHIPPED', CURRENT_DATE, now()) "
                "RETURNING id"
            ).scalar_one()
            line_id = conn.exec_driver_sql(
                "INSERT INTO inter_store_transfer_lines (transfer_id, source_product_id, "
                "destination_product_id, requested_quantity, shipped_quantity, "
                "received_quantity, unit_cost_at_shipment) VALUES "
                f"({transfer_id}, {product_a}, {product_b}, 10.000, 10.000, 10.000, 4.00) "
                "RETURNING id"
            ).scalar_one()
            receipt_id = conn.exec_driver_sql(
                "INSERT INTO inter_store_transfer_receipts (transfer_id, store_id, "
                "client_transaction_id, received_date, created_at) VALUES "
                f"({transfer_id}, {store_b}, 'rc-p2-backfill', CURRENT_DATE, now()) "
                "RETURNING id"
            ).scalar_one()
            conn.exec_driver_sql(
                "INSERT INTO inter_store_transfer_receipt_items "
                "(inter_store_transfer_receipt_id, inter_store_transfer_line_id, "
                "quantity_received) VALUES "
                f"({receipt_id}, {line_id}, 10.000)"
            )
    finally:
        engine.dispose()

    command.upgrade(cfg, M25_PHASE2_HEAD_REVISION)

    engine = create_engine(migrations_db)
    try:
        with engine.connect() as conn:
            line_damaged, line_short = conn.exec_driver_sql(
                "SELECT damaged_quantity, declared_short_quantity "
                f"FROM inter_store_transfer_lines WHERE id = {line_id}"
            ).one()
            item_damaged, item_short = conn.exec_driver_sql(
                "SELECT quantity_damaged, quantity_declared_short "
                f"FROM inter_store_transfer_receipt_items WHERE id = "
                f"(SELECT id FROM inter_store_transfer_receipt_items "
                f"WHERE inter_store_transfer_receipt_id = {receipt_id})"
            ).one()
    finally:
        engine.dispose()

    assert line_damaged == 0
    assert line_short == 0
    assert item_damaged == 0
    assert item_short == 0

    command.downgrade(cfg, "base")


def test_m25_phase2_downgrade_refuses_when_damaged_or_short_data_exists(
    migrations_db: str,
) -> None:
    """Mirrors the M7/M8/M9/M10/M25-Phase-1 downgrade-guard precedents:
    this migration's downgrade must refuse, before any destructive step,
    when a transfer line carries data (a nonzero damaged/declared-short
    quantity) the pre-Phase-2 schema cannot represent."""
    from sqlalchemy.exc import DBAPIError, InternalError

    cfg = _alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, M25_PHASE2_HEAD_REVISION)

    engine = create_engine(migrations_db)
    try:
        with engine.begin() as conn:
            store_a = conn.exec_driver_sql(
                "INSERT INTO stores (name, timezone, is_active, accounting_entity_id, "
                "created_at) VALUES ('P2 Guard Store A', 'UTC', true, "
                "(SELECT id FROM accounting_entities WHERE is_default = true), now()) "
                "RETURNING id"
            ).scalar_one()
            store_b = conn.exec_driver_sql(
                "INSERT INTO stores (name, timezone, is_active, accounting_entity_id, "
                "created_at) VALUES ('P2 Guard Store B', 'UTC', true, "
                "(SELECT id FROM accounting_entities WHERE is_default = true), now()) "
                "RETURNING id"
            ).scalar_one()
            category_id = conn.exec_driver_sql(
                "INSERT INTO product_categories (name, is_active, created_at) "
                "VALUES ('P2 Guard Cat', true, now()) RETURNING id"
            ).scalar_one()
            product_a = conn.exec_driver_sql(
                "INSERT INTO products (store_id, category_id, sku, name, unit_of_measure, "
                "is_weighed, current_price, current_cost, current_qty_on_hand, "
                "allow_negative_stock, is_active, created_at) VALUES "
                f"({store_a}, {category_id}, 'SKU-P2-GUARD', 'P2 Guard Product', 'each', "
                "false, 9.99, 4.00, 10.000, false, true, now()) RETURNING id"
            ).scalar_one()
            product_b = conn.exec_driver_sql(
                "INSERT INTO products (store_id, category_id, sku, name, unit_of_measure, "
                "is_weighed, current_price, current_cost, current_qty_on_hand, "
                "allow_negative_stock, is_active, created_at) VALUES "
                f"({store_b}, {category_id}, 'SKU-P2-GUARD', 'P2 Guard Product B', 'each', "
                "false, 9.99, 0, 0, false, true, now()) RETURNING id"
            ).scalar_one()
            transfer_id = conn.exec_driver_sql(
                "INSERT INTO inter_store_transfers (from_store_id, to_store_id, "
                "transfer_number, status, requested_date, created_at) VALUES "
                f"({store_a}, {store_b}, 'TR-P2-GUARD', 'SHIPPED', CURRENT_DATE, now()) "
                "RETURNING id"
            ).scalar_one()
            conn.exec_driver_sql(
                "INSERT INTO inter_store_transfer_lines (transfer_id, source_product_id, "
                "destination_product_id, requested_quantity, shipped_quantity, "
                "received_quantity, damaged_quantity, unit_cost_at_shipment) VALUES "
                f"({transfer_id}, {product_a}, {product_b}, 10.000, 10.000, 8.000, 2.000, 4.00)"
            )
    finally:
        engine.dispose()

    try:
        with pytest.raises((DBAPIError, InternalError)):
            command.downgrade(cfg, M25_PHASE1_HEAD_REVISION)

        assert _current_revision(migrations_db) == M25_PHASE2_HEAD_REVISION
        assert _table_count(migrations_db) == 70
    finally:
        # Remove the blocking row first -- the guard would otherwise
        # refuse this cleanup downgrade too, for the identical reason,
        # and poison the shared migrations database for every later test.
        engine = create_engine(migrations_db)
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    "DELETE FROM inter_store_transfer_lines WHERE transfer_id = %s",
                    (transfer_id,),
                )
                conn.exec_driver_sql(
                    "DELETE FROM inter_store_transfers WHERE id = %s", (transfer_id,)
                )
                conn.exec_driver_sql(
                    "DELETE FROM products WHERE id IN (%s, %s)", (product_a, product_b)
                )
                conn.exec_driver_sql("DELETE FROM product_categories WHERE id = %s", (category_id,))
                conn.exec_driver_sql("DELETE FROM stores WHERE id IN (%s, %s)", (store_a, store_b))
        finally:
            engine.dispose()
        command.downgrade(cfg, "base")


def test_m25_phase2_downgrade_refuses_when_custody_acceptance_exists(
    migrations_db: str,
) -> None:
    """Same guard, the other triggering condition: a custody-acceptance
    row, which has no table at all in the pre-Phase-2 schema."""
    from sqlalchemy.exc import DBAPIError, InternalError

    cfg = _alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, M25_PHASE2_HEAD_REVISION)

    engine = create_engine(migrations_db)
    try:
        with engine.begin() as conn:
            store_a = conn.exec_driver_sql(
                "INSERT INTO stores (name, timezone, is_active, accounting_entity_id, "
                "created_at) VALUES ('P2 Custody Store A', 'UTC', true, "
                "(SELECT id FROM accounting_entities WHERE is_default = true), now()) "
                "RETURNING id"
            ).scalar_one()
            store_b = conn.exec_driver_sql(
                "INSERT INTO stores (name, timezone, is_active, accounting_entity_id, "
                "created_at) VALUES ('P2 Custody Store B', 'UTC', true, "
                "(SELECT id FROM accounting_entities WHERE is_default = true), now()) "
                "RETURNING id"
            ).scalar_one()
            transfer_id = conn.exec_driver_sql(
                "INSERT INTO inter_store_transfers (from_store_id, to_store_id, "
                "transfer_number, status, requested_date, created_at) VALUES "
                f"({store_a}, {store_b}, 'TR-P2-CUSTODY', 'SHIPPED', CURRENT_DATE, now()) "
                "RETURNING id"
            ).scalar_one()
            receipt_id = conn.exec_driver_sql(
                "INSERT INTO inter_store_transfer_receipts (transfer_id, store_id, "
                "client_transaction_id, received_date, created_at) VALUES "
                f"({transfer_id}, {store_b}, 'rc-p2-custody', CURRENT_DATE, now()) "
                "RETURNING id"
            ).scalar_one()
            conn.exec_driver_sql(
                "INSERT INTO inter_store_transfer_custody_acceptances "
                "(transfer_id, inter_store_transfer_receipt_id, store_id, accepted_at, "
                "created_at) VALUES "
                f"({transfer_id}, {receipt_id}, {store_b}, now(), now())"
            )
    finally:
        engine.dispose()

    try:
        with pytest.raises((DBAPIError, InternalError)):
            command.downgrade(cfg, M25_PHASE1_HEAD_REVISION)

        assert _current_revision(migrations_db) == M25_PHASE2_HEAD_REVISION
        assert _table_count(migrations_db) == 70
    finally:
        # Remove the blocking row first -- see the identical comment in
        # test_m25_phase2_downgrade_refuses_when_damaged_or_short_data_exists.
        engine = create_engine(migrations_db)
        try:
            with engine.begin() as conn:
                conn.exec_driver_sql(
                    "DELETE FROM inter_store_transfer_custody_acceptances "
                    "WHERE transfer_id = %s",
                    (transfer_id,),
                )
                conn.exec_driver_sql(
                    "DELETE FROM inter_store_transfer_receipts WHERE id = %s", (receipt_id,)
                )
                conn.exec_driver_sql(
                    "DELETE FROM inter_store_transfers WHERE id = %s", (transfer_id,)
                )
                conn.exec_driver_sql("DELETE FROM stores WHERE id IN (%s, %s)", (store_a, store_b))
        finally:
            engine.dispose()
        command.downgrade(cfg, "base")


def test_m25_phase2_upgrade_downgrade_reupgrade_preserves_populated_phase1_data(
    migrations_db: str,
) -> None:
    """A purely additive extension must never touch existing rows --
    mirrors the M25 Phase 1 equivalent test, applied to this migration."""
    cfg = _alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, M25_PHASE1_HEAD_REVISION)

    engine = create_engine(migrations_db)
    try:
        with engine.begin() as conn:
            store_id = conn.exec_driver_sql(
                "INSERT INTO stores (name, timezone, is_active, accounting_entity_id, "
                "created_at) VALUES ('P2 Migration Test Store', 'UTC', true, "
                "(SELECT id FROM accounting_entities WHERE is_default = true), now()) "
                "RETURNING id"
            ).scalar_one()
        with engine.connect() as conn:
            before = conn.exec_driver_sql(
                "SELECT id, name, timezone FROM stores WHERE id = %s", (store_id,)
            ).one()
    finally:
        engine.dispose()

    command.upgrade(cfg, M25_PHASE2_HEAD_REVISION)
    command.downgrade(cfg, M25_PHASE1_HEAD_REVISION)
    command.upgrade(cfg, M25_PHASE2_HEAD_REVISION)

    engine = create_engine(migrations_db)
    try:
        with engine.connect() as conn:
            after = conn.exec_driver_sql(
                "SELECT id, name, timezone FROM stores WHERE id = %s", (store_id,)
            ).one()
    finally:
        engine.dispose()

    assert after == before
    command.downgrade(cfg, "base")


def test_m12_alembic_version_privilege_revoked_on_upgrade_and_restored_on_downgrade(
    migrations_db: str,
) -> None:
    """M12 Phase 8: erp_app must have NO privileges on alembic_version at
    the Phase 8 revision (it never legitimately touches migration
    metadata), and the downgrade must restore the exact previous grant --
    proven against the real privilege catalog, not just "the migration
    ran". M12 Phase 11 then grants back SELECT only (for the new
    /health/migration endpoint) at the true M12 head -- write immutability
    (no INSERT/UPDATE/DELETE) must still hold there."""
    cfg = _alembic_config()
    command.downgrade(cfg, "base")
    command.upgrade(cfg, M11_HEAD_REVISION)

    def _erp_app_privileges(table: str) -> set[str]:
        engine = create_engine(migrations_db)
        try:
            with engine.connect() as conn:
                rows = conn.exec_driver_sql(
                    "SELECT privilege_type FROM information_schema.role_table_grants "
                    "WHERE grantee = 'erp_app' AND table_name = %s",
                    (table,),
                ).all()
            return {row[0] for row in rows}
        finally:
            engine.dispose()

    assert _erp_app_privileges("alembic_version") == {"SELECT", "INSERT", "UPDATE", "DELETE"}

    command.upgrade(cfg, M12_PHASE8_REVISION)
    assert _erp_app_privileges("alembic_version") == set()

    command.upgrade(cfg, M12_HEAD_REVISION)
    assert _erp_app_privileges("alembic_version") == {"SELECT"}

    command.downgrade(cfg, M12_PHASE8_REVISION)
    assert _erp_app_privileges("alembic_version") == set()

    command.downgrade(cfg, M11_HEAD_REVISION)
    assert _erp_app_privileges("alembic_version") == {"SELECT", "INSERT", "UPDATE", "DELETE"}

    command.upgrade(cfg, M12_HEAD_REVISION)
    assert _erp_app_privileges("alembic_version") == {"SELECT"}

    command.downgrade(cfg, "base")
