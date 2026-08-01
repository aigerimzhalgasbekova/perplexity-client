# M4–M8 — model selection, threads, Deep Research: findings and design

**Source:** a live recon against the maintainer's own Pro account on **2026-08-01**,
after the M0/M3 fixtures turned out not to contain the answers. Probes:
`spike/probe_models.py` (page load only), `spike/probe_followup.py` (two search
queries), `spike/probe_clarify.py` (three research queries), `spike/probe_poll.py`
(one research query).
**Fixtures:** `spike/fixtures/models-config-2026-08-01.json`,
`research-clarify-2026-08-01.json`, `thread-multiturn-2026-08-01.json`.
**Asserted by:** `tests/test_models.py`, `tests/test_research.py`,
`tests/test_thread.py`, `tests/test_cli.py`.

## Headline: the mismatch PRD §9.4 could not find was there on the first try

Milestone 4 says: *"Force a real mismatch before trusting it — M0 confirmed
`display_model` and `user_selected_model` exist and agree on every capture, but never
saw them disagree… Requesting a model above the plan's entitlement is the likely way
to trigger one."*

Both halves turned out wrong, in opposite directions.

**The entitlement route is closed.** A model the plan cannot use is not refused after
the request — it is not *offerable*. In the picker, an included model renders as
`role="menuitemradio"` with `aria-checked`; an excluded one renders as a plain
`role="menuitem"` with a "Max" badge, and clicking it opens an upgrade prompt rather
than selecting anything. The same split is on the wire as
`search_config[].subscription_tier` (`pro` | `max`). So a Pro account cannot construct
a mismatch by over-reaching, and the tool refuses that request up front
(`ModelUnavailableError`) instead of spending a query to be told no.

**The mismatch happens anyway, on an entirely legitimate model.** Selecting **Sonar 2**
— `pro` tier, offered, selectable, confirmed checked in the DOM before submitting —
put `model_preference: "experimental"` on the wire, and the terminal frame came back:

```
user_selected_model : experimental      (what was asked for)
display_model       : turbo             (what answered)
```

`turbo` is labelled "Best — Adapts to each query". Perplexity silently routed the query
to auto-selection. This is exactly PRD §10's *"Silent server-side model substitution"*
row, and nothing in the answer betrays it: the text is fluent and correct either way.

`ask()` therefore raises `ModelMismatchError` naming both models. On this account, that
means asking for Sonar 2 may well *always* raise — which is the honest outcome, not a
bug to work around: the caller asked for a model and did not get it. `model="best"`
(the default) skips the check entirely, because auto-selection is the feature there.

## The picker button is named after its own state

`aria-label="Model"` only until a model is chosen. After that the button is labelled
with the selected model ("Sonar 2"), and a *thread* page's composer starts out labelled
"Best". Looking it up by one name works exactly once, on a fresh profile.

`pick_model` therefore searches every name the button can wear — `"Model"`, `"Best"`,
the requested label, and every label in the catalogue.

Two more consequences:

- **Selection persists in the profile across runs.** A run that picks Sonar 2 leaves the
  next run on Sonar 2. Mode and model are therefore always set explicitly, including
  when the answer is "best".
- **A thread composer resets to "Best"** regardless of the homepage, so continuing a
  thread with a specific model has to re-select on that page.

## Pointer clicks do not work on this menu; keypresses do

Neighbouring entries own submenus (the "Thinking" variants), and their poppers overlap
the target. Playwright's actionability check then retries the click until it times out:

```
<div role="menuitemradio" aria-haspopup="menu" …> from
<div data-radix-popper-content-wrapper> subtree intercepts pointer events
```

`locator.press("Enter")` focuses the entry and activates it without hit-testing. Every
selection is then **verified** by re-opening the menu and reading `aria-checked` before
the query is spent — a selection that silently fails is otherwise indistinguishable
from one that worked, and the bill arrives either way.

## `search_config` is the menu; `models` is a museum

`GET /rest/models/config/v2` carries both, and they are not the same list:

| | count | what it is |
|---|---|---|
| `models` | 87 search-mode ids | every model the site has ever shipped, id → label |
| `search_config` | 12 entries | what the picker actually offers, with tier |

