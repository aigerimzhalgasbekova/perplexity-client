"""Milestone 8: `doctor` -- the check that is allowed to cost a query.

It is the only part of this project that talks to the live site on purpose, so what is
testable offline is its judgement: which invariants it names, and that it reports every
one rather than stopping at the first failure. See CONTRIBUTING for why fixtures cannot
do `doctor`'s job.
"""

from perplexity_client import client
from perplexity_client.adapter import Citation, Response
from perplexity_client.errors import ChromeNotFoundError, IncompleteAnswerError

GOOD = Response(
    text="Canberra is the capital[1], not Sydney[2].",
    citations=[
        Citation(url="https://a.example", title="A", snippet=None),
        Citation(url="https://b.example", title="B", snippet="x"),
    ],
    model="pplx_pro",
    mode="search",
    thread_id="thread-1",
    complete=True,
)


class ThreadPage:
    """The one page `doctor` opens itself: the resumed-thread invariant."""

    blocks: list = [{"intended_usage": "ask_text"}]

    def goto(self, url, **kw):
        pass

    def evaluate(self, script, arg=None):
        return {"entries": [{"backend_uuid": "thread-1", "blocks": ThreadPage.blocks}]}


def doctor(monkeypatch, answer=GOOD, state="ok", version="Google Chrome 150.0"):
    import contextlib

    monkeypatch.setattr(client, "chrome_version", lambda: version)
    monkeypatch.setattr(client.Client, "status", lambda self: state)

    @contextlib.contextmanager
    def fake_chrome(**kw):
        yield None, ThreadPage()

    monkeypatch.setattr(client, "chrome", fake_chrome)

    def ask(self, query, **kw):
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(client.Client, "ask", ask)
    return client.Client().doctor()


def rows(result):
    return {name: (ok, detail) for name, ok, detail in result}


def test_every_invariant_is_named_and_holds(monkeypatch):
    got = rows(doctor(monkeypatch))
    assert all(ok for ok, _ in got.values()), got
    assert set(got) == {
        "chrome",
        "session",
        "completion signal",
        "answer text",
        "citations",
        "citation index",
        "thread id",
        "observed model",
        "observed mode",
        "thread blocks",
    }
    assert got["chrome"][1] == "Google Chrome 150.0"


def test_a_block_free_thread_document_is_a_failed_invariant(monkeypatch):
    # M5's silent-failure shape: drop THREAD_QUERY's parameters and the endpoint
    # answers with entries but no blocks -- research polls would then report nothing,
    # with no error anywhere (docs/M4-M8-findings.md). This is the live check for it.
    monkeypatch.setattr(ThreadPage, "blocks", [])
    got = rows(doctor(monkeypatch))
    assert got["thread blocks"][0] is False
    assert got["observed mode"][0] is True  # the rows above it still report


def test_a_marker_with_no_source_fails_the_index_invariant(monkeypatch):
    # The failure this exists to catch: an answer that cites [3] when three sources
    # never arrived reads as perfectly correct to everything downstream.
    answer = Response(**{**GOOD.__dict__, "text": "Canberra[3]."})
    got = rows(doctor(monkeypatch, answer))
    assert got["citation index"][0] is False
    assert got["completion signal"][0] is True  # the others still report


def test_an_answer_with_no_citations_is_reported_not_raised(monkeypatch):
    answer = Response(**{**GOOD.__dict__, "citations": [], "text": "Canberra."})
    got = rows(doctor(monkeypatch, answer))
    assert got["citations"] == (False, "0 sources")
    assert got["citation index"][0] is True  # no markers, nothing to misattribute


def test_a_failed_query_stops_after_naming_itself(monkeypatch):
    got = rows(doctor(monkeypatch, IncompleteAnswerError("cut off")))
    assert got["answer"][0] is False
    assert "IncompleteAnswerError" in got["answer"][1]
    assert "completion signal" not in got  # nothing below it could have run


def test_a_broken_session_never_spends_a_query(monkeypatch):
    spent = []
    monkeypatch.setattr(client, "chrome_version", lambda: "Google Chrome 150.0")
    monkeypatch.setattr(client.Client, "status", lambda self: "expired")
    monkeypatch.setattr(
        client.Client, "ask", lambda self, q, **kw: spent.append(q) or GOOD
    )
    got = rows(client.Client().doctor())
    assert got["session"] == (False, "expired")
    assert not spent


def test_a_missing_chrome_is_the_first_thing_reported(monkeypatch):
    def missing():
        raise ChromeNotFoundError("Google Chrome not found")

    monkeypatch.setattr(client, "chrome_version", missing)
    got = rows(client.Client().doctor())
    assert list(got) == ["chrome"]
    assert got["chrome"][0] is False


def test_an_answer_in_the_wrong_mode_is_a_failed_invariant(monkeypatch):
    answer = Response(**{**GOOD.__dict__, "mode": "research"})
    assert rows(doctor(monkeypatch, answer))["observed mode"] == (False, "research")
