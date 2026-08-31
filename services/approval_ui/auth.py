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
import logging
import os
import secrets
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

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
        to a store that lives in one process would be ceremony.

        Correction to an earlier version of this docstring: `infra/deploy.sh`
        does *not* refuse to make the service public without a password. If
        `overturn-ui-secret` is missing it logs a warning and deploys anyway,
        with `UI_ENV` empty -- `enabled` would be `False` and every route,
        including the three that change a case, would be ungated. What
        actually keeps a fresh deployment closed is Cloud Run's own IAM check
        (`--no-allow-unauthenticated`, set unconditionally in `deploy.sh`),
        which is a different layer entirely and does not depend on this
        property at all.
        """
        return bool(self.password)


def load_config() -> AuthConfig:
    """Resolve the door's configuration once, at startup.

    The signing secret must be supplied wherever more than one instance can
    serve a request. Generating one per process is fine on a laptop — it logs
    you out on restart, which is an annoyance and never a security failure — and
    it is broken on Cloud Run: a session minted by one instance is rejected by
    the next, so the login appears to accept the password and then silently
    refuse to let you in. Nothing in the logs says why.

    So a missing secret is a warning, not a silent default, and
    `infra/deploy.sh` sets one from Secret Manager.
    """
    password = os.getenv("OVERTURN_UI_PASSWORD", "").strip()
    secret = os.getenv("OVERTURN_UI_SECRET", "").strip()

    if password and not secret:
        logger.warning(
            "OVERTURN_UI_PASSWORD is set but OVERTURN_UI_SECRET is not. Sessions "
            "will be signed with a per-process key, so any deployment serving "
            "more than one instance will reject logins issued by another. Set "
            "OVERTURN_UI_SECRET to a stable value."
        )

    return AuthConfig(password=password, secret=secret or secrets.token_hex(32))


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
#
# The signed-out front door is served from this same process (see SITE_FILES in
# app.py) so the whole product lives at one hostname. It is a door, not a
# brochure -- the explanatory pages that used to sit here were removed, because
# no product a person actually uses puts "how it works" in its nav. It holds no
# case data, so it sits outside the door alongside the login screen.
#
# The pages that render a case are deliberately not in this set -- so
# `no_store_behind_the_door` in app.py still marks them `no-store, private` --
# but `require_session` below does not gate them on `path_is_public` either.
# It bypasses every safe method (GET, HEAD) before it ever reaches that check,
# so reading a case needs no session. Only the three routes that change one
# (approve, reject, co-sign) are POSTs, and those are what actually check the
# session below.
SITE_PATHS = frozenset(
    {
        "/",
        "/index.html",
        "/system.html",
        "/styles.css",
        "/app.js",
        "/architecture.svg",
    }
)

# /logout is public because signing out is not a privileged act: it clears a
# cookie the caller already holds, and demanding a session before you may drop
# one is a loop.
#
# /fleet is public because it carries no patient data at all -- agent
# identities, permissions and model names, derived at request time from
# `core/registry.py`. A judge evaluating the Discovery & Lifecycle track has to
# be able to reach it without first getting past a password meant to gate case
# records, not the catalogue of what may touch them.
PUBLIC_PATHS = (
    frozenset({"/login", "/logout", "/health", "/healthz", "/theme", "/fleet"}) | SITE_PATHS
)


def path_is_public(path: str) -> bool:
    return path in PUBLIC_PATHS
