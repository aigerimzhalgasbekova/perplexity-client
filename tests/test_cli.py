"""Milestone 7: the CLI wrapper -- argument wiring, output shape, exit codes.

`Client` is stubbed throughout. What is worth testing here is that flags reach the
call unchanged, that stdout stays machine-readable under `--json`, and that a caller
can tell "log in again" from "the tool broke" by the exit code alone (PRD §6).
"""

import json

import pytest

from perplexity_client import cli
from perplexity_client.adapter import Citation, Response
from perplexity_client.errors import (
    ChallengeEncounteredError,
    IncompleteAnswerError,
    ModelMismatchError,
    PplxError,
    SessionExpiredError,
)
from perplexity_client.research import ResearchTask

ANSWER = Response(
    text="Canberra is the capital[1].",
    citations=[Citation(url="https://example.com/a", title="A", snippet=None)],
    model="pplx_pro",
    mode="search",
    thread_id="thread-1",
    complete=True,
)


class FakeClient:
    """Records the call, hands back whatever the test set up."""

    calls: list[dict] = []
    result: object = ANSWER
    raises: Exception | None = None
    state: str = "ok"

    def ask(self, query, **kw):
        FakeClient.calls.append({"query": query, **kw})
        if FakeClient.raises:
            raise FakeClient.raises
        return FakeClient.result

    def task(self, task_id):
        return FakeClient.result

    def status(self):
        return FakeClient.state

    def doctor(self):
        return FakeClient.result


@pytest.fixture(autouse=True)
def stub(monkeypatch):
    FakeClient.calls = []
    FakeClient.result = ANSWER
    FakeClient.raises = None
    FakeClient.state = "ok"
    monkeypatch.setattr(cli, "Client", FakeClient)
    return FakeClient


# --- ask ----------------------------------------------------------------------


def test_ask_passes_every_flag_through(capsys):
    assert (
        cli.main(
            [
                "ask",
                "why",
                "--mode",
                "research",
                "--model",
                "Sonar 2",
                "--thread",
                "t-9",
                "--allow-incomplete",
            ]
        )
        == 0
    )
    assert FakeClient.calls == [
        {
            "query": "why",
            "mode": "research",
            "model": "Sonar 2",
            "thread_id": "t-9",
            "allow_incomplete": True,
        }
    ]


def test_ask_defaults_to_search_and_best():
    cli.main(["ask", "why"])
    assert FakeClient.calls[0]["mode"] == "search"
    assert FakeClient.calls[0]["model"] == "best"


def test_human_output_is_the_answer_then_its_sources(capsys):
    assert cli.main(["ask", "why"]) == 0
    out = capsys.readouterr()
    assert out.out.startswith("Canberra is the capital[1].")
    assert "[1] A" in out.out and "https://example.com/a" in out.out
    # The provenance line is stderr, so `pplx ask ... > answer.txt` gets the answer.
    assert "pplx_pro" in out.err and "thread-1" in out.err


