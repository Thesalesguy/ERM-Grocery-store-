"""API-contract schemas for fiscal configuration and submissions
(docs/M20_DESIGN.md Section 8).

`credential_reference` is a NAME/POINTER (e.g. an environment variable
name), never a secret value -- see docs/M20_DESIGN.md Section 6. No
schema in this module has a field that could carry a resolved credential,
because none is ever stored in this application's own database.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class FiscalConfigRead(BaseModel):
    id: int
    store_id: int
    is_enabled: bool
    provider_name: str
    credential_reference: str | None
    submission_endpoint: str | None
    retry_max_attempts: int
    updated_by: int | None
    created_at: datetime
    updated_at: datetime | None

    model_config = ConfigDict(from_attributes=True)


class FiscalConfigUpdate(BaseModel):
    store_id: int
    is_enabled: bool
    provider_name: str = Field(min_length=1, max_length=50)
    credential_reference: str | None = Field(default=None, max_length=255)
    submission_endpoint: str | None = Field(default=None, max_length=500)
    retry_max_attempts: int = Field(default=5, ge=1)

    model_config = ConfigDict(extra="forbid")


class FiscalSubmissionRead(BaseModel):
    id: int
    sale_id: int
    store_id: int
    status: str
    provider_name: str
    attempt_count: int
    max_attempts: int
    last_error: str | None
    last_attempted_at: datetime | None
    submitted_at: datetime | None
    fiscal_reference: str | None
    request_payload: dict[str, Any]
    response_payload: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime | None

    model_config = ConfigDict(from_attributes=True)
