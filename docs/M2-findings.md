# M2 — Cross-process pacing: findings and design

**Probe date:** 2026-07-31 · **Account:** Perplexity Pro · **Tooling:** `spike/probe_rate_limit.py`
**Fixture:** `tests/fixtures/rate-limit-status-2026-07-31.json` (its `sources` block --
per-connector monthly caps, irrelevant to queries -- is dropped; `modes` and
`free_queries` are verbatim)

## Headline: the server does not state a rate

PRD §9 milestone 2 says to read `GET /rest/rate-limit/status` "rather than guessing an
interval — the server states its own limit." **It does not.** Probed live on a Pro
account, the endpoint states *availability per mode* and nothing about frequency:

```json
{"free_queries": {"available": true, "remaining_detail": {"kind": "not_provided"}},
 "modes": {
   "pro_search":        {"available": true,  "remaining_detail": {"kind": "not_provided"}},
   "research":          {"available": true,  "remaining_detail": {"kind": "not_provided"}},
   "agentic_research":  {"available": false, "remaining_detail": {"kind": "exact", "remaining": 0}},
   "labs":              {"available": true,  "remaining_detail": {"kind": "not_provided"}}},
 "sources": {"<connector>": {"available": bool, "remaining_detail": {…}}, …}}
```

Three things follow:

1. **No rate, no window, no reset time.** There is no requests-per-minute figure, no
   `Retry-After`, no reset timestamp — anywhere. `/rest/user/settings` and
   `/api/auth/session` were checked too: they carry per-connector *monthly* caps
   (`sources.source_to_limit.*`), `subscription_tier`, and upload limits, but no query
   quota. **The interval floor is therefore a local guess and must be documented as
   one.** The PRD line claiming otherwise is amended.
2. **`remaining_detail.kind` is `"not_provided"` for exactly the modes we use.** The
   server declines to state a remaining count for `pro_search` and `research`; it only
   gives exact numbers for things already exhausted or narrowly capped. So a
   "queries left today" readout is not buildable. `available` is the only field about
   our modes that is always present and always actionable.
3. **`available: false` is a real, observed state** — `agentic_research` was already
   exhausted on the probed account, with `remaining: 0`. So the exhaustion path is
   confirmed on live data, not assumed.

Published figures (reported, not verified here: ~200 Pro searches and ~20 Deep Research
runs per day on Pro) are background only — nothing in the tool rations against them,
because the account cannot see its own counter.

**What the endpoint is good for:** a pre-flight gate. It costs one `fetch` on a page
that is already open, so a run can refuse to spend a query into an exhausted mode
instead of failing confusingly mid-stream. That is how M3's `ask()` should use it.

## Design

### 1. The lock is the load-bearing part, and it fixes a bug that exists today

`chrome()` launches Chrome on a single profile directory. Two concurrent `pplx` runs
cannot share it: the second either trips `ProfileInUseError` or races on
`DevToolsActivePort`. So the advisory lock in PRD §2 is not only about pacing — it is
what makes concurrent runs *queue* instead of *collide*. Every `chrome()` call takes it.

