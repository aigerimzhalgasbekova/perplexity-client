# M0 — Protocol spike findings

**Capture date:** 2026-07-31 · **Account:** Perplexity Pro · **Web app version:** `2.18`
**Fixtures:** `spike/fixtures/search-complete.sse`, `search-truncated.sse`,
`research-thread-resume.json`
**Tooling:** `spike/capture.py` (capture), `spike/make_fixtures.py` (redact + cut fixtures)

Every shape claim below is asserted in `spike/verify_findings.py`; run it to falsify them.

Answers the five questions in PRD §2. **All five confirmed.** Q1–Q4 on search mode,
Q5 on Deep Research.

## Headline

The stream path in PRD §2 is **confirmed**, and the §5 contracts are enforceable as
written. The terminal frame is self-contained: it carries the full answer text *and*
the full citation list in one payload, so text and citations are captured at the same
moment rather than sampled separately.

One finding falls outside the five questions and changes the architecture: **Playwright
cannot launch the browser** (see "Blocker" below).

## Blocker: Cloudflare blocks Playwright-launched browsers

PRD §2 assumes `pplx login` launches a Playwright-managed Chromium. That does not work.

| Launch method | Result |
|---|---|
| Playwright bundled Chromium | Cloudflare interstitial, never clears (5 reloads / 70s) |
| Playwright `channel="chrome"` (real Chrome binary) | Same — challenged, never clears |
| Normally-launched Chrome + CDP attach | **Works** — no challenge at all |
| The user's ordinary Chrome, by hand | Works (control) |

The site loaded fine in a normal browser at the same moment from the same IP, so this
targets the automated launch, not the address. Deep-linking `/search/new?q=…` triggers
the interstitial hardest; navigating the homepage and typing into the box is what a
human does and is what now works.

**Resolution, decided with the maintainer:** launch Chrome as a normal process with
`--remote-debugging-port` and attach over CDP (`connect_over_cdp`). Nothing spoofs a
fingerprint, patches `navigator.webdriver`, or solves a challenge — PRD §8 holds. The
tool simply does not add automation switches in the first place.

**Consequences for v1, not yet reflected in the PRD:**

- Chrome 136+ refuses `--remote-debugging-port` on the default profile, so the tool
  needs its own profile directory. The user logs into that profile once.
- **Headless works** — probed separately, see below. PRD §2's headless query context
  stands.
- The session is carried by the Chrome profile directory, not only by
  `storage_state`. `storage_state` is still written (and is what §2's atomic
  write-back protects), but the profile dir is the thing that actually keeps the login
  alive. PRD §2's session-lifecycle description needs updating to match.
- The tool depends on a Google Chrome install, which is a new prerequisite. The
  ~300MB `playwright install chromium` step in §2 may be droppable.

## Headless — **works** (`spike/headless_probe.py`)

The blocker above only rules out Playwright *launching* the browser. Headlessness itself
turned out to be fine: a normally-launched `--headless=new` Chrome attached over CDP is
not challenged and completes queries normally.

| Arm | Challenged | Session valid | Terminal frame |
|---|---|---|---|
| `--headless=new` × 3 runs | no | yes | yes (334–439 KB) |
| headed (control) | no | yes | yes (405 KB) |

Run against a *copy* of the logged-in profile, on Chrome 150. Three headless runs, three
passes — worth repeating since Cloudflare's decision is probabilistic and one green run
would not prove much.

Two things this establishes beyond the headless question:

- **The profile directory is portable.** The session survived being copied to a temp dir
  and driven from there, so nothing in it is machine- or path-pinned.
- Copying a live profile requires deleting its `Singleton{Lock,Cookie,Socket}` files
  first. They point at the running Chrome; left in place, the new instance hands off to
  the old one and exits without ever opening a debugging port.

## Q1 — Is the answer stream interceptable? **Yes**

`POST https://www.perplexity.ai/rest/sse/perplexity_ask` → `text/event-stream`.

Interception detail that matters: **`Network.getResponseBody` returns nothing for this
response** — streaming bodies are not buffered. The working method is CDP
`Network.streamResourceContent` on the request id, which tees the body into
`Network.dataReceived` events (base64). Playwright's own `response.body()` has the same
problem as `getResponseBody`.

Frames are **CRLF-delimited** (`\r\n\r\n`), per the SSE spec. Reading a capture in
Python text mode silently rewrites this to `\n\n`; the fixtures preserve the wire bytes,
and the adapter must split on `\r\n\r\n`.

The request payload is plain JSON and carries the control surface:

```json
{"params": {"mode": "copilot", "model_preference": "pplx_pro",
            "search_focus": "internet", "sources": ["web"],
            "dsl_query": "<the query>", "query_source": "home", ...}}
```

## Q2 — What is the terminal completion signal? **`final_sse_message: true`**

In a 136-frame stream, exactly **one** frame carries `"final_sse_message": true`
(block 133). It coincides with:

| Field | Terminal frame | All other frames |
|---|---|---|
| `final_sse_message` | `true` (1 frame) | `false` |
| `status` | `"COMPLETED"` (1 frame) | `"PENDING"` (133 frames) |
| `text_completed` | `true` | `true` on 1 earlier frame (132) too |

