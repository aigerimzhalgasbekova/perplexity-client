"""Milestone 1: session file writes and status classification.

Fixture-free by design -- these cover the logic that must not break silently. Live
behaviour (does Chrome attach, does perplexity.ai answer) is `pplx status`'s job,
not pytest's; see the testing posture in PRD §7.
"""

import contextlib
import json
import os
import stat

import pytest

from perplexity_client import adapter, chrome, client
from perplexity_client.adapter import classify, is_challenge

GOOD_STATE = {"cookies": [{"name": "__Secure-next-auth.session-token", "value": "x"}]}
ANON_STATE = {"cookies": [{"name": "pplx.session-id", "value": "x"}]}


class FakeCtx:
    def __init__(self, state, pages=()):
        self.state = state
        self.pages = list(pages)

    def storage_state(self):
        return self.state

    def cookies(self):
        return self.state["cookies"]


class FakePage:
    def __init__(self, title="Perplexity", url=adapter.HOME, authed=True, quota=None):
        self._title, self.url, self._authed = title, url, authed
        self._quota = quota or {}

    def goto(self, url, **kw):
        pass

    def title(self):
        return self._title

    def evaluate(self, script, arg=None):
        return self._quota if arg == adapter.RATE_LIMIT else self._authed


@pytest.fixture(autouse=True)
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("PPLX_CONFIG_DIR", str(tmp_path / "cfg"))
    return tmp_path / "cfg"


def test_save_session_writes_owner_only(config):
    assert chrome.save_session(FakeCtx(GOOD_STATE)) is True
    path = chrome.session_path()
    assert json.loads(path.read_text()) == GOOD_STATE
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(config.glob("*.tmp"))  # the temp file was renamed, not left behind


def test_save_session_does_not_write_through_another_writers_temp_file(config):
    # The race this encodes: with one shared temp name, two overlapping writers use the
    # same path and the loser's os.replace dies with ENOENT after the winner renames it
    # away. A per-pid name makes another writer's temp none of our business.
    chrome.save_session(FakeCtx(GOOD_STATE))
    other = chrome.session_path().with_name("session.json.999999.tmp")
    other.write_text("another process, mid-write")
    assert chrome.save_session(FakeCtx(GOOD_STATE)) is True
    assert other.read_text() == "another process, mid-write"


def test_save_session_refuses_to_clobber_with_anonymous_state(config):
    chrome.save_session(FakeCtx(GOOD_STATE))
    assert chrome.save_session(FakeCtx(ANON_STATE)) is False
    assert json.loads(chrome.session_path().read_text()) == GOOD_STATE


def test_save_session_overwrites_rotated_cookies(config):
    chrome.save_session(FakeCtx(GOOD_STATE))
    rotated = {"cookies": [{"name": "__Secure-next-auth.session-token", "value": "y"}]}
    assert chrome.save_session(FakeCtx(rotated)) is True
    assert json.loads(chrome.session_path().read_text()) == rotated


@pytest.mark.parametrize("title,url,authed,expected", [
    ("Perplexity", "https://www.perplexity.ai/", True, "ok"),
    ("Perplexity", "https://www.perplexity.ai/", False, "expired"),
    # The auth probe answers 200 with an empty body from behind an interstitial,
    # so a challenge must outrank it -- otherwise a block reads as "expired" and
    # sends the user off to re-login for no reason.
    ("Just a moment...", "https://www.perplexity.ai/", False, "challenged"),
    ("Just a moment...", "https://www.perplexity.ai/", True, "challenged"),
    ("", "https://www.perplexity.ai/cdn-cgi/challenge-platform/x", True, "challenged"),
])
def test_classify(title, url, authed, expected):
    assert classify(title, url, authed) == expected


def test_is_challenge_ignores_ordinary_pages():
    assert not is_challenge("Perplexity", "https://www.perplexity.ai/")
    assert not is_challenge(None, None)


def fake_chrome(ctx):
    @contextlib.contextmanager
    def _chrome(headless=True, url="about:blank"):
        yield ctx, ctx.pages[0] if ctx.pages else None
    return _chrome


def test_status_without_profile_never_launches_a_browser():
    assert client.Client().status() == "no-session"


def test_status_reports_no_session_for_a_profile_that_never_logged_in(monkeypatch):
    # An abandoned `pplx login` leaves the profile dir behind. That empty profile
    # draws a Cloudflare interstitial, so deciding on session.json (which nothing
    # reads back) would report `challenged` to a user who simply never logged in.
    chrome.profile_dir().mkdir(parents=True)
    monkeypatch.setattr(client, "chrome", fake_chrome(FakeCtx(ANON_STATE)))
    assert client.Client().status() == "no-session"


def test_status_reports_ok_when_the_profile_carries_a_session(monkeypatch):
    chrome.profile_dir().mkdir(parents=True)
    monkeypatch.setattr(client, "chrome", fake_chrome(FakeCtx(GOOD_STATE, [FakePage()])))
    assert client.Client().status() == "ok"


def test_find_chrome_honours_env(monkeypatch):
    monkeypatch.setenv("PPLX_CHROME", "/nowhere/chrome")
    assert chrome.find_chrome() == "/nowhere/chrome"


def test_find_chrome_raises_when_absent(monkeypatch):
    monkeypatch.delenv("PPLX_CHROME", raising=False)
    monkeypatch.setattr(chrome.shutil, "which", lambda _: None)
    monkeypatch.setattr(chrome.os.path, "exists", lambda _: False)
    with pytest.raises(chrome.ChromeNotFoundError):
        chrome.find_chrome()


def test_profile_owner_pid_ignores_stale_lock(config):
    chrome.profile_dir().mkdir(parents=True)
    # A pid that cannot be alive: a crashed Chrome leaves its lock behind, and
    # treating that as "in use" would brick the tool until manual cleanup.
    os.symlink("host-2147483647", chrome.profile_dir() / "SingletonLock")
    assert chrome.profile_owner_pid() is None


def test_profile_owner_pid_detects_live_lock(config):
    chrome.profile_dir().mkdir(parents=True)
    os.symlink(f"host-{os.getpid()}", chrome.profile_dir() / "SingletonLock")
    assert chrome.profile_owner_pid() == os.getpid()


def test_profile_owner_pid_treats_another_users_chrome_as_live(config, monkeypatch):
    # PermissionError is the one signal that *proves* the process exists; reading it
    # as "stale lock" hands us back the opaque port timeout this check exists to avoid.
    chrome.profile_dir().mkdir(parents=True)
    os.symlink("host-4242", chrome.profile_dir() / "SingletonLock")

    def denied(pid, sig):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(chrome.os, "kill", denied)
    assert chrome.profile_owner_pid() == 4242
