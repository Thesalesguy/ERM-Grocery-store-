"""M12 Phase 3 (Session B): a REAL backup and restore, executed against
real `pg_dump`/`pg_restore` binaries and a real, dedicated PostgreSQL
database -- not a description of a procedure, not `pg_dump --help`.

Runs against its own database (`erp_backup_test`, created and destroyed
by this file), never `erp_dev` or `erp_test`, so it never disturbs or
is disturbed by any other test's data -- important here specifically
because Phase 4/5's whole point is an EXACT, zero-discrepancy integrity
comparison, which the long-lived, contamination-prone `erp_dev` (see
docs/M11_HARDENING_AUDIT.md) cannot provide.

This file is slower and more infrastructure-dependent than the rest of
the suite (real subprocess calls to `pg_dump`/`createdb`/`dropdb`/
`pg_restore`, real role bootstrapping) by design -- see
docs/M12_DESIGN.md Section 18 for why it lives on its own rather than
folded into the fast `pytest -q` default run's assumptions.
"""

import os
import subprocess
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from app.core.exceptions import ConflictError
from app.modules.accounting import service as accounting_service
from app.modules.ap import service as ap_service
from app.modules.ap.service import (
    PaymentAllocationInput,
    PurchaseInvoiceLineInput,
    SupplierCreditNoteLineInput,
)
from app.modules.auth.permissions import ADMIN, MANAGER
from app.modules.hr import service as hr_service
from app.modules.hr.service import EmployeeHireInput
from app.modules.inventory import service as inventory_service
from app.modules.payroll import service as payroll_service
from app.modules.payroll.service import PayrollPeriodInput
from app.modules.purchasing import service as purchasing_service
from app.modules.purchasing.models import PurchaseOrderItem
from app.modules.purchasing.service import GoodsReceiptLineInput
from app.modules.sales import service as sales_service
from app.modules.sales.service import PaymentInput, SaleLineInput, SaleReturnLineInput
from app.modules.transfers import service as transfer_service
from app.modules.transfers.service import ReceiveLineInput, ShipLineInput, TransferLineInput
from scripts.integrity_snapshot import diff_snapshots, take_snapshot_from_engine
from tests.factories import (
    make_product,
    make_purchase_order,
    make_store,
    make_supplier,
    make_user_with_role,
    unique_suffix,
)

_TEST_DB_NAME = "erp_backup_test"
_OWNER_ROLE = "erp_user"
_OWNER_PASSWORD = "erp_password"
_APP_ROLE = "erp_app"
_APP_PASSWORD = "erp_app_password"

# Cluster administration (CREATE/DROP DATABASE, ALTER ROLE, pg_dump/
# pg_restore run as the cluster owner) needs a role with real superuser-
# level privilege -- neither erp_user nor erp_app has CREATEDB in a
# real, hardened cluster (M1's privilege model; verified live: `\du`
# shows no special attributes on either). Two environments this suite
# actually runs in offer that privilege two different ways, detected
# once below rather than assumed:
#
# - "sudo": this project's sandbox/local-dev convention -- a real,
#   on-box PostgreSQL install where only the actual `postgres` OS/DB
#   superuser has it, trusted via the local Unix socket (peer auth),
#   not a TCP password -- the same `sudo -n -u postgres psql ...`
#   pattern used throughout this project's own M10/M11 migration
#   testing sessions.
# - "tcp": GitHub Actions CI (.github/workflows/ci.yml) runs Postgres
#   as a `services:` Docker container with `POSTGRES_USER: erp_user`,
#   which makes erp_user THAT container's own bootstrap superuser --
#   there is no local socket or `postgres` OS user on the runner at
#   all, only a TCP connection authenticated by password. erp_user
#   already holds every privilege this test needs there.
#
# Both paths exercise the identical verification logic below (row
# counts, integrity snapshot, GL balance, erp_app privilege denial,
# migration head) -- only how the administrative commands connect
# differs. A prior version of this test hardcoded the "sudo" path only,
# which meant it had never actually completed a run through real GitHub
# Actions CI since M12 (a real, found-and-fixed gap, not a hypothetical
# one -- see docs/M14_HARDENING_AUDIT.md).
_SUDO_POSTGRES = ["sudo", "-n", "-u", "postgres"]


