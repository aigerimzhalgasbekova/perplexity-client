"""Milestone 3: `ask()` orchestration, against fakes.

What is testable here is ordering and gating -- that a query is never spent on a dead
session, a challenge or an exhausted mode, that the tee follows only the one request
that carries an answer, and that a cut stream raises. Whether Chrome attaches and
whether perplexity.ai answers is `pplx doctor`'s job, not pytest's (PRD §7).
"""

import base64
import contextlib

import pytest

from perplexity_client import adapter, chrome, client, pacing
from perplexity_client.errors import (ChallengeEncounteredError, IncompleteAnswerError,
                                      PplxError, QuotaExhaustedError, SessionExpiredError)

from test_adapter import COMPLETE, TRUNCATED  # noqa: E402
from test_session import ANON_STATE, GOOD_STATE, FakeCtx, FakePage  # noqa: E402

ASK_URL = "https://www.perplexity.ai" + adapter.ASK_PATH


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class FakeCDP:
    """Enough of a CDP session to drive the tee's handlers by hand."""

    def __init__(self):
        self.handlers, self.sent, self.detached = {}, [], False

    def send(self, method, params=None):
        self.sent.append((method, params))
        return {"bufferedData": ""} if method == "Network.streamResourceContent" else {}

    def on(self, event, fn):
        self.handlers[event] = fn

    def detach(self):
        self.detached = True

    def respond(self, rid, url, mime="text/event-stream"):
        self.handlers["Network.responseReceived"](
            {"requestId": rid, "response": {"url": url, "mimeType": mime}})

    def data(self, rid, raw: bytes):
        self.handlers["Network.dataReceived"]({"requestId": rid, "data": b64(raw)})

    def finish(self, rid):
        self.handlers["Network.loadingFinished"]({"requestId": rid})


class CDPCtx(FakeCtx):
    def __init__(self, state=GOOD_STATE, pages=(), cdp=None):
        super().__init__(state, pages)
        self.cdp = cdp or FakeCDP()

    def new_cdp_session(self, page):
        return self.cdp


class AskPage(FakePage):
    """A page whose textbox records what was typed and sent."""

    def __init__(self, cdp=None, feed=b"", **kw):
        super().__init__(**kw)
        self.typed, self.pressed, self.cdp, self.feed = None, None, cdp, feed
        self.waits = 0

    def get_by_role(self, role):
        assert role == "textbox"
        return self

    @property
    def first(self):
        return self

    def wait_for(self, timeout=None):
        pass

    def click(self):
        pass

    def fill(self, text):
        self.typed = text

    def press(self, key):
        self.pressed = key
        # The answer starts arriving once the query is sent -- and only then, which is
        # what makes "did ask() submit before waiting?" observable.
        if self.cdp is not None:
            self.cdp.respond("ask", ASK_URL)
            self.cdp.data("ask", self.feed)

    def wait_for_timeout(self, ms):
        self.waits += 1


def fake_chrome(ctx, seen=None):
    @contextlib.contextmanager
    def _chrome(headless=True, url="about:blank", interval=0.0):
        if seen is not None:
            seen["interval"] = interval
        yield ctx, ctx.pages[0] if ctx.pages else None
    return _chrome


@pytest.fixture(autouse=True)
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("PPLX_CONFIG_DIR", str(tmp_path / "cfg"))
    # A fake `wait_for_timeout` does not actually wait, so the cases that never receive
    # a stream would spin against the real 180s ceiling.
    monkeypatch.setenv("PPLX_ASK_TIMEOUT", "0.5")
    chrome.profile_dir().mkdir(parents=True)


def ask(monkeypatch, page, state=GOOD_STATE, seen=None, **kw):
    ctx = CDPCtx(state, [page], cdp=page.cdp)
    monkeypatch.setattr(client, "chrome", fake_chrome(ctx, seen))
    return client.Client().ask("what is a quokka", **kw)


