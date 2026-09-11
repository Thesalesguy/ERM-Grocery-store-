"""Cross-cutting DB integrity: foreign keys, and the runtime-role
privilege boundary on append-only ledgers (M1 task Section 1.C).
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session

from app.modules.products.models import Product
from tests.factories import unique_suffix


def test_foreign_key_violation_is_rejected(db: Session) -> None:
    db.add(
        Product(
            store_id=999_999_999,  # no such store
            sku=f"FK-{unique_suffix()}",
            name="Orphan product",
            current_price=0,
            current_cost=0,
            current_qty_on_hand=0,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


@pytest.mark.parametrize("ledger_table", ["audit_logs", "inventory_movements"])
def test_runtime_role_cannot_update_or_delete_ledger_tables(db: Session, ledger_table: str) -> None:
    """The application's runtime role (erp_app — see app/core/config.py and
    the M1 privilege-model migration) can INSERT and SELECT on these
    append-only tables, but UPDATE/DELETE must be rejected by PostgreSQL
    itself, not merely by application code choosing not to call them.

    This test connects using the same engine app.db.session uses for
    everything else — i.e. it proves the actual configured runtime
    connection is restricted, not a separately-constructed one.
    """
    if ledger_table == "audit_logs":
        insert_sql = text(
            "INSERT INTO audit_logs (action, entity_type, created_at) "
            "VALUES ('TEST_ACTION', 'test', now()) RETURNING id"
        )
        update_sql = text("UPDATE audit_logs SET action = 'HACKED' WHERE id = :row_id")
        delete_sql = text("DELETE FROM audit_logs WHERE id = :row_id")
    else:
        from tests.factories import make_product, make_store

        store = make_store(db)
        product = make_product(db, store)
        db.commit()
        insert_sql = text(
            "INSERT INTO inventory_movements "
            "(store_id, product_id, movement_type, quantity_delta, unit_cost_at_movement, "
            " resulting_quantity_on_hand, reference_type, created_at) "
            "VALUES (:store_id, :product_id, 'STOCK_ADJUSTMENT_IN', 1, 0, 1, "
            "'stock_adjustment', now()) RETURNING id"
        ).bindparams(store_id=store.id, product_id=product.id)
        update_sql = text("UPDATE inventory_movements SET reason = 'HACKED' WHERE id = :row_id")
        delete_sql = text("DELETE FROM inventory_movements WHERE id = :row_id")

    row_id = db.execute(insert_sql).scalar_one()
    db.commit()

    with pytest.raises(ProgrammingError, match="permission denied"):
        db.execute(update_sql, {"row_id": row_id})
    db.rollback()

    with pytest.raises(ProgrammingError, match="permission denied"):
        db.execute(delete_sql, {"row_id": row_id})
    db.rollback()
