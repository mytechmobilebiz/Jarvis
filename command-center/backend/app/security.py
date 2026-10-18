"""Password hashing, session tokens and the login throttle."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

import bcrypt

SESSION_COOKIE = "cc_session"
# Mutating requests made with the session cookie must carry this header. A
# cross-site form cannot set custom headers, so this (with SameSite=Strict)
# keeps the cookie from being used by another origin.
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "command-center"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("ascii")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("ascii"))
    except ValueError:
        return False


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Session tokens are stored hashed so a database read does not yield live sessions."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_equal(given: str, expected: str) -> bool:
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


class LoginThrottle:
    """Refuses further attempts for a key after too many recent failures.

    In-memory, so it is per API process; that is enough for a single-host
    private deployment and needs no extra service.
    """

    def __init__(self, max_failures: int, window_seconds: int) -> None:
        self.max_failures = max_failures
        self.window = window_seconds
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str, now: float) -> deque[float]:
        q = self._failures[key]
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def is_locked(self, key: str) -> bool:
        return len(self._prune(key, time.monotonic())) >= self.max_failures

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        self._prune(key, now).append(now)

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)
