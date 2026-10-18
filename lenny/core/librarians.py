#!/usr/bin/env python

"""
    Librarians: named admin accounts, kept in the database.

    A password is stored only as an Argon2 hash (salt and cost are inside the
    hash string, so there is no salt column). `is_super` marks the account that
    existed before this table did; the last enabled super user can never be
    disabled, so the instance cannot lock itself out.

    :copyright: (c) 2015 by AUTHORS
    :license: see LICENSE for more details
"""

import re
from typing import Optional

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import Boolean, Column, DateTime, Integer, String, func

from lenny.core.db import Base, session as db
from lenny.core.exceptions import LennyAPIError
from lenny.core.models import _xact_lock

MIN_PASSWORD = 12
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_hasher = PasswordHasher()
# Verified against when the username is unknown, so a miss costs as much as a hit.
_DUMMY_HASH = _hasher.hash("not-a-real-password")


class LibrarianError(LennyAPIError): pass


class Librarian(Base):
    __tablename__ = "librarians"

    id = Column(Integer, primary_key=True)
    username = Column(String(64), nullable=False, unique=True)
    # Null only for the account migrated from auth.env, which never had one.
    email = Column(String(254), unique=True)
    password_hash = Column(String(255), nullable=False)
    is_super = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    disabled_at = Column(DateTime(timezone=True))

    def to_dict(self) -> dict:
        return {
            "id": self.id, "username": self.username, "email": self.email,
            "is_super": bool(self.is_super), "disabled": self.disabled_at is not None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


def _check_password(password: str) -> None:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise LibrarianError(f"Password must be at least {MIN_PASSWORD} characters.")


def _check_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not _EMAIL.match(email) or len(email) > 254:
        raise LibrarianError("A valid email is required.")
    return email


def create(username: str, email: str, password: str, is_super: bool = False) -> Librarian:
    username = (username or "").strip()
    if not username or len(username) > 64:
        raise LibrarianError("A username of 1-64 characters is required.")
    email = _check_email(email)
    _check_password(password)
    try:
        _xact_lock("librarians")
        if db.query(Librarian).filter((Librarian.username == username) | (Librarian.email == email)).first():
            raise LibrarianError("That username or email is already in use.")
        row = Librarian(username=username, email=email, is_super=is_super,
                        password_hash=_hasher.hash(password))
        db.add(row)
        db.commit()
        return row
    except Exception:
        db.rollback()
        raise


def authenticate(username: str, password: str) -> Optional[Librarian]:
    """The enabled librarian with this password, or None."""
    row = db.query(Librarian).filter(
        Librarian.username == (username or ""), Librarian.disabled_at.is_(None)).first()
    try:
        _hasher.verify(row.password_hash if row else _DUMMY_HASH, password or "")
    except (VerificationError, InvalidHashError):
        return None
    return row


def get(librarian_id: int) -> Librarian:
    row = db.get(Librarian, librarian_id)
    if not row:
        raise LibrarianError("No such librarian.")
    return row


def list_all() -> list:
    return db.query(Librarian).order_by(Librarian.id).all()


def update(librarian_id: int, email: Optional[str] = None, password: Optional[str] = None) -> Librarian:
    try:
        _xact_lock("librarians")
        row = get(librarian_id)
        if email is not None:
            email = _check_email(email)
            taken = db.query(Librarian).filter(Librarian.email == email, Librarian.id != row.id).first()
            if taken:
                raise LibrarianError("That email is already in use.")
            row.email = email
        if password is not None:
            _check_password(password)
            row.password_hash = _hasher.hash(password)
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
        if disabled and row.is_super and row.disabled_at is None:
            others = db.query(Librarian).filter(
                Librarian.is_super.is_(True), Librarian.disabled_at.is_(None),
                Librarian.id != row.id).count()
            if not others:
                raise LibrarianError("The last enabled super user cannot be disabled.")
        row.disabled_at = func.now() if disabled else None
        row.updated_at = func.now()
        db.commit()
        return row
    except Exception:
        db.rollback()
        raise
