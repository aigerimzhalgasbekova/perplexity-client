"""Everything this tool knows about Perplexity, in one module.

Nothing else in the package names an endpoint, a JSON key or a DOM role. A frontend
change is then a patch to this file (PRD §4, adapter isolation) rather than a hunt
through the package.

Pure by design -- bytes and dicts in, `Response` out, no browser and no I/O beyond the
two `page.evaluate` probes and the CDP tee. That is what lets the parser be tested
against recorded fixtures instead of the live site (PRD §7).
"""

import base64
import dataclasses
import json
import re
from typing import Any, Literal

from playwright.sync_api import BrowserContext, CDPSession, Page

from .errors import (
    CitationError,
    IncompleteAnswerError,
    ModelUnavailableError,
    PplxError,
)

# Every payload here is JSON off the network: the keys are strings and the values are
# whatever perplexity.ai sent, which is exactly what `dict[str, Any]` says.
Json = dict[str, Any]

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
# One GET, run from the page so it carries the session. Used for the quota gate and
# for the model catalogue; both are reads of the site's own state, not answer content.
FETCH_JSON = """path => fetch(path, {credentials: 'include'})
    .then(r => r.json()).catch(() => null)"""
CHALLENGE_TITLES = ("just a moment", "attention required", "checking your browser")
SETTLE_TIMEOUT = 15.0
# The one request that carries an answer. The homepage fires ~40 other REST calls
# before it (M0), so the adapter keys on this path and ignores every other stream.
ASK_PATH = "/rest/sse/perplexity_ask"
# ...and its twin, which serves a *running* entry's stream to anyone who opens the
# thread. The distinction is load-bearing rather than cosmetic: `ASK_PATH` is a
# substring of this, so a tee that matched loosely while continuing a thread whose
# last turn was still generating would hand back the previous answer as this query's.
RECONNECT_PATH = ASK_PATH + "/reconnect/"
# The resume path: plain JSON, readable from any process with the session (M0 Q5).
#
# The query string is load-bearing, not decoration. Asked bare, this endpoint answers
# with entries that have **no `blocks` at all** -- ids, status and query text only --
# which reads exactly like "the answer is not there yet" and is really "you did not
# ask for it" (2026-08-01). `with_schematized_response` is the switch; the use-case
# list is what the frontend sends, kept whole rather than trimmed to the blocks this
# tool reads, because which of them gate which block is not documented anywhere.
THREAD = "/rest/thread/"
# limit/from_first as the frontend sends them: the first 10 turns of the thread. Every
# caller here either names the turn it wants or is reading a one-turn research thread,
# so paging past that has never been needed.
THREAD_QUERY = (
    "?with_parent_info=true&with_schematized_response=true&version=2.18"
    "&source=default&limit=10&offset=0&from_first=true"
) + "".join(
    f"&supported_block_use_cases={use}"
    for use in (  # noqa: SIM905 -- one wrapped string, not 32 quoted list items
        "answer_modes media_items knowledge_cards inline_entity_cards place_widgets "
        "finance_widgets sports_widgets news_widgets shopping_widgets jobs_widgets "
        "search_result_widgets inline_images inline_assets placeholder_cards "
        "diff_blocks inline_knowledge_cards entity_group_v2 refinement_filters "
        "canvas_mode maps_preview answer_tabs price_comparison_widgets "
        "preserve_latex generic_onboarding_widgets in_context_suggestions "
        "pending_followups inline_claims unified_assets workflow_steps "
        "workflow_widgets navigation_results background_agents"
    ).split()  # noqa: SIM905 -- one wrapped string, not 32 quoted list items
)
# CRLF per the SSE spec, and what `/rest/sse/perplexity_ask` sends. Reading a capture
# in text mode rewrites this to "\n\n" and the split then silently matches nothing
# (M0 Q1) -- hence bytes end to end.
FRAME_SEP = b"\r\n\r\n"
# ...but the spec permits bare LF, and a stream that used it would parse as zero frames
# while looking perfectly healthy: bytes arrive, nothing is malformed, and the answer
# simply never appears. Both endings are accepted rather than assumed.
FRAME_SPLIT = re.compile(rb"(?:\r\n|\r|\n){2}")
LINE_SPLIT = re.compile(rb"\r\n|\r|\n")
# No trailing space: `perplexity_ask` sends "data: {…}" and its reconnect twin sends
# "data:{…}". Requiring the space cost every frame of the second one -- silently, since
# a stream with no recognised frames is indistinguishable from one that sent nothing.
DATA = b"data:"
BOX_TIMEOUT = 30_000  # ms; the homepage has ~40 REST calls to get through first
# Fenced and inline code, removed before the citation-marker scan. See answer_from.
CODE = re.compile(r"```.*?```|`[^`\n]*`", re.S)


def is_challenge(title: str, url: str) -> bool:
    return any(
        t in (title or "").lower() for t in CHALLENGE_TITLES
    ) or "/cdn-cgi/challenge" in (url or "")


