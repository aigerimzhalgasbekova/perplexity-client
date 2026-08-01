"""The public surface: `login`, `status` (PRD milestone 1, US-7).

Orchestration only. Everything Perplexity-specific -- endpoints, probes, the answer
parser -- lives in `adapter`, so a frontend change is a patch there and not here.
"""

import contextlib
import sys
import time

from playwright.sync_api import BrowserContext, Page

from . import adapter
from .adapter import HOME, Response
from .chrome import chrome, chrome_version, profile_dir, save_session
from .errors import (
    ChallengeEncounteredError,
    ModelMismatchError,
    PplxError,
    QuotaExhaustedError,
    SessionExpiredError,
)
from .pacing import default_interval, env_float
from .research import OnClarify, ResearchTask

LOGIN_TIMEOUT = 600.0
# A ceiling, not an expectation: a search answer takes ~10-30s. It exists so a stream
# that stalls forever fails as an incomplete answer instead of hanging an agent loop.
ANSWER_TIMEOUT = 180.0
# Research only needs its *id*, which is on the first frame -- so this waits for the
# stream to start, not for it to finish.
SUBMIT_TIMEOUT = 60.0
# What `doctor` asks. Short, cheap, and certain to be answered with sources -- the
# invariants under test are the parser's, not Perplexity's knowledge.
DOCTOR_QUERY = "what is the capital of Australia"
POLL_MS = 250


def _has_session_cookie(ctx: BrowserContext) -> bool:
    # Context-level, not page-level: a login redirect can close the tab out from
    # under us, but cookies survive it.
    return any("session-token" in c["name"] for c in ctx.cookies())


def _authed(ctx: BrowserContext) -> bool:
    """Run the auth probe on a tab that is already on perplexity.ai.

    Relative fetch, so it only means anything from that origin -- mid-login the
    user may be parked on an SSO provider, which reads as "not done yet".
    """
    for page in ctx.pages:
        if page.url.startswith(HOME):
            return bool(page.evaluate(adapter.AUTH_PROBE))
    return False


def _settled(page: Page) -> tuple[str, str]:
    """Land on the homepage and give a real Chrome its usual chance to clear an
    interstitial by itself before anyone calls it a challenge."""
    page.goto(HOME, wait_until="domcontentloaded")
    deadline = time.monotonic() + adapter.SETTLE_TIMEOUT
    while adapter.is_challenge(page.title(), page.url) and time.monotonic() < deadline:
        page.wait_for_timeout(1000)
    return page.title(), page.url


def _blame(page: Page, what: str) -> None:
    """The page did not do what the adapter expects. Say which of the two causes it was.

    A challenge and a changed frontend both look like silence, and they have opposite
    fixes -- one is `pplx login`, the other is a patch to the adapter.
    """
    title = url = ""
    with contextlib.suppress(Exception):
        # The tab may be gone -- a crashed Chrome is one of the ways a stream never
        # arrives. Failing to read it is not the story; failing to explain would be.
        title, url = page.title(), page.url
    if adapter.is_challenge(title, url):
        raise ChallengeEncounteredError(
            "perplexity.ai served a bot-detection challenge instead of an answer; this "
            "tool never bypasses one. Open Chrome yourself, then re-run: pplx login"
        )
    raise PplxError(
        f"{what} -- perplexity.ai's frontend has most likely changed. Run: pplx doctor"
    )


