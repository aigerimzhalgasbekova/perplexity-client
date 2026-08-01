"""Milestone 2: the lock, the interval floor, and the backoff that outlives a process.

`wait_for` is pure, so most of this is arithmetic. The two things arithmetic cannot
prove -- that the lock actually excludes another *process*, and that it times out
rather than hanging forever -- get real tests, because they are the only reason this
module exists.
"""

import json
import os
import stat
import subprocess
import sys
import time

import pytest

from perplexity_client.errors import ChromeNotFoundError, LockTimeoutError
from perplexity_client.pacing import BACKOFF_CAP, paced, wait_for

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Prints the window it held the lock for, so the parent can check two runs never
# overlapped and were spaced by the floor.
CHILD = """
import json, sys, time
sys.path.insert(0, {root!r})
from perplexity_client.pacing import paced
with paced({path!r}, {interval!r}):
    start = time.time()
    time.sleep({hold!r})
    print(json.dumps([start, time.time()]))
"""


# Announces that it is inside the lock, then waits to be killed there.
HELD = """
import sys, time
sys.path.insert(0, {root!r})
from perplexity_client.pacing import paced
with paced({path!r}):
    print("holding", flush=True)
    time.sleep(30)
"""


@pytest.fixture
def lock(tmp_path):
    return tmp_path / "cfg" / "pplx.lock"


def test_wait_for_is_zero_when_nothing_ran():
    assert wait_for({}, 20, now=1000.0) == 0


def test_wait_for_returns_the_remainder():
    assert wait_for({"last": 990.0}, 20, now=1000.0) == pytest.approx(10)


def test_wait_for_is_zero_once_the_interval_has_passed():
    assert wait_for({"last": 900.0}, 20, now=1000.0) == 0


def test_wait_for_clamps_a_clock_that_jumped_backwards():
    # `last` is another process's wall clock; an NTP step must cost one interval, not
    # the six hours the raw arithmetic would ask for.
    assert wait_for({"last": 1_021_600.0}, 20, now=1000.0) == 20


@pytest.mark.parametrize(
    "fails,expected", [(1, 5), (2, 10), (4, 40), (99, BACKOFF_CAP)]
)
def test_backoff_doubles_and_caps(fails, expected):
    # `fails` is unbounded on disk, so the shift must be capped too, not just the value.
    # Interval is 1, not 0: a lock-only caller is exempt from the backoff entirely
    # (below), so 0 here would measure nothing.
    assert wait_for({"last": 1000.0, "fails": fails}, 1, now=1000.0) == expected


def test_a_lock_only_run_never_backs_off():
    # `status` and `login` are page loads, not queries. The backoff may raise the
    # floor a query-spending caller waits out; it must not invent one for the command
    # a user runs *to diagnose the failures that accrued it*.
    assert wait_for({"last": 1000.0, "fails": 5}, 0, now=1000.0) == 0


def test_a_backing_off_run_says_so(lock, capsys, monkeypatch):
    monkeypatch.setattr("perplexity_client.pacing.BACKOFF_BASE", 0.01)
    with pytest.raises(RuntimeError), paced(lock):
        raise RuntimeError("transient")
    with paced(lock, interval=0.001):
        pass
    assert "backing off" in capsys.readouterr().err


def test_backoff_never_shortens_the_interval():
    assert wait_for({"last": 1000.0, "fails": 1}, 20, now=1000.0) == 20


def test_paced_stamps_the_run_and_creates_an_owner_only_file(lock):
    before = time.time()
    with paced(lock):
        pass
    state = json.loads(lock.read_text())
    assert state["fails"] == 0
    assert before <= state["last"] <= time.time()
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600


def test_failure_count_survives_the_process_that_failed(lock, monkeypatch):
    monkeypatch.setattr(
        "perplexity_client.pacing.BACKOFF_BASE", 0.01
    )  # else 15s of sleep
    for expected in (1, 2):
        with pytest.raises(RuntimeError), paced(lock):
            raise RuntimeError("transient")
        assert json.loads(lock.read_text())["fails"] == expected
    with paced(lock, interval=0.01):  # a clean run that spent a query clears the debt
        pass
    assert json.loads(lock.read_text())["fails"] == 0


