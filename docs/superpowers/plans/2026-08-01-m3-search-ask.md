# Milestone 3 — Search-mode `ask()` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `Client().ask("query")` returns a `Response` with text, index-mapped citations, the observed model, the thread id and a real completion signal — or raises rather than handing back a truncated answer.

**Architecture:** All Perplexity protocol knowledge moves into one new `adapter.py` (PRD §4 "adapter isolation"; `client.py`'s own docstring says milestone 3 is when this happens). The adapter is pure: bytes and dicts in, `Response` out, no browser, no I/O — which is what makes it fixture-testable. `client.py` keeps orchestration only: take the lock, launch Chrome, gate on session/challenge/quota, drive the DOM, tee the SSE stream over CDP, hand the frames to the adapter.

**Tech Stack:** Python 3.10+, Playwright `connect_over_cdp` (attach only), CDP `Network.streamResourceContent`, pytest over dated fixtures.

## Global Constraints

- Python 3.10+. No new runtime dependency — Playwright and the stdlib only (PRD §7 maintainability).
- Playwright must never *launch* the browser; `chrome.chrome()` already handles launch + attach (M0 blocker).
- Every query-spending run goes through `chrome(headless=True, interval=pacing.default_interval())`; a page-load-only run passes `interval=0` (PRD §4).
- `Response.model` is strictly the **observed** model, never an echo of a request (US-6).
- `Response.complete` is `True` only when `final_sse_message: true` **and** `status == "COMPLETED"` were observed on the same frame (US-3, M0 Q2).
- No CAPTCHA or bot-detection bypass anywhere (PRD §8). A challenge is an error, not a retry.
- Fixtures carry a capture date in their filename so staleness is visible in review (PRD §7).
- Comments explain *why*, not *what*, matching the existing modules' register.
- No commits to `main`; work happens on `feat/m3-search-ask`.

---

## File Structure

| File | Responsibility |
|---|---|
| `perplexity_client/adapter.py` | **New.** Every Perplexity-specific fact: endpoints, probes, challenge titles, the SSE reader, the answer parser, `Response`/`Citation`. Pure. |
| `perplexity_client/client.py` | Orchestration only: `login`, `status`, `ask`. Imports its site knowledge from the adapter. |
| `perplexity_client/errors.py` | Add `IncompleteAnswerError`, `CitationError`, `SessionExpiredError`, `ChallengeEncounteredError`, `QuotaExhaustedError`. |
| `perplexity_client/__init__.py` | Export `Response`, `Citation` and the new errors. |
| `tests/test_adapter.py` | **New.** The parser, against the dated M0 fixtures. |
| `tests/test_ask.py` | **New.** `Client.ask()` orchestration against fakes — gating, ordering, error mapping. |
| `tests/test_session.py`, `tests/test_quota.py` | Follow the moved symbols to `adapter`. |
| `spike/fixtures/*` | Renamed with their capture date; `spike/verify_findings.py` follows. |
| `docs/M3-findings.md` | Already written — the design and its evidence. |
| `docs/PRD.md`, `README.md` | Amended per M3-findings §"PRD amendments". |

---

### Task 1: Move site knowledge into `adapter.py`

Pure move, no behaviour change. It lands first so every later task adds to one place.

**Files:**
- Create: `perplexity_client/adapter.py`
- Modify: `perplexity_client/client.py`, `tests/test_session.py`, `tests/test_quota.py`

**Interfaces:**
- Produces: `adapter.HOME`, `adapter.RATE_LIMIT`, `adapter.MODES`, `adapter.AUTH_PROBE`, `adapter.QUOTA_PROBE`, `adapter.CHALLENGE_TITLES`, `adapter.is_challenge(title, url) -> bool`, `adapter.classify(title, url, authed) -> str`, `adapter.quota(page) -> dict[str, bool]`, `adapter.exhausted(page) -> list[str]`
- Consumes: nothing

- [ ] **Step 1: Create `adapter.py` with the moved symbols**

Move verbatim from `client.py`: `HOME`, `RATE_LIMIT`, `MODES`, `AUTH_PROBE`, `QUOTA_PROBE`, `CHALLENGE_TITLES`, `is_challenge`, `classify`, `quota`, `exhausted`. Keep every comment. Module docstring:

```python
"""Everything this tool knows about Perplexity, in one module.

Nothing else in the package names an endpoint, a JSON key or a DOM role. A
frontend change is then a patch to this file (PRD §4, adapter isolation) rather
than a hunt through the package.

Pure by design -- bytes and dicts in, `Response` out, no browser and no I/O
except the two `page.evaluate` probes. That is what lets the parser be tested
against recorded fixtures instead of the live site (PRD §7).
"""
```

- [ ] **Step 2: Re-point `client.py` at the adapter**

```python
from . import adapter
from .adapter import HOME
```
and replace bare uses (`AUTH_PROBE` → `adapter.AUTH_PROBE`, `is_challenge` → `adapter.is_challenge`, `classify` → `adapter.classify`, `exhausted` → `adapter.exhausted`). Delete the moved definitions and the "constants live here until milestone 3" note from the docstring.

- [ ] **Step 3: Follow the move in the tests**

`tests/test_session.py`: `from perplexity_client.adapter import classify, is_challenge`; `FakePage.evaluate` compares against `adapter.RATE_LIMIT`; `FakePage(url=adapter.HOME)`.
`tests/test_quota.py`: `from perplexity_client.adapter import exhausted, quota`, and `adapter.RATE_LIMIT` in `Destroyed.evaluate`.

- [ ] **Step 4: Run the suite — a pure move must not change a single result**

Run: `.venv/bin/python -m pytest tests -q`
Expected: PASS, same count as before the move.

- [ ] **Step 5: Commit**

```bash
git add perplexity_client tests
git commit -m "M3: move site knowledge into one adapter module"
```

---

### Task 2: Date the fixtures

PRD §7 wants a capture date visible in review. The M0 fixtures have one only in prose.

**Files:**
- Rename: `spike/fixtures/search-complete.sse` → `search-complete-2026-07-31.sse`, `search-truncated.sse` → `search-truncated-2026-07-31.sse`, `research-thread-resume.json` → `research-thread-resume-2026-07-31.json`
- Modify: `spike/verify_findings.py`, `spike/make_fixtures.py`, `docs/M0-findings.md`

- [ ] **Step 1: Rename with git mv**

```bash
git mv spike/fixtures/search-complete.sse spike/fixtures/search-complete-2026-07-31.sse
git mv spike/fixtures/search-truncated.sse spike/fixtures/search-truncated-2026-07-31.sse
git mv spike/fixtures/research-thread-resume.json spike/fixtures/research-thread-resume-2026-07-31.json
```

- [ ] **Step 2: Follow the names**

In `spike/verify_findings.py` and `spike/make_fixtures.py`, add a `DATE = "2026-07-31"` constant and interpolate it into the three filenames. Update the fixture list in `docs/M0-findings.md` line 4-5.

- [ ] **Step 3: Verify M0's own evidence still runs**

Run: `.venv/bin/python spike/verify_findings.py`
Expected: three `ok:` lines, exit 0.

- [ ] **Step 4: Commit**

```bash
git add spike docs/M0-findings.md
git commit -m "M3: date the fixtures, so staleness is visible in review"
```

---

### Task 3: `Response`, `Citation`, and the answer parser

The core of the milestone. One parser over the `blocks` shape both transports share (`docs/M3-findings.md`).

**Files:**
- Modify: `perplexity_client/adapter.py`
- Test: `tests/test_adapter.py` (create)

**Interfaces:**
- Consumes: `adapter` from Task 1
- Produces:
  - `adapter.Citation(url: str, title: str, snippet: str | None)` — frozen dataclass
  - `adapter.Response(text: str, citations: list[Citation], model: str, mode: str, thread_id: str, complete: bool)` — frozen dataclass
  - `adapter.answer_from(entry: dict, complete: bool) -> Response`

- [ ] **Step 1: Write the failing tests**

```python
"""Milestone 3: the answer parser, against the dated M0 fixtures.

Green here means the parser still handles the site *as of the capture date* in the
filenames -- nothing more. Live drift is `pplx doctor`'s job (PRD §7).
"""

import json
import pathlib
import re

import pytest

from perplexity_client import adapter
from perplexity_client.errors import CitationError, IncompleteAnswerError

FIXTURES = pathlib.Path(__file__).parent.parent / "spike" / "fixtures"
COMPLETE = (FIXTURES / "search-complete-2026-07-31.sse").read_bytes()
TRUNCATED = (FIXTURES / "search-truncated-2026-07-31.sse").read_bytes()
THREAD = json.loads((FIXTURES / "research-thread-resume-2026-07-31.json").read_text())


def test_answer_from_reads_the_terminal_frame():
    frames = adapter.frames(COMPLETE)
    r = adapter.answer_from(adapter.terminal(frames), complete=True)
    assert r.text.startswith("The capital of Australia")
    assert len(r.citations) == 15
    assert r.model == "pplx_pro"
    assert r.mode == "search"
    assert r.thread_id and r.complete is True
    assert all(isinstance(c, adapter.Citation) and c.url for c in r.citations)


def test_citation_markers_all_resolve():
    r = adapter.answer_from(adapter.terminal(adapter.frames(COMPLETE)), complete=True)
    markers = {int(n) for n in re.findall(r"\[(\d+)\]", r.text)}
    assert markers
    assert all(r.citations[n - 1].url for n in markers)


def test_empty_snippet_becomes_none():
    r = adapter.answer_from(adapter.terminal(adapter.frames(COMPLETE)), complete=True)
    assert any(c.snippet is None for c in r.citations)
    assert all(c.snippet != "" for c in r.citations)


def test_a_marker_past_the_citation_list_is_an_error():
    # PRD §5: an unmapped marker is surfaced, never silently dropped -- it is how a
    # real URL ends up attached to a claim it does not support.
    entry = {"blocks": [
        {"intended_usage": "ask_text", "markdown_block": {"answer": "claim [3]"}},
        {"intended_usage": "web_results",
         "web_result_block": {"web_results": [{"url": "u", "name": "t", "snippet": ""}]}}],
        "display_model": "pplx_pro", "search_mode": "SEARCH", "backend_uuid": "id"}
    with pytest.raises(CitationError):
        adapter.answer_from(entry, complete=True)
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_adapter.py -q`
Expected: FAIL — `module 'perplexity_client.adapter' has no attribute 'Citation'`.

- [ ] **Step 3: Implement**

Add to `errors.py` first:

```python
class CitationError(PplxError):
    """A `[n]` marker in the answer has no citation `n`.

    PRD §5 makes this an error rather than a silent drop: the failure it guards is an
    answer that cites a real URL which does not support the claim, which reads as
    correct.
    """
```

Then in `adapter.py`:

```python
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


def _blocks(entry: dict) -> dict:
    return {b.get("intended_usage"): b for b in entry.get("blocks") or () if isinstance(b, dict)}


def _citations(results) -> list[Citation]:
    # `snippet` is "" rather than absent for some sources (M0 Q3); PRD §5 types it
    # `str | None`, so empty is None -- a caller checking `is None` should not have to
    # also check for the empty string.
    return [Citation(url=w.get("url", ""), title=w.get("name", ""),
                     snippet=w.get("snippet") or None)
            for w in results or () if isinstance(w, dict)]


def answer_from(entry: dict, complete: bool) -> Response:
    """A `Response` from one terminal SSE frame or one resume entry -- same shape.

    Text and citations come out of the *same* dict, which is what PRD §5's
    same-payload invariant asks for: Perplexity renumbers and appends sources while
    an answer streams, so sampling them a moment apart is how markers misattribute.
    """
    blocks = _blocks(entry)
    text = (blocks.get("ask_text", {}).get("markdown_block") or {}).get("answer") or ""
    cites = _citations((blocks.get("web_results", {}).get("web_result_block")
                        or {}).get("web_results"))
    if complete:
        # Enforced on complete answers only: a stream cut mid-answer may carry a marker
        # whose source had not arrived yet, and raising on output the caller explicitly
        # opted into would be a false alarm (docs/M3-findings.md).
        if unmapped := sorted({int(n) for n in re.findall(r"\[(\d+)\]", text)}
                              - set(range(1, len(cites) + 1))):
            raise CitationError(
                f"answer cites {unmapped} but only {len(cites)} sources were returned; "
                f"the citation index contract is broken -- run: pplx doctor")
    return Response(text=text, citations=cites,
                    model=entry.get("display_model") or "",
                    mode="research" if entry.get("search_mode") == "RESEARCH" else "search",
                    thread_id=entry.get("backend_uuid") or "", complete=complete)
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_adapter.py -q`
Expected: PASS (the two tests that call `adapter.frames`/`adapter.terminal` still fail — they land in Task 4; write Task 4 before running if you prefer a clean bar).

- [ ] **Step 5: Commit**

```bash
git add perplexity_client/adapter.py perplexity_client/errors.py tests/test_adapter.py
git commit -m "M3: one answer parser over the blocks shape both transports share"
```

---

### Task 4: The SSE reader and the completion signal

**Files:**
- Modify: `perplexity_client/adapter.py`
- Test: `tests/test_adapter.py`

**Interfaces:**
- Consumes: `adapter.answer_from` from Task 3
- Produces:
  - `adapter.frames(raw: bytes) -> list[dict]`
  - `adapter.terminal(frames: list[dict]) -> dict | None`
  - `adapter.Stream()` — incremental reader with `.feed(chunk: bytes) -> None`, `.frames: list[dict]`, `.done -> bool`
  - `adapter.parse_stream(frames: list[dict], allow_incomplete: bool) -> Response`

- [ ] **Step 1: Write the failing tests**

```python
def test_terminal_frame_is_the_one_that_says_so():
    frames = adapter.frames(COMPLETE)
    assert len(frames) > 100
    fin = adapter.terminal(frames)
    assert fin["final_sse_message"] is True and fin["status"] == "COMPLETED"


def test_a_final_flag_without_completed_status_is_not_terminal():
    # Both, not either: a frame claiming to be final while reporting anything else is
    # the exact case US-3 exists to catch.
    assert adapter.terminal([{"final_sse_message": True, "status": "FAILED"}]) is None


def test_truncated_stream_has_no_terminal_frame():
    assert adapter.terminal(adapter.frames(TRUNCATED)) is None


def test_truncated_stream_raises_by_default():
    with pytest.raises(IncompleteAnswerError):
        adapter.parse_stream(adapter.frames(TRUNCATED), allow_incomplete=False)


def test_truncated_stream_opted_into_returns_what_arrived():
    r = adapter.parse_stream(adapter.frames(TRUNCATED), allow_incomplete=True)
    assert r.complete is False
    # Real partial text, not an empty string: the diffs are replayed (M3-findings).
    assert r.text.startswith("The capital of Australia")
    assert 50 < len(r.text) < 1679
    assert r.thread_id and r.model


def test_a_frame_cut_mid_json_is_skipped_not_fatal():
    # The wire cuts wherever it cuts. make_fixtures cuts on frame boundaries, so slice
    # the bytes to get the case a killed stream actually produces.
    cut = adapter.frames(COMPLETE[: len(COMPLETE) // 3])
    assert cut and adapter.terminal(cut) is None


def test_stream_reassembles_across_arbitrary_chunk_boundaries():
    # CDP chunks have nothing to do with SSE framing (M3-findings): a frame routinely
    # spans two dataReceived events.
    s = adapter.Stream()
    for i in range(0, len(COMPLETE), 997):
        s.feed(COMPLETE[i:i + 997])
        assert not s.done or i + 997 >= len(COMPLETE) - 997 * 2
    assert s.done
    assert [f.get("uuid") for f in s.frames] == [f.get("uuid") for f in adapter.frames(COMPLETE)]


def test_stream_is_not_done_until_the_terminal_frame_lands():
    s = adapter.Stream()
    s.feed(TRUNCATED)
    assert not s.done and len(s.frames) > 10
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_adapter.py -q`
Expected: FAIL — no attribute `frames`.

- [ ] **Step 3: Implement**

Add `IncompleteAnswerError` to `errors.py`:

```python
class IncompleteAnswerError(PplxError):
    """The answer never signalled completion.

    Raised rather than returned, because PRD §10 rates a truncated answer entering an
    agent pipeline as fact the critical failure of this tool. Opt in with
    `allow_incomplete=True` to receive what did arrive.
    """
```

In `adapter.py`:

```python
# The one request that carries an answer. The homepage fires ~40 other REST calls
# before it (M0), so the adapter keys on this path and ignores everything else.
ASK_PATH = "/rest/sse/perplexity_ask"
# CRLF per the SSE spec. Reading a capture in text mode rewrites this to "\n\n" and
# the split then silently matches nothing (M0 Q1) -- hence bytes throughout.
FRAME_SEP = b"\r\n\r\n"
DATA = b"data: "


def _frame(block: bytes) -> dict | None:
    """The JSON object out of one SSE block, or None if there is not one yet."""
    for line in block.split(b"\r\n"):
        if line.startswith(DATA) and line[len(DATA):].lstrip().startswith(b"{"):
            try:
                return json.loads(line[len(DATA):])
            # A block cut mid-JSON is the normal tail of a killed stream, not a bug.
            except (ValueError, UnicodeDecodeError):
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

    `Network.dataReceived` hands over whatever bytes arrived, split wherever the
    network split them, so the last frame in the buffer is usually half-written.
    Complete frames are taken off the front and the remainder is kept for the next
    chunk; re-parsing the whole buffer on a timer would be the alternative, and it gets
    slower the longer the answer is.
    """

    def __init__(self) -> None:
        self.frames: list[dict] = []
        self._buf = b""

    def feed(self, chunk: bytes) -> None:
        self._buf += chunk
        *complete, self._buf = self._buf.split(FRAME_SEP)
        self.frames += [f for block in complete if (f := _frame(block)) is not None]

    @property
    def done(self) -> bool:
        return terminal(self.frames) is not None


def parse_stream(frames: list[dict], allow_incomplete: bool) -> Response:
    if fin := terminal(frames):
        return answer_from(fin, complete=True)
    if not allow_incomplete:
        raise IncompleteAnswerError(
            f"the answer stream ended without a completion signal after "
            f"{len(frames)} frames; pass allow_incomplete=True to accept a partial "
            f"answer")
    return _partial(frames)
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_adapter.py -q`
Expected: FAIL on the two partial-text tests until Task 5. All others PASS.

---

### Task 5: Replay the diffs for a partial answer

**Files:**
- Modify: `perplexity_client/adapter.py`
- Test: `tests/test_adapter.py`

**Interfaces:**
- Consumes: `adapter.Response`, `adapter.answer_from`
- Produces: `adapter._partial(frames: list[dict]) -> Response`

- [ ] **Step 1: Write the failing tests**

```python
def test_chunk_index_is_honoured_not_appended():
    # An out-of-order or repeated frame must not shift the whole answer by one token.
    fs = [{"backend_uuid": "id", "display_model": "m", "search_mode": "SEARCH",
           "blocks": [{"intended_usage": "ask_text", "diff_block": {
               "field": "markdown_block",
               "patches": [{"op": "replace", "path": "",
                            "value": {"chunks": ["a", "b"]}}]}}]},
          {"blocks": [{"intended_usage": "ask_text", "diff_block": {
              "field": "markdown_block",
              "patches": [{"op": "add", "path": "/chunks/3", "value": "d"},
                          {"op": "add", "path": "/chunks/2", "value": "c"}]}}]}]
    assert adapter._partial(fs).text == "abcd"


def test_unknown_patch_operations_are_ignored_not_guessed():
    fs = [{"blocks": [{"intended_usage": "ask_text", "diff_block": {
        "field": "markdown_block",
        "patches": [{"op": "replace", "path": "", "value": {"chunks": ["a"]}},
                    {"op": "remove", "path": "/chunks/0"},
                    {"op": "copy", "from": "/x", "path": "/chunks/9"}]}}]}]
    assert adapter._partial(fs).text == "a"


def test_partial_ignores_diffs_for_other_fields():
    fs = [{"blocks": [{"intended_usage": "ask_text", "diff_block": {
        "field": "plan_block",
        "patches": [{"op": "replace", "path": "", "value": {"chunks": ["nope"]}}]}}]}]
    assert adapter._partial(fs).text == ""
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_adapter.py -q -k partial or chunk or unknown`
Expected: FAIL — no attribute `_partial`.

- [ ] **Step 3: Implement**

```python
def _apply(chunks: list[str], patch: dict) -> list[str]:
    """One markdown_block patch. Two operations exist in the wild and only two are
    handled: `replace` at the root (a snapshot) and `add` at /chunks/<n> (one token).
    Anything else is ignored rather than guessed at -- a guess here invents text that
    was never sent (docs/M3-findings.md)."""
    path, op = patch.get("path", ""), patch.get("op")
    if op == "replace" and path == "":
        return [str(c) for c in (patch.get("value") or {}).get("chunks") or ()]
    if op == "add" and path.startswith("/chunks/"):
        try:
            i = int(path.rsplit("/", 1)[1])
        except ValueError:
            return chunks
        # Index honoured, not appended: a repeated or reordered frame would otherwise
        # shift every token after it.
        chunks += [""] * (i + 1 - len(chunks))
        chunks[i] = str(patch.get("value", ""))
    return chunks


def _partial(frames: list[dict]) -> Response:
    """What arrived, replayed. The assembled answer only ever appears on the terminal
    frame; before it, `ask_text` streams as JSON-patch fragments, so without this an
    `allow_incomplete=True` caller gets an empty string rather than the partial answer
    US-3 promises them."""
    chunks: list[str] = []
    latest: dict = {}
    for f in frames:
        latest = {**latest, **{k: v for k, v in f.items() if k in _CARRIED}}
        for block in f.get("blocks") or ():
            if not isinstance(block, dict) or block.get("intended_usage") != "ask_text":
                continue
            if md := block.get("markdown_block"):
                chunks = [str(c) for c in md.get("chunks") or ()]
            diff = block.get("diff_block") or {}
            if diff.get("field") == "markdown_block":
                for patch in diff.get("patches") or ():
                    if isinstance(patch, dict):
                        chunks = _apply(chunks, patch)
    # Built rather than parsed: `answer_from` reads an assembled entry, and a partial
    # stream has none. Citations are whatever was delivered, and the index contract is
    # not enforced -- see answer_from.
    base = answer_from(latest, complete=False)
    return dataclasses.replace(base, text="".join(chunks))
```

with `_CARRIED = ("backend_uuid", "display_model", "search_mode", "blocks")` — the
top-level fields that are present from the first frame (M0 Q5) and the last-seen
`blocks` for citations.

- [ ] **Step 4: Run the whole adapter suite**

Run: `.venv/bin/python -m pytest tests/test_adapter.py -q`
Expected: PASS, all of it.

- [ ] **Step 5: Commit**

```bash
git add perplexity_client tests/test_adapter.py
git commit -m "M3: SSE reader, completion signal, and diff replay for partial answers"
```

---

### Task 6: The resume parser

Four lines, because Task 3's parser already covers it. PRD §9 milestone 3 asks for it; its callers arrive in milestones 6–7.

**Files:**
- Modify: `perplexity_client/adapter.py`
- Test: `tests/test_adapter.py`

**Interfaces:**
- Produces: `adapter.parse_thread(body: dict, allow_incomplete: bool) -> Response`

- [ ] **Step 1: Write the failing tests**

```python
def test_resume_path_parses_with_the_same_parser():
    r = adapter.parse_thread(THREAD, allow_incomplete=False)
    assert r.complete is True and r.mode == "research"
    assert len(r.text) > 5000 and len(r.citations) == 30
    assert r.model and r.thread_id


def test_resume_path_of_an_unfinished_thread_raises():
    with pytest.raises(IncompleteAnswerError):
        adapter.parse_thread({"entries": [{"status": "PENDING"}]}, allow_incomplete=False)


def test_resume_path_with_no_entries_raises():
    with pytest.raises(IncompleteAnswerError):
        adapter.parse_thread({"entries": []}, allow_incomplete=False)
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_adapter.py -q -k resume`
Expected: FAIL — no attribute `parse_thread`.

- [ ] **Step 3: Implement**

```python
def parse_thread(body: dict, allow_incomplete: bool) -> Response:
    """`GET /rest/thread/<uuid>` -- the resume path (M0 Q5).

    Plain JSON rather than SSE, and already decoded, but the entry carries the same
    `blocks` the terminal frame does, so it is the same parser. There is no
    `final_sse_message` here; `status` is the signal.
    """
    entry = next(iter(body.get("entries") or ()), None) or {}
    complete = entry.get("status") == "COMPLETED"
    if not complete and not allow_incomplete:
        raise IncompleteAnswerError(
            f"thread is {entry.get('status') or 'unreadable'}, not COMPLETED")
    return answer_from(entry, complete=complete)
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_adapter.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add perplexity_client/adapter.py tests/test_adapter.py
git commit -m "M3: the resume parser, four lines over the same answer parser"
```

---

### Task 7: Drive the query and tee the stream

**Files:**
- Modify: `perplexity_client/adapter.py`
- Test: `tests/test_ask.py` (create)

**Interfaces:**
- Consumes: `adapter.Stream`, `adapter.ASK_PATH`
- Produces: `adapter.submit(page, query) -> None`, `adapter.tee(ctx, page) -> Stream`

- [ ] **Step 1: Write the failing tests**

```python
"""Milestone 3: `ask()` orchestration, against fakes.

What is testable here is ordering and gating -- that a query is never spent on a dead
session, an exhausted mode or a challenge, and that a truncated stream raises. Whether
Chrome attaches and whether perplexity.ai answers is `pplx doctor`'s job (PRD §7).
"""

import pytest

from perplexity_client import adapter, chrome, client
from perplexity_client.errors import (ChallengeEncounteredError, IncompleteAnswerError,
                                      QuotaExhaustedError, SessionExpiredError)


class FakeCDP:
    def __init__(self, chunks):
        self.chunks, self.handlers, self.sent = chunks, {}, []

    def send(self, method, params=None):
        self.sent.append(method)
        if method == "Network.streamResourceContent":
            return {"bufferedData": ""}
        return {}

    def on(self, event, fn):
        self.handlers[event] = fn

    def detach(self):
        pass


def test_tee_only_follows_the_ask_request():
    cdp = FakeCDP([])
    s = adapter.tee(FakeCtxWithCdp(cdp), object())
    cdp.handlers["Network.responseReceived"]({
        "requestId": "1", "response": {"url": "https://www.perplexity.ai/rest/other",
                                       "mimeType": "text/event-stream"}})
    assert "Network.streamResourceContent" not in cdp.sent
    cdp.handlers["Network.responseReceived"]({
        "requestId": "2",
        "response": {"url": "https://www.perplexity.ai" + adapter.ASK_PATH,
                     "mimeType": "text/event-stream"}})
    assert "Network.streamResourceContent" in cdp.sent


def test_tee_ignores_data_from_other_requests():
    # ~40 REST calls fire on the homepage before the query (M0); mixing any of their
    # bytes into the buffer would corrupt every frame after it.
    cdp = FakeCDP([])
    s = adapter.tee(FakeCtxWithCdp(cdp), object())
    cdp.handlers["Network.responseReceived"]({
        "requestId": "2",
        "response": {"url": "https://www.perplexity.ai" + adapter.ASK_PATH,
                     "mimeType": "text/event-stream"}})
    cdp.handlers["Network.dataReceived"]({"requestId": "9", "data": _b64(b"garbage")})
    cdp.handlers["Network.dataReceived"]({"requestId": "2", "data": _b64(COMPLETE)})
    assert s.done
```

with `_b64 = lambda b: base64.b64encode(b).decode()` and a small `FakeCtxWithCdp` whose `new_cdp_session` returns the fake.

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ask.py -q`
Expected: FAIL — no attribute `tee`.

- [ ] **Step 3: Implement in `adapter.py`**

```python
def tee(ctx, page) -> Stream:
    """Start copying the answer stream into a `Stream`.

    `Network.getResponseBody` returns nothing for a streaming body and neither does
    Playwright's `response.body()` -- `streamResourceContent` is the only method that
    works here, and it must be called on the response event, before the body is gone
    (M0 Q1).
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
                and r.get("mimeType") == "text/event-stream":
            rid = params["requestId"]
            got = cdp.send("Network.streamResourceContent", {"requestId": rid})
            stream.feed(base64.b64decode(got.get("bufferedData") or ""))

    def on_data(params):
        if params.get("requestId") == rid and params.get("data"):
            stream.feed(base64.b64decode(params["data"]))

    cdp.on("Network.responseReceived", on_response)
    cdp.on("Network.dataReceived", on_data)
    stream.cdp = cdp
    return stream


def submit(page, query: str) -> None:
    """Type the query and send it.

    DOM is control only, never content (PRD §2). Deep-linking `/search/new?q=…` draws
    the interstitial hardest, so this does what a human does: the homepage and the box.
    """
    box = page.get_by_role("textbox").first
    box.wait_for(timeout=BOX_TIMEOUT)
    box.click()
    box.fill(query)
    box.press("Enter")
```

- [ ] **Step 4: Run to verify pass**

Run: `.venv/bin/python -m pytest tests/test_ask.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add perplexity_client/adapter.py tests/test_ask.py
git commit -m "M3: tee the answer stream over CDP, submit through the box"
```

---

### Task 8: `Client.ask()`

**Files:**
- Modify: `perplexity_client/client.py`, `perplexity_client/errors.py`, `perplexity_client/__init__.py`
- Test: `tests/test_ask.py`

**Interfaces:**
- Consumes: everything above
- Produces: `Client.ask(query: str, allow_incomplete: bool = False) -> Response`

- [ ] **Step 1: Write the failing tests**

```python
def test_ask_refuses_a_dead_session_before_spending_a_query(monkeypatch):
    monkeypatch.setattr(client, "chrome", fake_chrome(FakeCtx(ANON_STATE, [FakePage()])))
    with pytest.raises(SessionExpiredError):
        client.Client().ask("q")


def test_ask_refuses_a_challenge_rather_than_working_around_it(monkeypatch):
    page = FakePage(title="Just a moment...")
    monkeypatch.setattr(client, "chrome", fake_chrome(FakeCtx(GOOD_STATE, [page])))
    with pytest.raises(ChallengeEncounteredError):
        client.Client().ask("q")


def test_ask_refuses_an_exhausted_mode_rather_than_failing_mid_stream(monkeypatch):
    page = FakePage(quota={"modes": {"pro_search": {"available": False}}})
    monkeypatch.setattr(client, "chrome", fake_chrome(FakeCtx(GOOD_STATE, [page])))
    with pytest.raises(QuotaExhaustedError):
        client.Client().ask("q")


def test_ask_returns_the_parsed_answer(monkeypatch):
    ...  # fake tee/submit, feed COMPLETE, assert Response fields
    assert r.complete and r.model == "pplx_pro" and len(r.citations) == 15


def test_ask_raises_on_a_truncated_stream(monkeypatch):
    ...  # feed TRUNCATED
    with pytest.raises(IncompleteAnswerError):
        client.Client().ask("q")


def test_ask_spends_the_pacing_interval_unlike_status(monkeypatch):
    seen = {}
    monkeypatch.setattr(client, "chrome", recording_chrome(seen))
    ...
    assert seen["interval"] == pacing.default_interval()
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_ask.py -q`
Expected: FAIL — `Client` has no attribute `ask`.

- [ ] **Step 3: Implement**

`errors.py`:

```python
class SessionExpiredError(PplxError):
    """No usable login. Re-running `pplx login` is the only fix."""


class ChallengeEncounteredError(PplxError):
    """perplexity.ai served a bot-detection challenge.

    A terminal state on purpose (PRD §8): the tool never solves, bypasses or retries
    around one.
    """


class QuotaExhaustedError(PplxError):
    """The account's quota for this mode is used up.

    Refused before the query is spent rather than discovered mid-stream. It is the only
    quota signal the account has -- no remaining count exists for these modes (M2).
    """
```

`client.py`:

```python
ANSWER_TIMEOUT = 180.0
POLL = 0.25


def _ready(ctx, page) -> None:
    """Fail before spending a query, never during one."""
    if not _has_session_cookie(ctx):
        raise SessionExpiredError("no session -- run: pplx login")
    page.goto(HOME, wait_until="domcontentloaded")
    deadline = time.monotonic() + adapter.SETTLE_TIMEOUT
    while adapter.is_challenge(page.title(), page.url) and time.monotonic() < deadline:
        page.wait_for_timeout(1000)
    state = adapter.classify(page.title(), page.url, bool(page.evaluate(adapter.AUTH_PROBE)))
    if state == "challenged":
        raise ChallengeEncounteredError(
            "perplexity.ai served a bot-detection challenge; this tool never bypasses "
            "one. Open Chrome yourself, then re-run: pplx login")
    if state != "ok":
        raise SessionExpiredError("session expired or revoked -- run: pplx login")
    if "search" in adapter.exhausted(page):
        raise QuotaExhaustedError(
            "the account's search quota is used up; it resets on Perplexity's schedule, "
            "which the account cannot see (docs/M2-findings.md)")


class Client:
    def ask(self, query: str, allow_incomplete: bool = False) -> Response:
        """One search-mode query. Blocks until the answer completes.

        Raises rather than returning a truncated answer (US-3): a wrong-but-plausible
        answer entering an agent pipeline as fact is the failure PRD §10 rates critical.
        """
        if not (query := query.strip()):
            raise PplxError("empty query")
        with chrome(headless=True, interval=default_interval()) as (ctx, page):
            _ready(ctx, page)
            stream = adapter.tee(ctx, page)
            adapter.submit(page, query)
            deadline = time.monotonic() + _env("PPLX_ASK_TIMEOUT", ANSWER_TIMEOUT)
            # Yield through Playwright, not time.sleep: CDP events only dispatch while
            # the greenlet yields, so a sleeping loop would receive nothing at all.
            while not stream.done and time.monotonic() < deadline:
                page.wait_for_timeout(int(POLL * 1000))
            if not stream.frames and adapter.is_challenge(page.title(), page.url):
                raise ChallengeEncounteredError(...)
            if not stream.frames:
                raise PplxError(
                    "no answer stream was intercepted -- perplexity.ai's frontend has "
                    "probably changed. Run: pplx doctor")
            return adapter.parse_stream(stream.frames, allow_incomplete)
```

`__init__.py`: export `Response`, `Citation` and the five new errors.

- [ ] **Step 4: Run the whole suite**

Run: `.venv/bin/python -m pytest tests -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add perplexity_client tests
git commit -m "M3: Client.ask() -- gate, submit, tee, parse"
```

---

### Task 9: Documentation

**Files:**
- Modify: `README.md`, `docs/PRD.md`

- [ ] **Step 1: README**

Status line → milestone 3. Add an `ask` example, the completeness contract, the citation contract, and `PPLX_ASK_TIMEOUT` to the variables table. State plainly that `ask` is a library call in this milestone and `pplx ask` lands in milestone 7.

- [ ] **Step 2: PRD**

Amend §2 (extraction reads `blocks`), §5 (the citation contract binds complete answers), §9 milestone 3 (✅ Done, "two parsers" corrected, pointing at `docs/M3-findings.md`) — mirroring how milestones 1 and 2 recorded their corrections.

- [ ] **Step 3: Verify both evidence scripts still run**

Run: `.venv/bin/python -m pytest tests -q && .venv/bin/python spike/verify_findings.py`
Expected: all PASS, exit 0.

- [ ] **Step 4: Commit**

```bash
git add README.md docs
git commit -m "M3: docs -- one parser not two, and the contracts ask() now enforces"
```

---

## Self-Review

**Spec coverage** — every M3 requirement maps to a task: text/citations/observed model/completion → Tasks 3–5; §5 citation contract → Task 3; US-3 raise-by-default and `allow_incomplete` → Tasks 4–5, 8; the resume parser PRD §9 asks for → Task 6; adapter isolation (PRD §4) → Task 1; dated fixtures (PRD §7) → Task 2; quota pre-flight (PRD §4) → Task 8; challenge handling (PRD §8) → Task 8.

**Out of scope, deliberately:** model selection (M4), `thread_id` as input (M5), Deep Research (M6), `pplx ask` (M7), `doctor` (M8). Listed in `docs/M3-findings.md` so the omissions are decisions on the record rather than gaps.

**Type consistency** — `answer_from(entry, complete)` is the single parser; `parse_stream` and `parse_thread` both call it; `Stream.feed/frames/done` are used identically in Tasks 4, 7 and 8.