**Use `final_sse_message`, corroborated by `status == "COMPLETED"`.** Do not use
`text_completed` — it goes true one frame *before* the terminal frame, so it would admit
a payload that is not yet final. `Response.complete` per PRD §5 is therefore a real
observed signal, not a heuristic, and the §10 "truncated answer" risk is mitigated as
the PRD intends.

Two trailing frames follow the terminal one (`event: end_of_stream` and an empty frame);
they carry no answer content.

## Q3 — Single payload or assembled? **Single, self-contained terminal payload**

Intermediate frames stream incrementally via `diff_block` JSON-patch operations, but the
terminal frame does **not** require replaying them. Its `text` field is a JSON *string*
holding the complete step list:

```
text (JSON string) → [ {step_type: "INITIAL_QUERY"},
                       {step_type: "SEARCH_WEB"},
                       {step_type: "SEARCH_RESULTS", content.web_results: [...]},
                       {step_type: "FINAL",          content.answer: (JSON string)} ]

FINAL.answer (JSON string) → {answer, web_results, chunks, extra_web_results,
                              structured_answer}
```

Note the **double JSON encoding**: `text` is a string that must be parsed, and inside it
`FINAL.content.answer` is again a string that must be parsed.

- Answer markdown, with inline `[n]` markers intact: `FINAL.answer.answer`
- Citations: `SEARCH_RESULTS.web_results` — verified **byte-identical** to
  `FINAL.answer.web_results` in this capture. Either is usable; they agree.
- Citation fields available: `name` (→ `Citation.title`), `url`, `snippet`,
  plus `meta_data.published_date` and a `trust` object.
  `snippet` is `""` (empty, not absent) for some sources — PRD §5's `str | None` should
  treat empty as `None`.

**Citation index contract (§5) verified on this capture:** markers found were
`[2,3,4,6,7,10,11,12,13,14,15]`, max marker 15, `len(web_results) == 15`, every marker
in range, and `citations[n-1]` resolves to the source the text is citing. Marker `[1]`
being unused is normal — the model cites a subset. The contract holds as
*every marker maps*, not *every source is cited*.

## Q4 — Is the serving model observable? **Yes, and distinguishable from the request**

The terminal frame carries both:

| Field | Value | Meaning |
|---|---|---|
| `display_model` | `"pplx_pro"` | the model that **served** the answer |
| `user_selected_model` | `"pplx_pro"` | what was **requested** |

This is exactly what US-6 needs: `Response.model` reads `display_model` (observed), and
`ModelMismatchError` compares it against the request. A silent server-side substitution
would show up as the two fields disagreeing.

Also on the terminal frame: `mode: "COPILOT"`, `search_mode: "SEARCH"`.

Two models observed so far, and both use the **same vocabulary** in `display_model`,
`user_selected_model` and the request's `model_preference`:

| Observed | Mode | `display_model` | `user_selected_model` |
|---|---|---|---|
| search | `SEARCH` | `pplx_pro` | `pplx_pro` |
| Deep Research | `RESEARCH` | `pplx_alpha` | `pplx_alpha` |

`GET /rest/models/config/v2` enumerates the full vocabulary — 113 entries keyed by the
exact `model_preference` value — plus the per-mode defaults:

```json
{"search": "pplx_pro", "research": "pplx_alpha", "agentic_research": "pplx_agentic_research",
 "study": "pplx_study", "document_review": "pplx_document_review",
 "browser_agent": "comet_browser_agent", "asi": "pplx_asi"}
```

