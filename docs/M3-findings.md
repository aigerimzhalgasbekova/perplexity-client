# M3 — Search-mode `ask()`: findings and design

**Source:** the M0 fixtures, re-read for the parser rather than for the protocol
questions — `spike/fixtures/search-complete.sse` (135 frames),
`search-truncated.sse` (66 frames, cut mid-answer),
`research-thread-resume.json`. Capture date 2026-07-31.
**Asserted by:** `tests/test_adapter.py`.

## Headline: one parser, not two

PRD §9 milestone 3 says *"Two parsers are needed, not one: the SSE stream and the
plain-JSON resume path."* That is **wrong**, and it was wrong in M0's own fixtures.

M0 documented the SSE terminal frame through its `text` field — a JSON *string*
holding a step list, inside which `FINAL.content.answer` is *again* a JSON string.
That path is real. But the same terminal frame also carries a `blocks` list, and it
is **the same `blocks` shape the resume path uses**:

| | SSE terminal frame | Resume `entries[0]` |
|---|---|---|
| answer | `blocks[ask_text].markdown_block.answer` | same |
| citations | `blocks[web_results].web_result_block.web_results` | same |
| observed model | `display_model` (top level) | same |
| requested model | `user_selected_model` (top level) | same |
| thread id | `backend_uuid` (top level) | same |
| mode | `search_mode` (top level) | same |

Verified against the fixtures: the answer read through `blocks` is **byte-identical**
(1679 chars) to the answer read through the double-encoded `text` path, and the 15
citations are the same list in the same order.

So there is one parser — `adapter.answer_from(entry)` — and it is fed by two
*finders*: "the frame with `final_sse_message`" for the live stream, and
`entries[0]` for the resume path. The resume path costs four lines rather than a
second parser, which is why it ships in this milestone as the PRD asks even though
its callers (`pplx result`, `Client().task()`) do not arrive until milestones 6–7.

**The double-encoded `text` path is not used and not kept as a fallback.** Two paths
that can disagree is exactly the citation-misattribution risk PRD §10 rates High; one
path with a loud error is the smaller failure surface. `blocks` is chosen over `text`
because it is the shape both transports share and the one the app's own resume
endpoint serves.

## A partial answer is a diff replay, and the vocabulary is two operations

Intermediate frames do not carry the answer — they carry JSON-patch fragments in
`blocks[ask_text].diff_block`, and the assembled `markdown_block` appears only on the
terminal frame. So `allow_incomplete=True` (US-3) cannot just read the last frame;
without replaying the diffs it would hand back an empty string, which is not "what was
received".

The operation vocabulary observed across both fixtures, **scoped to the block the
parser reads** — `intended_usage == "ask_text"`, `diff_block.field ==
"markdown_block"`:

| `op` | `path` | Complete | Truncated | Meaning |
|---|---|---|---|---|
| `replace` | `""` | 1 | 1 | snapshot: the whole `markdown_block`, including `chunks` |
| `add` | `/chunks/<n>` | 117 | 54 | append one token to `chunks` at index `n` |

That is the entire surface *for that block* — one snapshot, then tokens. Replaying it
is a list and a join, not an RFC 6902 implementation, and the parser handles only these
two operations: an unrecognised patch is ignored rather than guessed at, because a wrong
guess would produce plausible-looking text that never existed.

The scoping matters, because the aggregate is wider. Each answer also streams sibling
`ask_text_<n>_markdown` blocks — one per section, whose text is a duplicate of the
aggregate `ask_text` carries — and *those* use two further operations: `replace` at
`/progress` (5 in the complete capture, 2 in the truncated) and `add` at `/media_items`
(1, complete only). Neither ever appears on `ask_text`. The parser reads the aggregate
and ignores the siblings, so it never meets either — but the next person extending the
replay will, and would otherwise trust a table that had scoped itself silently.

The index in `/chunks/<n>` is honoured rather than appended blindly, and bounded to
`0 <= n <= len(chunks)` — append or overwrite, never outside the array. Both bounds
guard demonstrated harm on a path fed straight from the network: `int("-1")` makes the
padding a no-op and `chunks[-1] = value` **rewrites a token that really arrived**,
inventing text the server never sent, while a large index pads without limit (an index
of 2×10⁷ measured at 320 MB). Past the end is an error in RFC 6902 anyway, and every
index in both captures is simply the next one — the observed stream is append-only,
strictly increasing, no gaps and no duplicates.

`chunks[0]` arrives only in the root snapshot (both captures open with a one-element
`chunks`, and the first `add` is `/chunks/1`), so losing that one frame would fail
every later index against the bound and empty the whole partial answer rather than
shorten it. Padding across the gap instead — `i <= len(chunks) + k` — was considered
and rejected: a hole in the middle of `"".join(chunks)` reads as continuous prose with
a word silently missing, which is the plausible-but-wrong shape this milestone exists
to refuse, while the current bound degrades to truncation at the gap, which is what
`complete=False` already says. The loss is bounded either way; only one of the two is
honest about it.