def test_a_page_load_cannot_clear_a_debt_it_never_pays(lock, monkeypatch):
    # `status` and `login` are exempt from *waiting out* a backoff, so they must also be
    # exempt from *clearing* one -- otherwise diagnosing a problem erases the caution it
    # earned. `status` returning "challenged" is a clean run: without this guard, the
    # single most alarming thing the tool can report resets the pacing that being
    # challenged accrued, and a diagnose-then-retry loop never backs off at all.
    monkeypatch.setattr("perplexity_client.pacing.BACKOFF_BASE", 0.01)
    with pytest.raises(RuntimeError), paced(lock, interval=0.01):
        raise RuntimeError("challenged")
    assert json.loads(lock.read_text())["fails"] == 1
    with paced(lock):  # a lock-only page load: no query spent, no debt cleared
        pass
    assert json.loads(lock.read_text())["fails"] == 1


def test_a_local_misconfiguration_is_not_a_failure(lock):
    # Chrome missing, or the user's own Chrome parked on the profile, is this machine
    # -- not the account pushing back. Counting it means every retry of the fix is
    # slower than the last, and the error never heals on its own to clear the debt.
    with pytest.raises(ChromeNotFoundError), paced(lock):
        raise ChromeNotFoundError("install Chrome")
    assert json.loads(lock.read_text())["fails"] == 0


def test_a_cancelled_run_is_not_a_failure(lock):
    # Ctrl-C is a user decision, not a transient error; charging it a backoff would
    # punish the person for stopping a run they did not want.
    with pytest.raises(KeyboardInterrupt), paced(lock):
        raise KeyboardInterrupt
    assert json.loads(lock.read_text())["fails"] == 0


def test_a_corrupt_lock_file_is_not_fatal(lock):
    lock.parent.mkdir(parents=True)
    lock.write_text("{half-written")
    with paced(lock):
        pass
    assert json.loads(lock.read_text())["fails"] == 0


def test_the_floor_is_waited_out_across_processes(lock, tmp_path):
    interval, hold = 0.6, 0.4
    procs = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                CHILD.format(root=ROOT, path=str(lock), interval=interval, hold=hold),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    runs = []
    for p in procs:
        out, err = p.communicate(timeout=60)
        assert p.returncode == 0, err
        runs.append(json.loads(out))
    runs.sort()
    (_, first_end), (second_start, _) = runs
    # Mutual exclusion, then the floor measured from the previous run's *release*.
    assert second_start >= first_end + interval


def test_a_run_killed_mid_flight_still_stamps_the_clock(lock):
    # SIGTERM is what `timeout` and any supervisor send, and a shell loop around
    # `pplx` is precisely where those live. Python runs no `finally` for it, so the
    # release stamp never lands -- and a lock file created by the killed run then
    # reads back as *empty*, leaving the next iteration with no floor and no backoff
    # in the one case most likely to be hammering the account.
    before = time.time()
    proc = subprocess.Popen(
        [sys.executable, "-c", HELD.format(root=ROOT, path=str(lock))],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "holding"
        proc.terminate()
        assert proc.wait(timeout=30) != 0
    finally:
        proc.stdout.close()
    state = json.loads(lock.read_text())
    assert before <= state["last"] <= time.time()


def test_lock_acquisition_times_out_instead_of_hanging(lock, monkeypatch):
    monkeypatch.setenv("PPLX_LOCK_TIMEOUT", "0.3")
    # flock is per open file description, so this blocks even us
    with paced(lock), pytest.raises(LockTimeoutError), paced(lock):
        pass


def test_interval_default_reads_the_environment_at_call_time(monkeypatch):
    from perplexity_client import pacing

    monkeypatch.setenv("PPLX_MIN_INTERVAL", "3.5")
    assert pacing.default_interval() == 3.5
    monkeypatch.setenv("PPLX_MIN_INTERVAL", "nonsense")
    assert pacing.default_interval() == pacing.INTERVAL
