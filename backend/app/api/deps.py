import uuid
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decode_token
from app.db.session import get_db
from app.models.user import Role, User
from app.services.audit import record_audit

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")

DB = Annotated[AsyncSession, Depends(get_db)]


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


async def get_current_user(token: Annotated[str, Depends(oauth2_scheme)], db: DB) -> User:
    unauthorized = HTTPException(
        status.HTTP_401_UNAUTHORIZED, "Invalid or expired token", headers={"WWW-Authenticate": "Bearer"}
    )
    try:
        payload = decode_token(token, expected_type="access")
        user_id = uuid.UUID(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise unauthorized
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise unauthorized
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_roles(*allowed: Role):
    """RBAC guard. Usage: `Depends(require_roles(Role.ADMIN))`."""
    allowed_values = {r.value for r in allowed}

    async def checker(request: Request, user: CurrentUser, db: DB) -> User:
        if user.role not in allowed_values:
            record_audit(
                db,
                action="rbac.denied",
                actor_id=user.id,
                status="denied",
                ip_address=client_ip(request),
                details={"path": request.url.path, "required": sorted(allowed_values)},
            )
            await db.commit()
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient permissions")
        return user

    return checker
