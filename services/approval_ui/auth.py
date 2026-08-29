"""Login for the approval interface.

The contest rules ask for a hosted URL a judge can open, and providing one means
the service has to be publicly reachable. Everything behind it is synthetic — an
invented payer, invented policies, generated patients — but the interface still
shows what looks like a medical record, and putting that on an open URL with no
door at all would be careless in a way that undermines the point of the project.

So: one shared password, a signed session cookie, and no user accounts. That is
the right amount of authentication for a demonstration with no real data and a
credential printed in the README, and it is deliberately not presented as more
than that. `docs/SUBMISSION.md` states the limitation plainly rather than
implying this is how the system would ship.

The cookie is signed with HMAC rather than encrypted, and it carries nothing but
an expiry — there is no session state worth stealing. Signing is what stops
someone minting their own.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from dataclasses import dataclass

COOKIE_NAME = "overturn_session"
SESSION_HOURS = 12


@dataclass(frozen=True)
class AuthConfig:
    """How the door is configured, resolved once at startup."""

    password: str
    secret: str

    @property
    def enabled(self) -> bool:
        """No password configured means no door.

        Local development and the test suite run without one, and adding a login
        to a store that lives in one process would be ceremony. The deployed
        service always has one — `infra/deploy.sh` refuses to make the service
        public without it.
        """
        return bool(self.password)


def load_config() -> AuthConfig:
    password = os.getenv("OVERTURN_UI_PASSWORD", "").strip()
    # A signing secret that changes on restart is fine: it logs everyone out,
    # which is a mild annoyance and never a security failure. Deriving it from
    # the password would mean the cookie leaks a fact about the password.
    secret = os.getenv("OVERTURN_UI_SECRET", "").strip() or secrets.token_hex(32)
    return AuthConfig(password=password, secret=secret)


def _sign(payload: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def issue_session(config: AuthConfig) -> str:
    """A cookie value carrying an expiry and nothing else."""
    expires = int(time.time()) + SESSION_HOURS * 3600
    payload = str(expires)
    return f"{payload}.{_sign(payload, config.secret)}"


def session_is_valid(cookie: str | None, config: AuthConfig) -> bool:
    if not cookie or "." not in cookie:
        return False
    payload, _, signature = cookie.rpartition(".")
    # Constant time: a timing side channel on a signature check is the one place
    # this kind of comparison genuinely matters.
    if not hmac.compare_digest(signature, _sign(payload, config.secret)):
        return False
    try:
        return int(payload) > time.time()
    except ValueError:
        return False


def password_matches(supplied: str, config: AuthConfig) -> bool:
    return hmac.compare_digest(supplied.strip(), config.password)


# Paths that must work without a session. Health checks are probed by Cloud Run
# before anything else and must never be behind a login.
# Choosing a colour is not a privileged act, and the login page has the control
# on it like every other page.
PUBLIC_PATHS = frozenset({"/login", "/health", "/healthz", "/theme"})


def path_is_public(path: str) -> bool:
    return path in PUBLIC_PATHS
