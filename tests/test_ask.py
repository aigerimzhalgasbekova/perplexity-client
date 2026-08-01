"""Milestone 3: `ask()` orchestration, against fakes.

What is testable here is ordering and gating -- that a query is never spent on a dead
session, a challenge or an exhausted mode, that the tee follows only the one request
that carries an answer, and that a cut stream raises. Whether Chrome attaches and
whether perplexity.ai answers is `pplx doctor`'s job, not pytest's (PRD §7).
"""

import base64
import contextlib
import json
import pathlib

import pytest
from test_adapter import COMPLETE, TRUNCATED  # noqa: E402
from test_session import ANON_STATE, GOOD_STATE, FakeCtx, FakePage  # noqa: E402

from perplexity_client import adapter, chrome, client, pacing
from perplexity_client.errors import (
    ChallengeEncounteredError,
    IncompleteAnswerError,
    PplxError,
    QuotaExhaustedError,
    SessionExpiredError,
)

ASK_URL = "https://www.perplexity.ai" + adapter.ASK_PATH
MODELS = json.loads(
    (
        pathlib.Path(__file__).parent.parent
        / "spike"
        / "fixtures"
        / "models-config-2026-08-01.json"
    ).read_text()
)


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

    def respond(self, rid, url, mime="text/event-stream", status=200):
        self.handlers["Network.responseReceived"](
            {
                "requestId": rid,
                "response": {"url": url, "mimeType": mime, "status": status},
            }
        )

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


class Settled:
    """A composer menu that is already showing what the caller wants.

    Mode and model selection is covered properly in test_models.py against a menu
    that can be wrong; here it is only in the way, so every entry reports itself as
    the checked one and `_pick` returns without touching anything.
    """

    def count(self):
        return 1

    @property
    def first(self):
        return self

    def get_attribute(self, attr):
        return "true"

    def click(self, timeout=None):
        pass

    def press(self, key, timeout=None):
        pass


class Keys:
    """`page.keyboard`, which must not be `page` itself -- Escape would otherwise
    land in the textbox's `press` and submit the query."""

    def __init__(self):
        self.pressed = []

    def press(self, key):
        self.pressed.append(key)


class AskPage(FakePage):
    """A page whose textbox records what was typed and sent."""

    def __init__(self, cdp=None, feed=b"", **kw):
        super().__init__(**kw)
        self.typed, self.pressed, self.cdp, self.feed = None, None, cdp, feed
        self.waits = 0
        self.keyboard = Keys()

    def get_by_role(self, role, name=None, exact=False):
        return self if role == "textbox" else Settled()

    def evaluate(self, script, arg=None):
        if arg == adapter.MODEL_CONFIG:
            return MODELS
        return super().evaluate(script, arg)

    @property
    def first(self):
        return self

    @property
    def last(self):
        return self

    def wait_for(self, timeout=None):
        pass

    def click(self):
        pass

    def fill(self, text):
        self.typed = text
        # Setting mode and model waits on menus of its own, and the tests below count
        # waits to prove the *answer* loop did not spin. The query being typed is the
        # line between the two.
        self.waits = 0

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


def test_tee_ignores_a_second_answer_stream_by_default():
    # One query, one answer. Splicing a later stream onto this one would assemble a
    # frame out of two different answers' bytes.
    cdp = FakeCDP()
    adapter.tee(CDPCtx(cdp=cdp), object())
    cdp.respond("first", ASK_URL)
    cdp.finish("first")
    cdp.respond("second", ASK_URL)
    assert [p for m, p in cdp.sent if m == "Network.streamResourceContent"] == [
        {"requestId": "first"}
    ]


def test_tee_does_not_mistake_a_previous_turn_for_this_answer():
    # Continuing a thread whose last turn is still generating: opening that page
    # subscribes to `/rest/sse/perplexity_ask/reconnect/<old uuid>`, which contains
    # ASK_PATH as a substring. Binding to it would hand the *previous* answer back as
    # this query's -- complete, plausible, and about the wrong question.
    cdp = FakeCDP()
    s = adapter.tee(CDPCtx(cdp=cdp), object())
    cdp.respond("old", ASK_URL + "/reconnect/previous-turn")
    assert not [m for m, _ in cdp.sent if m == "Network.streamResourceContent"]
    cdp.respond("mine", ASK_URL)
    cdp.data("mine", COMPLETE)
    assert s.done


