# perplexity-client

Automate **your own** Perplexity account from Python and the shell.

Perplexity Pro does not include API access. If you already pay for Pro and want to
script your own queries, this drives a real, manually-authenticated browser session
on your machine — the same thing you would do by hand, done from a script. It is not
an API substitute, and it is not affiliated with Perplexity.

Status: **v1 feature-complete** — session bootstrap, cross-process pacing, search and
Deep Research, model selection, multi-turn threads, the full CLI, and `pplx doctor`.
See `docs/PRD.md`, and `docs/M*-findings.md` for what the live protocol actually does
as opposed to what it was assumed to do.

## Requirements

- Python 3.14+
- **Google Chrome**, installed. The tool launches your own Chrome as an ordinary
  process and attaches over CDP; it never downloads a browser and never adds
  automation switches.

## Install

```sh
pip install git+https://github.com/aigerimzhalgasbekova/perplexity-client
playwright install-deps   # not needed on macOS/Windows
```

## Use

```sh
pplx login                          # one-time manual login in a visible Chrome window
pplx status                         # ok | no-session | expired | challenged
pplx ask "what is a quokka"
pplx ask "what is a quokka" --json  # structured; warnings still go to stderr
pplx ask "..." --model "Sonar 2"    # or any model your plan offers; default "best"
pplx ask "..." --thread <id>        # continue that conversation
pplx ask "..." --mode research      # Deep Research: waits, showing real progress
pplx ask "..." --mode research --detach   # prints the task id and exits
pplx result <task_id>               # pick it up later, from any process
pplx doctor                         # one real query; checks every invariant
```

```python
from perplexity_client import Client

Client().status()   # "ok"

r = Client().ask("what is a quokka")
r.text        # the answer, with its inline [n] markers left in
r.citations   # [Citation(url=..., title=..., snippet=...)]; r.citations[n-1] is marker n
r.model       # the model that *served* it, never an echo of what was asked for
r.thread_id   # the thread; pass it back to continue the conversation
r.complete    # True only if Perplexity said the answer was finished

follow_up = Client().ask("and its predators?", thread_id=r.thread_id)

task = Client().ask("compare X and Y in depth", mode="research")   # returns at once
task.task_id                     # survives this process; `pplx result <id>` finds it
task.status                      # pending | running | awaiting_input | done | failed
task.progress                    # [(goal, "DONE" | "IN_PROGRESS"), ...] — real goals
answer = task.wait(timeout=900)  # a timeout raises; the task keeps running

Client().task(task_id).wait()    # same task, different process
```

`status` costs one page load, not one query. Exit codes: `0` ok, `1` session not
usable, `2` tool error, `3` (`pplx result` only) the task is not finished yet.

## Three things `ask` will not do quietly

**Return a truncated answer.** `ask()` raises `IncompleteAnswerError` unless
Perplexity signalled that the answer finished. A cut-off answer that reads as
complete is the one failure this tool takes seriously: it is wrong, it is plausible,
and nothing downstream can tell. Pass `allow_incomplete=True` to get what did arrive,
with `complete == False` on it.

**Return a citation that points at nothing.** `r.citations[n-1]` is the source for
marker `[n]`, and both come out of the same payload — Perplexity renumbers sources
while an answer streams, so sampling them a moment apart is how a claim ends up
attached to the wrong URL. A marker with no source raises `CitationError` rather than
being dropped. On a partial answer (`allow_incomplete=True`) the citations are
whatever had arrived when the stream was cut, and this check is not applied — a
source the answer cites may simply not have been delivered yet. Markers are read out
of the prose, not the raw markdown: `nums[0]` in a code block is not a citation.

**Let another model answer for the one you asked for.** `r.model` is always the model
that *served* the answer. If you named a model and a different one answered, `ask()`
raises `ModelMismatchError` naming both. This is not hypothetical: asking for Sonar 2
on a Pro account was answered by `turbo` ("Best") on the first attempt
(`docs/M4-M8-findings.md`). The default `model="best"` asks Perplexity to choose, so
nothing is a mismatch there. A model your plan does not include is refused *before* a
query is spent — the picker offers it as an upgrade, not as a choice.

