"""Password hashing and JWT helpers. No database or FastAPI code in here."""
from datetime import datetime, timedelta, timezone
from typing import Literal

import bcrypt
import jwt

from app.core.config import get_settings

TokenType = Literal["access", "refresh"]

# Used to burn the same CPU time when a login email doesn't exist,
# so response timing doesn't reveal which emails are registered.
DUMMY_HASH = bcrypt.hashpw(b"dummy-password", bcrypt.gensalt(get_settings().bcrypt_rounds)).decode()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(get_settings().bcrypt_rounds)).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except ValueError:
        return False


def create_token(subject: str, token_type: TokenType, extra: dict | None = None) -> str:
    s = get_settings()
    now = datetime.now(timezone.utc)
    ttl = timedelta(minutes=s.access_token_minutes) if token_type == "access" else timedelta(days=s.refresh_token_days)
    payload = {"sub": subject, "type": token_type, "iat": now, "exp": now + ttl, **(extra or {})}
    return jwt.encode(payload, s.jwt_secret, algorithm=s.jwt_algorithm)


def decode_token(token: str, expected_type: TokenType) -> dict:
    """Raises jwt.PyJWTError if the token is invalid, expired or of the wrong type."""
    s = get_settings()
    payload = jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_algorithm])
    if payload.get("type") != expected_type:
        raise jwt.InvalidTokenError("Wrong token type")
    return payload