def classify(title: str, url: str, authed: bool) -> str:
    """ok | expired | challenged -- `no-session` is decided before the page load.

    Challenge is checked first: the auth probe answers 200 with an empty body from
    behind an interstitial, which would otherwise read as `expired`."""
    if is_challenge(title, url):
        return "challenged"
    return "ok" if authed else "expired"


def quota(page: Page) -> dict[str, bool]:
    """`{mode: still available}` from a page already on perplexity.ai.

    Empty when the endpoint could not be read: a quota reading is advisory, and
    failing a command over it would be worse than not knowing.
    """
    try:
        body = page.evaluate(FETCH_JSON, RATE_LIMIT)
    except Exception:
        # The `evaluate` itself, not the fetch the probe already catches: a client-side
        # navigation can destroy the execution context between one probe and the next.
        # Only the `ok` path reaches here, so without this the sessions that crash are
        # exactly the healthy ones -- and on a non-PplxError, at the CLI's exit code
        # for "session not usable".
        return {}
    modes = body.get("modes") if isinstance(body, dict) else None
    return {
        name: bool(v.get("available"))
        for name, v in (modes or {}).items()
        if isinstance(v, dict)
    }


def exhausted(page: Page) -> list[str]:
    """Modes this tool can drive that the server says are used up."""
    q = quota(page)
    return [mode for mode, name in MODES.items() if q.get(name) is False]


def thread_path(uuid: str) -> str:
    return f"{THREAD}{uuid}{THREAD_QUERY}"


# --- models ------------------------------------------------------------------------
# Two different lists live behind this endpoint and confusing them is the whole trap:
# `models` is an internal registry of 87 search-mode ids -- every model the site has
# ever shipped -- while `search_config` is the dozen the picker actually offers. Asking
# for something from the first list that is not in the second silently does nothing.


MODEL_CONFIG = "/rest/models/config/v2"
# What `model="best"` means: let Perplexity choose, and never call the result a
# mismatch. It is a label in the menu like any other, but it maps to no single id --
# the homepage sends `pplx_pro` and a thread composer sends `turbo`, both labelled
# "Best" (docs/M4-M8-findings.md).
BEST = "Best"
MODE_LABELS = {"search": "Search", "research": "Deep research"}


def model_config(page: Page) -> Json:
    body = page.evaluate(FETCH_JSON, MODEL_CONFIG)
    return body if isinstance(body, dict) else {}


def offered(config: Json) -> dict[str, str]:
    """`{label: model_preference}` for the models the search picker offers.

    Read from `search_config`, not from `models`: the latter also holds ids for other
    surfaces, and two of them are labelled "Claude Sonnet 5" -- one being the browser
    agent's. Cross-checking each id against `models[id].mode == "search"` is what tells
    those apart.
    """
    registry = config.get("models") or {}

    def is_search(mid: object) -> bool:
        m = registry.get(str(mid))
        return isinstance(m, dict) and m.get("mode") == "search"

    out: dict[str, str] = {}
    for entry in config.get("search_config") or ():
        if not isinstance(entry, dict) or not (label := str(entry.get("label") or "")):
            continue
        # Non-reasoning first: it is what the menu's own entry sends, while the
        # "Thinking" variant sits behind a submenu this tool does not open.
        for key in ("non_reasoning_model", "reasoning_model"):
            if (mid := entry.get(key)) and is_search(mid):
                out.setdefault(label, str(mid))
                break
    return out


def model_label(config: Json, model_id: str) -> str:
    """The human name behind an observed `display_model`, or the id if unknown.

    Only for error messages -- `Response.model` stays the raw observed id, because a
    label is the site's presentation of a model and the id is the model.
    """
    entry = (config.get("models") or {}).get(model_id)
    label = entry.get("label") if isinstance(entry, dict) else None
    return f"{label} ({model_id})" if label else model_id


def _key(s: str) -> str:
    return "".join(s.split()).lower()


def is_best(name: str) -> bool:
    return _key(name) == _key(BEST)


def resolve(name: str, offers: dict[str, str]) -> tuple[str, str]:
    """`(menu label, expected model_preference)` for a requested model.

    The id is `""` for "best", which is the signal that no mismatch check applies:
    auto-selection returning something else is the feature, not the failure (US-6).
    Matching is case- and space-insensitive so a caller can pass "sonar 2", "Sonar 2"
    or the wire id `experimental` and mean the same thing.
    """

    if is_best(name):
        return BEST, ""
    for label, mid in offers.items():
        if _key(name) in (_key(label), _key(mid)):
            return label, mid
    raise ModelUnavailableError(
        f"no model called {name!r}. This account's picker offers: "
        f"{', '.join(sorted(offers) + [BEST])}"
    )


