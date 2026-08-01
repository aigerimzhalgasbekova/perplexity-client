"""The public surface: `login`, `status` (PRD milestone 1, US-7).

Orchestration only. Everything Perplexity-specific -- endpoints, probes, the answer
parser -- lives in `adapter`, so a frontend change is a patch there and not here.
"""

import sys
import time

from . import adapter
from .adapter import HOME
from .chrome import chrome, profile_dir, save_session
from .errors import PplxError

LOGIN_TIMEOUT = 600.0


def _has_session_cookie(ctx) -> bool:
    # Context-level, not page-level: a login redirect can close the tab out from
    # under us, but cookies survive it.
    return any("session-token" in c["name"] for c in ctx.cookies())


def _authed(ctx) -> bool:
    """Run the auth probe on a tab that is already on perplexity.ai.

    Relative fetch, so it only means anything from that origin -- mid-login the
    user may be parked on an SSO provider, which reads as "not done yet".
    """
    for page in ctx.pages:
        if page.url.startswith(HOME):
            return bool(page.evaluate(adapter.AUTH_PROBE))
    return False


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
                    # The cookie is the cheap gate; the probe is what actually
                    # proves the login landed, because a session-token cookie can
                    # appear mid-flow and a redirect chain can outlast any timer.
                    done = _has_session_cookie(ctx) and _authed(ctx)
                except Exception as e:  # window closed before the login finished
                    raise PplxError(f"browser closed before login completed: {e}") from e
                if done:
                    time.sleep(2)  # let the post-login redirects land before snapshotting
                    if not save_session(ctx):
                        raise PplxError(
                            "logged in, but no session cookie was left to save; "
                            "re-run: pplx login")
                    return
                time.sleep(2)
        raise PplxError(f"timed out after {timeout:.0f}s waiting for login")

    def status(self) -> str:
        """One real page load, then one of ok | no-session | expired | challenged."""
        if not profile_dir().exists():
            return "no-session"
        with chrome(headless=True) as (ctx, page):
            # Judged on the profile's own cookies, not on session.json: that file is
            # a write-only export in M1, and any abandoned `pplx login` leaves the
            # profile dir behind -- whose empty profile then draws a Cloudflare
            # interstitial and would report `challenged` to a user who never logged in.
            if not _has_session_cookie(ctx):
                return "no-session"
            try:
                page.goto(HOME, wait_until="domcontentloaded")
                deadline = time.monotonic() + adapter.SETTLE_TIMEOUT
                while (adapter.is_challenge(page.title(), page.url)
                       and time.monotonic() < deadline):
                    page.wait_for_timeout(1000)  # a real Chrome usually clears it itself
                title, url = page.title(), page.url
                authed = page.evaluate(adapter.AUTH_PROBE)
            except Exception as e:  # a network failure is not a traceback-worthy bug
                raise PplxError(f"could not reach {HOME}: {e}") from e
            if authed is None and not adapter.is_challenge(title, url):
                raise PplxError("could not reach perplexity.ai's session endpoint")
            state = adapter.classify(title, url, bool(authed))
            if state == "ok" and (used_up := adapter.exhausted(page)):
                # Quota is a different axis from session validity, so it warns rather
                # than changing the state word or the exit code (US-7 wants exactly one
                # of four words on stdout). ponytail: printed here rather than returned
                # because the caller that cares is the CLI, and returning it would mean
                # changing status()'s documented return type for an advisory string.
                print(f"warning: quota exhausted for: {', '.join(used_up)}",
                      file=sys.stderr)
            return state
