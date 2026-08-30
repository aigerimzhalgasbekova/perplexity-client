# PRD: perplexity-client

## 1. Overview

- **Product name:** perplexity-client
- **One-line summary:** A Python library and CLI that lets you automate your own Perplexity Pro account from scripts and AI agents by driving a real, manually-authenticated browser session.
- **Problem statement:** Perplexity's Pro subscription does not include API access; API access is a separate paid product with its own billing. Users who already pay for Pro and want to script queries, run bulk research, or plug Perplexity into an agent/tool-calling pipeline have no first-party way to do that with their existing subscription. `notebooklm-py` solved the equivalent problem for NotebookLM by driving a real, logged-in browser session instead of a nonexistent API; this project applies the same pattern to Perplexity.
- **Target audience:** Public open-source project (GitHub). Primary users are developers automating their own Perplexity Pro account with their own session.
- **Positioning:** The project is framed — in README, docs, and repo description — as *"automate your own Perplexity account,"* not as *"free Perplexity API"* or *"API access without paying."* This is a deliberate choice, not cosmetics; see §10.

## 2. Architecture

```
                     one-time, visible browser
   ┌──────────┐      ┌─────────────────────────┐
   │  pplx    │ ---> │  Google Chrome, launched │ ---> user logs in manually
   │  login   │      │  as a normal process     │      (password/SSO/2FA —
   └──────────┘      │  + CDP attach            │       whatever the account needs)
                     └─────────────────────────┘
                              │
                              ▼
                  ~/.config/perplexity-client/chrome-profile/  (the live session)
                  ~/.config/perplexity-client/pplx.lock        (advisory lock +
                                                                last-request stamp)

   ┌──────────────────┐        ┌────────────────────────────────┐
   │ Python caller /   │ -----> │ Client                          │
   │ CLI (`pplx ask`)  │        │  1. acquire flock on pplx.lock  │
   └──────────────────┘        │  2. enforce min-interval floor  │
                                │  3. launch Chrome --headless=new│
                                │     on the profile, attach CDP  │
                                └───────────────┬─────────────────┘
                                                │
                    ┌───────────────────────────┴──────────────────┐
                    │                                              │
                    ▼  CONTROL (DOM)                               ▼  DATA (stream)
        ┌───────────────────────────┐              ┌────────────────────────────┐
        │ navigate, set mode/model, │              │ intercept answer stream:   │
        │ type query, submit        │              │ text, citations, model,    │
        │                           │              │ terminal completion frame  │
        └───────────────────────────┘              └────────────────────────────┘
                    │                                              │
                    └──────────────────┬───────────────────────────┘
                                       ▼
                                perplexity.ai
                                       │
                                       ▼
              Response { text, citations[], model, mode,
                         thread_id, complete }
                                       │
                       complete == False  ──> raises IncompleteAnswerError
                                             (unless allow_incomplete=True)
```

### Data source decision (resolved)

**The answer stream is the single source of truth for answer content.** The adapter extracts `text`, `citations`, `model`, and the completion signal from the intercepted network stream that Perplexity's web app uses to deliver answers. The DOM is used only for *control* — navigation, selecting mode and model, entering the query, submitting — and never for extracting answer content.

Rationale: the stream carries structured payloads with an explicit terminal signal. DOM scraping provides neither, which makes the completeness contract (§5) and the citation-index contract (§5) unenforceable rather than merely harder.

**This decision was gated on Milestone 0 (§9), a devtools spike against a real Pro account. M0 is complete and confirmed it — see `docs/M0-findings.md`.** All five questions were answered against a live Pro session, with committed fixtures and a runnable check (`spike/verify_findings.py`). The DOM fallback below is therefore **not** in effect: `Response.complete` is a real observed signal and the citation-index contract is guaranteed, not best-effort.

