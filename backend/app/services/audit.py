from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditLog


def record_audit(
    db: AsyncSession,
    *,
    action: str,
    actor_id=None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    status: str = "success",
    ip_address: str | None = None,
    details: dict | None = None,
) -> None:
    """Stage an audit row in the current transaction. The caller commits."""
    db.add(
        AuditLog(
            actor_id=actor_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            status=status,
            ip_address=ip_address,
            details=details,
        )
    )
