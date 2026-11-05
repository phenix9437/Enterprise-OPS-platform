import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.user import utcnow


class ApprovalPolicy(Base):
    """An admin-defined rule. Policies can only make things STRICTER than the built-in baseline
    (high-risk tools always need approval); they can never switch that baseline off."""

    __tablename__ = "approval_policies"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    tool_pattern: Mapped[str] = mapped_column(String(120), default="*")       # glob, e.g. "gmail.*"
    min_risk: Mapped[str] = mapped_column(String(10), default="low")          # matches calls at/above this risk
    arg_conditions: Mapped[list] = mapped_column(JSON, default=list)          # all must match
    action: Mapped[str] = mapped_column(String(20))                           # require_approval | block
    approver_role: Mapped[str] = mapped_column(String(20), default="manager") # manager | admin
    allow_self_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_in_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)          # shown to reviewer / fed back to the agent
    created_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Approval(Base):
    """One request for a human decision, plus the decision. Kept forever (audit trail)."""

    __tablename__ = "approvals"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("tasks.id"), nullable=True, index=True)
    step_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)  # pending|approved|rejected|expired
    reason: Mapped[str] = mapped_column(Text, default="")
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    policies: Mapped[list] = mapped_column(JSON, default=list)                # names of the policies that applied
    required_role: Mapped[str] = mapped_column(String(20), default="manager")
    allow_self_approval: Mapped[bool] = mapped_column(Boolean, default=False)
    requested_by: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id"), index=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    proposed_calls: Mapped[list] = mapped_column(JSON)                        # what the agent asked to do
    final_calls: Mapped[list | None] = mapped_column(JSON, nullable=True)     # what was actually approved
    modified: Mapped[bool] = mapped_column(Boolean, default=False)
    modifications: Mapped[list | None] = mapped_column(JSON, nullable=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