# --- the answer ------------------------------------------------------------------
# One parser, two finders. The live stream's terminal frame and the resume endpoint's
# entry carry the same `blocks` list -- verified byte-identical against the M0
# fixtures, see docs/M3-findings.md -- so only *finding* the payload differs.


@dataclasses.dataclass(frozen=True)
class Citation:
    url: str
    title: str
    snippet: str | None


@dataclasses.dataclass(frozen=True)
class Response:
    text: str
    citations: list[Citation]
    model: str
    mode: str
    thread_id: str
    complete: bool


def _citations(results: Any) -> list[Citation]:
    # `snippet` arrives as "" rather than absent for some sources (M0 Q3). PRD §5 types
    # it `str | None`, so empty becomes None: a caller checking `is None` should not
    # have to also remember to check for the empty string.
    return [
        Citation(
            url=w.get("url") or "",
            title=w.get("name") or "",
            snippet=w.get("snippet") or None,
        )
        for w in results or ()
        if isinstance(w, dict)
    ]


def markers_in(text: str) -> set[int]:
    """The `[n]` citation markers in an answer's prose.

    Code is stripped first: `nums[0]` and `arr[10]` are not citations, and Perplexity
    is heavily used for programming questions. A marker never legitimately appears
    inside code, so nothing real is lost -- while scanning it would throw away a
    complete, correct answer after the query was already spent (M3).
    """
    return {int(n) for n in re.findall(r"\[(\d+)\]", CODE.sub("", text))}


def answer_from(entry: Json, complete: bool) -> Response:
    """A `Response` from one terminal SSE frame, or one resume entry -- same shape.

    Text and citations come out of the *same* dict, which is what PRD §5's same-payload
    invariant asks for: Perplexity renumbers and appends sources while an answer
    streams, so sampling the two a moment apart is how markers come to misattribute.
    """
    blocks = {
        b.get("intended_usage"): b
        for b in entry.get("blocks") or ()
        if isinstance(b, dict)
    }
    text = ((blocks.get("ask_text") or {}).get("markdown_block") or {}).get(
        "answer"
    ) or ""
    cites = _citations(
        ((blocks.get("web_results") or {}).get("web_result_block") or {}).get(
            "web_results"
        )
    )
    if complete:
        if not text:
            # Every lookup above is `or {}`-guarded, so a renamed block collapses to ""
            # and the marker check below then passes vacuously -- handing back
            # `complete=True, text=""`, which an agent reads as "Perplexity found
            # nothing". Plausible, actionable and wrong: PRD §10's critical row. A
            # finished answer with no text is not an outcome this protocol produces.
            raise IncompleteAnswerError(
                "the completion signal arrived but the answer block was empty -- "
                "perplexity.ai's frontend has most likely changed. Run: pplx doctor"
            )
        # Enforced on complete answers only: a stream cut mid-answer may carry a marker
        # whose source had not been delivered yet, and raising on output the caller
        # explicitly opted into would be a false alarm (docs/M3-findings.md).
        #
        # Read out of the prose, not the raw markdown: `nums[0]`, `arr[10]` and a
        # bracketed quantifier in a regex are not citations, and Perplexity is heavily
        # used for programming questions. Scanning the code too throws away a complete,
        # correct answer -- after the query is spent, with a message blaming the
        # frontend and prescribing a `doctor` run that spends another. A marker never
        # legitimately appears inside code, so nothing real is lost.
        markers = markers_in(text)
        if unmapped := sorted(markers - set(range(1, len(cites) + 1))):
            raise CitationError(
                f"the answer cites {unmapped} but only {len(cites)} sources came back, "
                f"so those markers point at nothing. Refusing to return an answer "
                f"whose citations cannot be trusted; run: pplx doctor"
            )
    return Response(
        text=text,
        citations=cites,
        model=entry.get("display_model") or "",
        # The server's own word, lowercased -- not
        # `"research" if ... else "search"`, which reports every
        # unrecognised or renamed mode as the one mode this milestone
        # claims to drive: a guess in the flattering direction, in a module
        # that elsewhere refuses to guess. PRD §5 amended to match.
        mode=str(entry.get("search_mode") or "unknown").lower(),
        # The *thread*, not this turn: `thread_url_slug` is the first entry's
        # backend_uuid and stays put as turns are added, so it is the handle that
        # continues the conversation (M5). They are equal on a one-turn thread, which
        # is why M3 could not tell them apart.
        thread_id=entry.get("thread_url_slug") or entry.get("backend_uuid") or "",
        complete=complete,
    )


# --- deep research -------------------------------------------------------------------
# All of this is readable from `GET /rest/thread/<uuid>` as well as from the stream,
# which is what lets a task be followed from a process that did not submit it (US-5).
# The trap PRD §5 names is real: the entry's own `status` stays PENDING while research
# waits for an answer, so only the workflow block can tell waiting from working.