def test_json_output_is_only_json(capsys):
    assert cli.main(["ask", "why", "--json"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["text"].startswith("Canberra")
    assert parsed["citations"][0]["url"] == "https://example.com/a"
    assert parsed["complete"] is True and parsed["thread_id"] == "thread-1"


def test_a_truncation_warning_survives_a_json_pipe(capsys):
    FakeClient.result = ANSWER.__class__(**{**ANSWER.__dict__, "complete": False})
    assert cli.main(["ask", "why", "--json"]) == 0
    out = capsys.readouterr()
    json.loads(out.out)  # stdout is still parseable
    assert "incomplete" in out.err


# --- research -----------------------------------------------------------------


class FakeTask(ResearchTask):
    waited: dict = {}

    def wait(self, timeout=None, allow_incomplete=False, on_progress=None):
        FakeTask.waited = {"timeout": timeout, "allow_incomplete": allow_incomplete}
        if on_progress:
            on_progress([("finding sources", "DONE"), ("writing", "IN_PROGRESS")])
        return ANSWER


def test_detach_prints_the_id_and_does_not_wait(capsys):
    FakeClient.result = FakeTask(task_id="task-7")
    assert cli.main(["ask", "why", "--mode", "research", "--detach"]) == 0
    assert capsys.readouterr().out.strip() == "task-7"
    assert not FakeTask.waited


def test_detach_is_refused_in_search_mode(capsys):
    assert cli.main(["ask", "why", "--detach"]) == 2
    assert "only applies to --mode research" in capsys.readouterr().err


def test_research_without_detach_waits_and_shows_progress(capsys):
    FakeClient.result = FakeTask(task_id="task-7")
    assert cli.main(["ask", "why", "--mode", "research"]) == 0
    out = capsys.readouterr()
    assert out.out.startswith("Canberra")
    assert "[1/2] writing" in out.err  # real goals, not a spinner


def test_result_prints_a_finished_task(capsys):
    FakeClient.result = FakeTask(task_id="task-7")
    assert cli.main(["result", "task-7"]) == 0
    assert capsys.readouterr().out.startswith("Canberra")
    assert FakeTask.waited["timeout"] == 0


def test_result_reports_a_task_that_is_still_running(capsys):
    class Running(FakeTask):
        @property
        def status(self):
            return "running"

        @property
        def progress(self):
            return [("finding sources", "DONE"), ("writing", "IN_PROGRESS")]

        def wait(self, timeout=None, allow_incomplete=False, on_progress=None):
            raise PplxError("still running")

    FakeClient.result = Running(task_id="task-7")
    # Exit 3, not 1 or 2: a polling shell loop has to tell "not yet" from "broken".
    assert cli.main(["result", "task-7"]) == 3
    err = capsys.readouterr().err
    assert "running" in err and "[1/2] writing" in err


def test_result_on_a_dead_session_exits_1_not_3(capsys):
    # Downgraded to exit 3, "log in again" reads as "not finished yet" and a polling
    # shell loop retries a dead session forever (adversarial review, 2026-08-01).
    class Dead(FakeTask):
        def wait(self, timeout=None, allow_incomplete=False, on_progress=None):
            raise SessionExpiredError("session expired or revoked -- run: pplx login")

    FakeClient.result = Dead(task_id="task-7")
    assert cli.main(["result", "task-7"]) == 1
    assert "pplx login" in capsys.readouterr().err


# --- exit codes ---------------------------------------------------------------


@pytest.mark.parametrize(
    "error,code",
    [
        (SessionExpiredError("expired"), 1),
        (ChallengeEncounteredError("challenged"), 1),
        (IncompleteAnswerError("cut off"), 2),
        (ModelMismatchError("wrong model"), 2),
        (PplxError("broke"), 2),
    ],
)
def test_exit_codes_separate_relogin_from_breakage(error, code, capsys):
    FakeClient.raises = error
    assert cli.main(["ask", "why"]) == code
    assert str(error) in capsys.readouterr().err


def test_status_still_reports_one_word(capsys):
    FakeClient.state = "expired"
    assert cli.main(["status"]) == 1
    out = capsys.readouterr()
    assert out.out.strip() == "expired"
    assert "pplx login" in out.err


# --- doctor -------------------------------------------------------------------


def test_doctor_names_the_invariant_that_failed(capsys):
    FakeClient.result = [
        ("chrome", True, "Google Chrome 141.0"),
        ("completion signal", True, "terminal frame observed"),
        ("citations", False, "0 sources"),
    ]
    assert cli.main(["doctor"]) == 2
    out = capsys.readouterr()
    assert "FAIL  citations: 0 sources" in out.out
    assert "ok    chrome" in out.out


def test_doctor_is_quiet_when_everything_holds(capsys):
    FakeClient.result = [("chrome", True, "Google Chrome 141.0")]
    assert cli.main(["doctor"]) == 0
    assert "FAIL" not in capsys.readouterr().out
