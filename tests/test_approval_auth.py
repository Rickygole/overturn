"""Tests for the login on the approval interface.

The contest asks for a hosted URL a judge can open, which means the service is
publicly reachable. Everything behind it is synthetic, but it still renders what
looks like a medical record, and an open URL with no door would undercut the
point of the project.

What is being defended here is narrow and worth stating: that a session cookie
cannot be forged, that health checks stay outside the door, and that a service
with no password configured does not silently pretend to be protected.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from core.store import MemoryStore
from services.approval_ui.app import create_app
from services.approval_ui.auth import (
    COOKIE_NAME,
    AuthConfig,
    issue_session,
    password_matches,
    path_is_public,
    session_is_valid,
)

PASSWORD = "correct-horse-battery-staple"


@pytest.fixture
def guarded(monkeypatch) -> TestClient:
    monkeypatch.setenv("OVERTURN_UI_PASSWORD", PASSWORD)
    monkeypatch.setenv("OVERTURN_UI_SECRET", "s" * 64)
    return TestClient(create_app(MemoryStore()), follow_redirects=False)


class TestTheDoorIsShut:
    @pytest.mark.parametrize("path", ["/", "/case/CASE-001", "/case/CASE-001/clinical"])
    def test_a_page_showing_a_case_redirects_to_login(self, guarded, path):
        response = guarded.get(path)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_a_post_is_also_guarded(self, guarded):
        response = guarded.post("/case/CASE-001/approve", data={"draft_attempt": 1})
        assert response.status_code == 303
        assert response.headers["location"] == "/login"

    def test_the_login_page_itself_is_reachable(self, guarded):
        assert guarded.get("/login").status_code == 200

    @pytest.mark.parametrize("path", ["/health", "/healthz"])
    def test_health_checks_stay_outside_the_door(self, guarded, path):
        """A health check behind a login reports the service unhealthy the
        moment the login starts working."""
        assert guarded.get(path).status_code == 200


class TestSigningIn:
    def test_the_right_password_gets_in(self, guarded):
        response = guarded.post("/login", data={"password": PASSWORD})
        assert response.status_code == 303
        assert response.headers["location"] == "/"
        assert COOKIE_NAME in response.cookies

    def test_the_session_then_works(self, guarded):
        guarded.post("/login", data={"password": PASSWORD})
        assert guarded.get("/").status_code == 200

    def test_the_wrong_password_does_not(self, guarded):
        response = guarded.post("/login", data={"password": "wrong"})
        assert response.status_code == 401
        assert COOKIE_NAME not in response.cookies

    def test_an_empty_password_does_not(self, guarded):
        assert guarded.post("/login", data={"password": ""}).status_code == 401

    def test_the_cookie_is_httponly_and_samesite(self, guarded):
        """It carries a session. Script access leaks it."""
        raw = guarded.post("/login", data={"password": PASSWORD}).headers["set-cookie"]
        assert "HttpOnly" in raw
        assert "SameSite=lax" in raw.replace("Samesite", "SameSite")

    def test_the_cookie_is_secure_over_https(self, monkeypatch):
        """Secure only where the connection is.

        A Secure cookie over http is dropped silently, which on a local run
        looks exactly like a login that takes the password and then refuses to
        let you in. Cloud Run is always https.
        """
        monkeypatch.setenv("OVERTURN_UI_PASSWORD", PASSWORD)
        monkeypatch.setenv("OVERTURN_UI_SECRET", "s" * 64)
        client = TestClient(
            create_app(MemoryStore()), base_url="https://testserver", follow_redirects=False
        )
        raw = client.post("/login", data={"password": PASSWORD}).headers["set-cookie"]
        assert "Secure" in raw

    def test_signing_out_clears_the_session(self, guarded):
        guarded.post("/login", data={"password": PASSWORD})
        guarded.post("/logout")
        assert guarded.get("/").status_code == 303


class TestForgery:
    def test_a_made_up_cookie_is_refused(self, guarded):
        guarded.cookies.set(COOKIE_NAME, "99999999999.deadbeef")
        assert guarded.get("/").status_code == 303

    def test_a_tampered_cookie_is_refused(self):
        config = AuthConfig(password=PASSWORD, secret="s" * 64)
        token = issue_session(config)
        flipped = token[:-1] + ("a" if token[-1] != "a" else "b")
        assert session_is_valid(flipped, config) is False

    def test_a_cookie_signed_with_another_secret_is_refused(self):
        issued = issue_session(AuthConfig(password=PASSWORD, secret="a" * 64))
        assert session_is_valid(issued, AuthConfig(password=PASSWORD, secret="b" * 64)) is False

    def test_an_expired_session_is_refused(self, monkeypatch):
        config = AuthConfig(password=PASSWORD, secret="s" * 64)
        token = issue_session(config)
        import services.approval_ui.auth as auth_module

        monkeypatch.setattr(auth_module.time, "time", lambda: 10**11)
        assert session_is_valid(token, config) is False


class TestNoPasswordConfigured:
    """Local runs and the test suite have no password, and must not pretend to."""

    def test_the_interface_is_open_when_no_password_is_set(self):
        client = TestClient(create_app(MemoryStore()), follow_redirects=False)
        assert client.get("/").status_code == 200

    def test_config_reports_itself_disabled(self):
        assert AuthConfig(password="", secret="x").enabled is False


class TestPublicPaths:
    def test_only_the_intended_paths_are_public(self):
        assert path_is_public("/login")
        assert path_is_public("/health")
        assert path_is_public("/healthz")
        assert not path_is_public("/")
        assert not path_is_public("/case/CASE-001")


class TestPasswordComparison:
    def test_whitespace_is_forgiven_but_content_is_not(self):
        config = AuthConfig(password=PASSWORD, secret="s")
        assert password_matches(f"  {PASSWORD}  ", config)
        assert not password_matches(PASSWORD.upper(), config)


class TestThemeControl:
    """A colour choice, without JavaScript.

    The page ships no script — there is a test asserting that — so the
    preference is a cookie and a redirect. It also means the control works with
    scripting disabled, for the same reason the rest of this interface does.
    """

    @pytest.fixture
    def client(self) -> TestClient:
        return TestClient(create_app(MemoryStore()), follow_redirects=False)

    def test_the_control_is_on_the_page(self, client):
        html = client.get("/").text
        assert 'action="/theme"' in html
        for mode in ("auto", "light", "dark"):
            assert f'value="{mode}"' in html

    def test_the_page_still_ships_no_script(self, client):
        assert "<script" not in client.get("/").text.lower()

    @pytest.mark.parametrize("mode", ["light", "dark"])
    def test_choosing_a_theme_sets_a_cookie_and_stamps_the_root(self, client, mode):
        response = client.post("/theme", data={"mode": mode, "back": "/"})
        assert response.status_code == 303
        assert response.cookies.get("overturn_theme") == mode
        html = client.get("/").text
        assert f'<html lang="en" data-theme="{mode}">' in html

    def test_auto_clears_the_choice_and_defers_to_the_system(self, client):
        client.post("/theme", data={"mode": "dark", "back": "/"})
        client.post("/theme", data={"mode": "auto", "back": "/"})
        # The CSS legitimately contains data-theme selectors, so check the tag.
        assert '<html lang="en">' in client.get("/").text

    def test_it_returns_you_to_the_page_you_were_on(self, client):
        response = client.post("/theme", data={"mode": "dark", "back": "/case/CASE-001"})
        assert response.headers["location"] == "/case/CASE-001"

    def test_it_will_not_redirect_off_site(self, client):
        """`back` comes from a form field, so it is attacker-controlled."""
        for hostile in ("https://example.com/", "//example.com/", "javascript:alert(1)"):
            response = client.post("/theme", data={"mode": "dark", "back": hostile})
            assert response.headers["location"] == "/"

    def test_the_theme_control_works_before_signing_in(self, monkeypatch):
        monkeypatch.setenv("OVERTURN_UI_PASSWORD", PASSWORD)
        monkeypatch.setenv("OVERTURN_UI_SECRET", "s" * 64)
        guarded = TestClient(create_app(MemoryStore()), follow_redirects=False)
        assert guarded.post("/theme", data={"mode": "dark", "back": "/login"}).status_code == 303
        assert '<html lang="en" data-theme="dark">' in guarded.get("/login").text


class TestBothPalettesAreDefined:
    """A token defined only inside a media query has no value when a reader
    stamps the opposite choice on the root."""

    def test_the_dark_palette_applies_from_both_rules(self):
        from pathlib import Path

        css = (
            Path(__file__).resolve().parents[1] / "services/approval_ui/templates/base.html"
        ).read_text()
        assert ':root:not([data-theme="light"])' in css
        assert ':root[data-theme="dark"]' in css

    def test_the_body_paints_its_own_background(self):
        from pathlib import Path

        css = (
            Path(__file__).resolve().parents[1] / "services/approval_ui/templates/base.html"
        ).read_text()
        assert "background" in css.split("body {")[1].split("}")[0]


class TestWhereYouLandAfterSigningIn:
    """The queue is the obvious landing page and the wrong first impression.

    Someone opening this for the first time should arrive at the case that best
    shows what the system does — the one where Verification rejected a draft and
    made it try again, which is the whole argument for the product.
    """

    def _seed(self, store, case_id: str, attempts: int) -> None:
        from core.gateway import GatewayHandle
        from core.schemas.case import CaseRecord
        from core.schemas.draft import AppealDraft
        from core.schemas.enums import AgentName, CaseStatus
        from core.state import CaseRepository

        case = CaseRecord(case_id=case_id, source_document_uri=f"gs://b/{case_id}.txt")
        case.drafts = [
            AppealDraft(case_id=case_id, attempt=n, subject_line="s", body="b" * 60)
            for n in range(1, attempts + 1)
        ]
        case.transition(CaseStatus.AWAITING_APPROVAL, actor="test")
        CaseRepository(store, GatewayHandle(AgentName.ORCHESTRATOR)).create(case)

    def test_it_lands_on_the_case_that_took_the_most_attempts(self, monkeypatch):
        monkeypatch.setenv("OVERTURN_UI_PASSWORD", PASSWORD)
        monkeypatch.setenv("OVERTURN_UI_SECRET", "s" * 64)
        store = MemoryStore()
        self._seed(store, "CASE-EASY", attempts=1)
        self._seed(store, "CASE-HARD", attempts=3)

        client = TestClient(create_app(store), follow_redirects=False)
        response = client.post("/login", data={"password": PASSWORD})
        assert response.headers["location"] == "/case/CASE-HARD"

    def test_it_falls_back_to_the_queue_when_nothing_is_waiting(self, monkeypatch):
        monkeypatch.setenv("OVERTURN_UI_PASSWORD", PASSWORD)
        monkeypatch.setenv("OVERTURN_UI_SECRET", "s" * 64)
        client = TestClient(create_app(MemoryStore()), follow_redirects=False)
        assert client.post("/login", data={"password": PASSWORD}).headers["location"] == "/"

    def test_a_failing_queue_read_does_not_break_signing_in(self, monkeypatch):
        """A broken landing page must not become a broken login."""
        monkeypatch.setenv("OVERTURN_UI_PASSWORD", PASSWORD)
        monkeypatch.setenv("OVERTURN_UI_SECRET", "s" * 64)

        class Exploding(MemoryStore):
            def query(self, *args, **kwargs):
                raise RuntimeError("firestore is unhappy")

        client = TestClient(create_app(Exploding()), follow_redirects=False)
        response = client.post("/login", data={"password": PASSWORD})
        assert response.status_code == 303
        assert response.headers["location"] == "/"
