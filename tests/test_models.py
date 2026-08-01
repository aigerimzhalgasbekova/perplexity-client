"""Milestone 4: choosing a model, and noticing when a different one answered.

Fixture-dated, like every other suite here: green means the picker still matches the
site as of 2026-08-01, not that it works today (PRD §7).
"""

import json
import pathlib
import re

import pytest

from perplexity_client import adapter
from perplexity_client.errors import ModelUnavailableError, PplxError

FIXTURES = pathlib.Path(__file__).parent.parent / "spike" / "fixtures"
CONFIG = json.loads((FIXTURES / "models-config-2026-08-01.json").read_text())


# --- the catalogue ------------------------------------------------------------


def test_offered_is_the_picker_list_not_the_registry():
    offers = adapter.offered(CONFIG)
    # 87 search-mode ids exist; the picker offers a dozen. Reading the wrong list is
    # how you end up "selecting" a model that never appears in the menu.
    assert len(CONFIG["models"]) > 80
    assert 5 < len(offers) < 20
    assert offers["Sonar 2"] == "experimental"


def test_the_browser_agent_twin_does_not_shadow_the_real_model():
    # search_config carries two entries labelled "Claude Sonnet 5"; the first is the
    # browser agent's. Only the one whose id is a search model may win.
    offers = adapter.offered(CONFIG)
    assert offers["Claude Sonnet 5"] == "claude50sonnet"


def test_a_thinking_only_model_still_resolves():
    # Gemini has no non-reasoning id at all, so the fallback to reasoning_model is
    # what keeps it in the menu list.
    assert adapter.offered(CONFIG)["Gemini 3.1 Pro"] == "gemini31pro_high"


def test_models_above_the_plan_are_still_listed():
    # Listing them is right -- the *picker* is what refuses them, and a caller who
    # asks for one deserves "your plan cannot pick this", not "no such model".
    assert "Claude Opus 5" in adapter.offered(CONFIG)


# --- resolving a requested name ------------------------------------------------


@pytest.mark.parametrize("name", ["Sonar 2", "sonar 2", "SONAR2", "experimental"])
def test_resolve_accepts_label_or_id_in_any_case(name):
    assert adapter.resolve(name, adapter.offered(CONFIG)) == ("Sonar 2", "experimental")


@pytest.mark.parametrize("name", ["best", "Best", "BEST"])
def test_best_resolves_to_no_expected_id(name):
    # The empty id is the whole contract: it is what tells `ask` not to check.
    assert adapter.resolve(name, adapter.offered(CONFIG)) == ("Best", "")


def test_an_unknown_model_names_the_alternatives():
    with pytest.raises(ModelUnavailableError) as e:
        adapter.resolve("gpt-9", adapter.offered(CONFIG))
    assert "Sonar 2" in str(e.value) and "Best" in str(e.value)


def test_a_research_model_is_not_a_search_model():
    # pplx_alpha is Deep Research's; asking for it by name in search mode is a
    # mistake, not a selection.
    with pytest.raises(ModelUnavailableError):
        adapter.resolve("pplx_alpha", adapter.offered(CONFIG))


def test_model_label_decodes_an_observed_id():
    assert adapter.model_label(CONFIG, "turbo") == "Best (turbo)"
    assert adapter.model_label(CONFIG, "nope") == "nope"


# --- driving the menus --------------------------------------------------------


class FakeLocator:
    """Matches names the way Playwright does: a *substring*, case-insensitive, for a
    bare string; the pattern itself for a compiled regex; equality under `exact`.
    Faithful on purpose -- the substring default is exactly the hazard `_name_rx`
    exists to close, and a fake that quietly matched whole words would hide it."""

    def __init__(self, page, role, name, exact=False):
        self.page, self.role, self.name, self.exact = page, role, name, exact

    def _hit(self, name):
        if self.name is None:
            return True
        if isinstance(self.name, re.Pattern):
            return bool(self.name.search(name))
        if self.exact:
            return name == self.name
        return self.name.lower() in name.lower()

    @property
    def _matches(self):
        return [
            i
            for i in self.page.items
            if i["role"] == self.role and self._hit(i["name"])
        ]

    def count(self):
        return len(self._matches)

    @property
    def first(self):
        return self

    def get_attribute(self, attr):
        assert attr == "aria-checked"
        return "true" if self._matches and self._matches[0].get("checked") else "false"

    def click(self, timeout=None):
        self._activate("click")

    def press(self, key, timeout=None):
        self._activate("press")

    def _activate(self, verb):
        if not self._matches:
            raise AssertionError(f"no {self.role} called {self.name}")
        # The *matched* entry's name, not the lookup's: what was acted on is the
        # thing under test, and the lookup may be a compiled pattern.
        self.page.acted.append((verb, self.role, self._matches[0]["name"]))
        self.page.activate(self._matches[0])

    def wait_for(self, timeout=None):
        pass


class FakePage:
    """Just enough Page to drive the pickers without a browser.

    Activating a radio checks it and unchecks its siblings, the way a real menu does.
    `sticky=False` models the failure the picker exists to catch: the entry is there,
    the keypress lands, and the selection does not take.
    """

    def __init__(self, items, sticky=True):
        self.items = items
        self.acted = []
        self.keys = []
        self.sticky = sticky
        self.keyboard = self

    def activate(self, item):
        if item["role"] != "menuitemradio" or not self.sticky:
            return
        for other in self.items:
            if other["role"] == "menuitemradio":
                other["checked"] = other is item

    def get_by_role(self, role, name=None, exact=False):
        return FakeLocator(self, role, name, exact)

    def press(self, key):
        self.keys.append(key)

    def wait_for_timeout(self, ms):
        pass