def test_a_reconnect_tee_takes_only_the_reconnect():
    # And the mirror image: following a task must not bind whatever new query the
    # page happens to fire while it watches.
    cdp = FakeCDP()
    s = adapter.tee(CDPCtx(cdp=cdp), object(), reconnect=True)
    cdp.respond("fresh", ASK_URL)
    assert not [m for m, _ in cdp.sent if m == "Network.streamResourceContent"]
    cdp.respond("mine", ASK_URL + "/reconnect/task-1")
    cdp.data("mine", b'data:{"status": "COMPLETED", "final_sse_message": true}\n\n')
    assert s.done


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
    assert s.frames == adapter.frames(COMPLETE)


def test_tee_does_not_bind_to_an_error_response(monkeypatch):
    # A 429 on this path with this mime type would bind `rid`, and the frontend's retry
    # -- the one carrying the actual answer -- would then be dropped as "not ours". The
    # user is told the frontend changed and sent to `doctor`, which spends another
    # query, on an account that just pushed back.
    cdp = FakeCDP()
    s = adapter.tee(CDPCtx(cdp=cdp), object())
    cdp.respond("429", ASK_URL, status=429)
    assert not [m for m, _ in cdp.sent if m == "Network.streamResourceContent"]
    cdp.respond("retry", ASK_URL)
    cdp.data("retry", COMPLETE)
    assert s.done


def test_tee_leaves_the_request_unbound_when_streaming_cannot_be_enabled():
    # `streamResourceContent` raises "Request not found" if the request finished during
    # the round-trip. Binding `rid` first leaves the stream silently dead -- every later
    # dataReceived is dropped as someone else's -- and the exception escapes the CDP
    # event dispatch, which surfaces as the raw traceback _blame exists to prevent.
    cdp = FakeCDP()
    calls = []

    def send(method, params=None):
        calls.append(method)
        if method == "Network.streamResourceContent" and len(calls) < 3:
            raise RuntimeError("Request not found")
        return {"bufferedData": ""}

    cdp.send = send
    s = adapter.tee(CDPCtx(cdp=cdp), object())
    cdp.respond("dead", ASK_URL)  # must not raise out of the handler
    cdp.respond("live", ASK_URL)
    cdp.data("live", COMPLETE)
    assert s.done


def test_tee_keeps_the_bytes_that_arrived_before_the_tee_started():
    # streamResourceContent answers with whatever was already buffered; dropping it
    # loses the head of the stream, and with it every frame boundary after it.
    cdp = FakeCDP()
    cdp.send = lambda m, p=None: (
        {"bufferedData": b64(COMPLETE)} if m == "Network.streamResourceContent" else {}
    )
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
    page = AskPage(
        cdp=cdp,
        feed=COMPLETE,
        quota={
            "modes": {
                "pro_search": {"available": True},
                "research": {"available": False},
            }
        },
    )
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
    # Distinct from the missing-box case above: the query *was* sent, so this one
    # actually cost the account a query and the message should not pretend otherwise.
    with pytest.raises(PplxError, match="no answer stream was intercepted"):
        ask(monkeypatch, AskPage(cdp=FakeCDP()))


def test_ask_still_explains_itself_when_the_tab_died(monkeypatch):
    # A dead tab is how a Chrome crash shows up. Diagnosing the silence must not itself
    # raise, or the user gets a Playwright traceback where the explanation should be.
    class Gone(AskPage):
        def press(self, key):
            self.dead = True

        def title(self):
            if getattr(self, "dead", False):
                raise RuntimeError("Target page, context or browser has been closed")
            return super().title()

    with pytest.raises(PplxError, match="frontend"):
        ask(monkeypatch, Gone(cdp=FakeCDP()))


def test_ask_diagnoses_a_query_box_that_never_appeared(monkeypatch):
    # The box not being there is how a frontend change first shows up, and it arrives
    # as a Playwright timeout -- which the CLI cannot map and says nothing useful.
    class NoBox(AskPage):
        def wait_for(self, timeout=None):
            raise RuntimeError("Timeout 30000ms exceeded waiting for get_by_role")

    with pytest.raises(PplxError, match="query box never appeared"):
        ask(monkeypatch, NoBox(cdp=FakeCDP()))


