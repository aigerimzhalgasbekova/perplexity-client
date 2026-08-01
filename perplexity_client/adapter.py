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

from .errors import CitationError, IncompleteAnswerError

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
# The one request that carries an answer. The homepage fires ~40 other REST calls
# before it (M0), so the adapter keys on this path and ignores every other stream.
ASK_PATH = "/rest/sse/perplexity_ask"
# CRLF per the SSE spec. Reading a capture in text mode rewrites this to "\n\n" and the
# split then silently matches nothing (M0 Q1) -- hence bytes end to end.
FRAME_SEP = b"\r\n\r\n"
DATA = b"data: "
BOX_TIMEOUT = 30_000  # ms; the homepage has ~40 REST calls to get through first
# Fenced and inline code, removed before the citation-marker scan. See answer_from.
CODE = re.compile(r"```.*?```|`[^`\n]*`", re.S)


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


def _citations(results) -> list[Citation]:
    # `snippet` arrives as "" rather than absent for some sources (M0 Q3). PRD §5 types
    # it `str | None`, so empty becomes None: a caller checking `is None` should not
    # have to also remember to check for the empty string.
    return [Citation(url=w.get("url") or "", title=w.get("name") or "",
                     snippet=w.get("snippet") or None)
            for w in results or () if isinstance(w, dict)]


def answer_from(entry: dict, complete: bool) -> Response:
    """A `Response` from one terminal SSE frame, or one resume entry -- same shape.

    Text and citations come out of the *same* dict, which is what PRD §5's same-payload
    invariant asks for: Perplexity renumbers and appends sources while an answer
    streams, so sampling the two a moment apart is how markers come to misattribute.
    """
    blocks = {b.get("intended_usage"): b
              for b in entry.get("blocks") or () if isinstance(b, dict)}
    text = ((blocks.get("ask_text") or {}).get("markdown_block") or {}).get("answer") or ""
    cites = _citations(((blocks.get("web_results") or {}).get("web_result_block")
                        or {}).get("web_results"))
    if complete:
        if not text:
            # Every lookup above is `or {}`-guarded, so a renamed block collapses to ""
            # and the marker check below then passes vacuously -- handing back
            # `complete=True, text=""`, which an agent reads as "Perplexity found
            # nothing". Plausible, actionable and wrong: PRD §10's critical row. A
            # finished answer with no text is not an outcome this protocol produces.
            raise IncompleteAnswerError(
                "the completion signal arrived but the answer block was empty -- "
                "perplexity.ai's frontend has most likely changed. Run: pplx doctor")
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
        markers = {int(n) for n in re.findall(r"\[(\d+)\]", CODE.sub("", text))}
        if unmapped := sorted(markers - set(range(1, len(cites) + 1))):
            raise CitationError(
                f"the answer cites {unmapped} but only {len(cites)} sources came back, "
                f"so those markers point at nothing. Refusing to return an answer whose "
                f"citations cannot be trusted; run: pplx doctor")
    return Response(text=text, citations=cites,
                    model=entry.get("display_model") or "",
                    # The server's own word, lowercased -- not
                    # `"research" if ... else "search"`, which reports every
                    # unrecognised or renamed mode as the one mode this milestone
                    # claims to drive: a guess in the flattering direction, in a module
                    # that elsewhere refuses to guess. PRD §5 amended to match.
                    mode=str(entry.get("search_mode") or "unknown").lower(),
                    thread_id=entry.get("backend_uuid") or "", complete=complete)


def _frame(block: bytes) -> dict | None:
    """The JSON object out of one SSE block, or None if there is not one there yet."""
    for line in block.split(b"\r\n"):
        if line.startswith(DATA) and line[len(DATA):].lstrip().startswith(b"{"):
            try:
                return json.loads(line[len(DATA):])
            except (ValueError, UnicodeDecodeError):
                # A block cut mid-JSON is the normal tail of a killed stream, not a bug.
                return None
    return None


def frames(raw: bytes) -> list[dict]:
    return [f for block in raw.split(FRAME_SEP) if (f := _frame(block)) is not None]


def terminal(frames: list[dict]) -> dict | None:
    """The completion signal: `final_sse_message` **and** `status == "COMPLETED"`.

    Both, per M0 Q2 -- and never `text_completed`, which goes true one frame early and
    would admit a payload that is not yet final.
    """
    return next((f for f in frames
                 if f.get("final_sse_message") and f.get("status") == "COMPLETED"), None)


class Stream:
    """Incremental SSE reader over CDP's chunk boundaries.

    `Network.dataReceived` hands over whatever bytes arrived, split wherever the network
    split them, so the last frame in the buffer is usually half-written -- and the
    terminal frame is ~400KB, several chunks on its own. Complete frames are taken off
    the front and the remainder is kept for the next chunk. Re-parsing the whole buffer
    on a timer is the alternative, and it gets slower the longer the answer runs.
    """

    def __init__(self) -> None:
        self.frames: list[dict] = []
        self.cdp = None  # set by `tee`, so the caller has something to detach
        # A closed connection is a definite end, terminal frame or not. Without it a
        # dropped stream costs the caller the whole answer timeout to learn nothing the
        # close had not already said.
        self.ended = False
        self._buf = b""

    def feed(self, chunk: bytes) -> None:
        self._buf += chunk
        *complete, self._buf = self._buf.split(FRAME_SEP)
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