WORKFLOW = "workflow_root"
AWAITING = "WORKFLOW_AWAITING_USER"
ASKED = "WORKFLOW_ITEM_USER_QUESTIONS"
ANSWERED = "WORKFLOW_ITEM_USER_RESPONSE"
# The button that advances the question wizard; it carries its shortcut in its label,
# so the name is matched as a prefix (see `_named`).
CONTINUE = "Continue"


@dataclasses.dataclass(frozen=True)
class Question:
    text: str
    options: list[str]
    multi: bool
    free_text: bool


def _block(entry: Json, usage: str, field: str) -> Json:
    for b in entry.get("blocks") or ():
        if isinstance(b, dict) and b.get("intended_usage") == usage:
            value = b.get(field)
            return value if isinstance(value, dict) else {}
    return {}


def _items(entry: Json) -> list[Json]:
    return [
        i
        for step in _block(entry, WORKFLOW, "workflow_block").get("steps") or ()
        if isinstance(step, dict)
        for i in step.get("items") or ()
        if isinstance(i, dict)
    ]


def plan_of(entry: Json) -> list[tuple[str, str]] | None:
    """Per-goal progress, or None when this entry has no plan (i.e. not research).

    A goal is done when the stream marks it `final`; `todo_task_status` stays
    INCOMPLETE even on a finished thread and means something else.
    """
    plan = _block(entry, "plan", "plan_block")
    goals = plan.get("goals")
    if not isinstance(goals, list):
        return None
    return [
        (
            str(g.get("description") or ""),
            "DONE" if g.get("final") else "IN_PROGRESS",
        )
        for g in goals
        if isinstance(g, dict)
    ]


def questions_of(entry: Json) -> list[Question]:
    """The clarifying questions still outstanding, if any.

    A finished thread keeps the questions it asked, so "was there a question" is not
    the test -- an answered or expired one is echoed back as a `..._USER_RESPONSE`
    item carrying the *same id*, and that is what retires it.
    """
    done = {i.get("id") for i in _items(entry) if i.get("type") == ANSWERED}
    out: list[Question] = []
    seen: set[object] = set()
    for item in _items(entry):
        if item.get("type") != ASKED or item.get("id") in done:
            continue
        if item.get("id") in seen:
            # Watching a task means reconnecting to its stream repeatedly, and every
            # reconnect replays the questions it has asked so far. Same id, same
            # question -- asking a caller to answer it twice would be this tool's
            # fault, not Perplexity's.
            continue
        seen.add(item.get("id"))
        payload = (item.get("payload") or {}).get("user_questions_payload") or {}
        for field in payload.get("fields") or ():
            if not isinstance(field, dict):
                continue
            out.append(
                Question(
                    text=str(field.get("field_name") or ""),
                    options=[
                        str(o.get("title"))
                        for o in field.get("options") or ()
                        if isinstance(o, dict)
                        and o.get("title")
                        and not o.get("is_free_text_selection")
                    ],
                    multi=bool(field.get("allow_multichoice")),
                    free_text=bool(field.get("allow_free_text")),
                )
            )
    return out


def task_status(entry: Json) -> str:
    """`pending | running | awaiting_input | done | failed` (PRD §5).

    The thread endpoint lower-cases what the stream shouts, so `status` is compared
    case-insensitively -- `completed` and `COMPLETED` are the same state.
    """
    status = str(entry.get("status") or "").upper()
    if status == "COMPLETED":
        return "done"
    if status in ("FAILED", "ERROR"):
        return "failed"
    workflow = _block(entry, WORKFLOW, "workflow_block")
    if workflow.get("status") == AWAITING or questions_of(entry):
        return "awaiting_input"
    if not entry:
        return "pending"
    return "running" if workflow or entry.get("blocks") else "pending"


def _goal_patch(goals: list[Json], patch: Json) -> list[Json]:
    """One `plan_block` patch. Two shapes exist: a whole new goal, and one goal
    turning `final`. Anything else is ignored rather than guessed at."""
    path, op = str(patch.get("path", "")), patch.get("op")
    parts = path.strip("/").split("/")
    if parts[:1] != ["goals"] or len(parts) < 2 or not parts[1].isdigit():
        return goals
    i = int(parts[1])
    if op == "add" and len(parts) == 2 and isinstance(patch.get("value"), dict):
        # Bounded like `_apply`: an index off the end of a list fed straight from the
        # network is an error in RFC 6902 anyway, and every one observed is the next.
        if 0 <= i <= len(goals):
            goals = goals[:i] + [patch["value"]] + goals[i:]
    elif op == "replace" and len(parts) == 3 and 0 <= i < len(goals):
        goals = [
            {**g, parts[2]: patch.get("value")} if n == i else g
            for n, g in enumerate(goals)
        ]
    return goals


