"""FastAPI dependencies for authentication and role-based authorization."""
from __future__ import annotations

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from ..database import get_db
from ..errors import ApiError
from ..models import User
from .tokens import decode_access_token

_bearer = HTTPBearer(auto_error=False)


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer), db: Session = Depends(get_db)
) -> User:
    unauthorized = ApiError(401, "not_authenticated", "Authentication required", {"WWW-Authenticate": "Bearer"})
    if creds is None:
        raise unauthorized
    payload = decode_access_token(creds.credentials)
    if payload is None:
        raise unauthorized
    user = db.get(User, payload["sub"])
    if user is None or not user.is_active:
        raise unauthorized
    return user


def require_role(*roles: str):
    def _dep(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise ApiError(403, "forbidden", "You do not have permission to perform this action")
        return user

    return _dep


def client_ip(request: Request) -> str | None:
    # Behind a reverse proxy, run uvicorn with --proxy-headers so this is the real client address.
    return request.client.host if request.client else None
