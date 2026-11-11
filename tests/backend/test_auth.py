import jwt

from app.database import SessionLocal
from app.models import AuditLog, User
from app.security.rate_limit import SlidingWindowLimiter
from conftest import PASSWORD, register

CSRF = {"X-Requested-With": "PDFMasterPro"}


def test_register_returns_token_and_hashes_password_with_argon2id(client):
    r = client.post("/api/auth/register", json={"full_name": " Alice ", "email": "Alice@Example.com", "password": PASSWORD})
    assert r.status_code == 201
    body = r.json()
    assert body["user"]["email"] == "alice@example.com" and body["user"]["full_name"] == "Alice"
    assert body["user"]["role"] == "user" and "password" not in str(body["user"])
    with SessionLocal() as db:
        user = db.query(User).one()
        assert user.password_hash.startswith("$argon2id$") and PASSWORD not in user.password_hash
        assert user.settings is not None


def test_register_rejects_duplicates_weak_passwords_and_bad_email(client):
    register(client)
    dup = client.post("/api/auth/register", json={"full_name": "A", "email": "ALICE@example.com", "password": PASSWORD})
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "email_taken"
    weak = client.post("/api/auth/register", json={"full_name": "B", "email": "b@example.com", "password": "short"})
    assert weak.status_code == 422 and weak.json()["error"]["code"] == "weak_password"
    bad = client.post("/api/auth/register", json={"full_name": "B", "email": "not-an-email", "password": PASSWORD})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "validation_error"


def test_login_success_and_failure_are_indistinguishable_for_unknown_users(client):
    register(client)
    ok = client.post("/api/auth/login", json={"email": "alice@example.com", "password": PASSWORD})
    assert ok.status_code == 200 and ok.json()["token_type"] == "bearer"
    wrong = client.post("/api/auth/login", json={"email": "alice@example.com", "password": "wrong password!"})
    unknown = client.post("/api/auth/login", json={"email": "nobody@example.com", "password": PASSWORD})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    with SessionLocal() as db:
        assert db.query(AuditLog).filter(AuditLog.action == "auth.login_failed").count() == 2


def test_inactive_user_cannot_login_or_use_existing_token(client):
    headers = register(client)
    with SessionLocal() as db:
        db.query(User).update({"is_active": False})
        db.commit()
    assert client.get("/api/auth/me", headers=headers).status_code == 401
    assert client.post("/api/auth/login", json={"email": "alice@example.com", "password": PASSWORD}).status_code == 401


def test_me_requires_a_valid_access_token(client, auth):
    assert client.get("/api/auth/me", headers=auth).json()["email"] == "alice@example.com"
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer garbage"}).status_code == 401
    forged = jwt.encode({"sub": "x", "typ": "access", "exp": 9999999999}, "another-secret-key-of-sufficient-length", "HS256")
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {forged}"}).status_code == 401
    unsigned = jwt.encode({"sub": "x", "typ": "access", "exp": 9999999999}, None, algorithm="none")
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {unsigned}"}).status_code == 401


def test_refresh_cookie_is_httponly_and_rotates(client):
    r = client.post("/api/auth/register", json={"full_name": "A", "email": "a@example.com", "password": PASSWORD})
    cookie_header = r.headers["set-cookie"].lower()
    assert "httponly" in cookie_header and "samesite=strict" in cookie_header and "path=/api/auth" in cookie_header
    first = client.cookies.get("pdfmaster_refresh")

    assert client.post("/api/auth/refresh").status_code == 403  # CSRF header missing
    ok = client.post("/api/auth/refresh", headers=CSRF)
    assert ok.status_code == 200
    second = client.cookies.get("pdfmaster_refresh")
    assert second and second != first
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {ok.json()['access_token']}"}).status_code == 200

    # Replaying the already-used token fails and revokes the newer one too (theft response).
    client.cookies.set("pdfmaster_refresh", first, path="/api/auth")
    assert client.post("/api/auth/refresh", headers=CSRF).status_code == 401
    client.cookies.clear()
    client.cookies.set("pdfmaster_refresh", second, path="/api/auth")
    assert client.post("/api/auth/refresh", headers=CSRF).status_code == 401


def test_logout_revokes_the_refresh_token(client):
    client.post("/api/auth/register", json={"full_name": "A", "email": "a@example.com", "password": PASSWORD})
    token = client.cookies.get("pdfmaster_refresh")
    assert client.post("/api/auth/logout", headers=CSRF).status_code == 204
    client.cookies.set("pdfmaster_refresh", token, path="/api/auth")
    assert client.post("/api/auth/refresh", headers=CSRF).status_code == 401


def test_rate_limiter_blocks_after_limit():
    limiter = SlidingWindowLimiter()
    assert all(limiter.check("k", 3) is None for _ in range(3))
    assert limiter.check("k", 3) > 0
    assert limiter.check("other", 3) is None


def test_auth_endpoints_are_rate_limited(client, monkeypatch):
    from app.config import get_settings
    monkeypatch.setattr(get_settings(), "auth_rate_limit_per_minute", 3)
    codes = [client.post("/api/auth/login", json={"email": "x@example.com", "password": "nope-nope-nope"}).status_code
             for _ in range(5)]
    assert codes == [401, 401, 401, 429, 429]
