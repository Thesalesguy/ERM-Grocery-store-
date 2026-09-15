from datetime import datetime

from pydantic import BaseModel, ConfigDict


class AuditLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int | None
    action: str
    entity_type: str
    entity_id: int | None
    before_state: dict | None
    after_state: dict | None
    ip_address: str | None
    user_agent: str | None
    created_at: datetime