1. **Interceptable** — `POST /rest/sse/perplexity_ask` → `text/event-stream`, teed via CDP `Network.streamResourceContent`. `getResponseBody` returns nothing for a streaming body, so this specific method is load-bearing. Frames are CRLF-delimited.
2. **Terminal signal: `final_sse_message: true`**, corroborated by `status == "COMPLETED"`. Note `text_completed` fires one frame *early* and must not be used.
3. **Single self-contained terminal payload** — full answer text and full citation list in one frame, double-JSON-encoded. No cross-frame assembly, so index integrity needs no reconstruction. **Amended by M3:** the same terminal frame *also* carries a `blocks` list, and the answer read through it is byte-identical to the double-encoded `text` path. The adapter reads `blocks`, because that is the shape the resume path (Q5) serves too — see `docs/M3-findings.md`. The `text` description above remains an accurate account of the wire format; it is simply not the path the parser takes.
4. **Yes** — `display_model` (served) and `user_selected_model` (requested) are separate fields on the terminal frame.
5. **Yes** — `backend_uuid` == `thread_url_slug` == URL path, present on the *first* frame while status is still `PENDING`, and the thread is readable from a fresh process via `GET /rest/thread/<uuid>` (plain JSON, not SSE — a second parser).

**Fallback, retained for the record and not currently in use:** had M0 disproved the above, the tool would fall back to DOM extraction with the consequences recorded here rather than silently — `Response.complete` degrading to a best-effort heuristic and the citation-index contract weakening from *guaranteed* to *best-effort*. A DOM-settle-timer masquerading as a completion signal is **not** an acceptable implementation of `complete`; if a future protocol change forces the fallback, `complete` must be documented as heuristic in both the docstring and the README.

- **Deployment model:** Runs entirely on the user's own machine. No server component, no hosted infrastructure. Each user authenticates their own Pro account locally; sessions are never shared or transmitted anywhere except to perplexity.ai itself.
- **Browser launch (established by M0):** Playwright must **not** launch the browser. Both its bundled Chromium and `channel="chrome"` are challenged by Cloudflare on sight and never clear, while a Google Chrome started as an ordinary process with `--remote-debugging-port` and attached via `connect_over_cdp` is not challenged at all. The tool therefore launches Chrome itself and attaches. Nothing spoofs a fingerprint, patches `navigator.webdriver`, or solves a challenge — see §8; the tool simply does not add automation switches in the first place. Chrome 136+ refuses `--remote-debugging-port` on the default profile, so the tool uses its own profile directory under `~/.config/perplexity-client/`.
- **Headless (established by M0):** `--headless=new` is *not* challenged — three of three probe runs completed queries normally, against a headed control. Queries run headless; only `login` is visible.
- **Session lifecycle:** `pplx login` opens a real, visible Chrome window on the tool's own profile directory for a one-time manual login (handles whatever auth the account needs — password, SSO, 2FA — without the tool ever touching credentials). **The profile directory is what carries the session**, and it is portable — M0 drove a copied profile from a temp directory successfully. Nothing else is written: Chrome rotates its own cookies inside that directory, so the tool exports no `storage_state` copy and there is no second credential-equivalent file to protect. On expiry, revocation, or a bot-detection challenge, the tool fails loudly and instructs the user to re-run `login`.
- **Session state check (established by M1):** neither cookie presence nor an HTTP status code can tell `ok` from `expired` — Perplexity serves anonymous visitors a `pplx.session-id` cookie and answers `200` on every `/rest/` endpoint without a login. The discriminator is NextAuth's `GET /api/auth/session`: `{"user": {...}}` when signed in, `{}` when not. It also answers `200 {}` from *behind* a Cloudflare interstitial, so `challenged` must be decided before that probe is trusted, or a block reads as `expired` and sends the user to re-login for nothing. Evidence: `spike/probe_status.py`.
- **Rate limits (established by M2):** Perplexity states no rate to its own account. `GET /rest/rate-limit/status` reports `available` per mode and `remaining_detail.kind == "not_provided"` for `pro_search` and `research`; no reset time, window or `Retry-After` appears there or on `/rest/user/settings`. Pacing is therefore a local floor by design, and the endpoint serves as a pre-flight exhaustion gate. Evidence: `spike/probe_rate_limit.py`, `docs/M2-findings.md`.
- **Copying a live profile** requires deleting its `Singleton{Lock,Cookie,Socket}` files first; left in place, a second Chrome hands off to the first and exits without opening a debugging port. Relevant to `doctor` and to any future profile-migration helper.

### Technology stack

