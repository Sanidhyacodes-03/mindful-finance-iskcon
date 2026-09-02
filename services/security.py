from argon2 import PasswordHasher
from argon2.exceptions import Argon2Error

_hasher = PasswordHasher()


def hash_password(plain_password):
    return _hasher.hash(plain_password)


def verify_password(stored_hash, plain_password):
    if not stored_hash:
        return False
    try:
        return _hasher.verify(stored_hash, plain_password)
    except Argon2Error:
        return False
    except Exception:
        # Never let a malformed/legacy hash 500 the login page.
        return False