def test_ask_calls_a_challenge_a_challenge_even_when_the_box_is_missing(monkeypatch):
    # The box is missing *because* an interstitial replaced the page. Sending that user
    # to `doctor` would be the wrong instruction entirely.
    class Blocked(AskPage):
        def wait_for(self, timeout=None):
            self._title = "Just a moment..."
            raise RuntimeError("Timeout 30000ms exceeded waiting for get_by_role")

    with pytest.raises(ChallengeEncounteredError):
        ask(monkeypatch, Blocked(cdp=FakeCDP()))


def test_ask_reports_an_unreachable_site_as_its_own_error(monkeypatch):
    # Otherwise a network failure escapes as a raw Playwright exception, which the CLI
    # does not map and the caller cannot catch as a PplxError. `status` already does
    # this; `ask` has the same page interaction and needs the same treatment.
    class Offline(AskPage):
        def goto(self, url, **kw):
            raise RuntimeError("net::ERR_NAME_NOT_RESOLVED")

    with pytest.raises(PplxError, match="could not reach"):
        ask(monkeypatch, Offline(cdp=FakeCDP()))


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
    # One of the ~40 homepage requests finishing must not end the answer's wait. The
    # spurious finish has to land *before* any answer bytes, or `done` short-circuits
    # the loop and the test passes whether or not the guard exists.
    class Noisy(AskPage):
        def press(self, key):
            self.cdp.respond("ask", ASK_URL)
            self.cdp.finish("something-else")

        def wait_for_timeout(self, ms):
            super().wait_for_timeout(ms)
            if self.typed:  # not during mode/model selection: the tee is not up yet
                self.cdp.data("ask", COMPLETE)  # the answer arrives while polling

    page = Noisy(cdp=FakeCDP())
    assert ask(monkeypatch, page).complete is True
    assert page.waits > 0  # it kept waiting past the unrelated request's finish


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


def test_ask_explains_a_browser_that_died_while_it_was_waiting(monkeypatch):
    # The poll loop is the longest-lived call in the flow -- up to the whole answer
    # timeout of a headless Chrome this tool launched itself -- and it was the one
    # page interaction with no mapping. Raw, it reaches a caller whose contract
    # (and the CLI's exit code 2) is `except PplxError`.
    class Dies(AskPage):
        def wait_for_timeout(self, ms):
            # Not before the query is typed: setting mode and model waits on menus of
            # its own, and dying in *those* is a different path with its own message.
            if self.typed:
                raise RuntimeError("Target page, context or browser has been closed")

    with pytest.raises(PplxError):
        ask(monkeypatch, Dies(cdp=FakeCDP()))


def test_ask_keeps_an_answer_that_arrived_before_the_browser_died(monkeypatch):
    # A dead browser after the terminal frame must not cost the query that bought it.
    class Dies(AskPage):
        def wait_for_timeout(self, ms):
            if not self.typed:
                return
            # The answer lands and Chrome goes away in the same poll: CDP has already
            # dispatched the frames the loop is about to be killed before re-reading.
            self.cdp.data("ask", COMPLETE)
            raise RuntimeError("Target page, context or browser has been closed")

    assert ask(monkeypatch, Dies(cdp=FakeCDP())).complete is True


def test_ask_refuses_an_answer_that_came_back_in_another_mode(monkeypatch):
    # M3 could only warn about this, because nothing selected the mode. Now that
    # `ask` sets it and confirms the menu took, an answer in the other mode means the
    # page did something else with the query -- and a research answer silently
    # returned to a caller asking for search spends the wrong quota under the wrong
    # name. Raising costs an answer already paid for, which is worth it here: `mode`
    # is the one field a caller cannot check after the fact.
    feed = COMPLETE.replace(b'"search_mode": "SEARCH"', b'"search_mode": "RESEARCH"')
    assert feed != COMPLETE
    with pytest.raises(PplxError, match="came back as 'research'"):
        ask(monkeypatch, AskPage(cdp=FakeCDP(), feed=feed))