# --- the tee -----------------------------------------------------------------------


def test_tee_only_follows_the_request_that_carries_an_answer():
    cdp = FakeCDP()
    adapter.tee(CDPCtx(cdp=cdp), object())
    cdp.respond("1", "https://www.perplexity.ai/rest/thread/list_recent")
    assert not [m for m, _ in cdp.sent if m == "Network.streamResourceContent"]
    cdp.respond("2", ASK_URL)
    assert ("Network.streamResourceContent", {"requestId": "2"}) in cdp.sent


def test_tee_ignores_a_non_streaming_response_on_the_ask_path():
    cdp = FakeCDP()
    adapter.tee(CDPCtx(cdp=cdp), object())
    cdp.respond("1", ASK_URL, mime="application/json")
    assert not [m for m, _ in cdp.sent if m == "Network.streamResourceContent"]


def test_tee_ignores_bytes_from_every_other_request():
    # ~40 REST calls fire on the homepage before the query (M0). Any of their bytes in
    # this buffer would corrupt every frame after them.
    cdp = FakeCDP()
    s = adapter.tee(CDPCtx(cdp=cdp), object())
    cdp.respond("ask", ASK_URL)
    cdp.data("other", b"garbage that is not a frame\r\n\r\n")
    cdp.data("ask", COMPLETE)
    assert s.done
    assert len(s.frames) == len(adapter.frames(COMPLETE))


def test_tee_keeps_the_bytes_that_arrived_before_the_tee_started():
    # streamResourceContent answers with whatever was already buffered; dropping it
    # loses the head of the stream, and with it every frame boundary after it.
    cdp = FakeCDP()
    cdp.send = lambda m, p=None: ({"bufferedData": b64(COMPLETE)}
                                  if m == "Network.streamResourceContent" else {})
    s = adapter.tee(CDPCtx(cdp=cdp), object())
    cdp.respond("ask", ASK_URL)
    assert s.done


# --- gating ------------------------------------------------------------------------


def test_ask_refuses_a_dead_session_before_spending_a_query(monkeypatch):
    page = AskPage()
    with pytest.raises(SessionExpiredError):
        ask(monkeypatch, page, state=ANON_STATE)
    assert page.typed is None


def test_ask_refuses_an_expired_session_before_spending_a_query(monkeypatch):
    page = AskPage(authed=False)
    with pytest.raises(SessionExpiredError):
        ask(monkeypatch, page)
    assert page.typed is None


def test_ask_refuses_a_challenge_rather_than_working_around_it(monkeypatch):
    # PRD §8: the tool never solves, bypasses or retries around a challenge.
    # The settle window is real -- a live Chrome usually clears an interstitial itself
    # -- but a fake page never will, so waiting it out here buys only 15 seconds.
    monkeypatch.setattr(adapter, "SETTLE_TIMEOUT", 0)
    page = AskPage(title="Just a moment...")
    with pytest.raises(ChallengeEncounteredError):
        ask(monkeypatch, page)
    assert page.typed is None


def test_ask_refuses_an_exhausted_mode_rather_than_failing_mid_stream(monkeypatch):
    page = AskPage(quota={"modes": {"pro_search": {"available": False}}})
    with pytest.raises(QuotaExhaustedError):
        ask(monkeypatch, page)
    assert page.typed is None


def test_ask_proceeds_when_only_a_mode_it_is_not_using_is_exhausted(monkeypatch):
    cdp = FakeCDP()
    page = AskPage(cdp=cdp, feed=COMPLETE,
                   quota={"modes": {"pro_search": {"available": True},
                                    "research": {"available": False}}})
    assert ask(monkeypatch, page).complete is True


def test_ask_rejects_an_empty_query_without_launching_anything(monkeypatch):
    monkeypatch.setattr(client, "chrome", fake_chrome(CDPCtx()))
    with pytest.raises(PplxError):
        client.Client().ask("   ")


# --- the answer --------------------------------------------------------------------


