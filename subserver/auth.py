"""User accounts.

Credentials live in ``data/users.json``.  Passwords are stored as PBKDF2-HMAC-
SHA256 digests with a per-user random salt -- no third-party hashing library is
needed, and the iteration count is recorded per user so it can be raised later
without invalidating existing accounts.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from .paths import UnsafePath, check_username

PBKDF2_ITERATIONS = 240_000
SALT_BYTES = 16
MIN_PASSWORD_LENGTH = 8


class AuthError(Exception):
    """Registration or login was refused; the message is safe to show a user."""


def hash_password(password: str, salt: bytes | None = None, iterations: int = PBKDF2_ITERATIONS) -> dict[str, Any]:
    salt = salt or os.urandom(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return {"algorithm": "pbkdf2_sha256", "iterations": iterations, "salt": salt.hex(), "hash": digest.hex()}


def verify_password(password: str, record: dict[str, Any]) -> bool:
    try:
        salt = bytes.fromhex(record["salt"])
        expected = bytes.fromhex(record["hash"])
        iterations = int(record["iterations"])
    except (KeyError, TypeError, ValueError):
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest, expected)


class UserStore:
    """Thread-safe JSON-backed user directory."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._users: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            self._users = {}
            return
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self._users = data.get("users", {}) if isinstance(data, dict) else {}
        except (ValueError, OSError):
            # A corrupt file must not lock everyone out silently; keep the bad
            # copy aside so an operator can inspect it.
            backup = self.path.with_name(self.path.name + f".corrupt-{int(time.time())}")
            try:
                self.path.replace(backup)
            except OSError:
                pass
            self._users = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "users": self._users}, fh, indent=2, sort_keys=True)
        tmp.replace(self.path)
        try:
            os.chmod(self.path, 0o600)  # credentials are not world-readable
        except OSError:
            pass

    # -- Accounts ---------------------------------------------------------------
    def count(self) -> int:
        with self._lock:
            return len(self._users)

    def exists(self, username: str) -> bool:
        with self._lock:
            return username.strip().lower() in self._users

    def get(self, username: str) -> dict[str, Any] | None:
        with self._lock:
            user = self._users.get((username or "").strip().lower())
            return dict(user) if user else None

    def usernames(self) -> list[str]:
        with self._lock:
            return sorted(self._users)

    def create(self, username: str, password: str, display_name: str = "") -> dict[str, Any]:
        try:
            username = check_username(username)
        except UnsafePath as exc:
            raise AuthError(str(exc)) from exc
        if len(password or "") < MIN_PASSWORD_LENGTH:
            raise AuthError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
        with self._lock:
            if username in self._users:
                raise AuthError("That username is already taken.")
            record = {
                "username": username,
                "display_name": (display_name or username).strip()[:64],
                "password": hash_password(password),
                "created_at": time.time(),
                "last_login": None,
                "preferences": {},
            }
            self._users[username] = record
            self._save()
            return dict(record)

    def authenticate(self, username: str, password: str) -> dict[str, Any]:
        username = (username or "").strip().lower()
        with self._lock:
            record = self._users.get(username)
        if record is None or not verify_password(password or "", record.get("password", {})):
            # One message for both cases, so the form does not confirm which
            # usernames exist.
            raise AuthError("Incorrect username or password.")
        with self._lock:
            self._users[username]["last_login"] = time.time()
            self._save()
            return dict(self._users[username])

    def change_password(self, username: str, old_password: str, new_password: str) -> None:
        self.authenticate(username, old_password)
        if len(new_password or "") < MIN_PASSWORD_LENGTH:
            raise AuthError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
        with self._lock:
            self._users[username.strip().lower()]["password"] = hash_password(new_password)
            self._save()

    def set_preferences(self, username: str, preferences: dict[str, Any]) -> None:
        with self._lock:
            user = self._users.get(username.strip().lower())
            if user is None:
                raise AuthError("No such user.")
            user["preferences"] = preferences
            self._save()

    def preferences(self, username: str) -> dict[str, Any]:
        user = self.get(username)
        return dict(user.get("preferences", {})) if user else {}


def load_or_create_secret(path: str | Path) -> bytes:
    """Return the persistent session-signing key, creating it on first run.

    Persisting it means restarting the server does not log everyone out.
    """
    path = Path(path)
    if path.is_file():
        data = path.read_bytes().strip()
        if len(data) >= 32:
            return data
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_bytes(48)
    path.write_bytes(secret)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return secret
