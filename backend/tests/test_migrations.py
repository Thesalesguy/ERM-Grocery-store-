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
from sqlalchemy import create_engine, inspect

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

    command.upgrade(cfg, "head")
    # M9 adds two tables: supplier_products, replenishment_plans (the
    # products.target_stock_quantity/minimum_stock_quantity columns and
    # the purchase_orders/inter_store_transfers.replenishment_plan_id
    # back-links are columns, not tables).
    assert _table_count(migrations_db) == 46

    command.downgrade(cfg, M0_REVISION)
    assert _table_count(migrations_db) == 8

    command.upgrade(cfg, "head")
    assert _table_count(migrations_db) == 46
    assert _current_revision(migrations_db) == M9_HEAD_REVISION


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
    assert role_count == 5
    assert permission_count == 34


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
    command.upgrade(cfg, "head")

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
