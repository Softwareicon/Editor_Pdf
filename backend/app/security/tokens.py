"""Short-lived JWT access tokens and opaque, revocable, rotating refresh tokens."""
from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import RefreshToken, User

ALGORITHM = "HS256"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime) -> datetime:
    """SQLite returns naive datetimes; treat them as UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def create_access_token(user: User) -> tuple[str, int]:
    s = get_settings()
    ttl = s.access_token_minutes * 60
    now = _now()
    payload = {"sub": user.id, "role": user.role, "typ": "access", "iat": now,
               "exp": now + timedelta(seconds=ttl), "jti": uuid.uuid4().hex}
    return jwt.encode(payload, s.secret_key, algorithm=ALGORITHM), ttl


def decode_access_token(token: str) -> dict | None:
    try:
        payload = jwt.decode(token, get_settings().secret_key, algorithms=[ALGORITHM],
                             options={"require": ["exp", "sub", "typ"]})
    except jwt.PyJWTError:
        return None
    return payload if payload.get("typ") == "access" else None


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_refresh_token(db: Session, user: User) -> str:
    token = secrets.token_urlsafe(48)
    db.add(RefreshToken(user_id=user.id, token_hash=_hash(token),
                        expires_at=_now() + timedelta(days=get_settings().refresh_token_days)))
    return token


def consume_refresh_token(db: Session, token: str) -> User | None:
    """Validate and revoke (rotate) a refresh token. Reuse of a revoked token revokes the whole family."""
    row = db.query(RefreshToken).filter(RefreshToken.token_hash == _hash(token)).one_or_none()
    if row is None:
        return None
    if row.revoked_at is not None:
        revoke_all_refresh_tokens(db, row.user_id)  # likely token theft
        db.commit()
        return None
    if aware(row.expires_at) <= _now():
        return None
    row.revoked_at = _now()
    user = db.get(User, row.user_id)
    return user if user and user.is_active else None


def revoke_refresh_token(db: Session, token: str) -> None:
    row = db.query(RefreshToken).filter(RefreshToken.token_hash == _hash(token)).one_or_none()
    if row and row.revoked_at is None:
        row.revoked_at = _now()


def revoke_all_refresh_tokens(db: Session, user_id: str) -> None:
    db.query(RefreshToken).filter(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None)).update(
        {"revoked_at": _now()})


def purge_expired_refresh_tokens(db: Session) -> int:
    n = db.query(RefreshToken).filter(RefreshToken.expires_at < _now()).delete()
    db.commit()
    return n
