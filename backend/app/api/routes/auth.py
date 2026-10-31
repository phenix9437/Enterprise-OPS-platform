import uuid
from typing import Annotated

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.api.deps import DB, client_ip
from app.core.security import DUMMY_HASH, create_token, decode_token, hash_password, verify_password
from app.models.user import Role, User
from app.schemas.auth import RefreshRequest, TokenPair, UserCreate, UserRead
from app.services.audit import record_audit

router = APIRouter(prefix="/auth", tags=["auth"])


def _issue_tokens(user: User) -> TokenPair:
    return TokenPair(
        access_token=create_token(str(user.id), "access", {"role": user.role}),
        refresh_token=create_token(str(user.id), "refresh"),
    )


@router.post("/register", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def register(body: UserCreate, request: Request, db: DB):
    # Bootstrap rule: the very first account becomes ADMIN, everyone after is VIEWER.
    # An admin then promotes people via PATCH /users/{id}/role.
    user_count = await db.scalar(select(func.count()).select_from(User))
    role = Role.ADMIN if user_count == 0 else Role.VIEWER

    user = User(
        email=body.email,
        full_name=body.full_name,
        hashed_password=hash_password(body.password),
        role=role.value,
    )
    db.add(user)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Email already registered")

    record_audit(
        db, action="auth.register", actor_id=user.id, resource_type="user",
        resource_id=str(user.id), ip_address=client_ip(request), details={"role": role.value},
    )
    await db.commit()
    return user


@router.post("/login", response_model=TokenPair)
async def login(form: Annotated[OAuth2PasswordRequestForm, Depends()], request: Request, db: DB):
    """`username` field carries the email (OAuth2 password-flow convention)."""
    email = form.username.lower()
    user = await db.scalar(select(User).where(User.email == email))

    # Always run one bcrypt check so timing doesn't reveal whether the email exists.
    valid = verify_password(form.password, user.hashed_password if user else DUMMY_HASH)

    if not (user and valid and user.is_active):
        record_audit(
            db, action="auth.login", actor_id=user.id if user else None, status="failure",
            ip_address=client_ip(request), details={"email": email},
        )
        await db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password")

    record_audit(db, action="auth.login", actor_id=user.id, ip_address=client_ip(request))
    await db.commit()
    return _issue_tokens(user)


@router.post("/refresh", response_model=TokenPair)
async def refresh(body: RefreshRequest, db: DB):
    try:
        payload = decode_token(body.refresh_token, expected_type="refresh")
        user_id = uuid.UUID(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token")
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token")
    return _issue_tokens(user)