def entry_from_frames(frames: list[Json]) -> Json:
    """An entry-shaped view of a research stream, for a task that is still running.

    `GET /rest/thread/<uuid>` carries no blocks at all while an entry is `pending`
    (observed 2026-08-01, `spike/probe_poll.py`): the plan and the workflow only
    appear once it completes. So progress and clarifying questions can *only* be read
    off the stream, and this assembles the same shape the thread endpoint would
    eventually serve, so that one set of readers works on both.
    """
    status, workflow_status = "", ""
    goals: list[Json] = []
    items: list[Json] = []
    for f in frames:
        status = str(f.get("status") or status)
        for block in f.get("blocks") or ():
            if not isinstance(block, dict):
                continue
            usage = block.get("intended_usage")
            patches = (block.get("diff_block") or {}).get("patches") or ()
            if usage == "plan":
                if snap := _snapshot(block, "plan_block"):
                    goals = [g for g in snap.get("goals") or () if isinstance(g, dict)]
                for patch in patches:
                    if isinstance(patch, dict):
                        goals = _goal_patch(goals, patch)
            elif usage == WORKFLOW:
                for patch in patches:
                    if not isinstance(patch, dict):
                        continue
                    if patch.get("path") == "/status":
                        workflow_status = str(patch.get("value") or "")
                    value = patch.get("value")
                    # Every workflow item arrives whole, as the value of one `add` --
                    # so the questions need no patching, only collecting.
                    if isinstance(value, dict) and value.get("type"):
                        items.append(value)
    return {
        "status": status,
        "blocks": [
            {"intended_usage": "plan", "plan_block": {"goals": goals}},
            {
                "intended_usage": WORKFLOW,
                "workflow_block": {
                    "status": workflow_status,
                    "steps": [{"items": items}],
                },
            },
        ],
    }


def answer_clarifiers(page: Page, answers: list[str]) -> None:
    """Drive the question wizard: choose an option, press Continue, repeat.

    One question is on screen at a time and its options are `role=radio` (verified
    live 2026-08-01). The selection is confirmed before advancing, because Continue
    with nothing chosen is indistinguishable from Skip -- the first attempt at this
    clicked the option's *text*, advanced happily, and submitted nothing.

    There is a 60-second window before the server answers for us, so this is the one
    place in the tool that hurries.
    """
    for i, answer in enumerate(answers):
        option = page.get_by_role("radio", name=answer)
        if not option.count():
            raise PplxError(
                f"answer {i + 1} ({answer!r}) is not one of the options offered; "
                f"free-text answers are not supported (docs/M4-M8-findings.md)"
            )
        option.first.click(timeout=BOX_TIMEOUT)
        page.wait_for_timeout(400)
        if option.first.get_attribute("aria-checked") != "true":
            raise PplxError(f"selecting the answer {answer!r} did not take")
        advance = page.get_by_role("button", name=CONTINUE)
        if not advance.count():
            raise PplxError(
                "the clarifying-question wizard has no Continue button -- "
                "perplexity.ai's frontend has most likely changed. Run: pplx doctor"
            )
        advance.first.click(timeout=BOX_TIMEOUT)
        page.wait_for_timeout(1200)


def _frame(block: bytes) -> Json | None:
    """The JSON object out of one SSE block, or None if there is not one there yet."""
    for line in LINE_SPLIT.split(block):
        if line.startswith(DATA) and line[len(DATA) :].lstrip().startswith(b"{"):
            try:
                obj = json.loads(line[len(DATA) :])
            except ValueError, UnicodeDecodeError:
                # A block cut mid-JSON is the normal tail of a killed stream, not a bug.
                return None
            return obj if isinstance(obj, dict) else None
    return None


def frames(raw: bytes) -> list[Json]:
    return [f for block in FRAME_SPLIT.split(raw) if (f := _frame(block)) is not None]


def terminal(frames: list[Json]) -> Json | None:
    """The completion signal: `final_sse_message` **and** `status == "COMPLETED"`.

    Both, per M0 Q2 -- and never `text_completed`, which goes true one frame early and
    would admit a payload that is not yet final.
    """
    return next(
        (
            f
            for f in frames
            if f.get("final_sse_message") and f.get("status") == "COMPLETED"
        ),
        None,
    )


class Stream:
    """Incremental SSE reader over CDP's chunk boundaries.

    `Network.dataReceived` hands over whatever bytes arrived, split wherever the network
    split them, so the last frame in the buffer is usually half-written -- and the
    terminal frame is ~400KB, several chunks on its own. Complete frames are taken off
    the front and the remainder is kept for the next chunk. Re-parsing the whole buffer
    on a timer is the alternative, and it gets slower the longer the answer runs.
    """

    def __init__(self) -> None:
        self.frames: list[Json] = []
        # set by `tee`, so the caller has something to detach
        self.cdp: CDPSession | None = None
        # A closed connection is a definite end, terminal frame or not. Without it a
        # dropped stream costs the caller the whole answer timeout to learn nothing the
        # close had not already said.
        self.ended = False
        self._buf = b""

    def feed(self, chunk: bytes) -> None:
        self._buf += chunk
        *complete, self._buf = FRAME_SPLIT.split(self._buf)
        self.frames += [f for block in complete if (f := _frame(block)) is not None]

    def close(self) -> None:
        """No more bytes are coming. Flush the block `feed` was holding back.

        That block is held back because it is *usually* half-written -- but if the
        connection ended right after the terminal frame and before its separator, it is
        the whole answer, and refusing it would burn the query that bought a complete
        one. `_frame` returns None for genuinely half-written JSON, so this cannot
        admit a partial frame.
        """
        self.feed(FRAME_SEP)
        self.ended = True

    @property
    def done(self) -> bool:
        return terminal(self.frames) is not None