| Layer | Technology | Notes |
|---|---|---|
| Language | Python 3.14+ | |
| Browser | **Google Chrome, system install** | A prerequisite, not a download. Launched by the tool as a normal process; `playwright install chromium` is *not* used — its Chromium is challenged (see above) |
| Browser automation | Playwright (Python), `connect_over_cdp` | Attach only. Playwright never launches the browser |
| Answer extraction | CDP `Network.streamResourceContent` | Primary data path; `getResponseBody` and Playwright's `response.body()` both return nothing for a streaming body |
| CLI | Python stdlib `argparse` | No CLI framework dependency at this command count |
| Cross-process coordination | stdlib `fcntl.flock` on a lock file | Serializes concurrent runs; carries the last-request timestamp |
| Session storage | The Chrome profile directory itself | Chrome owns it; the tool exports no second copy of the session |
| Packaging | `pyproject.toml` (PEP 621) | `pip install git+https://github.com/aigerimzhalgasbekova/perplexity-client` — GitHub-only for v1 |
| Testing | `pytest` over dated fixtures, plus `pplx doctor` live check | Fixtures never run against the live site; see §7 |

## 3. User Stories

- **US-1: Ask a quick question**
  - As a developer, I want to run `pplx ask "..."` and get back Perplexity's answer with sources, so that I can automate my Pro account from a script.
  - Acceptance criteria:
    - Running `pplx ask "<query>"` after a successful `login` returns a human-readable answer with a list of cited sources by default.
    - `pplx ask "<query>" --json` returns the same data as structured JSON on stdout.
    - If no valid session exists, the command exits non-zero with a message naming the cause and telling the user to run `pplx login`.

- **US-2: Use it as a Python library from an agent**
  - As a developer building an AI agent, I want to import `Client` and call `.ask()` to get a structured `Response`, so that I can feed the answer and correctly-attributed citations into my own pipeline without parsing text.
  - Acceptance criteria:
    - `Client().ask("query")` returns a `Response` with `.text`, `.citations`, `.model`, `.mode`, `.thread_id`, `.complete`.
    - Inline `[n]` markers in `.text` resolve to `.citations[n-1]` (see the citation contract in §5).
    - No network/browser setup code is required beyond having run `login` once.

- **US-3: Never silently receive a truncated answer**
  - As a developer feeding results into an agent, I want a partial answer to be impossible to mistake for a complete one, so that a truncated response cannot enter my pipeline as fact.
  - Acceptance criteria:
    - `Response.complete` is `True` only when the terminal completion signal identified in M0 was observed.
    - `ask()` raises `IncompleteAnswerError` when the answer did not complete; callers who want partial output must opt in with `allow_incomplete=True`, which returns a `Response` with `.complete == False`.
    - The CLI exits non-zero on an incomplete answer and prints a truncation warning to stderr; `--allow-incomplete` opts into printing what was received.
    - A test asserts that a fixture representing a stream cut off mid-answer produces `complete == False` and raises by default.

- **US-4: Choose search mode vs. Deep Research**
  - As a user, I want to pick between fast Search and slower Deep Research, so that I can trade off speed for depth.
  - Acceptance criteria:
    - `mode="search"` blocks and returns a `Response`.
    - `mode="research"` returns a `ResearchTask` immediately; `.wait(timeout=...)` blocks until completion and returns a `Response`, or raises on timeout without cancelling the underlying task.
    - CLI `pplx ask "..." --mode research` polls and prints on completion, showing progress while waiting. Progress is real, not a spinner: the stream's plan block reports each goal as `IN_PROGRESS` or `DONE`.
    - Research may stop to ask clarifying questions; by default they are skipped so an unattended run never hangs (§5).

- **US-5: Walk away from a long Deep Research run**
  - As a user, I want a research task to survive the process that started it, so that a multi-minute run isn't lost to Ctrl-C, a crash, or a closed laptop.
  - Acceptance criteria:
    - `pplx ask "..." --mode research --detach` submits, prints the task id, and exits zero without waiting.
    - `pplx result <task_id>` retrieves a completed result, or reports still-running status, from a *different* process than the one that submitted it.
    - `Client().task(task_id)` reconstructs a `ResearchTask` from an id alone.
    - **M0 confirmed durability, so this story is in.** The id is `backend_uuid`, present on the first stream frame while the task is still `PENDING`, so `--detach` can print it and exit immediately without waiting for any result.

