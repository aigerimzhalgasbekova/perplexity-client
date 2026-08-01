"""Milestone 5: continuing a conversation, and telling a turn from a thread.

The fixture is a real two-turn thread captured on 2026-08-01 — and it doubles as the
model-mismatch fixture PRD §9.4 asked for, because its first turn is the one where
Sonar 2 was requested and `turbo` answered.
"""

import json
import pathlib

import pytest

from perplexity_client import adapter, client
from perplexity_client.errors import (
    IncompleteAnswerError,
    ModelMismatchError,
    PplxError,
)

FIXTURES = pathlib.Path(__file__).parent.parent / "spike" / "fixtures"
THREAD = json.loads((FIXTURES / "thread-multiturn-2026-08-01.json").read_text())
MODELS = json.loads((FIXTURES / "models-config-2026-08-01.json").read_text())
SLUG = "d236db28-526f-49a1-9a21-faa33c3ad928"
TURN_2 = "34fe6257-be0a-478d-9a7b-aa168bf47176"


def test_the_fixture_really_is_two_turns_of_one_thread():
    entries = THREAD["entries"]
    assert len(entries) == 2
    assert [e["backend_uuid"] for e in entries] == [SLUG, TURN_2]
    # One slug, one context, two turns -- oldest first.
    assert {e["thread_url_slug"] for e in entries} == {SLUG}
    assert len({e["context_uuid"] for e in entries}) == 1


def test_a_thread_reads_back_its_most_recent_answer():
    # M3 took entries[0], which is right on a one-turn thread and returns the opening
    # answer of every other one -- silently, and to a caller who asked for the latest.
    r = adapter.parse_thread(THREAD, allow_incomplete=False)
    assert "New Zealand" in r.text
    assert r.complete is True


def test_a_named_turn_wins_over_the_most_recent_one():
    r = adapter.parse_thread(THREAD, allow_incomplete=False, entry_id=SLUG)
    assert "Canberra" in r.text


def test_thread_id_is_the_thread_not_the_turn():
    # Passing back a turn's own uuid would continue from that turn rather than from
    # the conversation; the slug is the same on both entries by design.
    for entry_id in (SLUG, TURN_2):
        r = adapter.parse_thread(THREAD, allow_incomplete=False, entry_id=entry_id)
        assert r.thread_id == SLUG


def test_an_unknown_turn_is_not_silently_the_latest_one():
    with pytest.raises(IncompleteAnswerError):
        adapter.parse_thread(THREAD, allow_incomplete=False, entry_id="no-such-turn")


def test_thread_url_is_where_the_composer_lives():
    assert adapter.thread_url(SLUG) == f"{adapter.HOME}search/{SLUG}"


# --- the mismatch, as captured ------------------------------------------------


def test_the_first_turn_records_a_real_model_substitution():
    entry = THREAD["entries"][0]
    assert entry["user_selected_model"] == "experimental"  # Sonar 2, as requested
    assert entry["display_model"] == "turbo"  # "Best", as served
    # Response.model is the observed one, never an echo of the request (US-6).
    assert adapter.parse_thread(THREAD, False, SLUG).model == "turbo"


def test_verify_raises_on_that_pairing_and_names_both_models():
    class Page:
        def evaluate(self, script, arg=None):
            return MODELS

    r = adapter.parse_thread(THREAD, False, SLUG)
    with pytest.raises(ModelMismatchError) as e:
        client._verify(Page(), r, "search", "experimental")
    assert "Sonar 2 (experimental)" in str(e.value)
    assert "Best (turbo)" in str(e.value)


def test_best_accepts_whatever_answered():
    # "" is what `resolve("best", ...)` returns as the expected id, and it is the
    # whole mechanism by which auto-selection is not a mismatch.
    class Page:
        def evaluate(self, script, arg=None):
            return MODELS

    r = adapter.parse_thread(THREAD, False, SLUG)
    assert adapter.resolve("best", adapter.offered(MODELS))[1] == ""
    client._verify(Page(), r, "search", "")  # does not raise


def test_the_second_turn_agrees_with_itself():
    entry = THREAD["entries"][1]
    assert entry["user_selected_model"] == entry["display_model"] == "turbo"


# --- typing into the right box -------------------------------------------------


class Boxes:
    """A page with two textboxes: the original query above, the composer below."""

    def __init__(self):
        self.filled = []

    def get_by_role(self, role, name=None, exact=False):
        assert role == "textbox"
        return self

    @property
    def first(self):
        return Box(self, "first")

    @property
    def last(self):
        return Box(self, "last")


class Box:
    def __init__(self, page, which):
        self.page, self.which = page, which

    def wait_for(self, timeout=None):
        pass

    def click(self):
        pass

    def fill(self, text):
        self.page.filled.append((self.which, text))

    def press(self, key):
        pass


def test_a_follow_up_types_into_the_composer_not_the_original_query():
    page = Boxes()
    adapter.submit(page, "and New Zealand?", follow_up=True)
    assert page.filled == [("last", "and New Zealand?")]


def test_a_first_query_still_uses_the_homepage_box():
    page = Boxes()
    adapter.submit(page, "what is a quokka")
    assert page.filled == [("first", "what is a quokka")]


def test_continuing_a_thread_is_refused_without_a_usable_page():
    # `_open_thread` is what turns a navigation failure into a PplxError rather than a
    # raw Playwright error escaping the documented contract.
    class Blocked:
        url = "https://www.perplexity.ai/cdn-cgi/challenge-platform/x"

        def goto(self, url, **kw):
            pass

        def title(self):
            return "Just a moment..."

        def wait_for_timeout(self, ms):
            pass

    with pytest.raises(PplxError, match="bot-detection challenge"):
        client._open_thread(Blocked(), SLUG)