# Top-level fields that are present from the first frame (M0 Q5) and are all a partial
# answer can report about itself.
_CARRIED = ("backend_uuid", "display_model", "search_mode")


def _snapshot(block: Json, field: str) -> Json | None:
    """The whole value of one block field, however it arrived.

    Mid-stream a block may hold the field outright or a diff whose first operation
    replaces the root, and both mean the same thing: here is the current value.
    """
    if isinstance(full := block.get(field), dict) and full:
        return full
    diff = block.get("diff_block") or {}
    if diff.get("field") == field:
        for patch in diff.get("patches") or ():
            if (
                isinstance(patch, dict)
                and patch.get("op") == "replace"
                and patch.get("path") == ""
            ):
                value = patch.get("value")
                return value if isinstance(value, dict) else {}
    return None


def _apply(chunks: list[str], patch: Json) -> list[str]:
    """One `markdown_block` patch.

    Two operations exist in the wild and only two are handled -- `replace` at the root
    (a whole snapshot) and `add` at /chunks/<n> (one token). Anything else is ignored
    rather than guessed at: a guess invents text that was never sent, which is worse
    than a short answer the caller already knows is incomplete.
    """
    path, op = patch.get("path", ""), patch.get("op")
    if op == "replace" and path == "":
        return [str(c) for c in (patch.get("value") or {}).get("chunks") or ()]
    if op == "add" and path.startswith("/chunks/"):
        try:
            i = int(path.rsplit("/", 1)[1])
        except ValueError:
            return chunks
        # Append or overwrite, never outside the array. Both bounds guard real harm on
        # a path fed straight from the network: `int("-1")` makes the padding a no-op
        # and `chunks[-1] = ...` rewrites a token that really arrived -- inventing text
        # the server never sent -- while a large index pads without limit (a 2e7 index
        # measured at 320MB). Past the end is an error in RFC 6902 anyway, and every
        # index in both captures is simply the next one.
        if not 0 <= i <= len(chunks):
            return chunks
        chunks += [""] * (i + 1 - len(chunks))
        chunks[i] = str(patch.get("value", ""))
    return chunks


def _partial(frames: list[Json]) -> Response:
    """What arrived, replayed.

    The assembled answer only ever appears on the terminal frame; before it, `ask_text`
    streams as JSON-patch fragments. Without replaying them an `allow_incomplete=True`
    caller gets an empty string instead of the partial answer US-3 promises.
    """
    chunks: list[str] = []
    web: Json | None = None
    latest: Json = {}
    for f in frames:
        latest.update({k: v for k, v in f.items() if k in _CARRIED})
        for block in f.get("blocks") or ():
            if not isinstance(block, dict):
                continue
            usage = block.get("intended_usage")
            if usage == "web_results":
                # Citations stream as a diff as well, but only ever as one whole-value
                # replace -- they are settled in one go rather than token by token.
                web = _snapshot(block, "web_result_block") or web
            elif usage == "ask_text":
                if snap := _snapshot(block, "markdown_block"):
                    chunks = [str(c) for c in snap.get("chunks") or ()]
                diff = block.get("diff_block") or {}
                if diff.get("field") == "markdown_block":
                    for patch in diff.get("patches") or ():
                        if isinstance(patch, dict):
                            chunks = _apply(chunks, patch)
    # An entry assembled by hand, because a cut stream never sent one. Citations are
    # whatever had been delivered, and the index contract is deliberately not enforced
    # over them -- see answer_from.
    entry = {
        **latest,
        "blocks": [{"intended_usage": "web_results", "web_result_block": web or {}}],
    }
    return dataclasses.replace(answer_from(entry, complete=False), text="".join(chunks))


def parse_stream(frames: list[Json], allow_incomplete: bool) -> Response:
    if fin := terminal(frames):
        return answer_from(fin, complete=True)
    if not allow_incomplete:
        raise IncompleteAnswerError(
            f"the answer stream ended after {len(frames)} frames without a completion "
            f"signal, so this answer is cut off. Pass allow_incomplete=True to take "
            f"what arrived"
        )
    return _partial(frames)


