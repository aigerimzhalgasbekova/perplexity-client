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
   │  pplx    │ ---> │  Playwright (headed)     │ ---> user logs in manually
   │  login   │      │  Chromium window          │      (password/SSO/2FA —
   └──────────┘      └─────────────────────────┘       whatever the account needs)
                              │
                              ▼
                  storage_state written atomically to
                  ~/.config/perplexity-client/session.json   (mode 600)
                  ~/.config/perplexity-client/pplx.lock      (advisory lock +
                                                              last-request stamp)

   ┌──────────────────┐        ┌────────────────────────────────┐
   │ Python caller /   │ -----> │ Client                          │
   │ CLI (`pplx ask`)  │        │  1. acquire flock on pplx.lock  │
   └──────────────────┘        │  2. enforce min-interval floor  │
                                │  3. load session, launch        │
                                │     headless context            │
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

**This decision is gated on Milestone 0 (§9), a devtools spike against a real Pro account.** The spike is not a formality — the protocol has not been inspected yet, and this PRD deliberately does not name specific frame types, event names, or payload shapes, because none have been verified. M0 must establish and document:

1. That the answer stream is interceptable from a Playwright context (via WebSocket framing events, response interception, or equivalent).
2. Which frame/event constitutes the **terminal completion signal**, by name.
3. Whether answer text and citations arrive in a single terminal payload, or must be assembled across frames — and if assembled, how index integrity is preserved.
4. Whether the model that actually served the answer is observable in the stream.
5. Whether a Deep Research task id maps to a durable, re-navigable thread URL (see §6).

**Fallback, if M0 disproves the above:** fall back to DOM extraction and record the consequences explicitly in this document rather than silently: `Response.complete` degrades to a best-effort heuristic, and the citation-index contract weakens from *guaranteed* to *best-effort*. A DOM-settle-timer masquerading as a completion signal is **not** an acceptable implementation of `complete` — if it comes to that, `complete` must be documented as heuristic in both the docstring and the README.

- **Deployment model:** Runs entirely on the user's own machine. No server component, no hosted infrastructure. Each user authenticates their own Pro account locally; sessions are never shared or transmitted anywhere except to perplexity.ai itself.
- **Session lifecycle:** `pplx login` opens a real, visible Chromium window for a one-time manual login (handles whatever auth the account needs — password, SSO, 2FA — without the tool ever touching credentials). The resulting Playwright `storage_state` is persisted locally and reused headlessly. On clean exit of any run, the possibly-rotated state is written back atomically under the advisory lock, so cookie rotation extends session life instead of being discarded. On expiry, revocation, or a bot-detection challenge, the tool fails loudly and instructs the user to re-run `login`.

### Technology stack

| Layer | Technology | Notes |
|---|---|---|
| Language | Python 3.10+ | |
| Browser automation | Playwright (Python), Chromium | Version-pinned. One-time `playwright install chromium` (~300MB) |
| Answer extraction | Playwright network/WebSocket interception | Primary data path; see decision above |
| CLI | Python stdlib `argparse` | No CLI framework dependency at this command count |
| Cross-process coordination | stdlib `fcntl.flock` on a lock file | Serializes concurrent runs; carries the last-request timestamp |
| Session storage | Playwright `storage_state` JSON, atomic write (`os.replace`) | Owner-only permissions (600), set before rename |
| Packaging | `pyproject.toml` (PEP 621) | `pip install git+https://github.com/<org>/perplexity-client` — GitHub-only for v1 |
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
    - CLI `pplx ask "..." --mode research` polls and prints on completion, showing progress while waiting.

- **US-5: Walk away from a long Deep Research run**
  - As a user, I want a research task to survive the process that started it, so that a multi-minute run isn't lost to Ctrl-C, a crash, or a closed laptop.
  - Acceptance criteria:
    - `pplx ask "..." --mode research --detach` submits, prints the task id, and exits zero without waiting.
    - `pplx result <task_id>` retrieves a completed result, or reports still-running status, from a *different* process than the one that submitted it.
    - `Client().task(task_id)` reconstructs a `ResearchTask` from an id alone.
    - If M0 (§2) proves task ids are not durably re-navigable, this story is cut and `task_id` is renamed to signal process-locality — it must not be shipped implying durability it lacks.

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
- Atomic session write-back on clean exit, preserving rotated cookies.
- No CAPTCHA or bot-detection bypass anywhere in the tool — an explicit non-goal, not an unimplemented feature.

### Resilience
- **Cross-process rate limiting**: a minimum interval between requests enforced via a persisted timestamp under an advisory lock, so concurrent `pplx ask` invocations serialize instead of stampeding the account. Configurable, with backoff on transient failures.
- Adapter isolation: all Perplexity-specific stream parsing and DOM control lives behind one internal adapter module, so a frontend change is a localized patch.
- `pplx doctor` for live-site breakage detection (US-8).

All of the above is v1.

## 5. Data Model