`ask` refuses before it spends a query rather than after, generally: no session, an
expired one, a bot-detection challenge, a mode the account has used up, an unknown
model, or a mode/model selector that did not take, all fail up front.

## Deep Research asks questions

A research run may stop and ask you clarifying questions before it starts, and it is
not predictable from the query. By default they are **skipped** — an unattended run is
the point of this tool, and the server itself gives up waiting after 60 seconds, so
skipping costs a minute and nothing else.

```python
Client().ask("...", mode="research", on_clarify="skip")   # the default
Client().ask("...", mode="research", on_clarify="raise")  # ClarificationRequiredError,
                                                          # carrying .questions
Client().ask("...", mode="research",
             on_clarify=lambda qs: [q.options[0] for q in qs])   # answer them
```

A callable gets the parsed questions and returns one answer per question, each of which
must be one of that question's offered options — free-text answers are not supported.

## One account, one run at a time

Runs serialize across processes on an advisory lock — a second `pplx` waits for the
first rather than fighting it for the browser profile. Runs that spend a query also
wait out a minimum interval, and a failed run makes the next such run wait longer.
`status` and `login` only take the lock — they load a page rather than spend a query,
so they never wait out an interval or a backoff, however badly the last run went.

| Variable | Default | What |
|---|---|---|
| `PPLX_MIN_INTERVAL` | `20` | Seconds between queries. A local floor, not a server rule: Perplexity states no rate to its own account (`docs/M2-findings.md`) |
| `PPLX_LOCK_TIMEOUT` | `2100` | Seconds to wait for another run before giving up. Above the longest legitimate hold — a non-detached research wait (`PPLX_WAIT_TIMEOUT`) keeps the lock while it blocks |
| `PPLX_WAIT_TIMEOUT` | `1800` | Seconds a non-detached research `wait()` blocks before raising. The task keeps running server-side; `pplx result <id>` picks it up |
| `PPLX_ASK_TIMEOUT` | `180` | Seconds to wait for one answer. A ceiling, not an expectation — a search answer takes ~10–30 s; this is what stops a stalled stream from hanging an agent loop |
| `PPLX_SUBMIT_TIMEOUT` | `60` | Seconds to wait for a research task's *id*, which arrives on the first frame — not for its answer |

A `ResearchTask.wait()` holds the lock for as long as it waits, because it holds a
browser on the same profile. That is the trade for `--detach` being possible at all:
detach instead, and poll with `pplx result` from a process that only holds the lock for
a moment.

Turning the interval down is your risk to take: parallel hammering of one account is
the fastest way to get it flagged, and the account cannot see its own quota counter.

## What it stores

Under `~/.config/perplexity-client/` (override with `PPLX_CONFIG_DIR`):

| Path | What |
|---|---|
| `chrome-profile/` | The Chrome profile that carries your login |
| `pplx.lock` | Advisory lock, plus the last-run timestamp and failure count |

**The profile directory is password-equivalent.** Anyone who can read it can use
your account. The tool keeps no second copy of the session anywhere else.
The tool never sees, types or stores a password, 2FA code or SSO credential — login
is always manual, in a real browser window.

Versions up to 0.2.0 also wrote a `session.json` export that nothing ever read back.
Any run of a later version deletes it. If you copied or backed up your config
directory before upgrading, **that copy still holds a usable session** — delete it.

## What it will not do

No CAPTCHA or bot-detection bypass, ever. If Perplexity challenges the session, the
tool reports `challenged` and stops. No multi-account use, no resale, no parallel
hammering of one account — same-account runs are serialized on purpose. You are
responsible for your own account's compliance with Perplexity's terms.

## License

MIT — see [LICENSE](LICENSE).
