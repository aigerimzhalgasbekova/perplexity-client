"""Cross-process pacing: one advisory lock, an interval floor, persisted backoff.

Nothing here knows about Perplexity or about browsers. It enforces two rules for the
whole machine, not for one `Client`: only one run at a time, and not too fast.

The lock is load-bearing beyond pacing -- two runs cannot share one Chrome profile
directory at all, so without it a concurrent run collides instead of queueing.

Both pieces of state live *in* the locked file rather than beside it, so there is
nothing to keep in sync. It is written in place under the exclusive lock, never by
atomic rename: a rename would swap the inode every waiting process is queued on.
"""

import contextlib
import errno
import json
import os
import pathlib
import sys
import time
from collections.abc import Iterator

from .errors import LocalError, LockTimeoutError

try:
    import fcntl
except ImportError:  # Windows: no flock. Degrade to pre-M2 behaviour, loudly.
    fcntl = None  # type: ignore[assignment]

# Via a flag rather than `fcntl is None` at the use site: mypy types the name from the
# successful import and would call the Windows branch dead code.
HAVE_FLOCK = fcntl is not None

# A guess, and documented as one: M2 established that Perplexity states no rate to its
# own account (docs/M2-findings.md), so there is nothing better to derive it from. An
# answer takes ~10-30s to generate, so this barely touches sequential use and only
# bites the rapid-loop case that PRD §10 names.
INTERVAL = 20.0
# Longer than `login`'s 10-minute manual window, which holds the lock the whole time.
LOCK_TIMEOUT = 900.0
BACKOFF_BASE = 5.0
# Deliberately close to the interval floor. Failures are not classified (see `paced`),
# so a local misconfiguration accrues backoff exactly like a sulking server does; a cap
# of minutes would make the tool look hung long after the user had fixed the cause.
BACKOFF_CAP = 60.0


def env_float(name: str, default: float) -> float:
    """Read at call time, not import time -- otherwise tests and shells that set the
    variable after import silently get the default."""
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def default_interval() -> float:
    return env_float("PPLX_MIN_INTERVAL", INTERVAL)


def wait_for(state: dict[str, float], interval: float, now: float) -> float:
    """Seconds to sleep before the next run may start."""
    floor = interval
    # `interval and ...`: the backoff may raise a floor, never create one. A lock-only
    # caller (`interval=0` -- `status`, `login`) is a page load, not a query, and must
    # stay fast to diagnose with even when the last runs failed. Its failures still
    # *count*, so they slow the next caller that does spend quota.
    if interval and (fails := state.get("fails", 0)):
        # Capped exponent as well as value: `fails` persists across runs and an
        # unbounded shift would be a silly way to hang.
        floor = max(floor, min(BACKOFF_CAP, BACKOFF_BASE * 2 ** min(fails - 1, 20)))
    # `last` is another process's *wall* clock (monotonic is not comparable across
    # processes), so an NTP step can put it in the future. Clamping to a single floor
    # means a clock jump costs one interval, not a day.
    return max(0.0, min(floor, state.get("last", 0.0) + floor - now))


def _read(fd: int) -> dict[str, float]:
    """`{last, fails}`, normalised. A file half-written by a crash reads as empty."""
    try:
        state = json.loads(os.pread(fd, 4096, 0) or b"{}")
        return {
            "last": float(state.get("last", 0)),
            "fails": int(state.get("fails", 0)),
        }
    except (ValueError, TypeError, AttributeError, OSError):
        return {"last": 0.0, "fails": 0}


def _write(fd: int, last: float, fails: int) -> None:
    # No fsync: losing the stamp to a power cut costs one un-paced run, and fsyncing
    # every run for that is not a trade worth making.
    os.ftruncate(fd, 0)
    os.pwrite(fd, json.dumps({"last": last, "fails": fails}).encode(), 0)


def _acquire(fd: int, path: pathlib.Path) -> None:
    if not HAVE_FLOCK:
        # Shipping an untested msvcrt implementation of the thing that protects the
        # session file would be worse than saying so. The floor and the backoff below
        # still work (they are just file I/O), they are merely racy.
        print(
            "warning: no cross-process lock on this platform -- concurrent pplx "
            "runs are not serialized",
            file=sys.stderr,
        )
        return
    deadline = time.monotonic() + env_float("PPLX_LOCK_TIMEOUT", LOCK_TIMEOUT)
    notified = False
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as e:
            if e.errno not in (errno.EACCES, errno.EAGAIN):
                raise
            if time.monotonic() > deadline:
                raise LockTimeoutError(
                    f"another pplx run has held {path} for too long. If none is "
                    f"running, delete that file."
                ) from e
            if not notified:  # a silent multi-minute wait reads as a hang
                print("waiting for another pplx run to finish...", file=sys.stderr)
                notified = True
            time.sleep(0.2)


@contextlib.contextmanager
def paced(path: str | pathlib.Path, interval: float = 0.0) -> Iterator[None]:
    """Hold the lock for the whole block, after waiting out the interval floor.

    `interval=0` is lock-only, which is what a page load (`status`, `login`) wants;
    callers that spend a query pass `default_interval()`.

    Not re-entrant: a nested `paced()` on the same path deadlocks against itself.
    """
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        _acquire(fd, path)
        state = _read(fd)
        if wait := wait_for(state, interval, time.time()):
            if state["fails"]:  # the floor is expected and silent; a backoff is not
                print(
                    f"backing off {wait:.0f}s after {state['fails']} failed run(s)...",
                    file=sys.stderr,
                )
            time.sleep(wait)
        fails = int(state["fails"])
        # Stamped again on release below; this one is for the run that never gets
        # there. SIGTERM -- what `timeout` and any supervisor send, and the agent loop
        # is exactly where those live -- skips `finally` entirely, which used to leave
        # a freshly created lock file *empty*: no floor and no backoff for the next
        # iteration, in the one case most likely to be looping.
        _write(fd, time.time(), fails)
        try:
            yield
        except LocalError:
            raise  # this machine, not the account: see errors.LocalError
        except Exception:
            fails += 1  # a fresh process each iteration is the agent case, so the
            raise  # count has to outlive us to mean anything
        else:
            # Only a run that actually spent a query may clear the debt. A lock-only
            # page load is already exempt from *waiting out* a backoff (see `wait_for`),
            # so letting it *clear* one means diagnosing a problem erases the caution it
            # earned -- and `status` returns "challenged" without raising, so the single
            # most alarming thing this tool can report would reset the pacing that being
            # challenged accrued. Diagnose-then-retry is the natural agent loop.
            # A successful `login` was weighed on the same rule and rejected: it is the
            # one event that really does prove the earlier failures are fixed, but
            # exempting it makes the exemption a property of the command rather than of
            # spending a query -- and the 60s cap keeps what that costs small.
            if interval:
                fails = 0
        finally:
            # Stamped on release, not on acquire: the lock is held for the whole run,
            # so "time since the last run finished" is the only spacing that exists.
            # KeyboardInterrupt lands here too, leaving `fails` untouched -- a user
            # cancelling is not a transient failure.
            _write(fd, time.time(), fails)
    finally:
        os.close(fd)  # releases the flock
