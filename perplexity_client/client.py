"""Session bootstrap: `login` and `status` (PRD milestone 1, US-7).

ponytail: the Perplexity-specific constants live here until milestone 3's stream
parser justifies a separate adapter module. They are the only site knowledge in
the package -- chrome.py is site-agnostic.
"""

import time

from .chrome import (PplxError, chrome, profile_dir, save_session, session_path)

HOME = "https://www.perplexity.ai/"
# NextAuth's session endpoint: {} when anonymous, {"user": {...}} when signed in.
# Cookie presence proves nothing -- an expired cookie is still a cookie -- and every
# /rest/ endpoint answers 200 for anonymous visitors too, so this is the one probe
# that reflects what the *server* thinks of the session.
AUTH_PROBE = """() => fetch('/api/auth/session', {credentials: 'include'})
    .then(r => r.json()).then(j => !!(j && j.user)).catch(() => null)"""
CHALLENGE_TITLES = ("just a moment", "attention required", "checking your browser")
SETTLE_TIMEOUT = 15.0
LOGIN_TIMEOUT = 600.0


def is_challenge(title: str, url: str) -> bool:
    return (any(t in (title or "").lower() for t in CHALLENGE_TITLES)
            or "/cdn-cgi/challenge" in (url or ""))


def classify(title: str, url: str, authed: bool, had_session: bool) -> str:
    """One of ok | no-session | expired | challenged.

    Challenge is checked first: the auth probe answers 200 with an empty body from
    behind an interstitial, which would otherwise read as `expired`."""
    if is_challenge(title, url):
        return "challenged"
    if authed:
        return "ok"
    return "expired" if had_session else "no-session"


def _has_session_cookie(ctx) -> bool:
    # Context-level, not page-level: a login redirect can close the tab out from
    # under us, but cookies survive it.
    return any("session-token" in c["name"] for c in ctx.cookies())


class Client:
    def login(self, timeout: float = LOGIN_TIMEOUT) -> None:
        """Open a visible Chrome and wait for a manual login.

        The tool never sees or types a credential -- password, SSO and 2FA are all
        handled by the user in a real browser window (PRD §8).
        """
        with chrome(headless=False, url=HOME) as (ctx, _page):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    done = _has_session_cookie(ctx)
                except Exception as e:  # window closed before the login finished
                    raise PplxError(f"browser closed before login completed: {e}") from e
                if done:
                    time.sleep(2)  # let the post-login redirects land before snapshotting
                    save_session(ctx)
                    return
                time.sleep(2)
        raise PplxError(f"timed out after {timeout:.0f}s waiting for login")

    def status(self) -> str:
        """One real page load, then one of STATES. Costs a page load, not a query."""
        had_session = session_path().exists()
        if not had_session and not profile_dir().exists():
            return "no-session"
        with chrome(headless=True) as (_ctx, page):
            try:
                page.goto(HOME, wait_until="domcontentloaded")
                deadline = time.monotonic() + SETTLE_TIMEOUT
                while is_challenge(page.title(), page.url) and time.monotonic() < deadline:
                    page.wait_for_timeout(1000)  # a real Chrome usually clears it itself
                title, url = page.title(), page.url
                authed = page.evaluate(AUTH_PROBE)
            except Exception as e:  # a network failure is not a traceback-worthy bug
                raise PplxError(f"could not reach {HOME}: {e}") from e
            if authed is None and not is_challenge(title, url):
                raise PplxError("could not reach perplexity.ai's session endpoint")
            return classify(title, url, bool(authed), had_session)