def _ready(ctx: BrowserContext, page: Page, mode: str = "search") -> None:
    """Fail before a query is spent, never during one.

    Each of these costs a page load or a fetch; the thing being protected is a query,
    which the account has a finite and invisible supply of (docs/M2-findings.md).
    """
    if not _has_session_cookie(ctx):
        raise SessionExpiredError("no session yet -- run: pplx login")
    try:
        title, url = _settled(page)
        authed = bool(page.evaluate(adapter.AUTH_PROBE))
    except Exception as e:  # a network failure is not a traceback-worthy bug
        raise PplxError(f"could not reach {HOME}: {e}") from e
    state = adapter.classify(title, url, authed)
    if state == "challenged":
        raise ChallengeEncounteredError(
            "perplexity.ai served a bot-detection challenge; this tool never bypasses "
            "one. Open Chrome yourself, then re-run: pplx login"
        )
    if state != "ok":
        raise SessionExpiredError("session expired or revoked -- run: pplx login")
    if mode in adapter.exhausted(page):
        raise QuotaExhaustedError(
            f"the account's {mode} quota is used up. It resets on Perplexity's own "
            f"schedule, which the account cannot see (docs/M2-findings.md)"
        )


def _submit_task(
    page: Page,
    stream: adapter.Stream,
    query: str,
    follow_up: bool,
    on_clarify: OnClarify,
) -> ResearchTask:
    """Send a research query and hand back its id as soon as the id exists.

    `backend_uuid` is on the *first* frame, while status is still PENDING (M0 Q5), so
    this returns in seconds rather than in the tens of minutes the run itself takes --
    which is what makes `--detach` possible at all.
    """
    try:
        adapter.submit(page, query, follow_up=follow_up)
    except Exception:
        _blame(page, "the query box never appeared")
    deadline = time.monotonic() + env_float("PPLX_SUBMIT_TIMEOUT", SUBMIT_TIMEOUT)
    while time.monotonic() < deadline:
        for frame in stream.frames:
            if uuid := frame.get("backend_uuid"):
                return ResearchTask(
                    task_id=str(uuid),
                    thread_id=str(frame.get("thread_url_slug") or uuid),
                    on_clarify=on_clarify,
                )
        if stream.ended:
            break
        page.wait_for_timeout(POLL_MS)
    _blame(page, "the research task was submitted but never reported an id")
    raise AssertionError("unreachable")  # _blame always raises


def _open_thread(page: Page, thread_id: str) -> None:
    page.goto(adapter.thread_url(thread_id), wait_until="domcontentloaded")
    deadline = time.monotonic() + adapter.SETTLE_TIMEOUT
    while adapter.is_challenge(page.title(), page.url) and time.monotonic() < deadline:
        page.wait_for_timeout(1000)
    if adapter.is_challenge(page.title(), page.url):
        raise ChallengeEncounteredError(
            "perplexity.ai served a bot-detection challenge on that thread; this tool "
            "never bypasses one. Open Chrome yourself, then re-run: pplx login"
        )


def _configure(page: Page, mode: str, model: str) -> str:
    """Set mode and model on whatever composer is on screen. Returns the expected
    `model_preference`, or `""` when "best" was asked for and anything is allowed.

    Mode first, deliberately: switching it resets the model to that mode's default,
    so the other order silently discards the model that was just chosen (2026-08-01).
    """
    adapter.pick_mode(page, mode)
    if mode != "search":
        # Research has one model (`pplx_alpha`) and no picker of its own; asking for a
        # search model there would select nothing and mean nothing.
        if not adapter.is_best(model):
            raise PplxError(
                f"model {model!r} cannot be requested in {mode} mode -- research runs "
                f"on Perplexity's own research model. Pass model='best'"
            )
        return ""
    offers = adapter.offered(adapter.model_config(page))
    if not offers and not adapter.is_best(model):
        # Otherwise `resolve` blames the account -- "no model called 'Sonar 2'. This
        # account's picker offers: Best" -- for what is really a failed fetch, and
        # sends the user to check a subscription that was never the problem.
        raise PplxError(
            f"could not read the model catalogue ({adapter.MODEL_CONFIG}), so "
            f"{model!r} cannot be checked against it. Retry, or pass model='best'"
        )
    label, expected = adapter.resolve(model, offers)
    adapter.pick_model(page, label, offers)
    return expected