Resolving a requested name against `models` finds plenty of ids that no menu will ever
show. `offered()` reads `search_config` and cross-checks each id against
`models[id].mode == "search"` — which is also what disambiguates the two entries both
labelled "Claude Sonnet 5", one of them being the browser agent's.

`display_model` is decoded through `models` for error messages only. `Response.model`
stays the raw observed id: the label is Perplexity's presentation of a model, the id is
the model.

## Multi-turn: the frontend links the turns, not us

Typing into a thread page's composer produces:

```
query_source      : "followup"        (vs "home")
last_backend_uuid : <previous entry's backend_uuid>
frontend_context_uuid: absent
```

None of which the tool sets. Continuing a conversation is therefore *navigate to the
thread and type in its box* — `adapter.thread_url()` plus `submit(follow_up=True)`,
which takes the **last** textbox on the page rather than the first (the first is the
original query, and typing there edits it).

The identifiers separate cleanly:

| field | scope | used for |
|---|---|---|
| `backend_uuid` | one turn | `ResearchTask.task_id` |
| `thread_url_slug` | the whole thread, stable as turns are added | `Response.thread_id` |
| `context_uuid` | the whole thread | not used |

**This corrects M3.** `Response.thread_id` was `backend_uuid`, which is the same value
on a one-turn thread and diverges the moment there are two — passing it back would have
continued from the wrong turn. `GET /rest/thread/<slug>` returns `entries` oldest-first,
so `parse_thread` now takes the **last** entry (or a named one), where M3 took the first
and would have returned the opening answer of every multi-turn thread.

## Deep Research: skipping a clarifying question costs nothing but a minute

PRD §5 has this as an API call: *"the questions arrive alongside an
`answer_submission_uuid` and a `response_endpoint`, so answers are submitted through
the API rather than driven through the DOM."*

`response_endpoint` is not a URL. Its value is
`handle_perplexity_research_clarifying_answers` — a handler *name*. No request
carrying it was ever observed, in four research runs, on any transport this capture
could see.

What *was* observed, twice:

- **Nobody has to answer.** The payload carries its own `timeout_seconds: 60`. Sixty-five
  seconds after the questions arrived, with the client doing nothing at all, the stream
  showed `WORKFLOW_ITEM_USER_RESPONSE` with `responses: []` and the workflow moved to
  `WORKFLOW_AWAITING_NEXT_STEPS`. **`on_clarify="skip"` is therefore literally waiting**
  — there is no Skip to press. (`spike/capture.py` pressed ⌘-Enter at t=24s in an M0 run
  and the release still did not come until t=99s, i.e. the timeout, not the shortcut.)
- **Answering is DOM.** One question is shown at a time; its options are `role="radio"`
  and a `Continue` button advances. Driving that on a three-question wizard released the
  workflow at t=26s against a t=19s arrival — well inside the 60s timeout, so the click
  path, not the timer, is what let research proceed.

One wrinkle worth writing down: the stream's echo of the answer is `responses: []`
**either way** — answered or expired. Timing is the only evidence of which happened, so
`answer_clarifiers` confirms `aria-checked` on each radio before pressing Continue. An
earlier attempt clicked the option's *text*, advanced happily, and submitted nothing;
Continue with nothing selected is indistinguishable from Skip.

**Not implemented:** free-text answers. An option list may end with an "Other" entry
(`is_free_text_selection: true`); answering through it was never captured, so an answer
that matches no offered option raises rather than guessing at a text field.

## Following a task: two sources, because neither is enough

### The thread document has to be asked properly

`GET /rest/thread/<uuid>`, requested bare, returns entries with **no `blocks` at all** —
ids, status and the query text. That reads exactly like "the answer is not ready" and
is really "you did not ask for it". The frontend sends:

```
?with_parent_info=true&with_schematized_response=true&version=2.18&source=default
&limit=10&offset=0&from_first=true&supported_block_use_cases=…(32 of them)
```

`with_schematized_response=true` is the switch. This cost an afternoon: the first
multi-turn fixture came back block-free and looked like a protocol change.

### …and even asked properly, a *running* entry has almost nothing

| entry state | blocks served |
|---|---|
| `PENDING` (running) | `answer_tabs`, `pending_followups` — no plan, no workflow |
| `COMPLETED` | `plan`, `ask_text`, `workflow_root`, `web_results`, … |

