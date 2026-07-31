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

from perplexity_client.errors import LockTimeoutError
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


@pytest.mark.parametrize("fails,expected", [(1, 5), (2, 10), (4, 40), (99, BACKOFF_CAP)])
def test_backoff_doubles_and_caps(fails, expected):
    # `fails` is unbounded on disk, so the shift must be capped too, not just the value.
    assert wait_for({"last": 1000.0, "fails": fails}, 0, now=1000.0) == expected


def test_a_backing_off_run_says_so(lock, capsys, monkeypatch):
    monkeypatch.setattr("perplexity_client.pacing.BACKOFF_BASE", 0.01)
    with pytest.raises(RuntimeError):
        with paced(lock):
            raise RuntimeError("transient")
    with paced(lock, interval=0.0):
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
    monkeypatch.setattr("perplexity_client.pacing.BACKOFF_BASE", 0.01)  # else 15s of sleep
    for expected in (1, 2):
        with pytest.raises(RuntimeError):
            with paced(lock):
                raise RuntimeError("transient")
        assert json.loads(lock.read_text())["fails"] == expected
    with paced(lock):  # a clean run clears the debt
        pass
    assert json.loads(lock.read_text())["fails"] == 0


def test_a_cancelled_run_is_not_a_failure(lock):
    # Ctrl-C is a user decision, not a transient error; charging it a backoff would
    # punish the person for stopping a run they did not want.
    with pytest.raises(KeyboardInterrupt):
        with paced(lock):
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
    procs = [subprocess.Popen(
        [sys.executable, "-c",
         CHILD.format(root=ROOT, path=str(lock), interval=interval, hold=hold)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
    runs = []
    for p in procs:
        out, err = p.communicate(timeout=60)
        assert p.returncode == 0, err
        runs.append(json.loads(out))
    runs.sort()
    (_, first_end), (second_start, _) = runs
    # Mutual exclusion, then the floor measured from the previous run's *release*.
    assert second_start >= first_end + interval


def test_lock_acquisition_times_out_instead_of_hanging(lock, monkeypatch):
    monkeypatch.setenv("PPLX_LOCK_TIMEOUT", "0.3")
    with paced(lock):  # flock is per open file description, so this blocks even us
        with pytest.raises(LockTimeoutError):
            with paced(lock):
                pass


def test_interval_default_reads_the_environment_at_call_time(monkeypatch):
    from perplexity_client import pacing
    monkeypatch.setenv("PPLX_MIN_INTERVAL", "3.5")
    assert pacing.default_interval() == 3.5
    monkeypatch.setenv("PPLX_MIN_INTERVAL", "nonsense")
    assert pacing.default_interval() == pacing.INTERVAL