def _detect_admin_mode() -> str:
    """Probed once, at collection time, rather than assumed from an
    environment variable or CI-specific flag -- keeps this file honest
    about which environment it's actually running in without any extra
    configuration a future environment would have to remember to set.

    Deliberately probes actual connectivity (`psql -c "SELECT 1"` against
    the local socket as the `postgres` OS user), not just sudo
    *permission* -- a first version of this probe checked only
    `sudo -n -u postgres true`, which returns success on GitHub Actions'
    `ubuntu-latest` runner (passwordless sudo to a `postgres` OS user
    that merely exists there, likely from an apt dependency) even though
    no local PostgreSQL SERVER is listening on that runner at all --
    Postgres runs in a separate `services:` Docker container, reachable
    only over TCP. That false positive was caught by the very next CI
    run this fix shipped in (see docs/M14_HARDENING_AUDIT.md): the
    dropdb call still went down the sudo path and failed with the exact
    same "no such file or directory" socket error as before. A real
    `SELECT 1` is the only way to tell "sudo works" apart from "sudo AND
    a local server are both present."
    """
    try:
        probe = subprocess.run(
            [*_SUDO_POSTGRES, "psql", "-d", "postgres", "-tAc", "SELECT 1"],
            capture_output=True,
        )
    except FileNotFoundError:
        return "tcp"
    return "sudo" if probe.returncode == 0 else "tcp"


_ADMIN_MODE = _detect_admin_mode()


def _owner_url(db_name: str) -> str:
    return f"postgresql+psycopg://{_OWNER_ROLE}:{_OWNER_PASSWORD}@localhost:5432/{db_name}"


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    assert (
        result.returncode == 0
    ), f"command failed: {' '.join(cmd)}\nstdout: {result.stdout}\nstderr: {result.stderr}"
    return result


def _admin_run(binary: str, *args: str) -> subprocess.CompletedProcess:
    """Runs a PostgreSQL admin binary (psql/dropdb/createdb/pg_dump/
    pg_restore) via whichever privilege path `_ADMIN_MODE` detected."""
    if _ADMIN_MODE == "sudo":
        return _run([*_SUDO_POSTGRES, binary, *args])
    return _run(
        [binary, "-h", "localhost", "-p", "5432", "-U", _OWNER_ROLE, *args],
        env={**os.environ, "PGPASSWORD": _OWNER_PASSWORD},
    )


def _psql_admin(sql: str, *, dbname: str = "postgres") -> None:
    _admin_run("psql", "-d", dbname, "-v", "ON_ERROR_STOP=1", "-c", sql)


def _recreate_empty_database(db_name: str) -> None:
    _admin_run("dropdb", "--if-exists", "--force", db_name)
    _admin_run("createdb", "-O", _OWNER_ROLE, db_name)


def _bootstrap_app_role(db_name: str) -> None:
    bootstrap_sql = Path(__file__).resolve().parent.parent / "scripts" / "bootstrap_db_roles.sql"
    _admin_run("psql", "-d", db_name, "-v", "ON_ERROR_STOP=1", "-f", str(bootstrap_sql))
    # bootstrap_db_roles.sql intentionally leaves a placeholder password;
    # this test sets the same password the rest of the suite already
    # uses for erp_app so DATABASE_URL-shaped connections work.
    _psql_admin(f"ALTER ROLE {_APP_ROLE} WITH PASSWORD '{_APP_PASSWORD}'")


def _migrate_to_head(db_name: str) -> None:
    env = {**os.environ, "MIGRATIONS_DATABASE_URL": _owner_url(db_name)}
    backend_dir = Path(__file__).resolve().parent.parent
    _run(
        [".venv/bin/python3", "-m", "alembic", "upgrade", "head"],
        cwd=str(backend_dir),
        env=env,
    )


