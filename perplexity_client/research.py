"""Deep Research: submit, walk away, come back for it (US-4, US-5).

The task lives on Perplexity's side, so this holds no state worth losing -- a
`ResearchTask` is an id and a cached snapshot. Nothing is kept between processes, and
`--detach` works because the id is enough to find the task again.

Two sources, because neither is sufficient alone (docs/M4-M8-findings.md):

  * `GET /rest/thread/<uuid>` says whether the task is finished, and carries the
    finished answer -- but while an entry is `pending` it carries no blocks at all,
    so it cannot report progress and cannot see a clarifying question.
  * Opening the thread page subscribes to
    `/rest/sse/perplexity_ask/reconnect/<uuid>`, a live feed of the same diffs the
    original run sent -- the plan filling in, the workflow's questions, the answer.
    Its frames are LF-delimited and its data lines have no space after the colon,
    unlike the ask stream's; see `adapter.FRAME_SPLIT`.

So `wait()` opens the page once, reads everything in flight off that stream, and polls
the thread document to learn the task is over and to collect the finished answer. A
submitting process is not privileged: this is the same view from anywhere, which is
US-5.
"""

import contextlib
import dataclasses
import sys
import time
from collections.abc import Callable

from playwright.sync_api import Page

from . import adapter
from .adapter import Json, Question, Response
from .chrome import chrome
from .errors import ClarificationRequiredError, PplxError

# What a caller may pass for `on_clarify`: skip them (the default, because an
# unattended client is the primary use case), refuse to guess, or answer them.
type OnClarify = str | Callable[[list[Question]], list[str]]

# Gentle on purpose: polling every 5s got the thread endpoint to start answering with
# an empty body after ~150 requests (docs/M4-M8-findings.md). Progress arrives on the
# stream anyway, so this only has to notice that the task finished.
POLL_SECONDS = 15.0
# `wait(timeout=None)` still has a ceiling: PRD §7 asks that the tool never block
# indefinitely by default, and a research run that has gone half an hour without
# finishing is a fault to report, not a wait to extend.
WAIT_TIMEOUT = 1800.0