# --- driving the page ---------------------------------------------------------------
# DOM is control only, never content (PRD §2): navigate, type, submit. Answer content
# comes off the stream, which is the only place the completeness and citation-index
# contracts are enforceable.


def tee(ctx: BrowserContext, page: Page, reconnect: bool = False) -> Stream:
    """Start copying the answer stream into a `Stream`. Call before submitting.

    `Network.getResponseBody` returns nothing for a streaming body and neither does
    Playwright's `response.body()`; `streamResourceContent` is the only method that
    works here (M0 Q1), and it has to be asked for on the response event, while the
    body is still going past.

    Binds to the first matching stream and only that one: for a query there is exactly
    one answer stream, and splicing a later one onto it would assemble frames out of
    two different answers' bytes.

    `reconnect` picks the *other* stream -- `/rest/sse/perplexity_ask/reconnect/<uuid>`,
    which serves a running entry to anyone who opens its thread. The two are told apart
    exactly rather than by substring, because `ASK_PATH` is a prefix of the reconnect
    URL: continuing a thread whose last turn is still generating would otherwise bind
    the previous turn's stream and return *its* answer, complete and plausible and
    about the wrong question.
    """
    stream = Stream()
    cdp = ctx.new_cdp_session(page)
    cdp.send("Network.enable")
    rid: str | None = None

    def wanted(url: str) -> bool:
        if reconnect:
            return RECONNECT_PATH in url
        return url.rstrip("/").endswith(ASK_PATH)

    def on_response(params: Json) -> None:
        nonlocal rid
        r = params.get("response") or {}
        # Keyed on the one request that carries an answer: the homepage fires dozens of
        # others, and their bytes in this buffer would corrupt every frame after them.
        if (
            rid is None
            and wanted(r.get("url") or "")
            and r.get("mimeType") == "text/event-stream"
            and 200 <= (r.get("status") or 0) < 300
        ):
            # Status checked too: an error response on this path would bind `rid`, and
            # the frontend's retry -- the one carrying the actual answer -- would then
            # be dropped as someone else's. The user gets told the frontend changed
            # when in fact the server pushed back.
            try:
                got = cdp.send(
                    "Network.streamResourceContent",
                    {"requestId": params.get("requestId")},
                )
            except Exception:
                # "Request not found" if it finished during the round-trip. Binding
                # anyway would leave the stream silently dead, since every later
                # dataReceived arrives without a `data` field once streaming is off;
                # and letting this escape the handler surfaces as a raw traceback from
                # inside the poll loop, bypassing the diagnosis in `_blame`.
                return
            rid = params.get("requestId")
            # Whatever arrived before we asked. Dropping it loses the head of the
            # stream, and with it every frame boundary after it.
            stream.feed(base64.b64decode(got.get("bufferedData") or ""))

    def on_data(params: Json) -> None:
        if params.get("requestId") == rid and params.get("data"):
            stream.feed(base64.b64decode(params["data"]))

    def on_end(params: Json) -> None:
        # CDP delivers every `dataReceived` before the request finishes, so nothing is
        # lost by stopping here -- and both endings, clean and failed, mean the same
        # thing to us: no more bytes are coming, so the held-back tail can be flushed.
        if params.get("requestId") == rid:
            stream.close()

    cdp.on("Network.responseReceived", on_response)
    cdp.on("Network.dataReceived", on_data)
    cdp.on("Network.loadingFinished", on_end)
    cdp.on("Network.loadingFailed", on_end)
    stream.cdp = cdp
    return stream


# The composer's menus. Both are Radix dropdowns whose trigger button is named for
# whatever is currently selected -- "Model" only until a model has been picked, "Search"
# only until the mode changes -- so each is looked up under every name it can wear.
MENU_ITEM: Literal["menuitemradio"] = "menuitemradio"
# The same list, without the radio: a model this plan may not pick, offered as an
# upgrade. Playwright types its roles as literals, hence the annotations.
LOCKED_ITEM: Literal["menuitem"] = "menuitem"


def _named(name: str, wanted: str | None) -> bool:
    """Does this accessible name start with `wanted` as a whole word?

    Menu entries carry their badges in the name -- "Kimi K3 New Thinking", "Best
    Selects the best available model" -- so an equality test matches nothing and a
    substring test matches "Grok 4" against "Grok 4.5".
    """
    if wanted is None:
        return True
    got = " ".join((name or "").split()).lower()
    want = " ".join(wanted.split()).lower()
    return got == want or got.startswith(want + " ")


def _open_menu(page: Page, names: tuple[str, ...]) -> None:
    # Escape first: a menu left open by a previous step would be *closed* by the
    # click meant to open this one.
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)
    for name in names:
        button = page.get_by_role("button", name=name, exact=True)
        if button.count():
            button.first.click(timeout=BOX_TIMEOUT)
            page.wait_for_timeout(800)
            return
    raise PplxError(
        f"no composer menu button called any of {names} -- perplexity.ai's frontend "
        f"has most likely changed. Run: pplx doctor"
    )


