"""Argon2id password hashing (argon2-cffi defaults to the Argon2id variant)."""
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.low_level import Type

_hasher = PasswordHasher(type=Type.ID)
# Verified against when the account does not exist, so response time doesn't reveal that.
_DUMMY_HASH = _hasher.hash("pdfmaster-dummy-password")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)
