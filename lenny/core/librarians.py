#!/usr/bin/env python

"""
    Librarians: named admin accounts, kept in the database.

    A password is stored only as an Argon2 hash (salt and cost are inside the
    hash string, so there is no salt column). `is_super` marks the one
    super user (the account that existed before this table did). Making someone
    else super user hands the role over: the previous one becomes a normal member.
    The super user can be neither demoted nor disabled, so the instance cannot
    lock itself out.

    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

import hashlib
import re
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import Boolean, Column, DateTime, Integer, String, func

from lenny.core.db import Base, session as db
from lenny.core.cache import Cache
from lenny.core.exceptions import LennyAPIError
from lenny.core.models import _xact_lock

MIN_PASSWORD = 12
MAX_PASSWORD = 1024  # argon2 hashes any length; this only bounds what a request can make us chew on
MAX_FAILURES = 10    # wrong passwords per librarian ...
FAILURE_WINDOW = 300  # ... per this many seconds, login and step-up checks alike
INT4_MAX = 2**31 - 1
_USERNAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_hasher = PasswordHasher()
# Verified against when the username is unknown, so a miss costs as much as a hit.
_DUMMY_HASH = _hasher.hash("not-a-real-password")


class LibrarianError(LennyAPIError): pass


class TooManyAttempts(LennyAPIError): pass


class Librarian(Base):
    __tablename__ = "librarians"

    id = Column(Integer, primary_key=True)
    # The login handle; derived from the email when an account is added.
    username = Column(String(64), nullable=False, unique=True)
    # What people see: team list, "signed in as". Null only for the migrated account.
    name = Column(String(100))
    # Null only for the account migrated from auth.env, which never had one.
    email = Column(String(254), unique=True)
    password_hash = Column(String(255), nullable=False)
    is_super = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    disabled_at = Column(DateTime(timezone=True))
    # Bumped by a logout; signed into tokens so it ends this person's sessions.
    token_version = Column(Integer, nullable=False, default=0, server_default="0")

    def session_fingerprint(self) -> str:
        """Changes whenever the password does or the person logs out; signed into tokens so either ends old sessions."""
        return hashlib.sha256(f"{self.password_hash}:{self.token_version or 0}".encode()).hexdigest()[:16]

    def to_dict(self) -> dict:
        return {
            "id": self.id, "username": self.username, "name": self.name,
            "display_name": self.name or self.username, "email": self.email,
            "is_super": bool(self.is_super), "disabled": self.disabled_at is not None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


def _check_password(password: str) -> None:
    if not isinstance(password, str) or not MIN_PASSWORD <= len(password) <= MAX_PASSWORD:
        raise LibrarianError(f"Password must be {MIN_PASSWORD}-{MAX_PASSWORD} characters.")


def _check_email(email: str) -> str:
    email = email.strip().lower() if isinstance(email, str) else ""
    if not _EMAIL.match(email) or len(email) > 254:
        raise LibrarianError("A valid email is required.")
    return email


def _check_flag(value) -> None:
    if not isinstance(value, bool):
        raise LibrarianError("is_super must be true or false.")


def _check_name(name) -> str:
    name = " ".join(name.split()) if isinstance(name, str) else ""
    if not name or len(name) > 100 or any(ord(c) < 32 for c in name):
        raise LibrarianError("A name of 1-100 characters is required.")
    return name


def _derive_username(email: str) -> str:
    """The email's local part reduced to a safe handle, made unique with a number."""
    base = re.sub(r"[^a-z0-9._-]", "", email.split("@")[0])[:56].lstrip("._-") or "user"
    candidate, n = base, 1
    while db.query(Librarian).filter(Librarian.username == candidate).first():
        n += 1
        candidate = f"{base}{n}"
    return candidate


def create(name: str, email: str, password: str, is_super: bool = False,
           username: Optional[str] = None) -> Librarian:
    name = _check_name(name)
    _check_flag(is_super)
    email = _check_email(email)
    _check_password(password)
    if username is not None and not (isinstance(username, str) and _USERNAME.match(username)):
        raise LibrarianError("A username may use lowercase letters, digits, dots, dashes and underscores.")
    try:
        _xact_lock("librarians")
        if db.query(Librarian).filter(Librarian.email == email).first():
            raise LibrarianError("That email is already in use.")
        if username and db.query(Librarian).filter(Librarian.username == username).first():
            raise LibrarianError("That username is already in use.")
        row = Librarian(name=name, username=username or _derive_username(email), email=email,
                        is_super=False, password_hash=_hasher.hash(password))
        db.add(row)
        if is_super:
            _make_only_super(row)
        db.commit()
        return row
    except Exception:
        db.rollback()
        raise