- **US-6: Pick a model and know which one answered**
  - As a user, I want to choose the underlying model and be certain which model actually served my answer, so that a silent server-side fallback can't go unnoticed.
  - Acceptance criteria:
    - `ask(query, model=...)` requests a specific model; omitting it defaults to `"best"` (Perplexity's auto-select).
    - `Response.model` is strictly the **observed** model — the one that served the answer — never an echo of the requested value.
    - When a specific model was requested and the observed model differs, `ask()` raises `ModelMismatchError` naming both. When `"best"` was requested, any observed model is valid and no mismatch check applies.

- **US-7: Recover from a broken or blocked session**
  - As a user, I want an unambiguous signal about my session's real state, so that I re-authenticate instead of debugging a confusing failure.
  - Acceptance criteria:
    - `pplx status` performs a real authenticated navigation to perplexity.ai and reports exactly one of: `ok`, `no-session`, `expired`, `challenged`.
    - `status` costs one page load, not one query, and this is documented in `--help`.
    - Any run that hits a login wall or a bot-detection/CAPTCHA challenge fails immediately with an error naming the cause and telling the user to run `pplx login` — the tool never attempts to solve or bypass the challenge.

- **US-8: Find out the scraper broke before my users do**
  - As the maintainer, I want a command that verifies the adapter against the live site, so that a Perplexity frontend change is detected by me rather than reported by a user.
  - Acceptance criteria:
    - `pplx doctor` runs one real query and asserts every adapter invariant: completion signal observed, citations parsed and index-mapped, `thread_id` returned, observed model reported.
    - It exits non-zero with the specific failed invariant named.
    - It is never run in CI (which has no account) and is documented as manual/scheduled.

## 4. Features

### Querying
- **Search mode** (default): synchronous ask/answer.
- **Deep Research mode**: asynchronous submit + poll/wait, with detach and resume-by-id (US-5).
- **Model selection**: optional `model`, defaulting to `"best"`; observed-model verification with mismatch as an error (US-6).
- **Multi-turn threads**: responses carry `thread_id`; passing it back continues that conversation.
- **Citations**: every `Response` includes cited sources, index-mapped to inline markers in the answer text.
- **Completeness guarantee**: incomplete answers raise rather than return (US-3).

### Interfaces
- Python library (`Client`) as the core.
- CLI (`pplx`) as a thin wrapper: `login`, `status`, `ask`, `result`, `doctor`. Human-readable by default; `--json` for structured output.

### Session and safety
- `pplx login`: one-time manual login in a visible browser; no password/2FA automation anywhere in the tool.
- No session file: Chrome keeps the rotated cookies in its own profile directory, and the tool writes no copy of them.
- No CAPTCHA or bot-detection bypass anywhere in the tool — an explicit non-goal, not an unimplemented feature.

### Resilience
- **Cross-process rate limiting**: a minimum interval between requests enforced via a persisted timestamp under an advisory lock, so concurrent `pplx ask` invocations serialize instead of stampeding the account. Configurable (`PPLX_MIN_INTERVAL`, default 20 s), with exponential backoff whose failure count is persisted in the lock file — an agent loop is a *fresh process* each iteration, so in-process backoff state would be discarded exactly when it matters — and stamped on lock acquire as well as release, since `SIGTERM` (what `timeout` and any supervisor send) skips `finally` and would otherwise leave a killed run's iteration entirely unpaced. The backoff raises the floor for callers that spend a query; it never delays a lock-only page load (`status`, `login`), and a `LocalError` — Chrome missing, profile busy — is not counted at all, because it cannot heal on its own to clear the debt it accrues. **A lock-only page load also cannot *clear* the debt (M3)**: it is exempt from waiting one out, so letting it reset one means diagnosing a problem erases the caution it earned — and since `status` returns `challenged` without raising, the most alarming thing the tool can report would otherwise reset the pacing that being challenged accrued. The interval is a local, conservative default, not a server-stated one: M2 established that Perplexity states no rate anywhere (§9).
- **Quota pre-flight**: `GET /rest/rate-limit/status` reports `available` per mode; a run refuses to spend a query into an exhausted mode rather than failing mid-stream. It is the only quota signal the account can see — no remaining count is exposed for the modes this tool uses.
- Adapter isolation: all Perplexity-specific stream parsing and DOM control lives behind one internal adapter module, so a frontend change is a localized patch.
- `pplx doctor` for live-site breakage detection (US-8).

All of the above is v1.

## 5. Data Model

| Entity | Field | Type | Description |
|---|---|---|---|
| `Response` | `text` | str | Answer text, retaining inline `[n]` citation markers |
| | `citations` | list[Citation] | Sources, ordered so `citations[n-1]` is marker `n` |
| | `model` | str | **Observed** model that served the answer |
| | `mode` | str | **Observed** mode: `"search"` \| `"research"`, the two this tool drives. **M3:** anything else is reported as the server's own value, lowercased, rather than folded into `"search"` — until mode *selection* lands (US-4, M4–6), `ask()` inherits the profile's UI setting, so this field is the only thing that makes a mismatch visible |
| | `thread_id` | str | Conversation thread this response belongs to |
| | `complete` | bool | `True` only if the terminal completion signal was observed |
| `Citation` | `url` | str | Source URL |
| | `title` | str | Source title |
| | `snippet` | str \| None | Cited excerpt; `None` when the stream did not carry one |
| `ResearchTask` | `task_id` | str | Handle for a Deep Research request. **M0 confirmed durability across processes**; it is `backend_uuid`, available on the first stream frame |
| | `status` | str | `"pending"` \| `"running"` \| `"awaiting_input"` \| `"done"` \| `"failed"` |
| | `progress` | list[tuple[str, str]] \| None | Per-goal `(description, "IN_PROGRESS"\|"DONE")` from the stream's plan block; `None` for search mode |
| | `questions` | list[Question] \| None | Set only in `awaiting_input`; see below |
| | `thread_id` | str | Thread the task belongs to |
| `Question` | `text` | str | The clarifying question |
| | `options` | list[str] | Offered choices |
| | `multi` | bool | Whether more than one may be selected |
| | `free_text` | bool | Whether a free-text answer is accepted |
| `Session` (on-disk) | — | the Chrome profile directory | Login session; owned by Chrome, not a Python-facing entity |
| `LockFile` (on-disk) | — | advisory lock; JSON `{last, fails}` at mode 600 | Cross-process pacing, backoff state, and browser-profile serialization. Written in place under the lock — never by atomic rename, which would swap the inode the lock is held on |

### Citation index contract

`text` retains Perplexity's inline `[n]` markers, and `citations[n-1]` is the source for marker `n`. Text and citations **must be captured from the same terminal payload** — never sampled at different moments — because Perplexity renumbers and appends sources while an answer streams. A marker with no corresponding entry in `citations` is an error, surfaced as such, not a silent drop. This invariant is covered by a fixture test and asserted live by `pplx doctor`.

**Markers are read out of the prose** (M3): fenced and inline code is stripped before the scan, because `nums[0]` and `arr[10]` are not citations and Perplexity is heavily used for programming questions. Enforcing the contract against raw markdown would discard a complete, correct answer after the query was already spent.

**The contract binds complete answers** (M3). A stream cut mid-answer may legitimately carry a marker whose source had not been delivered yet, so enforcing it on a partial answer would raise on output the caller explicitly asked for with `allow_incomplete=True`. Completeness is the guarantee that holds unconditionally; the citation contract holds wherever there is a complete answer to hold it over.

### Deep Research clarifying questions

**Deep Research may stop and ask the user clarifying questions before it runs**, and it blocks until they are answered or skipped. M0 observed a broad query triggering four multiple-choice questions while a narrow one ran straight through, so this is not predictable from the query alone and cannot be avoided by phrasing.

The trap: **the top-level `status` stays `PENDING` while it waits**, indistinguishable from working. A client keying on `status` alone hangs until its timeout. The real signal is `WORKFLOW_AWAITING_NEXT_STEPS` with `tool_name: "clarifying_questions"` inside the stream's workflow block, which is what `status == "awaiting_input"` above maps to.

The questions arrive structured — each with options, `allow_multichoice` and `allow_free_text` — alongside an `answer_submission_uuid` and a `response_endpoint`. **Amended by M6:** `response_endpoint` is a *handler name* (`handle_perplexity_research_clarifying_answers`), not a URL, and no request carrying it was observed in four research runs — so answers are driven through the DOM after all (`role=radio` per option, then Continue), which is the same control-only use of the page as everything else here. Free-text answers are not supported.

**Skipping costs no action at all** (M6): the payload carries `timeout_seconds: 60`, and a client that does nothing sees the workflow released with `responses: []` about a minute later. The stream's echo is `responses: []` whether the questions were answered or expired, so timing is the only evidence of which happened.

**Default behaviour is to skip**, because an unattended client is the primary use case (§1) and blocking on a question nobody will answer is the worse failure. Callers who want control pass `on_clarify=` to answer or to raise.

### Storage scope

No query or answer history is persisted. Only the login session and the pacing timestamp live on disk. Deep Research resumption works by passing a `task_id` back in, not by local task storage — the task lives on Perplexity's side.

## 6. API / Interface Design

### Python API
```
Client()
Client().login()                          # visible browser, one-time manual login
Client().status() -> "ok"|"no-session"|"expired"|"challenged"
Client().ask(query,
             mode="search",               # "search" | "research"
             model="best",
             thread_id=None,
             allow_incomplete=False,
             on_clarify="skip") -> Response | ResearchTask
                                          # "skip" | "raise" | callable(list[Question]) -> list[str]
Client().task(task_id) -> ResearchTask    # reconstruct from id; M0-confirmed

ResearchTask.wait(timeout=None) -> Response
ResearchTask.status -> str
ResearchTask.progress -> list[tuple[str, str]] | None
# Clarifying questions are answered inside `wait()` via `on_clarify=<callable>`. A
# standalone `answer()` method was built and removed: while a task is pending the
# thread document carries no workflow block, so a fresh process can never see
# `awaiting_input` in time to use it -- and the server's own 60s window retires the
# question anyway (docs/M4-M8-findings.md; adversarial review, 2026-08-01).
```

Errors: `IncompleteAnswerError`, `ModelMismatchError`, `SessionExpiredError`, `ChallengeEncounteredError`.

### CLI
```
pplx login
pplx status                                   # one page load; prints one of 4 states
pplx doctor                                   # one real query; asserts adapter invariants
pplx ask "<query>" [--mode search|research]
                    [--model <name>]
                    [--thread <thread_id>]
                    [--detach]                # research only: print task id, exit
                    [--allow-incomplete]
                    [--json]
pplx result <task_id> [--json]                # retrieve a detached research result
```

Default output is human-readable text plus a citations list; `--json` prints the structured equivalent. Truncation warnings go to stderr so they survive `--json | jq`.

## 7. Non-Functional Requirements

| Requirement | Target |
|---|---|
| Search-mode latency | Comparable to the web UI; no overhead beyond browser automation itself |
| Deep Research handling | Never blocks indefinitely by default; caller-configurable `.wait()` timeout; a timeout never cancels the underlying task |
| Answer integrity | An incomplete answer never returns as if complete; citation markers never misattribute (§5 contracts) |
| Concurrency safety | Concurrent same-account runs serialize via advisory lock; pacing floor holds across processes, not just within one `Client`. POSIX only: Windows has no `flock`, so runs there are unserialized and say so on stderr (M2). The session write is race-free on every platform regardless, via a per-pid temp name |
| Session durability | Rotated cookies written back atomically on clean exit; a crashed run never corrupts the session file |
| Security | Session file at mode 600; no credential ever touches tool code or logs; no CAPTCHA/bot-detection bypass under any circumstance |
| Breakage detection | `pplx doctor` catches live-site drift; fixtures carry a capture date so staleness is visible in review |
| Cost | Zero hosting/API cost; no browser download — Google Chrome is a prerequisite the user already has |
| Maintainability | No dependency beyond browser automation and packaging; all site-specific logic in one adapter module |

### Testing posture (explicit)

Unit and adapter tests run against **dated** recorded fixtures and cannot detect frontend drift — a green suite means the parser still handles the site *as of the fixture capture date*, nothing more. Live-site health is `pplx doctor`'s job: run manually before releases and on a scheduled job against the maintainer's own account. This split is stated in CONTRIBUTING so no one mistakes green CI for a working tool.

## 8. Out of Scope

- Official Perplexity API usage or fallback.
- Automated password/2FA/SSO entry — login is always a manual, one-time, visible-browser step.
- Solving, bypassing, or automating past CAPTCHAs or bot-detection challenges.
- Spaces/Collections support.
- Token-by-token streaming output to the caller. (The adapter reads the stream internally; it surfaces the completed answer.)
- Local caching or history of past queries and answers.
- Multi-account operation. Concurrent **same-account** runs are supported but serialized, not parallelized — parallel same-account querying is explicitly not a goal, since it is the fastest way to get an account flagged.
- PyPI publishing.
- Any GUI.

## 9. Milestones

**M0 — Protocol spike (gates everything else). ✅ Done — `docs/M0-findings.md`.** All five §2 questions answered against a live Pro session; fixtures in `spike/fixtures/`, claims asserted by `spike/verify_findings.py`. The stream path was confirmed, so the DOM fallback is not in effect. §2, §5, §9 and §10 have been amended with what it found — chiefly that Playwright must not launch the browser, and that Deep Research can block on clarifying questions.

**v1:**
1. Session bootstrap — `pplx login` and `pplx status` with its four states. **✅ Done.** The tool launches Google Chrome itself on its own profile (a free port per run, no fixed 9222) and reaps it afterwards, since `browser.close()` over CDP only disconnects. A live Chrome holding the profile is detected via its `SingletonLock` pid and named in the error, because otherwise a second launch hands off silently and the only symptom is a port timeout. The planned `storage_state` export was dropped: nothing ever read it back, so it was a credential-equivalent file on disk with no consumer.
2. Cross-process pacing — lock file, interval floor, backoff. **✅ Done — `docs/M2-findings.md`.** The premise that `GET /rest/rate-limit/status` states the server's own limit is **wrong**: probed live, it reports *availability per mode* and no rate, window or reset, and `remaining_detail.kind` is `"not_provided"` for exactly the two modes the tool uses. The interval floor is therefore a documented local default (20 s, `PPLX_MIN_INTERVAL`), and the endpoint is used as a pre-flight quota gate instead. The lock also fixes a real collision: two runs cannot share one Chrome profile, so they must queue rather than race.
3. Search-mode `ask()` — text, citations, observed model, completion signal, with the §5 contracts enforced and tested. **✅ Done — `docs/M3-findings.md`.** The premise that **two parsers are needed is wrong**: the SSE terminal frame and the plain-JSON resume entry carry the same `blocks` list, so one parser serves both and the resume path costs four lines. Two things the fixtures forced that the milestone did not anticipate: a partial answer has to be assembled by replaying the stream's chunk diffs (the finished markdown only ever appears on the terminal frame, so `allow_incomplete=True` would otherwise return an empty string), and the citation-index contract binds complete answers only. The wait also ends on the connection closing, not just on the terminal frame — a dropped stream otherwise cost the full answer timeout to report what the close already had.
4. Model selection with observed-model verification and `ModelMismatchError`. **✅ Done — `docs/M4-M8-findings.md`.** The premise that **entitlement is how you force a mismatch is wrong**: a model above the plan is not offered at all — it renders as a plain `menuitem` instead of a `menuitemradio` — so the request cannot be made, and is refused before a query is spent. A real mismatch turned up on the first legitimate attempt instead: Sonar 2 (`experimental`) was requested, confirmed selected, and `turbo` served the answer. Two further corrections: `GET /rest/models/config/v2` enumerates 87 search ids of which only ~12 are pickable (`search_config` is the menu, `models` is the registry), and the picker's button is named after the *selected* model, so it cannot be found by one name.
5. Multi-turn thread continuation via `thread_id`. **✅ Done — `docs/M4-M8-findings.md`.** The frontend links the turns itself (`query_source: "followup"`, `last_backend_uuid`) when the query is typed into the thread's own composer, so continuation is navigation plus the *last* textbox. This **corrects M3's `thread_id`**, which was `backend_uuid` — the per-turn id, identical to the thread's only until a second turn exists — and `parse_thread`, which took the first entry and would have returned the opening answer of every multi-turn thread.
6. Deep Research — async submit, `.wait()`, `--detach`, `pplx result`, `Client().task()`, plus the `awaiting_input` state and `on_clarify` handling (§5). **✅ Done — `docs/M4-M8-findings.md`.** Smaller than it looks in one way and larger in another: the stream parser is indeed shared, but §5's claim that clarifying answers go **through the API is wrong** — `response_endpoint` is a handler name, not a URL, and answering is a DOM wizard (`role=radio`, then Continue). Skipping needs no action at all: the payload's own `timeout_seconds: 60` expires and research continues. `GET /rest/thread/<uuid>` answers **without any blocks** unless asked with `with_schematized_response=true` and the frontend's `supported_block_use_cases` list.
7. CLI wrapper with human-readable default, `--json`, and correct exit codes. **✅ Done.** Adds exit code `3` for `pplx result` on a task that has not finished, so a polling loop can tell "not yet" from "broken".
8. `pplx doctor` plus dated fixtures and the CONTRIBUTING note on testing posture. `doctor` also checks for Chrome and reports its version (§10). **✅ Done.** It reports one row per invariant rather than raising on the first: which invariants still hold is what identifies the part of the frontend that moved.

**Future:** Spaces/Collections, caller-facing streaming, PyPI listing, optional local history.

## 10. Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Truncated answer returned as complete | **Critical** — a wrong-but-plausible answer enters an agent pipeline as fact, undetected | `Response.complete` derived from an explicit terminal signal; raises by default; fixture test for a mid-stream cutoff; asserted live by `doctor` |
| Perplexity changes its frontend/protocol | High — core functionality stops working | All site logic in one adapter; pinned Playwright; `pplx doctor` for detection; dated fixtures so staleness is visible |
| Citation misattribution | High — answer cites a real URL that doesn't support the claim; looks correct | Same-payload capture invariant (§5); unmapped marker is an error; fixture + live assertion |
| Concurrent runs stampede the account | Medium-high — pacing defeated in exactly the agent use case the tool targets | Interval floor persisted in a lock file, enforced with `flock` across processes, not per-`Client`. Two runs also cannot share the Chrome profile at all, so the lock is what makes them queue instead of collide |
| The pacing interval is a guess | Medium — too fast risks the account, too slow annoys; and there is no feedback signal | Accepted, not papered over: M2 confirmed the server states no rate, so nothing better exists. Chosen below the natural spacing of sequential use (~10–30 s per answer) so it only bites on rapid loops, documented as a guess, and overridable |
| Bot detection / account flagged | Medium-high — irreversible loss of a paid account | Conservative pacing by default; serialized same-account access; hard rule against bypassing any challenge; README states the account-owner risk plainly |
| Session file is credential-equivalent | Medium | Mode 600 set before atomic rename; documented in README as password-equivalent |
| Session file corrupted by concurrent writes | Medium — bricks the tool until manual re-login | Atomic `os.replace` under the same advisory lock as pacing |
| Silent server-side model substitution | Medium — task-critical model swap goes unnoticed | `Response.model` strictly observed; mismatch raises when a specific model was requested |
| Deep Research result lost with its process | Medium | **Resolved by M0** — `backend_uuid` arrives on the first frame and the thread is readable from a fresh process, so detach + resume-by-id (US-5) is buildable as specified |
| Deep Research blocks on a clarifying question nobody answers | **Medium-high** — an unattended agent hangs to its timeout and the run is wasted; invisible because `status` still reads `PENDING` | Detect `WORKFLOW_AWAITING_NEXT_STEPS`, surface as `status == "awaiting_input"`, skip by default (§5) |
| Chrome not installed, or a Chrome update breaks CDP attach | **Medium-high** — the tool cannot launch at all, and unlike a pinned Playwright browser this dependency updates itself under us | Chrome is a documented prerequisite; `pplx doctor` checks for the binary and for a working attach, and reports the Chrome version so a breaking update is identifiable |
| Cloudflare extends challenges to CDP-attached Chrome | **High** — the current approach stops working entirely and §8 forbids bypassing it | No mitigation available by design; the honest outcome is that the tool stops working and says so. `doctor` distinguishes "challenged" from other failures so the cause is unambiguous |
| ToS and maintainer-side legal exposure | **Medium-high, borne by the maintainer** — this is a public repo automating a service whose vendor sells API access separately. A disclaimer is a social protection, not a legal one, and the table should not price this at zero. | Framed and named throughout as *automating your own account* rather than API substitution; no resale, no multi-account, no bypass, no evasion; conservative pacing; README disclaims affiliation and places account-compliance responsibility on the user. Accepted as a conscious decision, not a checkbox. |