| Entity | Field | Type | Description |
|---|---|---|---|
| `Response` | `text` | str | Answer text, retaining inline `[n]` citation markers |
| | `citations` | list[Citation] | Sources, ordered so `citations[n-1]` is marker `n` |
| | `model` | str | **Observed** model that served the answer |
| | `mode` | str | `"search"` \| `"research"` |
| | `thread_id` | str | Conversation thread this response belongs to |
| | `complete` | bool | `True` only if the terminal completion signal was observed |
| `Citation` | `url` | str | Source URL |
| | `title` | str | Source title |
| | `snippet` | str \| None | Cited excerpt; `None` when the stream did not carry one |
| `ResearchTask` | `task_id` | str | Handle for a Deep Research request; durable across processes iff M0 confirms |
| | `status` | str | `"pending"` \| `"running"` \| `"done"` \| `"failed"` |
| | `thread_id` | str | Thread the task belongs to |
| `Session` (on-disk) | — | Playwright `storage_state` JSON | Login session; not a Python-facing entity |
| `LockFile` (on-disk) | — | advisory lock + last-request timestamp | Cross-process pacing and session-write serialization |

### Citation index contract

`text` retains Perplexity's inline `[n]` markers, and `citations[n-1]` is the source for marker `n`. Text and citations **must be captured from the same terminal payload** — never sampled at different moments — because Perplexity renumbers and appends sources while an answer streams. A marker with no corresponding entry in `citations` is an error, surfaced as such, not a silent drop. This invariant is covered by a fixture test and asserted live by `pplx doctor`.

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
             allow_incomplete=False) -> Response | ResearchTask
Client().task(task_id) -> ResearchTask    # reconstruct from id (gated on M0)

ResearchTask.wait(timeout=None) -> Response
ResearchTask.status -> str
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
| Concurrency safety | Concurrent same-account runs serialize via advisory lock; pacing floor holds across processes, not just within one `Client` |
| Session durability | Rotated cookies written back atomically on clean exit; a crashed run never corrupts the session file |
| Security | Session file at mode 600; no credential ever touches tool code or logs; no CAPTCHA/bot-detection bypass under any circumstance |
| Breakage detection | `pplx doctor` catches live-site drift; fixtures carry a capture date so staleness is visible in review |
| Cost | Zero hosting/API cost; one-time ~300MB Chromium download |
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

**M0 — Protocol spike (gates everything else).** Inspect a real Pro session in devtools and answer the five questions in §2. Output is a short written finding committed alongside this PRD, plus a captured fixture. If the stream path is disproven, update §2, §5, and §10 to record the DOM-fallback consequences *before* writing adapter code.

**v1:**
1. Session bootstrap — `pplx login`, atomic write-back, `pplx status` with its four states.
2. Cross-process pacing — lock file, interval floor, backoff.
3. Search-mode `ask()` — text, citations, observed model, completion signal, with the §5 contracts enforced and tested.
4. Model selection with observed-model verification and `ModelMismatchError`.
5. Multi-turn thread continuation via `thread_id`.
6. Deep Research — async submit, `.wait()`, `--detach`, `pplx result`, `Client().task()`.
7. CLI wrapper with human-readable default, `--json`, and correct exit codes.
8. `pplx doctor` plus dated fixtures and the CONTRIBUTING note on testing posture.

**Future:** Spaces/Collections, caller-facing streaming, PyPI listing, optional local history.

## 10. Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Truncated answer returned as complete | **Critical** — a wrong-but-plausible answer enters an agent pipeline as fact, undetected | `Response.complete` derived from an explicit terminal signal; raises by default; fixture test for a mid-stream cutoff; asserted live by `doctor` |
| Perplexity changes its frontend/protocol | High — core functionality stops working | All site logic in one adapter; pinned Playwright; `pplx doctor` for detection; dated fixtures so staleness is visible |
| Citation misattribution | High — answer cites a real URL that doesn't support the claim; looks correct | Same-payload capture invariant (§5); unmapped marker is an error; fixture + live assertion |
| Concurrent runs stampede the account | Medium-high — pacing defeated in exactly the agent use case the tool targets | Interval floor persisted in a lock file, enforced with `flock` across processes, not per-`Client` |
| Bot detection / account flagged | Medium-high — irreversible loss of a paid account | Conservative pacing by default; serialized same-account access; hard rule against bypassing any challenge; README states the account-owner risk plainly |
| Session file is credential-equivalent | Medium | Mode 600 set before atomic rename; documented in README as password-equivalent |
| Session file corrupted by concurrent writes | Medium — bricks the tool until manual re-login | Atomic `os.replace` under the same advisory lock as pacing |
| Silent server-side model substitution | Medium — task-critical model swap goes unnoticed | `Response.model` strictly observed; mismatch raises when a specific model was requested |
| Deep Research result lost with its process | Medium | Detach + resume-by-id (US-5), gated on M0; if unavailable, durability is not claimed |
| ToS and maintainer-side legal exposure | **Medium-high, borne by the maintainer** — this is a public repo automating a service whose vendor sells API access separately. A disclaimer is a social protection, not a legal one, and the table should not price this at zero. | Framed and named throughout as *automating your own account* rather than API substitution; no resale, no multi-account, no bypass, no evasion; conservative pacing; README disclaims affiliation and places account-compliance responsibility on the user. Accepted as a conscious decision, not a checkbox. |