@dataclasses.dataclass
class ResearchTask:
    """A Deep Research run in flight, or one that finished before this process began."""

    task_id: str
    thread_id: str = ""
    on_clarify: OnClarify = "skip"
    _entry: Json = dataclasses.field(default_factory=dict, repr=False)
    _answered: set[str] = dataclasses.field(default_factory=set, repr=False)

    def __post_init__(self) -> None:
        self.thread_id = self.thread_id or self.task_id

    @property
    def status(self) -> str:
        """`pending | running | awaiting_input | done | failed`, as last seen.

        Reads the cached snapshot rather than the network, so that a caller can ask
        twice without spending two page loads; `refresh()` is the one that costs.
        """
        return adapter.task_status(self._entry)

    @property
    def progress(self) -> list[tuple[str, str]] | None:
        return adapter.plan_of(self._entry)

    @property
    def questions(self) -> list[Question]:
        return adapter.questions_of(self._entry)

    def refresh(self) -> ResearchTask:
        """One page load -- not one query -- against the thread endpoint."""
        with chrome(headless=True) as (_ctx, page):
            page.goto(adapter.HOME, wait_until="domcontentloaded")
            self._poll(page)
        return self

    def answer(self, responses: list[str]) -> None:
        """Answer the outstanding clarifying questions from a standalone process.

        Rarely the right call: the server gives 60 seconds before answering for you,
        and a fresh browser spends a chunk of that getting to the page. Inside
        `wait()`, `on_clarify=<callable>` is the version that has the window to spare.
        """
        with chrome(headless=True) as (_ctx, page):
            self._open(page)
            self._poll(page)
            if self.status != "awaiting_input":
                raise PplxError(
                    f"task {self.task_id} is {self.status}, not waiting for an answer"
                )
            adapter.answer_clarifiers(page, responses)

    def wait(
        self,
        timeout: float | None = None,
        allow_incomplete: bool = False,
        on_progress: Callable[[list[tuple[str, str]]], None] | None = None,
    ) -> Response:
        """Block until the task settles, then return its answer.

        A timeout raises and leaves the task running -- it is on Perplexity's side and
        nothing here could cancel it even if that were wanted (PRD §7). The id in the
        message is enough to pick it up again later.
        """
        limit = timeout if timeout is not None else WAIT_TIMEOUT
        deadline = time.monotonic() + limit
        with chrome(headless=True) as (ctx, page):
            # Teed before navigating: opening a running thread subscribes to
            # `/rest/sse/perplexity_ask/reconnect/<uuid>`, and that stream is the only
            # place a *running* task's plan and questions exist -- the thread document
            # carries no plan and no workflow block until the entry completes.
            stream = adapter.tee(ctx, page, reconnect=True)
            try:
                self._open(page)
                return self._loop(
                    page, stream, deadline, limit, allow_incomplete, on_progress
                )
            finally:
                with contextlib.suppress(Exception):
                    if stream.cdp is not None:
                        stream.cdp.detach()

    def _loop(
        self,
        page: Page,
        stream: adapter.Stream,
        deadline: float,
        limit: float,
        allow_incomplete: bool,
        on_progress: Callable[[list[tuple[str, str]]], None] | None,
    ) -> Response:
        """The poll loop, split out only so `wait` can detach the tee in a `finally`."""
        while True:
            body = self._poll(page, stream)
            # Settled is the *document's* verdict, not the merged view's. The stream
            # can carry its terminal frame while the thread document still says
            # PENDING, and returning then would parse an unfinished document and
            # raise IncompleteAnswerError over an answer that is complete and already
            # paid for. Waiting one more poll costs 15s and cannot be wrong.
            settled = adapter.task_status(adapter.entry_of(body, self.task_id))
            if settled == "done":
                return adapter.parse_thread(body, allow_incomplete, self.task_id)
            if settled == "failed":
                raise PplxError(f"research task {self.task_id} failed")
            # Everything else comes from the live view, which is the only place a
            # running task's plan and questions exist.
            state = self.status
            if state == "awaiting_input":
                self._clarify(page)
            if on_progress and (goals := self.progress):
                # Real progress, not a spinner (US-4): these are the run's own
                # goals, and they arrive whether or not anyone is watching.
                on_progress(goals)
            if time.monotonic() > deadline:
                raise PplxError(
                    f"research task {self.task_id} is still {state} after "
                    f"{limit:.0f}s; it has not been cancelled -- retrieve it "
                    f"later with: pplx result {self.task_id}"
                )
            # Through Playwright rather than time.sleep: this page is live, and a
            # sleeping greenlet stops servicing it.
            page.wait_for_timeout(int(POLL_SECONDS * 1000))

    # --- internals ----------------------------------------------------------

    def _open(self, page: Page) -> None:
        # The thread's own page, not the homepage: polling would work from either, but
        # the clarifying-question wizard is DOM, and it only exists here.
        page.goto(adapter.thread_url(self.thread_id), wait_until="domcontentloaded")
        page.wait_for_timeout(1500)

    def _poll(self, page: Page, stream: adapter.Stream | None = None) -> Json:
        """Read the task's state. The thread document decides whether it is finished;
        the live stream, if there is one, is the only source of everything else.

        A poll that comes back with no entry is kept rather than believed: sustained
        polling gets the endpoint to answer with an empty body (observed after ~150
        requests, `spike/probe_poll.py`), and treating that as "the task vanished"
        would turn a throttle into a state change.
        """
        body = page.evaluate(adapter.FETCH_JSON, adapter.thread_path(self.task_id))
        body = body if isinstance(body, dict) else {}
        if entry := adapter.entry_of(body, self.task_id):
            self._entry = entry
        if adapter.task_status(self._entry) != "done" and stream and stream.frames:
            self._entry = adapter.entry_from_frames(stream.frames)
        return body

    def _clarify(self, page: Page) -> None:
        questions = self.questions
        outstanding = {q.text for q in questions} - self._answered
        if not questions or not outstanding:
            return
        if self.on_clarify == "skip":
            # Nothing to do, and nothing to press: the payload carries its own
            # `timeout_seconds` (60 observed) after which the server answers with an
            # empty response and research continues (docs/M4-M8-findings.md).
            print(
                f"note: research asked {len(questions)} clarifying question(s); "
                f"skipping, which costs up to a minute of waiting",
                file=sys.stderr,
            )
            self._answered |= {q.text for q in questions}
            return
        if self.on_clarify == "raise":
            raise ClarificationRequiredError(
                f"research stopped to ask {len(questions)} clarifying question(s); "
                f"answer them with on_clarify=<callable>, or pass on_clarify='skip'",
                list(questions),
            )
        if not callable(self.on_clarify):
            raise PplxError(
                f"on_clarify must be 'skip', 'raise' or a callable, not "
                f"{self.on_clarify!r}"
            )
        answers = self.on_clarify(list(questions))
        if len(answers) != len(questions):
            raise PplxError(
                f"on_clarify returned {len(answers)} answers for {len(questions)} "
                f"questions"
            )
        adapter.answer_clarifiers(page, list(answers))
        self._answered |= {q.text for q in questions}
