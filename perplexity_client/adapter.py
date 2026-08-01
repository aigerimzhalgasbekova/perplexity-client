"""Everything this tool knows about Perplexity, in one module.

Nothing else in the package names an endpoint, a JSON key or a DOM role. A frontend
change is then a patch to this file (PRD §4, adapter isolation) rather than a hunt
through the package.

Pure by design -- bytes and dicts in, `Response` out, no browser and no I/O beyond the
two `page.evaluate` probes and the CDP tee. That is what lets the parser be tested
against recorded fixtures instead of the live site (PRD §7).
"""

HOME = "https://www.perplexity.ai/"
# The account's only quota signal. It reports availability per mode and no rate at all
# -- no window, no reset, no remaining count for the modes this tool drives (M2:
# `remaining_detail.kind == "not_provided"`). See docs/M2-findings.md.
RATE_LIMIT = "/rest/rate-limit/status"
# The two modes the tool can drive, mapped to that endpoint's names. Others
# (`agentic_research`, `labs`) are deliberately ignored: warning about a mode we never
# use is noise, and one of them was already exhausted on the probed account.
MODES = {"search": "pro_search", "research": "research"}
# NextAuth's session endpoint: {} when anonymous, {"user": {...}} when signed in.
# Cookie presence proves nothing -- an expired cookie is still a cookie -- and every
# /rest/ endpoint answers 200 for anonymous visitors too, so this is the one probe
# that reflects what the *server* thinks of the session.
AUTH_PROBE = """() => fetch('/api/auth/session', {credentials: 'include'})
    .then(r => r.json()).then(j => !!(j && j.user)).catch(() => null)"""
QUOTA_PROBE = """path => fetch(path, {credentials: 'include'})
    .then(r => r.json()).catch(() => null)"""
CHALLENGE_TITLES = ("just a moment", "attention required", "checking your browser")
SETTLE_TIMEOUT = 15.0


def is_challenge(title: str, url: str) -> bool:
    return (any(t in (title or "").lower() for t in CHALLENGE_TITLES)
            or "/cdn-cgi/challenge" in (url or ""))


def classify(title: str, url: str, authed: bool) -> str:
    """ok | expired | challenged -- `no-session` is decided before the page load.

    Challenge is checked first: the auth probe answers 200 with an empty body from
    behind an interstitial, which would otherwise read as `expired`."""
    if is_challenge(title, url):
        return "challenged"
    return "ok" if authed else "expired"


def quota(page) -> dict[str, bool]:
    """`{mode: still available}` from a page already on perplexity.ai.

    Empty when the endpoint could not be read: a quota reading is advisory, and
    failing a command over it would be worse than not knowing.
    """
    try:
        body = page.evaluate(QUOTA_PROBE, RATE_LIMIT)
    except Exception:
        # The `evaluate` itself, not the fetch the probe already catches: a client-side
        # navigation can destroy the execution context between one probe and the next.
        # Only the `ok` path reaches here, so without this the sessions that crash are
        # exactly the healthy ones -- and on a non-PplxError, at the CLI's exit code
        # for "session not usable".
        return {}
    modes = body.get("modes") if isinstance(body, dict) else None
    return {name: bool(v.get("available"))
            for name, v in (modes or {}).items() if isinstance(v, dict)}


def exhausted(page) -> list[str]:
    """Modes this tool can drive that the server says are used up."""
    q = quota(page)
    return [mode for mode, name in MODES.items() if q.get(name) is False]