So the finished answer, `status`, and completion all come from the thread document —
and progress and clarifying questions **cannot**. Polling alone would make
`on_clarify` dead code.

### The live state comes from a reconnect stream — with different framing

Opening a running task's thread page subscribes to:

```
GET /rest/sse/perplexity_ask/reconnect/<backend_uuid>   →   text/event-stream
```

It matches `ASK_PATH` as a substring, so the tee binds to it unchanged — and then
parsed **zero frames** out of 4.4 KB of perfectly good data, across three live runs.
The first bytes say why:

```
b': hello\n\nevent:message\ndata:{"answer_modes":[],…
```

Two differences from `/rest/sse/perplexity_ask`, and each one alone is fatal:

| | ask stream | reconnect stream |
|---|---|---|
| frame separator | `\r\n\r\n` | `\n\n` |
| data line | `data: {…}` | `data:{…}` |

M0 established CRLF framing and it was true — of the one stream M0 looked at. The
parser now accepts either ending (`FRAME_SPLIT`) and `data:` with or without the space.

**This is the failure mode this project is most afraid of, in miniature.** Nothing
errored. Bytes arrived, the buffer filled, no exception was raised anywhere — and a
running research task simply reported no progress and no questions, indistinguishable
from a task that had not started. It was found by dumping 400 raw bytes, not by
reading code.

With the framing fixed, the stream is a live feed: one page load, then frames arrive
continuously (1 → 663 over 30s on a real run), goals fill in, and the state moves
`running` → `done`. So `wait()` opens the page once and listens, and polls the thread
document every 15s only to learn the task is over and collect the finished answer.

`entry_from_frames()` assembles those stream diffs into the same entry shape the thread
document eventually serves, so one set of readers (`task_status`, `plan_of`,
`questions_of`) works on both.

**The state trap is real and is handled.** The entry's own `status` stays `PENDING`
while research waits for an answer, so `task_status()` reads the workflow block
instead. A question counts as outstanding only until a `WORKFLOW_ITEM_USER_RESPONSE`
with the *same id* appears — a finished thread still carries the questions it asked an
hour ago, and taking their presence as "waiting" would strand every completed task.

Two smaller corrections:

- The thread endpoint lower-cases `status` (`completed`) where the stream shouts it
  (`COMPLETED`). M3's `parse_thread` compared exactly and would have called every
  resumed thread incomplete.
- **Sustained polling gets throttled.** At one poll every 5s, the endpoint began
  returning bodies with no entries after ~150 requests. `POLL_SECONDS` is 15, and a
  poll that comes back empty is kept rather than believed — otherwise a throttle reads
  as the task vanishing.

## What this cost, and what it did not answer

Seven search queries and seven research queries on the maintainer's own account.
Nothing here bypasses a challenge, spoofs a fingerprint, or automates a credential
(PRD §8); the probes drive the same buttons a person would.

Verified live, end to end, with the shipped code rather than an imitation of it:

- `pplx doctor` — every invariant green against the live site (Chrome 150, session,
  completion signal, 10 citations index-mapped, thread id, observed model, mode).
- `Client().ask(mode="research")` → id in ~9s → `wait()` → complete answer with
  citations, `model == "pplx_alpha"`, `mode == "research"`.
- Model selection: Sonar 2 picked, confirmed checked, and the substitution caught.
- Multi-turn: a second turn linked by the frontend and read back correctly.
- Clarifying questions: answered through the wizard (release at t=26s against a 60s
  timeout, so the clicks did it), and skipped by doing nothing (release at t=99s
  against questions at t=34s — the timeout did it).

Still unobserved:

- the request that submits a clarifying answer. The DOM path works and is what ships;
  the wire shape is unknown, so nothing here reproduces it directly.
- free-text clarifying answers ("Other" + a text field).
- whether `GET /rest/thread/<entry_uuid>` resolves for a *non-first* entry of a
  multi-turn thread — every code path uses the slug, so it never has to.
- whether the reconnect stream survives a research run of tens of minutes, or drops
  and needs re-opening. The 15s poll of the thread document is the backstop either
  way: a dropped stream costs progress reporting, never the answer.