`ProfileInUseError` stays, checked after the lock is acquired: it now means a Chrome
*outside* the tool (the user's own window on that profile), which waiting cannot fix.

### 2. Interval floor: local, conservative, and only for callers that spend quota

`paced(interval)` sleeps out the remainder of `interval` since the last run's release.
- `chrome()` defaults to `interval=0.0` — lock only, and lock only in every state:
  the backoff is exempted from it too (§3), not just the floor. `status` and `login`
  are page loads, not queries; making a user wait to check a broken session is hostile.
- M3's `ask()` passes `pacing.DEFAULT_INTERVAL` (20 s, `PPLX_MIN_INTERVAL` overrides).

**Why 20 s:** an answer takes ~10–30 s to generate (M0 captures), so sequential
human-ish use is barely affected; the floor only bites on the loop-in-an-agent case
PRD §10 names. It is anti-stampede, not quota rationing — the account cannot see its
own counter (finding 2), so rationing would be theatre.

The floor's only production caller arrives in M3. In M2 it is exercised by tests,
including a real two-process one.

### 3. Backoff: one integer, and it must outlive the process

A failure count lives in the lock file, so a *fresh* process backs off too — the agent
case is a shell loop, where in-process backoff state is discarded every iteration.

```
wait  = clamp(0, floor, last + floor - now)
floor = max(interval, 5 s · 2^(fails-1) capped at 60 s)   # when fails > 0 and interval > 0
```

Clean exit resets `fails` to 0; any `Exception` increments it. `KeyboardInterrupt` does
neither — cancelling a run is a decision, not a failure.

**The backoff raises a floor; it never creates one.** `interval > 0` guards it, so a
lock-only caller is exempt. Without that guard §2's rule and this one contradict each
other, and the one that loses is diagnosis: after five failed runs, `pplx status` — the
command you run *to investigate them* — slept the full 60 s cap before loading a page.
The failures still count, so they are owed by the next caller that actually spends a
query. Measured both ways in `test_a_lock_only_run_never_backs_off` and
`test_backoff_doubles_and_caps`.

**A local misconfiguration is not the account pushing back.** `errors.LocalError`
(`ChromeNotFoundError`, `ProfileInUseError`) is re-raised uncounted. The earlier trade —
"backing off costs one delayed run, and classifying every error is a branch that would
rot" — was wrong on its first clause: `fails` only clears on a *clean* run, and an error
that cannot heal on its own never produces one, so the cost is every run until the user
fixes it, each one slower than the last. One marker class is not a branch that rots.
Everything else stays unclassified, which is why the cap is **60 s rather than minutes**
and why a backing-off run says so on stderr.

### 3b. The stamp has to survive the thing that actually kills a run

`last` used to be written only on release. `SIGTERM` — what `timeout` and every
supervisor send, and a shell loop around `pplx` is exactly where those live — runs no
`finally` in CPython, so a killed run left the lock file it had just created **empty**:
zero state, meaning no floor *and* a laundered `fails`, in the one case most likely to be
looping. The stamp is now written on acquire as well (after the wait, before the body)
and again on release, so an interrupted run still spaces the next one — from its start
rather than its release, which is strictly more conservative than the nothing it left
before. Cost: one extra `pwrite` per run. `test_a_run_killed_mid_flight_still_stamps_the_clock`
kills a real child process.

**Clock:** wall clock, because two processes must compare timestamps; `time.monotonic`
is per-process. NTP can therefore move `last` into the future, so the wait is clamped
to at most one `floor` — a clock jump must never park the tool for a day.

### 4. Quota surfacing

`status` already holds an open page, so it reads `/rest/rate-limit/status` for free and
warns on **stderr** when `pro_search` or `research` is unavailable. Advisory means
advisory: the `page.evaluate` is wrapped, not just the `fetch` inside it, because a
client-side navigation can destroy the execution context between the auth probe and
this one. Only the `ok` branch reads quota, so an escape there would crash exactly the
*healthy* sessions — and as a non-`PplxError`, which the CLI does not map, so it would
surface as a traceback at exit code 1, the code that means "session not usable". Stdout keeps the
US-7 contract exactly: one word, one of `ok | no-session | expired | challenged`. Quota
is a different axis from session validity, so an exhausted mode does not change the
exit code.

`agentic_research` and `labs` are ignored: the tool cannot drive them, and the probed
account already had `agentic_research` exhausted — warning about a mode the tool never
uses is noise.

### Rejected

- **Deriving the interval from the endpoint** — the premise is false (headline).
- **A `pplx quota` command / public `Client.quota()`** — nothing in v1's CLI (§6) needs
  it, and M3 reads the endpoint from its own open page. Three lines to add when a
  caller exists.
- **Blocking `flock` with no deadline** — `login` can hold the lock for 10 minutes;
  an unattended `ask` deserves a bounded wait and a message naming the cause.

## What the lock actually fixed (verified, not assumed)

Two `pplx status` runs launched simultaneously against the live account both returned
`ok`, the second printing `waiting for another pplx run to finish...`.

The same two runs with the lock removed do **not** merely collide on Chrome's profile
singleton — they corrupt the session write. `save_session` builds a *shared* temp name
(`session.json.tmp`), so two overlapping writers race: one's `os.replace` moves the
file out from under the other, which then dies with `FileNotFoundError`. Reproduced
directly, without Chrome, by four threads calling `save_session` in a loop.

That is PRD §10's "Session file corrupted by concurrent writes" row, and its stated
mitigation — "atomic `os.replace` under the same advisory lock as pacing" — is only
true as of this milestone.

The temp name is now per-pid as well, which fixes the race *without* the lock. That is
not belt-and-braces for its own sake: `spike/capture.py` writes the session file
outside the lock entirely, and Windows has no lock at all (below). Re-verified with
four concurrent processes × 300 writes: no error, and no stray temp file left behind.

### Windows has no cross-process lock

`fcntl.flock` does not exist there. The import is guarded, so the package still
imports and every command still runs, but `_acquire` warns on stderr and returns:
concurrent runs are not serialized on Windows. The interval floor and the backoff
still function — they are only file I/O — they are merely racy.

The alternative was an `msvcrt.locking` path that nothing in this project can test
(M0 and M1 were both captured on macOS). Shipping an untested implementation of the
one mechanism that protects a credential-equivalent file is worse than stating the
gap. What remains is exactly the pre-M2 behaviour, plus a warning — not a regression.

## On-disk format (PRD §5 `LockFile`)

`~/.config/perplexity-client/pplx.lock`, mode 600, JSON: `{"last": <unix ts>,
"fails": <int>}`. Written under the exclusive lock, so no atomic-rename dance is
needed — and it must not be, since the rename would swap the inode the lock is held on.
An unparseable file (crash mid-write) is treated as empty state rather than an error.
