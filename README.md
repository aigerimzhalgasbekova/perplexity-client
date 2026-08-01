# perplexity-client

Automate **your own** Perplexity account from Python and the shell.

Perplexity Pro does not include API access. If you already pay for Pro and want to
script your own queries, this drives a real, manually-authenticated browser session
on your machine — the same thing you would do by hand, done from a script. It is not
an API substitute, and it is not affiliated with Perplexity.

Status: **milestone 3** — session bootstrap (`login`, `status`), cross-process pacing,
and search-mode `ask()` from Python. `pplx ask` on the command line lands in milestone
7; model selection, threads and Deep Research in 4–6. See `docs/PRD.md`.

## Requirements

- Python 3.10+
- **Google Chrome**, installed. The tool launches your own Chrome as an ordinary
  process and attaches over CDP; it never downloads a browser and never adds
  automation switches.

## Install

```sh
pip install git+https://github.com/<org>/perplexity-client
playwright install-deps   # not needed on macOS/Windows
```

## Use

```sh
pplx login     # one-time manual login in a visible Chrome window
pplx status    # ok | no-session | expired | challenged
```

```python
from perplexity_client import Client

Client().status()   # "ok"

r = Client().ask("what is a quokka")
r.text        # the answer, with its inline [n] markers left in
r.citations   # [Citation(url=..., title=..., snippet=...)]; r.citations[n-1] is marker n
r.model       # the model that *served* it, never an echo of what was asked for
r.thread_id   # the thread, for the multi-turn continuation coming in milestone 5
r.complete    # True only if Perplexity said the answer was finished
```

`status` costs one page load, not one query. Exit codes: `0` ok, `1` session not
usable, `2` tool error.

## Two things `ask` will not do quietly

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

`ask()` does not yet *choose* the mode — it types into the box and inherits whatever
the profile's Chrome UI is set to (selecting it lands in milestones 4–6). `r.mode` is
the mode that actually served the answer, and `ask` warns on stderr when that is not
`search`.

`ask` also refuses before it spends a query rather than after: no session, an expired
one, a bot-detection challenge, or a mode the account has used up each fail up front.

## One account, one run at a time

Runs serialize across processes on an advisory lock — a second `pplx` waits for the
first rather than fighting it for the browser profile. Runs that spend a query also
wait out a minimum interval, and a failed run makes the next such run wait longer.
`status` and `login` only take the lock — they load a page rather than spend a query,
so they never wait out an interval or a backoff, however badly the last run went.

| Variable | Default | What |
|---|---|---|
| `PPLX_MIN_INTERVAL` | `20` | Seconds between queries. A local floor, not a server rule: Perplexity states no rate to its own account (`docs/M2-findings.md`) |
| `PPLX_LOCK_TIMEOUT` | `900` | Seconds to wait for another run before giving up. Longer than `login`'s manual window |
| `PPLX_ASK_TIMEOUT` | `180` | Seconds to wait for one answer. A ceiling, not an expectation — a search answer takes ~10–30 s; this is what stops a stalled stream from hanging an agent loop |

Turning the interval down is your risk to take: parallel hammering of one account is
the fastest way to get it flagged, and the account cannot see its own quota counter.

## What it stores

Under `~/.config/perplexity-client/` (override with `PPLX_CONFIG_DIR`):

| Path | What |
|---|---|
| `chrome-profile/` | The Chrome profile that carries your login |
| `session.json` | Exported `storage_state`, mode 600 |
| `pplx.lock` | Advisory lock, plus the last-run timestamp and failure count |

**Both are password-equivalent.** Anyone who can read them can use your account.
The tool never sees, types or stores a password, 2FA code or SSO credential — login
is always manual, in a real browser window.

## What it will not do

No CAPTCHA or bot-detection bypass, ever. If Perplexity challenges the session, the
tool reports `challenged` and stops. No multi-account use, no resale, no parallel
hammering of one account — same-account runs are serialized on purpose. You are
responsible for your own account's compliance with Perplexity's terms.