def authenticate(username: str, password: str) -> Optional[Librarian]:
    """The enabled librarian with this password, or None. Raises TooManyAttempts after
    MAX_FAILURES wrong passwords for one username inside FAILURE_WINDOW (even the right
    one is refused until it passes), so a stolen session cannot guess the password."""
    if not (isinstance(username, str) and isinstance(password, str)
            and len(username) <= 64 and len(password) <= MAX_PASSWORD and "\x00" not in username):
        return None
    who = username.strip().lower() if "@" in username else username
    row = db.query(Librarian).populate_existing().filter(
        (Librarian.username == who) | (Librarian.email == who), Librarian.disabled_at.is_(None)).first()
    # One counter per account, so alternating email and username cannot double the guesses.
    key = hashlib.sha256((f"id:{row.id}" if row else username).encode()).hexdigest()
    if Cache._count("librarian:login", key) >= MAX_FAILURES:
        raise TooManyAttempts("Too many wrong passwords. Try again in a few minutes.")
    try:
        _hasher.verify(row.password_hash if row else _DUMMY_HASH, password)
    except (VerificationError, InvalidHashError):
        Cache._record("librarian:login", key, FAILURE_WINDOW)
        return None
    return row


def get(librarian_id: int) -> Librarian:
    row = db.get(Librarian, librarian_id) if 0 < librarian_id <= INT4_MAX else None
    if not row:
        raise LibrarianError("No such librarian.")
    return row


def end_sessions(librarian_id: int) -> None:
    """Invalidate every token this librarian holds (logout: on every device)."""
    try:
        row = get(librarian_id)
        row.token_version = (row.token_version or 0) + 1
        db.commit()
    except Exception:
        db.rollback()
        raise


def get_active(librarian_id: int) -> Optional[Librarian]:
    """The librarian if enabled, read fresh so a disable takes effect on the next request."""
    return db.query(Librarian).populate_existing().filter(
        Librarian.id == librarian_id, Librarian.disabled_at.is_(None)).first()


def list_all() -> list:
    return db.query(Librarian).order_by(Librarian.id).all()


def _make_only_super(row: Librarian) -> None:
    """There is one super user: promoting `row` demotes everyone else, in the caller's transaction."""
    if row.disabled_at is not None:
        raise LibrarianError("A disabled account cannot be made super user.")
    db.query(Librarian).filter(Librarian.is_super.is_(True), Librarian.id != row.id).update(
        {Librarian.is_super: False, Librarian.updated_at: func.now()}, synchronize_session="fetch")
    row.is_super = True


def update(librarian_id: int, email: Optional[str] = None, password: Optional[str] = None,
           is_super: Optional[bool] = None, name: Optional[str] = None) -> Librarian:
    try:
        _xact_lock("librarians")
        row = get(librarian_id)
        if is_super is not None:
            _check_flag(is_super)
        if name is not None:
            row.name = _check_name(name)
        if email is not None:
            email = _check_email(email)
            taken = db.query(Librarian).filter(Librarian.email == email, Librarian.id != row.id).first()
            if taken:
                raise LibrarianError("That email is already in use.")
            row.email = email
        if password is not None:
            _check_password(password)
            row.password_hash = _hasher.hash(password)
        if is_super is True and not row.is_super:
            _make_only_super(row)
        elif is_super is False and row.is_super:
            raise LibrarianError("The super user cannot be demoted; make someone else super user instead.")
        row.updated_at = func.now()
        db.commit()
        return row
    except Exception:
        db.rollback()
        raise


def set_disabled(librarian_id: int, disabled: bool) -> Librarian:
    try:
        _xact_lock("librarians")
        row = get(librarian_id)
        if disabled and row.is_super:
            raise LibrarianError("The super user cannot be disabled; make someone else super user first.")
        row.disabled_at = func.now() if disabled else None
        row.updated_at = func.now()
        db.commit()
        return row
    except Exception:
        db.rollback()
        raise
