"""Supplier records."""

from sqlalchemy.orm import Session

from tests.factories import make_supplier


def test_create_supplier(db: Session) -> None:
    supplier = make_supplier(db, contact_name="Jane Doe", phone="+255700000000")
    db.commit()

    assert supplier.id is not None
    assert supplier.is_active is True
    assert supplier.contact_name == "Jane Doe"
