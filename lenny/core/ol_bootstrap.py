#!/usr/bin/env python
"""
Internet Archive / Open Library auth bootstrap.

This module is invoked in two ways:

1. As a CLI module inside the `lenny_api` container, by `docker/utils/ol_configure.sh`:

       printf '%s' "$password" | docker exec -i lenny_api \
           python -m lenny.core.ol_bootstrap "$email"

   It reads the password from stdin so it never appears in argv, environment,
   or `docker inspect` output. On success, it writes three newline-separated
   values to stdout (access, secret, screenname). On failure it writes a
   single `ERROR:<CODE>:<msg>` line to stderr and exits non-zero.

2. As a library, by the `/admin/ol/login` route — see `acquire_keys()`.

The module never touches the filesystem: persisting credentials is the caller's
responsibility.
"""

import fcntl
import logging
import os
import stat
import sys
import tempfile
import threading
from contextlib import contextmanager
from typing import Mapping, Tuple

from lenny.core.exceptions import InvalidOLCredentialsError

logger = logging.getLogger(__name__)

# ─── Cross-process env-file lock ─────────────────────────────────────────────
# uvicorn runs LENNY_WORKERS separate *processes*; within each, FastAPI handles
# requests on an event loop / threadpool. A read-modify-write of an env file is
# only crash-safe (mkstemp + os.replace) per single write — it is NOT safe
# against two workers writing the same file concurrently (lost update) or
# against a multi-file invariant (LENDING_MODE + EXTERNAL_AUTH_ENABLED) being
# interleaved. We serialize with:
#   - fcntl.flock on a lock file  → exclusion across worker processes
#   - threading.RLock             → exclusion across threads in one process,
#                                    plus same-thread reentrancy so nested
#                                    callers grabbing the same lock don't
#                                    self-deadlock.
_locks_guard = threading.Lock()
_locks: "dict[str, _FileLock]" = {}


class _FileLock:
    __slots__ = ("rlock", "fd", "depth")

    def __init__(self) -> None:
        self.rlock = threading.RLock()
        self.fd = None
        self.depth = 0


@contextmanager
def env_lock(lock_path: str):
    """Serialize env-file mutations across worker processes and threads.

    Reentrant for nested calls on the same thread (e.g. update_auth_config →
    _apply_external_enabled → _apply_lending_state all take the same lock).

    Best-effort on the cross-process flock: if the lock file's directory does
    not exist (e.g. under TESTING, where ``/app`` is absent) or flock is
    unsupported, we still hold the in-process RLock but skip flock. That is safe
    because such environments run a single worker; production always has the
    ``/app`` env-file mount.
    """
    with _locks_guard:
        lk = _locks.get(lock_path)
        if lk is None:
            lk = _FileLock()
            _locks[lock_path] = lk

    lk.rlock.acquire()
    try:
        if lk.depth == 0:
            try:
                lk.fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
                fcntl.flock(lk.fd, fcntl.LOCK_EX)
            except OSError as exc:
                # Dir missing or flock unsupported — fall back to in-process
                # lock only (single-worker environments).
                logger.debug("env_lock: flock unavailable for %s (%s); in-process lock only", lock_path, exc)
                if lk.fd is not None:
                    try:
                        os.close(lk.fd)
                    except OSError:
                        pass
                    lk.fd = None
        lk.depth += 1
        try:
            yield
        finally:
            lk.depth -= 1
            if lk.depth == 0 and lk.fd is not None:
                try:
                    fcntl.flock(lk.fd, fcntl.LOCK_UN)
                finally:
                    os.close(lk.fd)
                    lk.fd = None
    finally:
        lk.rlock.release()


