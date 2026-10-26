import hashlib
import hmac
import secrets

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from .db import get_db
from .models import User


class NotAuthenticated(Exception):
    pass


class Forbidden(Exception):
    def __init__(self, message: str = "You are not allowed to do that."):
        self.message = message


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if algo != "scrypt":
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
    return hmac.compare_digest(digest.hex(), digest_hex)


def authenticate(db: Session, username: str, password: str) -> User | None:
    """Return the user when the credentials are right (the caller checks approval/active state)."""
    user = db.query(User).filter(User.username == username).first()
    if user and verify_password(password, user.password_hash):
        return user
    return None


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    uid = request.session.get("uid")
    user = db.get(User, uid) if uid else None
    if not user or not user.is_active or not user.is_approved:
        request.session.clear()
        raise NotAuthenticated()
    request.state.user = user
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise Forbidden("Admins only.")
    return user


def require_approver(user: User = Depends(current_user)) -> User:
    if not user.is_approver:
        raise Forbidden("Only approvers can approve transfers.")
    return user