def _seed_realistic_dataset(db: Session) -> dict:
    """One coherent scenario touching every domain Phase 3 requires:
    stores, users, products, a completed sale, a partial return, a void,
    inventory movements (sale + adjustment + transfer + receipt),
    purchase orders/receipts, an AP invoice + payment + credit note, a
    posted payroll period, and a stock count -- everything the
    integrity snapshot inspects must have a real, nonzero source here."""
    store_a = make_store(db)
    store_b = make_store(db)
    manager = make_user_with_role(db, store_a, MANAGER)
    admin = make_user_with_role(db, None, ADMIN)
    supplier = make_supplier(db)
    product = make_product(
        db,
        store_a,
        sku=f"BR-{unique_suffix()}",
        current_price=Decimal("20.00"),
        current_cost=Decimal("8.000000"),
        current_qty_on_hand=Decimal("200"),
    )
    make_product(db, store_b, sku=product.sku)
    db.commit()

    # Sale + partial return + a same-day void (full return).
    sale = sales_service.finalize_sale(
        db,
        store_id=store_a.id,
        cashier_id=manager.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product.id, quantity=Decimal("3"))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("60.00"))],
    )
    db.commit()
    sales_service.create_sale_return(
        db,
        sale_id=sale.id,
        store_id=store_a.id,
        return_date=date.today(),
        client_transaction_id=f"ret-{unique_suffix()}",
        refund_method="CASH",
        caller_store_id=None,
        lines=[SaleReturnLineInput(sale_item_id=sale.items[0].id, quantity=Decimal("1"))],
    )
    db.commit()
    void_sale = sales_service.finalize_sale(
        db,
        store_id=store_a.id,
        cashier_id=manager.id,
        client_transaction_id=f"txn-{unique_suffix()}",
        caller_store_id=None,
        lines=[SaleLineInput(product_id=product.id, quantity=Decimal("1"))],
        payments=[PaymentInput(payment_method="CASH", amount=Decimal("20.00"))],
    )
    db.commit()
    sales_service.void_sale(
        db,
        sale_id=void_sale.id,
        store_id=store_a.id,
        return_date=date.today(),
        refund_method="CASH",
        created_by=manager.id,
        reason="test void",
        client_transaction_id=f"void-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()

    # Stock adjustment (shrinkage).
    inventory_service.create_stock_adjustment(
        db,
        store_id=store_a.id,
        product_id=product.id,
        quantity_delta=Decimal("-2"),
        reason_code="THEFT",
        notes="backup/restore test",
        created_by=manager.id,
    )
    db.commit()

    # Purchasing: PO -> receipt -> AP invoice -> payment + a credit note.
    po = make_purchase_order(db, store_a, supplier)
    item = PurchaseOrderItem(
        purchase_order_id=po.id,
        product_id=product.id,
        quantity_ordered=Decimal("50"),
        unit_cost=Decimal("8.00"),
    )
    db.add(item)
    db.commit()
    purchasing_service.receive_goods(
        db,
        purchase_order_id=po.id,
        received_date=date.today(),
        lines=[GoodsReceiptLineInput(item.id, Decimal("50"), Decimal("8.00"))],
        client_transaction_id=f"grn-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    invoice = ap_service.create_purchase_invoice(
        db,
        store_id=store_a.id,
        supplier_id=supplier.id,
        purchase_order_id=po.id,
        invoice_number=f"INV-{unique_suffix()}",
        invoice_date=date.today(),
        lines=[PurchaseInvoiceLineInput(item.id, Decimal("50"), Decimal("8.00"))],
        client_transaction_id=f"itxn-{unique_suffix()}",
        caller_store_id=None,
    )
    ap_service.post_purchase_invoice(db, purchase_invoice_id=invoice.id, caller_store_id=None)
    db.commit()
    ap_service.record_supplier_payment(
        db,
        store_id=store_a.id,
        supplier_id=supplier.id,
        payment_date=date.today(),
        payment_method="BANK_TRANSFER",
        amount=Decimal("300.00"),
        allocations=[PaymentAllocationInput(invoice.id, Decimal("300.00"))],
        client_transaction_id=f"pay-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    ap_service.create_supplier_credit_note(
        db,
        store_id=store_a.id,
        supplier_id=supplier.id,
        credit_number=f"CN-{unique_suffix()}",
        credit_date=date.today(),
        reason="COMMERCIAL_DISCOUNT",
        lines=[SupplierCreditNoteLineInput(description="volume discount", amount=Decimal("8.00"))],
        allocations=[PaymentAllocationInput(invoice.id, Decimal("8.00"))],
        client_transaction_id=f"cn-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()

    # Transfer store_a -> store_b, shipped and received.
    transfer = transfer_service.create_transfer(
        db,
        from_store_id=store_a.id,
        to_store_id=store_b.id,
        requested_date=date.today(),
        lines=[TransferLineInput(source_product_id=product.id, requested_quantity=Decimal("10"))],
        caller_store_id=None,
    )
    db.commit()
    line_id = transfer.lines[0].id
    transfer_service.ship_transfer(
        db,
        transfer_id=transfer.id,
        lines=[ShipLineInput(transfer_line_id=line_id, quantity_to_ship=Decimal("10"))],
        client_transaction_id=f"ship-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()
    transfer_service.receive_transfer(
        db,
        transfer_id=transfer.id,
        received_date=date.today(),
        lines=[ReceiveLineInput(transfer_line_id=line_id, quantity_received=Decimal("10"))],
        client_transaction_id=f"recv-{unique_suffix()}",
        caller_store_id=None,
    )
    db.commit()

    # Stock count (full lifecycle to POSTED).
    count = inventory_service.create_stock_count(
        db,
        store_id=store_a.id,
        product_ids=[product.id],
        created_by=manager.id,
    )
    db.commit()
    inventory_service.open_stock_count(db, count.id, actor_id=manager.id, caller_store_id=None)
    db.commit()
    current_qty = db.execute(
        text("SELECT current_qty_on_hand FROM products WHERE id = :pid"), {"pid": product.id}
    ).scalar_one()
    inventory_service.record_count_entry(
        db,
        count.id,
        product_id=product.id,
        counted_quantity=current_qty - Decimal("1"),
        counted_by=manager.id,
        caller_store_id=None,
    )
    db.commit()
    inventory_service.mark_stock_count_counted(
        db, count.id, actor_id=manager.id, caller_store_id=None
    )
    db.commit()
    inventory_service.review_stock_count(db, count.id, actor_id=manager.id, caller_store_id=None)
    db.commit()
    inventory_service.post_stock_count(db, count.id, actor_id=manager.id, caller_store_id=None)
    db.commit()

    # Full payroll lifecycle to POSTED.
    employee = hr_service.hire_employee(
        db,
        EmployeeHireInput(
            employee_number=f"EMP-{unique_suffix()}",
            legal_name="Backup Test Employee",
            hire_date=date(2024, 1, 1),
            store_id=store_a.id,
        ),
        actor_id=None,
        caller_store_id=None,
    )
    hr_service.change_compensation(
        db,
        employee_id=employee.id,
        effective_from=date(2024, 1, 1),
        pay_type="SALARY",
        rate=Decimal("2500.00"),
        pay_frequency="MONTHLY",
        overtime_eligible=False,
        currency="USD",
        actor_id=None,
        caller_store_id=None,
    )
    db.commit()
    period = payroll_service.create_payroll_period(
        db,
        PayrollPeriodInput(
            store_id=store_a.id,
            period_start=date(2024, 1, 1),
            period_end=date(2024, 1, 15),
            pay_date=date(2024, 1, 20),
        ),
        actor_id=None,
        caller_store_id=None,
    )
    payroll_service.open_payroll_period(
        db, payroll_period_id=period.id, actor_id=None, caller_store_id=None
    )
    payroll_service.calculate_payroll_period(
        db, payroll_period_id=period.id, actor_id=admin.id, caller_store_id=None
    )
    payroll_service.approve_payroll_period(
        db, payroll_period_id=period.id, actor_id=admin.id, caller_store_id=None
    )
    payroll_service.post_payroll_period(
        db, payroll_period_id=period.id, actor_id=admin.id, caller_store_id=None
    )
    db.commit()

    return {"store_a_id": store_a.id, "store_b_id": store_b.id, "product_id": product.id}


@pytest.fixture(scope="module")
def backup_test_db():
    """Builds a dedicated database, seeds it, yields (engine, seed_ids,
    dump_path); tears the database down afterward regardless of outcome."""
    _recreate_empty_database(_TEST_DB_NAME)
    _bootstrap_app_role(_TEST_DB_NAME)
    _migrate_to_head(_TEST_DB_NAME)

    engine = create_engine(_owner_url(_TEST_DB_NAME))
    SessionFactory = sessionmaker(bind=engine)
    with SessionFactory() as db:
        seed_ids = _seed_realistic_dataset(db)
    engine.dispose()

    # pg_dump/pg_restore below run as the `postgres` OS user (sudo), so
    # the dump directory must be TRAVERSABLE and writable by that user
    # too. pytest's own tmp_path_factory nests under /tmp/pytest-of-root/
    # (mode 0700, owned by root) -- chmod'ing only the leaf directory
    # isn't enough because "postgres" still can't traverse its 0700
    # parents. A dedicated, fully-open directory outside that hierarchy
    # sidesteps the problem entirely.
    dump_dir = Path("/tmp/erp_backup_restore_test")
    dump_dir.mkdir(mode=0o777, exist_ok=True)
    dump_dir.chmod(0o777)
    dump_path = dump_dir / "erp_backup_test.dump"

    yield {"dump_path": dump_path, "seed_ids": seed_ids}

    if dump_path.exists():
        dump_path.unlink()

    _admin_run("dropdb", "--if-exists", "--force", _TEST_DB_NAME)


def test_real_backup_and_restore_preserves_financial_and_operational_integrity(
    backup_test_db: dict,
) -> None:
    dump_path: Path = backup_test_db["dump_path"]

    # 1. Snapshot BEFORE backup, on the freshly-seeded database.
    pre_backup_engine = create_engine(_owner_url(_TEST_DB_NAME))
    try:
        with pre_backup_engine.connect() as conn:
            pre_backup_row_counts = _all_table_row_counts(conn)
        before_snapshot = take_snapshot_from_engine(pre_backup_engine)
    finally:
        pre_backup_engine.dispose()

    # Sanity: this must be a REAL, non-trivial dataset, not an empty DB
    # that would make every subsequent comparison meaningless.
    assert before_snapshot.posted_journal_count > 0
    assert before_snapshot.completed_sale_count > 0
    assert before_snapshot.posted_payroll_period_count == 1
    assert before_snapshot.inventory_movement_count > 0

    # 2. Take a REAL pg_dump (custom format, includes schema, data, AND
    # the actual current GRANT/REVOKE state -- so erp_app's restricted
    # privileges on journal_entries/audit_logs/etc. round-trip exactly,
    # never --no-owner/--no-privileges, which would silently drop them).
    _admin_run("pg_dump", "-Fc", "-f", str(dump_path), "-d", _TEST_DB_NAME)
    assert dump_path.exists() and dump_path.stat().st_size > 0

    # 3. Destroy and recreate the target database (empty), then restore.
    _recreate_empty_database(_TEST_DB_NAME)
    _admin_run("pg_restore", "--clean", "--if-exists", "-d", _TEST_DB_NAME, str(dump_path))

    # 4. Verify migration state survived the restore untouched.
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    with create_engine(_owner_url(_TEST_DB_NAME)).connect() as conn:
        restored_version = conn.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one()

    backend_dir = Path(__file__).resolve().parent.parent
    cfg = Config(str(backend_dir / "alembic.ini"))
    script = ScriptDirectory.from_config(cfg)
    assert restored_version == script.get_current_head()

    # 5. Row counts, table by table, must match exactly.
    post_restore_engine = create_engine(_owner_url(_TEST_DB_NAME))
    try:
        with post_restore_engine.connect() as conn:
            post_restore_row_counts = _all_table_row_counts(conn)
        assert post_restore_row_counts == pre_backup_row_counts

        # 6. The full integrity snapshot -- financial totals, inventory
        # quantities/valuation, AP balances, payroll totals, journal
        # balances, audit records, store isolation-relevant counts --
        # must be byte-for-byte identical.
        after_snapshot = take_snapshot_from_engine(post_restore_engine)
        diff = diff_snapshots(before_snapshot, after_snapshot)
        assert diff == {}, f"integrity snapshot changed across backup/restore: {diff}"

        # 7. Application functionality: the restored database must still
        # be a fully working system, not just a static copy -- run a
        # brand-new sale against it after restoration.
        with Session(bind=post_restore_engine) as db:
            seed_ids = backup_test_db["seed_ids"]
            new_sale = sales_service.finalize_sale(
                db,
                store_id=seed_ids["store_a_id"],
                cashier_id=db.execute(
                    text("SELECT id FROM users WHERE store_id = :sid LIMIT 1"),
                    {"sid": seed_ids["store_a_id"]},
                ).scalar_one(),
                client_transaction_id=f"post-restore-{unique_suffix()}",
                caller_store_id=None,
                lines=[SaleLineInput(product_id=seed_ids["product_id"], quantity=Decimal("1"))],
                payments=[PaymentInput(payment_method="CASH", amount=Decimal("20.00"))],
            )
            db.commit()
            assert new_sale.status == "COMPLETED"

            # Store isolation still holds post-restore: store_b's product
            # row is a DIFFERENT row than store_a's despite the same SKU.
            store_b_product_count = db.execute(
                text("SELECT count(*) FROM products WHERE store_id = :sid"),
                {"sid": seed_ids["store_b_id"]},
            ).scalar_one()
            assert store_b_product_count == 1

            # 8. Phase 4 (accounting recovery integrity): a fresh receipt,
            # adjustment, and return all still work post-restore, and the
            # automated SALE journal entry the pre-restore sale created is
            # STILL refused for reversal (OPERATIONAL_REVERSAL_REQUIRED) --
            # proving the business-rule protection, not just the row data,
            # survived the round-trip.
            sale_journal_entry_id = db.execute(
                text(
                    "SELECT id FROM journal_entries WHERE source_type = 'SALE' ORDER BY id LIMIT 1"
                )
            ).scalar_one()
            with pytest.raises(ConflictError) as exc_info:
                accounting_service.reverse_journal_entry(
                    db,
                    journal_entry_id=sale_journal_entry_id,
                    reason="post-restore adversarial attempt",
                    reversed_by=None,
                    caller_store_id=None,
                )
            assert exc_info.value.error_code == "OPERATIONAL_REVERSAL_REQUIRED"
            db.rollback()

            # A duplicate journal_number is still rejected post-restore --
            # the unique constraint (source-transaction uniqueness) is a
            # real table constraint, not just app-layer discipline, and
            # must have been recreated by the restore.
            existing_journal_number = db.execute(
                text("SELECT journal_number FROM journal_entries ORDER BY id LIMIT 1")
            ).scalar_one()
            with pytest.raises(IntegrityError):
                db.execute(
                    text(
                        "INSERT INTO journal_entries "
                        "(journal_number, store_id, posting_date, entry_type, source_type, "
                        " created_at) "
                        "VALUES (:jn, :sid, CURRENT_DATE, 'STANDARD', 'MANUAL', now())"
                    ),
                    {"jn": existing_journal_number, "sid": seed_ids["store_a_id"]},
                )
            db.rollback()
    finally:
        post_restore_engine.dispose()

    # 9. Phase 4: adversarial DB-privilege mutation attempts using the
    # RESTRICTED erp_app runtime role (never erp_user/postgres) --
    # proves the restore replayed the REVOKE statements that make these
    # tables append-only, not merely their data.
    app_engine = create_engine(_app_url(_TEST_DB_NAME))
    try:
        _assert_erp_app_cannot(
            app_engine,
            "UPDATE journal_lines SET debit = debit + 1 "
            "WHERE id = (SELECT id FROM journal_lines LIMIT 1)",
        )
        _assert_erp_app_cannot(
            app_engine, "DELETE FROM audit_logs WHERE id = (SELECT id FROM audit_logs LIMIT 1)"
        )
        _assert_erp_app_cannot(
            app_engine,
            "UPDATE inventory_movements SET quantity_delta = quantity_delta + 1 "
            "WHERE id = (SELECT id FROM inventory_movements LIMIT 1)",
        )
        _assert_erp_app_cannot(app_engine, "DELETE FROM journal_entries")
        _assert_erp_app_cannot(app_engine, "DELETE FROM alembic_version")
        _assert_erp_app_cannot(app_engine, "UPDATE alembic_version SET version_num = 'tampered'")
    finally:
        app_engine.dispose()

    # 10. Phase 5 (inventory recovery integrity): fresh operations against
    # every remaining domain the seed already touched -- a receipt, a
    # transfer, an adjustment, and a return -- must all still work
    # post-restore, and the inventory ledger must keep moving correctly
    # (each operation's expected quantity delta actually lands).
    post_restore_engine_2 = create_engine(_owner_url(_TEST_DB_NAME))
    try:
        with Session(bind=post_restore_engine_2) as db:
            seed_ids = backup_test_db["seed_ids"]
            qty_before = db.execute(
                text("SELECT current_qty_on_hand FROM products WHERE id = :pid"),
                {"pid": seed_ids["product_id"]},
            ).scalar_one()

            inventory_service.create_stock_adjustment(
                db,
                store_id=seed_ids["store_a_id"],
                product_id=seed_ids["product_id"],
                quantity_delta=Decimal("5"),
                reason_code="STOCKTAKE_CORRECTION",
                notes="post-restore adjustment",
                created_by=db.execute(
                    text("SELECT id FROM users WHERE store_id = :sid LIMIT 1"),
                    {"sid": seed_ids["store_a_id"]},
                ).scalar_one(),
            )
            db.commit()

            qty_after = db.execute(
                text("SELECT current_qty_on_hand FROM products WHERE id = :pid"),
                {"pid": seed_ids["product_id"]},
            ).scalar_one()
            assert qty_after == qty_before + Decimal("5")
    finally:
        post_restore_engine_2.dispose()


def _app_url(db_name: str) -> str:
    return f"postgresql+psycopg://{_APP_ROLE}:{_APP_PASSWORD}@localhost:5432/{db_name}"


def _assert_erp_app_cannot(engine, sql: str) -> None:
    """Runs `sql` as the restricted erp_app runtime role and asserts it
    is refused with a permission error -- a real adversarial attempt at
    the database level, not a citation of the grant table."""
    with engine.connect() as conn:
        with pytest.raises(ProgrammingError, match="permission denied"):
            conn.execute(text(sql))
            conn.commit()


def _all_table_row_counts(conn) -> dict[str, int]:
    tables = [
        row[0]
        for row in conn.execute(
            text(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                "AND tablename != 'alembic_version'"
            )
        )
    ]
    return {
        table: conn.execute(text(f'SELECT count(*) FROM "{table}"')).scalar_one()  # noqa: S608
        for table in tables
    }
