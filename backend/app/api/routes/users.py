import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select

from app.api.deps import DB, CurrentUser, client_ip, require_roles
from app.models.audit import AuditLog
from app.models.user import Role, User
from app.schemas.auth import AuditLogRead, RoleUpdate, UserRead
from app.services.audit import record_audit

router = APIRouter(tags=["users"])
AdminOnly = Depends(require_roles(Role.ADMIN))


@router.get("/users/me", response_model=UserRead)
async def me(user: CurrentUser):
    return user


@router.get("/users", response_model=list[UserRead], dependencies=[AdminOnly])
async def list_users(db: DB):
    return (await db.scalars(select(User).order_by(User.created_at))).all()


@router.patch("/users/{user_id}/role", response_model=UserRead)
async def change_role(
    user_id: uuid.UUID, body: RoleUpdate, request: Request, db: DB,
    admin: User = Depends(require_roles(Role.ADMIN)),
):
    if user_id == admin.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Admins cannot change their own role")
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    old_role = target.role
    target.role = body.role.value
    record_audit(
        db, action="user.role_change", actor_id=admin.id, resource_type="user",
        resource_id=str(target.id), ip_address=client_ip(request),
        details={"from": old_role, "to": body.role.value},
    )
    await db.commit()
    return target


@router.get("/audit-logs", response_model=list[AuditLogRead], dependencies=[AdminOnly])
async def audit_logs(db: DB, limit: int = 100, offset: int = 0):
    limit = max(1, min(limit, 500))
    stmt = select(AuditLog).order_by(AuditLog.created_at.desc()).limit(limit).offset(offset)
    return (await db.scalars(stmt)).all()