**Markers are read out of the prose, not the raw markdown.** Fenced and inline code are
stripped first. `nums[0]`, `arr[10]` and a bracketed quantifier in a regex are not
citations, and Perplexity is heavily used for programming questions — scanning the raw
text throws away a complete, correct answer *after the query is spent*, with a message
blaming the frontend and prescribing a `doctor` run that spends another. `[0]` is the
common case, since `citations[n-1]` gives it no meaning and it is unmapped by
construction. A marker never legitimately appears inside code, so nothing real is lost.

**The citation-index contract is enforced on complete answers only.** A stream cut
mid-answer may legitimately carry a marker whose source had not been delivered yet;
raising on output the caller explicitly opted into would be a false alarm. Completeness
is the thing US-3 guarantees, and it is guaranteed unconditionally.

## Completion, unchanged from M0

`final_sse_message: true`, corroborated by `status == "COMPLETED"` — **both**, not
either. A frame that claims to be final while reporting something other than
`COMPLETED` is not a completed answer, and treating it as one is the exact failure
US-3 exists to prevent. `text_completed` stays unused: M0 measured it firing one frame
early.

The resume path has no `final_sse_message`; there `status == "COMPLETED"` on the entry
is the signal, as M0 recorded.

## A completion signal with no answer is refused

Every lookup into the payload is `or {}`-guarded, so a renamed block, a renamed
`markdown_block` or a renamed `answer` key all collapse to `""` — and the citation
check then passes vacuously over zero markers. That produced `complete=True, text=""`,
which an agent reads as *"Perplexity found nothing"*: plausible, actionable and wrong,
which is precisely PRD §10's critical row. The two High-rated risks compose here — the
trigger is a frontend change, and adapter isolation contains the second risk while
doing nothing about the first.

`answer_from` therefore refuses a complete answer with no text. A finished answer with
no text is not an outcome this protocol produces, and every capture contradicts it.

## Frame boundaries are not chunk boundaries

CDP's `Network.dataReceived` delivers whatever bytes arrived, in internal chunks that
have nothing to do with SSE framing — a frame routinely spans two events, and the last
frame in the buffer at any moment is usually half-written. The reader therefore
accumulates raw bytes and splits complete `\r\n\r\n` blocks off the front, keeping the
remainder for the next event. Parsing per-event, or re-parsing the whole buffer on a
timer, both get this wrong or get slow; this gets neither.

A frame that still will not parse as JSON is skipped, not fatal. At the end of a
truncated stream the trailing fragment is exactly that, and it is the normal case for
US-3, not an error.

The held-back block has to be **flushed when the connection ends**, though. It is held
back because it is *usually* half-written — but if the stream closes right after the
terminal frame and before its trailing separator, that block is the entire answer, and
refusing it burns the query that bought a complete one. The live capture only avoids
this by ending with `event: end_of_stream`, whose separator flushes the terminal frame
ahead of it. `Stream.close()` does the flush; a genuinely half-written tail still fails
to parse and is still dropped.

## Mode is observed here, not chosen

`submit` does what a person does — the homepage, the box, Enter — and so inherits
whatever mode the profile's UI is set to. Nothing in `ask()` selects search mode;
selecting it is US-4's content, milestones 4–6. Two consequences are worth writing
down rather than discovering:

- `_ready` gates on the **search** quota. If the profile's selector is parked on Deep
  Research, that gate checks the wrong axis, and the run spends research quota.
- `Response.mode` is the observed mode, so the mismatch is visible — and `ask()` now
  says so on stderr when it is not `"search"`. Warned rather than raised, on the same
  reasoning as `status()`'s quota warning: discarding an answer the account has
  already paid for is a worse outcome than reporting it honestly.

The mode discriminator is explicit for the same reason `Response.model` is strictly
observed: `"research" if search_mode == "RESEARCH" else "search"` reports every
unrecognised or renamed mode as the one mode this milestone claims to drive, which is a
guess in the flattering direction.

## What this milestone does not do

- **Selecting the mode** — milestones 4–6, as above. It is reported, not requested.
- **Model selection and `ModelMismatchError`** — milestone 4. `Response.model` is
  already strictly the observed `display_model`, so nothing here needs revisiting;
  what is missing is the request side and the comparison.
- **`thread_id` as an input** — milestone 5. It is returned, not accepted.
- **Deep Research** — milestone 6, including `awaiting_input` and `on_clarify`.
- **`pplx ask` on the command line** — milestone 7. `ask()` is a library call in this
  milestone; wiring it to `argparse` with `--json` and exit codes is that milestone's
  whole content.
- **`pplx doctor`** — milestone 8.

## PRD amendments this milestone forces

1. §9 milestone 3's "two parsers are needed, not one" is wrong — corrected above.
2. §2's data-source description should name `blocks`, not the double-encoded `text`
   step list, as the extraction path. The `text` path remains accurate as a
   description of the wire format; it is simply not what the adapter reads.
3. §5 should say the citation-index contract binds complete answers. It is silent on
   incomplete ones today, and the honest reading — enforce it on a partial answer —
   would make `allow_incomplete=True` raise on output the caller asked for.
4. §5 types `mode` as `"search" | "research"`. It is the **observed** mode, and those
   are the two this tool drives; anything else comes back as the server's own value,
   lowercased, rather than being folded into `"search"`.
