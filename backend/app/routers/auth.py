"""Authentication: register, login, refresh (rotating cookie), logout, current user."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import get_settings
from ..database import get_db
from ..errors import ApiError
from ..models import User, UserSettings
from ..schemas.auth import LoginRequest, RegisterRequest, TokenResponse, UserOut
from ..security import tokens
from ..security.deps import client_ip, get_current_user
from ..security.passwords import hash_password, needs_rehash, verify_password
from ..security.rate_limit import limit_auth
from ..services import audit

router = APIRouter(prefix="/api/auth", tags=["Authentication"])
REFRESH_COOKIE = "pdfmaster_refresh"
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "PDFMasterPro"


def _set_refresh_cookie(response: Response, token: str) -> None:
    s = get_settings()
    response.set_cookie(REFRESH_COOKIE, token, max_age=s.refresh_token_days * 86400, httponly=True,
                        secure=s.cookie_secure, samesite=s.cookie_samesite.lower(), path="/api/auth")


def _clear_refresh_cookie(response: Response) -> None:
    s = get_settings()
    response.delete_cookie(REFRESH_COOKIE, path="/api/auth", httponly=True, secure=s.cookie_secure,
                           samesite=s.cookie_samesite.lower())


def _require_csrf_header(request: Request) -> None:
    # Cookie-authenticated endpoints: a custom header cannot be sent cross-site without a CORS preflight
    # that only allow-listed origins pass.
    if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise ApiError(403, "csrf_check_failed", "Missing or invalid X-Requested-With header")


def _session(db: Session, user: User, response: Response) -> TokenResponse:
    access, ttl = tokens.create_access_token(user)
    _set_refresh_cookie(response, tokens.issue_refresh_token(db, user))
    return TokenResponse(access_token=access, expires_in=ttl, user=UserOut.model_validate(user))


@router.post("/register", response_model=TokenResponse, status_code=201, dependencies=[Depends(limit_auth)])
def register(body: RegisterRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    s = get_settings()
    if not s.registration_enabled:
        raise ApiError(403, "registration_disabled", "Registration is currently disabled")
    if len(body.password) < s.min_password_length:
        raise ApiError(422, "weak_password", f"Password must be at least {s.min_password_length} characters")
    email = body.email.lower()
    if db.query(User).filter(func.lower(User.email) == email).first():
        raise ApiError(409, "email_taken", "An account with this email already exists")
    user = User(full_name=body.full_name, email=email, password_hash=hash_password(body.password))
    user.settings = UserSettings()
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise ApiError(409, "email_taken", "An account with this email already exists")
    audit.record(db, "auth.register", user.id, ip=client_ip(request))
    result = _session(db, user, response)
    db.commit()
    return result


@router.post("/login", response_model=TokenResponse, dependencies=[Depends(limit_auth)])
def login(body: LoginRequest, request: Request, response: Response, db: Session = Depends(get_db)):
    user = db.query(User).filter(func.lower(User.email) == body.email.lower()).first()
    if not verify_password(body.password, user.password_hash if user else None) or not user.is_active:
        audit.record(db, "auth.login_failed", user.id if user else None, ip=client_ip(request))
        db.commit()
        raise ApiError(401, "invalid_credentials", "Incorrect email or password")
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(body.password)
    audit.record(db, "auth.login", user.id, ip=client_ip(request))
    result = _session(db, user, response)
    db.commit()
    return result


@router.post("/refresh", response_model=TokenResponse, dependencies=[Depends(_require_csrf_header)])
def refresh(request: Request, response: Response, db: Session = Depends(get_db)):
    token = request.cookies.get(REFRESH_COOKIE)
    user = tokens.consume_refresh_token(db, token) if token else None
    if user is None:
        db.commit()
        err = ApiError(401, "invalid_refresh_token", "Session expired. Please sign in again.")
        # Clear the dead cookie on the error response too.
        tmp = Response()
        _clear_refresh_cookie(tmp)
        err.headers = {"set-cookie": tmp.headers["set-cookie"]}
        raise err
    result = _session(db, user, response)
    db.commit()
    return result


@router.post("/logout", status_code=204, dependencies=[Depends(_require_csrf_header)])
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    token = request.cookies.get(REFRESH_COOKIE)
    if token:
        tokens.revoke_refresh_token(db, token)
        db.commit()
    _clear_refresh_cookie(response)
    response.status_code = 204
    return response


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return user