def _verify(page: Page, r: Response, mode: str, expected: str) -> None:
    """The answer arrived -- was it the one that was ordered? (US-6)

    `Response.model` is the *observed* model throughout, so this compares against it
    rather than overwriting it. Checked after the fact because the substitution
    happens server-side: the request really did carry the right `model_preference`
    (docs/M4-M8-findings.md).
    """
    if r.mode != mode:
        raise PplxError(
            f"asked in {mode!r} mode but the answer came back as {r.mode!r} -- "
            f"perplexity.ai's frontend has most likely changed. Run: pplx doctor"
        )
    if not expected or not r.model or r.model == expected:
        return
    # Only now, on the way out: naming the models costs a fetch, and the happy path
    # should not pay for the error path's vocabulary.
    config = adapter.model_config(page)
    raise ModelMismatchError(
        f"asked for {adapter.model_label(config, expected)} but "
        f"{adapter.model_label(config, r.model)} served the answer. Perplexity "
        f"substituted it server-side; pass model='best' to accept whatever it picks"
    )


class Client:
    def ask(
        self,
        query: str,
        mode: str = "search",
        model: str = "best",
        thread_id: str | None = None,
        allow_incomplete: bool = False,
        on_clarify: OnClarify = "skip",
    ) -> Response | ResearchTask:
        """One query. Search mode blocks and answers; research hands back a task.

        Raises `IncompleteAnswerError` rather than returning a truncated answer (US-3):
        a wrong-but-plausible answer entering an agent pipeline as fact is the failure
        PRD §10 rates critical, and it is invisible unless the tool refuses.

        `thread_id` continues an existing conversation: the query is typed into that
        thread's own composer, which is what makes the frontend link the turns (M5).
        """
        if not (query := query.strip()):
            raise PplxError("empty query")
        if mode not in adapter.MODE_LABELS:
            raise PplxError(f"unknown mode {mode!r}; expected search or research")
        with chrome(headless=True, interval=default_interval()) as (ctx, page):
            _ready(ctx, page, mode)
            if thread_id:
                # The thread's own page, not the homepage: same DOM verbs, but the
                # composer down there is the one that continues the conversation.
                _open_thread(page, thread_id)
            expected = _configure(page, mode, model)
            stream = adapter.tee(ctx, page)
            if mode == "research":
                try:
                    return _submit_task(
                        page, stream, query, bool(thread_id), on_clarify
                    )
                finally:
                    with contextlib.suppress(Exception):
                        if stream.cdp is not None:
                            stream.cdp.detach()
            try:
                try:
                    adapter.submit(page, query, follow_up=bool(thread_id))
                except Exception:
                    # The box not being there has the same two causes as a stream that
                    # never arrives, and _blame already tells them apart. Raw, this is a
                    # Playwright timeout that says nothing about either.
                    _blame(page, "the query box never appeared")
                deadline = time.monotonic() + env_float(
                    "PPLX_ASK_TIMEOUT", ANSWER_TIMEOUT
                )
                # Suppressed, not propagated: this is the longest-lived call in the
                # flow -- up to the whole answer timeout of a Chrome this tool launched
                # -- and a browser that dies here would reach the caller as a raw
                # Playwright error, past a contract (and the CLI's exit code) that is
                # `except PplxError`. Falling through keeps any frames that did arrive:
                # _blame diagnoses an empty stream, and parse_stream tells a partial one
                # apart from a complete one, which a raise here could not.
                with contextlib.suppress(Exception):
                    # Yielding through Playwright, not time.sleep: CDP events only
                    # dispatch while the greenlet yields, so a sleeping loop would
                    # receive nothing at all and every answer would "time out".
                    while (
                        not stream.done
                        and not stream.ended
                        and time.monotonic() < deadline
                    ):
                        page.wait_for_timeout(POLL_MS)
                if not stream.frames:
                    _blame(
                        page,
                        "the query was submitted but no answer stream was intercepted",
                    )
            finally:
                # It outlives the page otherwise, and `ask` is the call an agent loop
                # repeats. Best-effort: a teardown failure must not mask the answer.
                with contextlib.suppress(Exception):
                    if stream.cdp is not None:
                        stream.cdp.detach()
            r = adapter.parse_stream(stream.frames, allow_incomplete)
            _verify(page, r, mode, expected)
            return r

    def task(self, task_id: str) -> ResearchTask:
        """Rebuild a research task from its id alone (US-5).

        Costs nothing until something is asked of it. The id is a `backend_uuid`, and
        a research thread's first entry is its own thread, so it doubles as the slug.
        """
        if not (task_id := task_id.strip()):
            raise PplxError("empty task id")
        return ResearchTask(task_id=task_id)

    def doctor(self) -> list[tuple[str, bool, str]]:
        """Spend one real query and check every invariant this tool relies on (US-8).

        `(invariant, held, detail)` per row rather than a raised exception, because
        the useful output is the whole list: knowing that citations parsed but the
        completion signal did not is what points at which part of the frontend moved.

        Never run in CI -- it needs a logged-in account and it costs a query. See
        CONTRIBUTING.md.
        """
        rows: list[tuple[str, bool, str]] = []

        def check(name: str, ok: bool, detail: str) -> None:
            rows.append((name, ok, detail))

        try:
            check("chrome", True, chrome_version())
        except PplxError as e:
            check("chrome", False, str(e))
            return rows  # nothing below this can run without a browser
        state = self.status()
        check("session", state == "ok", state)
        if state != "ok":
            return rows
        try:
            r = self.ask(DOCTOR_QUERY)
        except PplxError as e:
            check("answer", False, f"{type(e).__name__}: {e}")
            return rows
        if isinstance(r, ResearchTask):  # pragma: no cover - search mode never is
            check("answer", False, "search mode returned a research task")
            return rows
        check("completion signal", r.complete, "terminal frame observed")
        check("answer text", bool(r.text.strip()), f"{len(r.text)} chars")
        check("citations", bool(r.citations), f"{len(r.citations)} sources")
        markers = sorted(adapter.markers_in(r.text))
        check(
            "citation index",
            all(1 <= n <= len(r.citations) for n in markers),
            f"markers {markers[:5]}{'...' if len(markers) > 5 else ''} -> "
            f"{len(r.citations)} sources",
        )
        check("thread id", bool(r.thread_id), r.thread_id)
        check("observed model", bool(r.model), r.model)
        check("observed mode", r.mode == "search", r.mode)
        # M5's silent-failure shape, checked live: a thread fetched without
        # THREAD_QUERY's parameters answers with entries but no blocks at all
        # (docs/M4-M8-findings.md), and every research poll rides this endpoint.
        # Free -- it re-reads the answer just paid for; a fresh thread's slug is its
        # first entry's backend_uuid, so `thread_id` is the right handle here.
        try:
            with chrome(headless=True) as (_ctx, page):
                page.goto(HOME, wait_until="domcontentloaded")
                body = page.evaluate(
                    adapter.FETCH_JSON, adapter.thread_path(r.thread_id)
                )
                entry = adapter.entry_of(body if isinstance(body, dict) else {})
                blocks = entry.get("blocks") or ()
                check(
                    "thread blocks",
                    bool(blocks),
                    f"{len(blocks)} blocks on the resumed thread",
                )
        except Exception as e:
            check("thread blocks", False, f"{type(e).__name__}: {e}")
        return rows

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
                    raise PplxError(
                        f"browser closed before login completed: {e}"
                    ) from e
                if done:
                    time.sleep(
                        2
                    )  # let the post-login redirects land before snapshotting
                    if not save_session(ctx):
                        raise PplxError(
                            "logged in, but no session cookie was left to save; "
                            "re-run: pplx login"
                        )
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
                while (
                    adapter.is_challenge(page.title(), page.url)
                    and time.monotonic() < deadline
                ):
                    page.wait_for_timeout(
                        1000
                    )  # a real Chrome usually clears it itself
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
                print(
                    f"warning: quota exhausted for: {', '.join(used_up)}",
                    file=sys.stderr,
                )
            return state