class OLBootstrapError(Exception):
    """Raised when IA auth fails. `code` is a stable machine-readable classifier."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def acquire_keys(email: str, password: str) -> Tuple[str, str, str]:
    """Exchange IA email + password for S3 access/secret keys.

    Returns `(access, secret, screenname)`. Raises `OLBootstrapError` with a
    stable `.code` on any failure — callers translate to HTTP status / UI.

    Never logs credentials. Never writes to disk.
    """
    if not email or "@" not in email:
        raise OLBootstrapError("BAD_EMAIL", "Email must be a valid address.")
    if not password:
        raise OLBootstrapError("BAD_PASSWORD", "Password must not be empty.")

    try:
        from internetarchive.config import get_auth_config  # type: ignore
    except ImportError as exc:
        raise OLBootstrapError(
            "MISSING_DEP",
            f"`internetarchive` package not installed in this environment: {exc}",
        ) from None

    try:
        config = get_auth_config(email, password)
    except Exception as exc:
        msg = str(exc) or exc.__class__.__name__
        low = msg.lower()
        if any(s in low for s in ("invalid", "incorrect", "403", "unauthorized", "401")):
            raise OLBootstrapError("INVALID_CREDENTIALS", msg) from None
        if any(s in low for s in ("connection", "timeout", "dns", "resolve", "unreachable")):
            raise OLBootstrapError("IA_UNREACHABLE", msg) from None
        raise OLBootstrapError("UNKNOWN", msg) from None

    s3 = (config or {}).get("s3") or {}
    access = s3.get("access") or ""
    secret = s3.get("secret") or ""
    if not access or not secret:
        raise OLBootstrapError(
            "NO_KEYS",
            "archive.org accepted the credentials but returned no S3 keys.",
        )

    screenname = (config or {}).get("screenname") or email
    return access, secret, screenname


def _as_user_error(err: OLBootstrapError) -> InvalidOLCredentialsError:
    """Translate a bootstrap error into the typed exception the API layer expects."""
    return InvalidOLCredentialsError(f"{err.code}: {err.message}")


def update_env_file(env_path: str, updates: Mapping[str, str]) -> None:
    """Atomically rewrite `env_path`, replacing or appending `updates`.

    Mirrors `docker/utils/ol_configure.sh`'s `env_set`: preserves unrelated
    lines byte-for-byte, writes the new file with 0644 perms before moving it
    into place, and never leaves a half-written file behind.

    0644, not 0600: this runs inside the container, which has no USER
    directive and so writes as root. Over the bind mount that makes the file
    root-owned on the host — 0600 would then lock out the host user
    entirely, including `docker compose` itself needing to read this file
    for `env_file:` on the next `make update`, which is exactly the bug this
    comment is here to stop someone re-introducing. Anyone with host
    filesystem read access already has bigger problems than a 644 file.

    Keys missing from the file are appended at the end. Newline characters are
    stripped from all values to prevent env-file corruption.
    """
    if not updates:
        return

    # Hold the per-file lock across the whole read-modify-write so concurrent
    # workers can't lose each other's updates (the os.replace below is atomic,
    # but two unsynchronized RMW cycles still race).
    with env_lock(env_path + ".lock"):
        remaining = {k: v.replace("\n", "").replace("\r", "") for k, v in updates.items()}
        fd, tmp_path = tempfile.mkstemp(
            prefix=".env.", dir=os.path.dirname(os.path.abspath(env_path))
        )
        try:
            with os.fdopen(fd, "w") as out:
                os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
                try:
                    with open(env_path, "r") as src:
                        for line in src:
                            stripped = line.rstrip("\n")
                            key, sep, _ = stripped.partition("=")
                            if sep and key in remaining:
                                out.write(f"{key}={remaining.pop(key)}\n")
                            else:
                                out.write(line if line.endswith("\n") else line + "\n")
                except FileNotFoundError:
                    pass
                for key, value in remaining.items():
                    out.write(f"{key}={value}\n")
            os.replace(tmp_path, env_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


def main() -> None:
    if len(sys.argv) != 2:
        sys.stderr.write("ERROR:USAGE:Expected exactly one argument (email)\n")
        sys.exit(64)

    email = sys.argv[1].strip()
    # Read password from stdin — keeps it out of argv and process env.
    # rstrip only trailing CR/LF so that shell `printf '%s'` (no trailing
    # newline) and `echo` (with newline) both produce the same password.
    password = sys.stdin.read().rstrip("\r\n")

    try:
        access, secret, screenname = acquire_keys(email, password)
    except OLBootstrapError as err:
        sys.stderr.write(f"ERROR:{err.code}:{err.message}\n")
        # Distinct exit codes help the shell script branch on failure class.
        codes = {
            "BAD_EMAIL": 2,
            "BAD_PASSWORD": 2,
            "MISSING_DEP": 3,
            "INVALID_CREDENTIALS": 4,
            "IA_UNREACHABLE": 5,
            "NO_KEYS": 6,
            "UNKNOWN": 7,
        }
        sys.exit(codes.get(err.code, 1))

    sys.stdout.write(f"{access}\n{secret}\n{screenname}\n")


if __name__ == "__main__":
    main()
