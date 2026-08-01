"""Milestone 2: reading the account's only quota signal.

Against a dated capture of the live endpoint, per PRD §7 -- green here means the
parser still handles the site *as of that date*, nothing more.
"""

import json
import pathlib

import pytest

from perplexity_client import adapter, client
from perplexity_client.adapter import exhausted, quota

from test_session import FakeCtx, FakePage, fake_chrome  # noqa: E402

FIXTURE = json.loads(
    (pathlib.Path(__file__).parent / "fixtures" / "rate-limit-status-2026-07-31.json")
    .read_text())
GOOD_STATE = {"cookies": [{"name": "__Secure-next-auth.session-token", "value": "x"}]}


@pytest.fixture(autouse=True)
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("PPLX_CONFIG_DIR", str(tmp_path / "cfg"))


def test_quota_reads_availability_per_mode():
    # The capture: agentic_research was already used up on the probed Pro account,
    # which is why the exhausted path is not hypothetical.
    assert quota(FakePage(quota=FIXTURE)) == {
        "pro_search": True, "research": True, "agentic_research": False, "labs": True}


def test_exhausted_ignores_modes_this_tool_cannot_drive():
    # agentic_research is False in the fixture and must still not be reported.
    assert exhausted(FakePage(quota=FIXTURE)) == []


def test_exhausted_names_a_used_up_mode():
    spent = {"modes": {"pro_search": {"available": True},
                       "research": {"available": False}}}
    assert exhausted(FakePage(quota=spent)) == ["research"]


@pytest.mark.parametrize("body", [None, {}, {"modes": None}, "<html>", {"modes": {"x": 1}}])
def test_quota_is_advisory_and_never_raises(body):
    # A quota reading must not be able to fail a command: the endpoint is not part of
    # any contract, and an unparseable body is a warning we skip, not an error.
    assert quota(FakePage(quota=body)) == {}


def test_quota_survives_the_evaluate_itself_failing():
    # The probe's own `.catch` covers the *fetch*; it cannot cover Playwright tearing
    # the execution context down under it, which a client-side navigation does between
    # one probe and the next. Only the `ok` branch reads quota, so an escape here
    # crashes exactly the healthy sessions -- as a non-PplxError, which the CLI does
    # not map, so it surfaces as a traceback at the exit code meaning "not usable".
    class Destroyed(FakePage):
        def evaluate(self, script, arg=None):
            if arg == adapter.RATE_LIMIT:
                raise RuntimeError("Execution context was destroyed by a navigation")
            return True

    assert quota(Destroyed()) == {}
    assert exhausted(Destroyed()) == []


def test_status_warns_on_stderr_without_changing_the_state_word(monkeypatch, capsys):
    spent = {"modes": {"pro_search": {"available": False}}}
    from perplexity_client import chrome as chrome_mod
    chrome_mod.profile_dir().mkdir(parents=True)
    monkeypatch.setattr(client, "chrome",
                        fake_chrome(FakeCtx(GOOD_STATE, [FakePage(quota=spent)])))
    assert client.Client().status() == "ok"
    out = capsys.readouterr()
    assert out.out == ""  # stdout stays parseable: US-7's one word, printed by the CLI
    assert "search" in out.err