Still not established: whether the two fields ever actually *disagree*. Every capture so
far served what was requested, so `ModelMismatchError` is built on a field pair that is
confirmed to exist but has not been seen firing. Milestone 4 should force a downgrade
(e.g. request a model above the plan's entitlement) to see it.

## Q5 — Is a Deep Research task id durably re-navigable? **Yes**

Verified end to end: a Deep Research run was started in one process, that process exited,
and a **fresh process** re-opened `/search/<uuid>` and read back the complete answer.
**US-5 (detach / resume-by-id) is unblocked.**

The id is the same one search mode uses — `backend_uuid` == `thread_url_slug` == URL path.
It is assigned **before the research runs**: the URL existed while the thread was still
awaiting input, which is exactly what `--detach` needs (return an id immediately, resume
later).

Two independent read paths, both confirmed:

| Path | Endpoint | Completion signal |
|---|---|---|
| Live stream | `POST /rest/sse/perplexity_ask` | `final_sse_message: true` |
| Resume | `GET /rest/thread/<uuid>?with_parent_info=true&…` | `status == "COMPLETED"` |

**The resume path is plain JSON, not SSE**, and its shape differs from the stream — the
adapter needs two parsers, not one:

```
entries[0].status                                    -> "COMPLETED"
entries[0].blocks[intended_usage=="ask_text"]
          .markdown_block.answer                     -> answer markdown
entries[0].blocks[intended_usage=="web_results"]
          .web_result_block.web_results               -> citations (same fields as §Q3)
```

No double encoding here — the resume path is already decoded. Verified on the research
thread: 11 880 chars of answer, 30 citations, markers `[1]`–`[17]`, all in range. The same
extraction was verified against a *search* thread and matched its SSE result exactly (same
text, same 15 citations, same order), so one resume parser serves both modes.

**Listing tasks** — `GET /rest/thread/list_recent` returns every thread as
`{uuid, title, link, status, task_description, answer_preview, mode_type}`. This is the
natural backing for a `pplx status <id>` / task-list command; no polling of the SSE
channel is needed.

**Research-only fields**, all on the same entry:

- `search_mode: "RESEARCH"` (search mode reports `"SEARCH"`) — the mode discriminator.
- `search_implementation_mode: "multi_step"`.
- A `plan` block with `progress: "DONE"` and a goal list — a real progress signal for a
  long-running task, better than a spinner.
- Extra blocks not present in search: `workflow_root`, `answer_tabs`,
  `sources_answer_mode`, `pending_followups`.
- `reconnectable: false` on the completed research entry (search's terminal frame said
  `true`). Re-attaching to a *live* stream is therefore still unproven — but it does not
  matter, because the resume path works and is simpler.

### Deep Research uses the same protocol as search

Confirmed on the wire: research submits to the **same** `POST /rest/sse/perplexity_ask`
with the same `mode: "copilot"`, differing only in `model_preference: "pplx_alpha"`.
The terminal signal is the same `final_sse_message: true` with `status: "COMPLETED"`, and
the status progression is the same `PENDING` → `COMPLETED`. **Deep Research is a model
choice, not a second API** — one stream parser serves both modes, and §5 does not need a
separate research transport.

**`backend_uuid` is present on the very first frame, while `status` is still `PENDING`.**
This is precisely what US-5's `--detach` requires: submit, read the id off frame 1, exit.
Verified on a 129-frame research stream (`048448f5-…`, first frame `PENDING`, id already
set and equal to `thread_url_slug`).

### The clarifying-question state, and the gap it opens in PRD §5

**Deep Research may ask clarifying questions before it runs**, and blocks until they are
answered or skipped. Observed: 4 multiple-choice questions with a `Skip ⌘Enter`
affordance. It is not deterministic — a broad query ("compare the economic policies of
Australia and New Zealand since 2020") triggered it; a narrow one ("what are the main
exports of New Zealand") ran straight through in 27 s.

**The top-level `status` field does not reveal this** — it stays `PENDING`, identical to
"still working". A client keying only on `status` waits forever. The state is instead
visible inside the `workflow_root` block:

| Signal | Value while awaiting |
|---|---|
| `workflow.status` | `"WORKFLOW_AWAITING_NEXT_STEPS"` |
| `tool_name` | `"clarifying_questions"` |
| item `type` | `"WORKFLOW_ITEM_USER_RESPONSE"` with `responses: []` |

Full workflow vocabulary observed: statuses `WORKFLOW_PENDING`,
`WORKFLOW_AWAITING_NEXT_STEPS`, `WORKFLOW_COMPLETED`; item types
`WORKFLOW_ITEM_CONTENT`, `_QUERIES`, `_SOURCES`, `_USER_QUESTIONS`, `_USER_RESPONSE`;
tools `search_web`, `load_skill`, `clarifying_questions`.

The questions arrive **structured**, not as prose — each carries `field_name`, an
`options` list, `allow_multichoice` and `allow_free_text` — alongside an
`answer_submission_uuid` and `response_endpoint:
"handle_perplexity_research_clarifying_answers"`. So answers can be submitted through
the API rather than driven through the DOM. (The DOM path is worse anyway: the visible
"Skip" is not clickable as a text node, only `Meta+Enter` works.)

**Required PRD §5 change:** `ResearchTask.status = "pending" | "running" | "done" |
"failed"` needs an awaiting-input state, and the API should let the caller either supply
answers up front or skip by default. Skipping is the safe default for an unattended
client, since it is what keeps `ask()` non-blocking.

`plan_block.progress` moves `IN_PROGRESS` → `DONE` per goal, which gives
`ResearchTask` real progress reporting rather than an opaque spinner.

## Notes for implementation

- `thread_id` for PRD §5 = `backend_uuid`. It is the URL slug, so multi-turn
  continuation (v1 milestone 5) can navigate to `/search/<backend_uuid>` directly.
- The homepage fires ~40 REST calls before the query; the adapter should key on the
  `perplexity_ask` request id and ignore the rest.
- `POST /rest/visitor/ask-complete` fires when the answer finishes and is a possible
  cross-check, but `final_sse_message` is the contractual signal.
- **`GET /rest/rate-limit/status`** exists and is polled by the web app before each query.
  Milestone 2 (pacing / interval floor) should read it rather than guess an interval —
  the server states its own limit.
- Fixtures are redacted: `author_id`, `author_username`, `author_image` and
  `read_write_token` are replaced with `REDACTED`. **`read_write_token` grants write
  access to the thread** — never commit a raw capture. `spike/captures/` is gitignored;
  only `spike/fixtures/` is committed.
