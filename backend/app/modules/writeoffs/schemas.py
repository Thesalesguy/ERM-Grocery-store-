"""Pydantic schemas for the approval-gated transfer write-off workflow.

See app.modules.writeoffs.models for the full design rationale.
"""

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ApprovalDecision = Literal["APPROVED", "DENIED"]


class ApprovalPolicyRuleCreate(BaseModel):
    """Admin-managed policy data (docs/M24D_TECHNICAL_CONTRACT.md Section
    10.1) -- never a hard-coded threshold. `scope_store_id` omitted/None
    means the store-agnostic fallback tier."""

    action_type: Literal["TRANSFER_WRITE_OFF"] = "TRANSFER_WRITE_OFF"
    scope_store_id: int | None = None
    min_amount: Decimal = Field(ge=0)
    max_amount: Decimal | None = Field(default=None, gt=0)
    required_role_id: int
    escalation_role_id: int | None = None
    is_active: bool = True


class ApprovalPolicyRuleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    action_type: str
    scope_store_id: int | None
    min_amount: Decimal
    max_amount: Decimal | None
    required_role_id: int
    escalation_role_id: int | None
    is_active: bool
    created_by: int | None


class TransferWriteOffCreate(BaseModel):
    discrepancy_id: int
    quantity: Decimal = Field(gt=0)
    reason: str = Field(min_length=1, max_length=2000)
    client_transaction_id: str = Field(min_length=1, max_length=100)


class TransferWriteOffRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    transfer_id: int
    transfer_line_id: int
    discrepancy_id: int
    quantity: Decimal
    unit_cost: Decimal
    amount: Decimal
    store_id: int
    reason: str
    initiated_by: int
    initiated_at: datetime
    required_role_id: int
    matched_rule_id: int | None
    status: str
    journal_entry_id: int | None
    reversal_journal_entry_id: int | None
    client_transaction_id: str


class WriteOffDecisionRequest(BaseModel):
    # Mandatory on both APPROVED and DENIED (docs/M24D_TECHNICAL_CONTRACT.md
    # Section 10.4) -- the service layer re-validates this; the schema
    # bound only rejects an obviously-empty request earlier.
    decision_notes: str = Field(min_length=1, max_length=2000)


class WriteOffApprovalRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    write_off_id: int
    initiated_by: int
    approved_by: int
    decision: str
    decided_at: datetime
    decision_notes: str


class WriteOffReverseRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)