def test_ask_returns_the_parsed_answer(monkeypatch):
    cdp = FakeCDP()
    page = AskPage(cdp=cdp, feed=COMPLETE)
    r = ask(monkeypatch, page)
    assert page.typed == "what is a quokka" and page.pressed == "Enter"
    assert r.complete is True and r.model == "pplx_pro" and r.mode == "search"
    assert len(r.citations) == 15 and r.thread_id


def test_ask_raises_on_a_truncated_stream(monkeypatch):
    with pytest.raises(IncompleteAnswerError):
        ask(monkeypatch, AskPage(cdp=FakeCDP(), feed=TRUNCATED))


def test_ask_returns_the_partial_answer_when_opted_in(monkeypatch):
    r = ask(monkeypatch, AskPage(cdp=FakeCDP(), feed=TRUNCATED), allow_incomplete=True)
    assert r.complete is False and r.text.startswith("The capital of Australia")


def test_ask_names_a_challenge_that_appeared_after_submitting(monkeypatch):
    # An empty stream and an interstitial is a block, not adapter drift, and sending
    # the user to `doctor` for it would be the wrong instruction.
    class Blocked(AskPage):
        def press(self, key):
            self._title = "Just a moment..."

    with pytest.raises(ChallengeEncounteredError):
        ask(monkeypatch, Blocked(cdp=FakeCDP()))


def test_ask_says_the_adapter_is_broken_when_no_stream_ever_arrives(monkeypatch):
    with pytest.raises(PplxError, match="frontend"):
        ask(monkeypatch, AskPage(cdp=FakeCDP()))


def test_ask_stops_waiting_once_the_answer_completes(monkeypatch):
    # A fixed sleep would cost every caller the timeout; polling has to end on the
    # terminal frame.
    page = AskPage(cdp=FakeCDP(), feed=COMPLETE)
    ask(monkeypatch, page)
    assert page.waits == 0


def test_ask_stops_waiting_when_the_connection_closes_short(monkeypatch):
    # A dropped stream is a definite end. Waiting out the full ceiling for it would
    # cost three minutes to say what the closed connection already said.
    class Dropped(AskPage):
        def press(self, key):
            self.cdp.respond("ask", ASK_URL)
            self.cdp.data("ask", TRUNCATED)
            self.cdp.finish("ask")

    monkeypatch.setenv("PPLX_ASK_TIMEOUT", "600")
    page = Dropped(cdp=FakeCDP())
    with pytest.raises(IncompleteAnswerError):
        ask(monkeypatch, page)
    assert page.waits == 0


def test_ask_ignores_another_request_finishing(monkeypatch):
    class Noisy(AskPage):
        def press(self, key):
            self.cdp.respond("ask", ASK_URL)
            self.cdp.finish("something-else")
            self.cdp.data("ask", COMPLETE)

    assert ask(monkeypatch, Noisy(cdp=FakeCDP())).complete is True


def test_ask_gives_up_at_the_timeout(monkeypatch):
    monkeypatch.setenv("PPLX_ASK_TIMEOUT", "0")
    with pytest.raises(IncompleteAnswerError):
        ask(monkeypatch, AskPage(cdp=FakeCDP(), feed=TRUNCATED))


def test_ask_detaches_the_cdp_session(monkeypatch):
    # It outlives the page otherwise, and `ask` is the call an agent loop repeats.
    cdp = FakeCDP()
    ask(monkeypatch, AskPage(cdp=cdp, feed=COMPLETE))
    assert cdp.detached


# --- pacing ------------------------------------------------------------------------


def test_ask_spends_the_pacing_interval_unlike_status(monkeypatch):
    # `status` loads a page; `ask` spends a query. PRD §4 paces only the latter.
    seen = {}
    ask(monkeypatch, AskPage(cdp=FakeCDP(), feed=COMPLETE), seen=seen)
    assert seen["interval"] == pacing.default_interval()