def menu(checked="Best", locked=("Claude Opus 5",), labels=("Best", "Sonar 2")):
    items = [{"role": "button", "name": "Model"}]
    items += [
        {
            "role": "menuitem" if label in locked else "menuitemradio",
            "name": label,
            "checked": label == checked,
        }
        for label in (*labels, *locked)
    ]
    return items


def composer(**kw):
    """Both composer menus on one page: the mode picker and the model picker."""
    return [
        {"role": "button", "name": "Search"},
        {"role": "menuitemradio", "name": "Search", "checked": True},
        {"role": "menuitemradio", "name": "Deep research", "checked": False},
        *menu(**kw),
    ]


def test_pick_activates_by_keyboard_not_by_pointer():
    # Sibling entries own submenus whose poppers cover the target, so a real click is
    # intercepted and retried until it times out (observed 2026-08-01).
    page = FakePage(menu())
    adapter.pick_model(page, "Sonar 2")
    assert ("press", "menuitemradio", "Sonar 2") in page.acted


def test_pick_verifies_the_selection_took():
    page = FakePage(menu(), sticky=False)
    with pytest.raises(PplxError, match="did not take"):
        adapter.pick_model(page, "Sonar 2")


def test_a_locked_model_is_refused_before_a_query_is_spent():
    page = FakePage(menu())
    with pytest.raises(ModelUnavailableError, match="plan"):
        adapter.pick_model(page, "Claude Opus 5")
    assert not any(a[0] == "press" for a in page.acted)


def test_picking_the_model_that_is_already_selected_is_a_no_op():
    page = FakePage(menu(checked="Sonar 2"))
    adapter.pick_model(page, "Sonar 2")
    assert not any(a[0] == "press" for a in page.acted)


def test_the_picker_button_is_found_under_its_selected_name():
    # After a pick the button is labelled with the model rather than "Model", and on
    # a thread page it reads "Best" before anything has been picked at all -- so the
    # catalogue's labels have to be candidate button names too.
    items = [i for i in menu(checked="Sonar 2") if i["name"] != "Model"]
    items.insert(0, {"role": "button", "name": "Sonar 2"})
    page = FakePage(items)
    adapter.pick_model(page, "Best", adapter.offered(CONFIG))
    assert ("press", "menuitemradio", "Best") in page.acted


def test_a_missing_picker_button_blames_the_frontend():
    page = FakePage([i for i in menu() if i["role"] != "button"])
    with pytest.raises(PplxError, match="doctor"):
        adapter.pick_model(page, "Sonar 2")


def test_mode_is_picked_by_its_menu_label():
    page = FakePage(
        [
            {"role": "button", "name": "Search"},
            {"role": "menuitemradio", "name": "Search", "checked": True},
            {"role": "menuitemradio", "name": "Deep research", "checked": False},
        ]
    )
    adapter.pick_mode(page, "research")
    assert ("press", "menuitemradio", "Deep research") in page.acted


def test_an_unknown_mode_is_refused_without_touching_the_page():
    page = FakePage([])
    with pytest.raises(PplxError, match="unknown mode"):
        adapter.pick_mode(page, "sideways")
    assert not page.acted


def test_an_unreadable_catalogue_is_not_reported_as_an_unavailable_model():
    # `resolve` against an empty catalogue says "no model called 'Sonar 2'. This
    # account's picker offers: Best" -- which blames the subscription for a failed
    # fetch and sends the user to check something that was never wrong.
    from perplexity_client import client

    class Offline(FakePage):
        def evaluate(self, script, arg=None):
            return None  # the fetch probe swallows its own errors and returns null

    page = Offline(composer())
    with pytest.raises(PplxError, match="could not read the model catalogue"):
        client._configure(page, "search", "Sonar 2")


def test_best_still_works_when_the_catalogue_cannot_be_read():
    # No catalogue is needed to pick "Best": nothing has to be resolved and nothing
    # can mismatch. Failing here would break the default over a transient fetch.
    from perplexity_client import client

    class Offline(FakePage):
        def evaluate(self, script, arg=None):
            return None

    page = Offline(composer())
    assert client._configure(page, "search", "best") == ""


def test_search_cannot_substring_match_deep_research():
    # Playwright's default `name=` match is a case-insensitive substring, and
    # "Search" is a substring of "Deep research" -- a bare-label lookup acts on
    # whichever entry the DOM renders first, and the after-pick verification then
    # reads the same wrong entry back (adversarial review, 2026-08-01). Deep
    # research listed first is the ordering that would have triggered it.
    page = FakePage(
        [
            {"role": "button", "name": "Search"},
            {"role": "menuitemradio", "name": "Deep research", "checked": False},
            {"role": "menuitemradio", "name": "Search", "checked": True},
        ]
    )
    adapter.pick_mode(page, "search")
    assert not any(a[0] == "press" for a in page.acted)  # already selected
    adapter.pick_mode(page, "research")
    presses = [name for verb, _, name in page.acted if verb == "press"]
    assert presses == ["Deep research"]


def test_menu_names_carry_badges_and_still_match():
    # Real names: "Kimi K3 New Thinking", "Best Selects the best available model".
    assert adapter._named("Kimi K3 New Thinking", "Kimi K3")
    assert adapter._named("Best Selects the best available model", "Best")
    # ...but a version bump is a different model, not a badge.
    assert not adapter._named("Grok 4.5", "Grok 4")