def _selected(page: Page, label: str) -> bool:
    """Is the entry called `label` the one the open menu shows as chosen?"""
    item = page.get_by_role(MENU_ITEM, name=label)
    if not item.count():
        return False
    return item.first.get_attribute("aria-checked") == "true"


def _pick(page: Page, label: str, names: tuple[str, ...], what: str) -> None:
    _open_menu(page, names)
    if _selected(page, label):
        page.keyboard.press("Escape")  # already there; nothing to spend a click on
        return
    item = page.get_by_role(MENU_ITEM, name=label)
    if not item.count():
        if page.get_by_role(LOCKED_ITEM, name=label).count():
            raise ModelUnavailableError(
                f"{label!r} is not included in this account's plan -- the picker "
                f"offers it as an upgrade, not as a choice. Pick another model, or "
                f"pass model='best'"
            )
        raise PplxError(
            f"the {what} menu has no entry called {label!r} -- perplexity.ai's "
            f"frontend has most likely changed. Run: pplx doctor"
        )
    # Pressed, not clicked: neighbouring entries own submenus whose poppers cover this
    # one, and a pointer click is then intercepted until it times out (2026-08-01).
    item.first.press("Enter")
    page.wait_for_timeout(1200)
    _open_menu(page, (*names, label))
    took = _selected(page, label)
    page.keyboard.press("Escape")
    if not took:
        # Refused here rather than after the answer: the query is the expensive part,
        # and an answer from the wrong model is exactly what US-6 exists to prevent.
        raise PplxError(
            f"selecting the {what} {label!r} did not take -- refusing to spend a "
            f"query on the wrong {what}. Run: pplx doctor"
        )


def pick_mode(page: Page, mode: str) -> None:
    """Set search vs Deep Research. Do this *before* the model: changing mode resets
    the model to that mode's default (observed 2026-08-01)."""
    label = MODE_LABELS.get(mode)
    if label is None:
        raise PplxError(f"unknown mode {mode!r}; expected one of {sorted(MODE_LABELS)}")
    _pick(page, label, tuple(MODE_LABELS.values()), "mode")


def pick_model(page: Page, label: str, offers: dict[str, str] | None = None) -> None:
    """Choose a model by its menu label. `offers` only widens the button-name search."""
    names = ("Model", BEST, label, *(offers or {}))
    _pick(page, label, names, "model")


def submit(page: Page, query: str, follow_up: bool = False) -> None:
    """Type the query and send it.

    Deep-linking `/search/new?q=…` draws the Cloudflare interstitial hardest (M0), so
    this does what a person does: the homepage, the box, Enter.

    `follow_up` takes the *last* textbox instead of the first: a thread page has the
    answer above and the composer below, and typing into the first one edits the
    original query rather than continuing the conversation. Each of the two is the
    box that was verified live for its own page (M3 homepage, M5 thread).
    """
    boxes = page.get_by_role("textbox")
    box = boxes.last if follow_up else boxes.first
    box.wait_for(timeout=BOX_TIMEOUT)
    box.click()
    box.fill(query)
    box.press("Enter")


def thread_url(thread_id: str) -> str:
    """Where a thread lives. Continuing one means typing into its own composer: that
    is what makes the frontend send `query_source: "followup"` and `last_backend_uuid`
    (observed 2026-08-01), neither of which this tool sets itself."""
    return f"{HOME}search/{thread_id}"


def entry_of(body: Json, entry_id: str = "") -> Json:
    """The turn a caller means out of a thread document.

    `entries` is oldest-first, so the *last* one is the answer to the most recent
    question -- taking the first, as M3 did, silently returns the opening answer of
    every multi-turn thread (M5). An explicit id wins, which is how a research task
    resumes by its own `backend_uuid` rather than by whatever else is in the thread.
    """
    entries = [e for e in body.get("entries") or () if isinstance(e, dict)]
    if entry_id:
        return next((e for e in entries if e.get("backend_uuid") == entry_id), {})
    return entries[-1] if entries else {}


def parse_thread(body: Json, allow_incomplete: bool, entry_id: str = "") -> Response:
    """`GET /rest/thread/<uuid>` -- the resume path (M0 Q5).

    Plain JSON rather than SSE, and already decoded, but the entry carries the same
    `blocks` the terminal frame does, so it is the same parser. There is no
    `final_sse_message` out here; `status` is the completion signal -- and it is
    lower-case here where the stream shouts it (`completed` vs `COMPLETED`, observed
    2026-08-01), so it is compared case-insensitively.
    """
    entry = entry_of(body, entry_id)
    complete = str(entry.get("status") or "").upper() == "COMPLETED"
    if not complete and not allow_incomplete:
        raise IncompleteAnswerError(
            f"the thread is {entry.get('status') or 'unreadable'}, not COMPLETED"
        )
    return answer_from(entry, complete=complete)
