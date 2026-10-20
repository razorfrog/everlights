"""Password, session and token checks for the remote (tunnel) entrance.

The LAN port stays open, as before. The remote port serves a public live-show
page and puts everything else behind either a session cookie (set by logging in
with the admin password) or a bearer token (for Siri Shortcuts).
"""

import hashlib
import hmac
import secrets
import threading
import time

ITERATIONS = 240_000
SESSION_DAYS = 180
COOKIE = "el_session"
MAX_FAILURES = 5
FAILURE_WINDOW = 15 * 60


def hash_password(password):
    if len(password) < 8:
        raise ValueError("use at least 8 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_password(password, stored):
    try:
        _, iterations, salt, digest = stored.split("$")
        check = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
    except (AttributeError, ValueError):
        return False
    return hmac.compare_digest(check.hex(), digest)


def _sign(secret, payload):
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_session(secret, now=None):
    expires = int((now or time.time()) + SESSION_DAYS * 86400)
    return f"{expires}.{_sign(secret, str(expires))}"


def check_session(secret, value, now=None):
    if not secret or not value or "." not in value:
        return False
    expires, sig = value.split(".", 1)
    if not expires.isdigit() or int(expires) < (now or time.time()):
        return False
    return hmac.compare_digest(sig, _sign(secret, expires))


def check_token(expected, header):
    if not expected or not header or not header.startswith("Bearer "):
        return False
    return hmac.compare_digest(header[len("Bearer "):].strip(), expected)


def new_secret():
    return secrets.token_urlsafe(32)


class LoginLimiter:
    """Blocks an address after MAX_FAILURES wrong passwords within FAILURE_WINDOW."""

    def __init__(self):
        self.lock = threading.Lock()
        self.failures = {}

    def blocked(self, who, now=None):
        now = now or time.time()
        with self.lock:
            recent = [t for t in self.failures.get(who, []) if now - t < FAILURE_WINDOW]
            self.failures[who] = recent
            return len(recent) >= MAX_FAILURES

    def failed(self, who, now=None):
        with self.lock:
            self.failures.setdefault(who, []).append(now or time.time())

    def succeeded(self, who):
        with self.lock:
            self.failures.pop(who, None)