def _snapshot(block: dict, field: str) -> dict | None:
    """The whole value of one block field, however it arrived.

    Mid-stream a block may hold the field outright or a diff whose first operation
    replaces the root, and both mean the same thing: here is the current value.
    """
    if full := block.get(field):
        return full
    diff = block.get("diff_block") or {}
    if diff.get("field") == field:
        for patch in diff.get("patches") or ():
            if isinstance(patch, dict) and patch.get("op") == "replace" \
                    and patch.get("path") == "":
                return patch.get("value") or {}
    return None


def _apply(chunks: list[str], patch: dict) -> list[str]:
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


def _partial(frames: list[dict]) -> Response:
    """What arrived, replayed.

    The assembled answer only ever appears on the terminal frame; before it, `ask_text`
    streams as JSON-patch fragments. Without replaying them an `allow_incomplete=True`
    caller gets an empty string instead of the partial answer US-3 promises.
    """
    chunks: list[str] = []
    web, latest = None, {}
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
    entry = {**latest, "blocks": [{"intended_usage": "web_results",
                                   "web_result_block": web or {}}]}
    return dataclasses.replace(answer_from(entry, complete=False), text="".join(chunks))


def parse_stream(frames: list[dict], allow_incomplete: bool) -> Response:
    if fin := terminal(frames):
        return answer_from(fin, complete=True)
    if not allow_incomplete:
        raise IncompleteAnswerError(
            f"the answer stream ended after {len(frames)} frames without a completion "
            f"signal, so this answer is cut off. Pass allow_incomplete=True to take "
            f"what arrived")
    return _partial(frames)


# --- driving the page ---------------------------------------------------------------
# DOM is control only, never content (PRD §2): navigate, type, submit. Answer content
# comes off the stream, which is the only place the completeness and citation-index
# contracts are enforceable.


def tee(ctx, page) -> Stream:
    """Start copying the answer stream into a `Stream`. Call before submitting.

    `Network.getResponseBody` returns nothing for a streaming body and neither does
    Playwright's `response.body()`; `streamResourceContent` is the only method that
    works here (M0 Q1), and it has to be asked for on the response event, while the
    body is still going past.
    """
    stream = Stream()
    cdp = ctx.new_cdp_session(page)
    cdp.send("Network.enable")
    rid = None

    def on_response(params):
        nonlocal rid
        r = params.get("response") or {}
        # Keyed on the one request that carries an answer: the homepage fires dozens of
        # others, and their bytes in this buffer would corrupt every frame after them.
        if rid is None and ASK_PATH in (r.get("url") or "") \
                and r.get("mimeType") == "text/event-stream" \
                and 200 <= (r.get("status") or 0) < 300:
            # Status checked too: an error response on this path would bind `rid`, and
            # the frontend's retry -- the one carrying the actual answer -- would then
            # be dropped as someone else's. The user gets told the frontend changed
            # when in fact the server pushed back.
            try:
                got = cdp.send("Network.streamResourceContent",
                               {"requestId": params.get("requestId")})
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

    def on_data(params):
        if params.get("requestId") == rid and params.get("data"):
            stream.feed(base64.b64decode(params["data"]))

    def on_end(params):
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


def submit(page, query: str) -> None:
    """Type the query and send it.

    Deep-linking `/search/new?q=…` draws the Cloudflare interstitial hardest (M0), so
    this does what a person does: the homepage, the box, Enter.
    """
    box = page.get_by_role("textbox").first
    box.wait_for(timeout=BOX_TIMEOUT)
    box.click()
    box.fill(query)
    box.press("Enter")


def parse_thread(body: dict, allow_incomplete: bool) -> Response:
    """`GET /rest/thread/<uuid>` -- the resume path (M0 Q5).

    Plain JSON rather than SSE, and already decoded, but the entry carries the same
    `blocks` the terminal frame does, so it is the same parser. There is no
    `final_sse_message` out here; `status` is the completion signal.

    Nothing calls this until `pplx result` and `Client().task()` (milestones 6-7). It
    ships now because PRD §9 puts it in this milestone, and because it turned out to
    cost four lines rather than the second parser that was budgeted for it.
    """
    entry = next(iter(body.get("entries") or ()), None) or {}
    complete = entry.get("status") == "COMPLETED"
    if not complete and not allow_incomplete:
        raise IncompleteAnswerError(
            f"the thread is {entry.get('status') or 'unreadable'}, not COMPLETED")
    return answer_from(entry, complete=complete)
