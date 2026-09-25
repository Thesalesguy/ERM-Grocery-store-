"""M20 Session I: fiscal credentials/secrets never leak -- via API
responses, logs, or the audit trail's own recorded values
(docs/M20_DESIGN.md Section 6).

`credential_reference` is a NAME/POINTER (e.g. an environment variable
name), never a secret value -- no route or log ever resolves it to
anything, because nothing resolvable is ever stored in this
application's own database.
"""

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.audit.models import AuditLog
from app.modules.auth.permissions import ADMIN, CASHIER
from app.modules.fiscal import service as fiscal_service
from app.modules.fiscal.models import FiscalConfig
from app.modules.fiscal.schemas import FiscalConfigRead
from tests.factories import (
    DEFAULT_TEST_PASSWORD,
    make_store,
    make_user,
    make_user_with_role,
    unique_suffix,
)
from tests.helpers import auth_headers


def test_fiscal_config_read_schema_has_no_field_that_could_carry_a_resolved_secret() -> None:
    """The schema itself is the security boundary: it declares no field
    named/shaped like a resolved credential value -- only the reference
    NAME, which is not sensitive."""
    field_names = set(FiscalConfigRead.model_fields)
    assert field_names == {
        "id",
        "store_id",
        "is_enabled",
        "provider_name",
        "credential_reference",
        "submission_endpoint",
        "retry_max_attempts",
        "updated_by",
        "created_at",
        "updated_at",
    }


def test_get_config_endpoint_returns_only_the_reference_name_never_a_secret_value(
    client: TestClient, db: Session
) -> None:
    store = make_store(db)
    username = f"fiscal_admin_sec_{unique_suffix()}"
    admin = make_user_with_role(db, store, ADMIN, username=username)
    fiscal_service.upsert_config(
        db,
        store_id=store.id,
        is_enabled=True,
        provider_name="FAKE",
        credential_reference="FISCAL_API_KEY_STORE_X",
        submission_endpoint="https://example.invalid/submit",
        retry_max_attempts=5,
        updated_by=admin.id,
    )
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.get("/api/v1/fiscal/config", params={"store_id": store.id}, headers=headers)
    assert response.status_code == 200
    body = response.json()
    # The reference NAME is visible (it is not a secret) -- but nothing
    # resembling a resolved value (this app never had one to leak).
    assert body["credential_reference"] == "FISCAL_API_KEY_STORE_X"
    assert set(body) == {
        "id",
        "store_id",
        "is_enabled",
        "provider_name",
        "credential_reference",
        "submission_endpoint",
        "retry_max_attempts",
        "updated_by",
        "created_at",
        "updated_at",
    }


def test_cashier_without_fiscal_permission_cannot_read_config_at_all(
    client: TestClient, db: Session
) -> None:
    store = make_store(db)
    db.add(
        FiscalConfig(
            store_id=store.id,
            is_enabled=True,
            provider_name="FAKE",
            credential_reference="SECRET_NAME",
        )
    )
    username = f"fiscal_cashier_sec_{unique_suffix()}"
    make_user_with_role(db, store, CASHIER, username=username)
    db.commit()
    headers = auth_headers(client, username, DEFAULT_TEST_PASSWORD)

    response = client.get("/api/v1/fiscal/config", params={"store_id": store.id}, headers=headers)
    assert response.status_code == 403
    assert "SECRET_NAME" not in response.text


def test_fiscal_config_update_is_audited_without_leaking_a_secret_value(db: Session) -> None:
    """credential_reference is a name, not a secret, so recording it in
    the audit trail is safe (docs/M20_DESIGN.md Section 6) -- this test
    proves the audit event exists and carries that name, not that it's
    hidden (there is nothing sensitive to hide)."""
    store = make_store(db)
    admin = make_user(db, store)
    fiscal_service.upsert_config(
        db,
        store_id=store.id,
        is_enabled=True,
        provider_name="FAKE",
        credential_reference="FISCAL_API_KEY_STORE_Y",
        submission_endpoint=None,
        retry_max_attempts=5,
        updated_by=admin.id,
    )
    db.commit()

    entry = (
        db.execute(
            select(AuditLog)
            .where(
                AuditLog.entity_type == "fiscal_config", AuditLog.action == "FISCAL_CONFIG_UPDATED"
            )
            .order_by(AuditLog.id.desc())
        )
        .scalars()
        .first()
    )
    assert entry is not None
    assert entry.after_state["credential_reference"] == "FISCAL_API_KEY_STORE_Y"
    assert entry.after_state["is_enabled"] is True


def test_logging_redaction_list_covers_credential_reference() -> None:
    """core/logging.py's existing secret-redaction list (M12) is extended
    with credential_reference (docs/M20_DESIGN.md Section 6) -- proven by
    reading the actual redaction set, not just trusting the docstring."""
    from app.core.logging import _REDACTED_KEYS

    assert "credential_reference" in _REDACTED_KEYS
