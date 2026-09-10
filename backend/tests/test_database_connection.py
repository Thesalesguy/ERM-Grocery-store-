"""Verifies the SQLAlchemy engine can actually connect to PostgreSQL and that
the foundational migration produced the expected tables."""

from sqlalchemy import inspect, text

from app.db.session import engine


def test_can_execute_a_query() -> None:
    with engine.connect() as conn:
        result = conn.execute(text("SELECT 1")).scalar_one()
    assert result == 1


def test_foundation_tables_exist() -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    expected = {
        "stores",
        "users",
        "roles",
        "permissions",
        "role_permissions",
        "user_roles",
        "audit_logs",
    }
    assert expected.issubset(tables)


def test_users_table_has_expected_unique_constraints() -> None:
    inspector = inspect(engine)
    unique_columns = {tuple(uc["column_names"]) for uc in inspector.get_unique_constraints("users")}
    assert ("username",) in unique_columns
    assert ("email",) in unique_columns
