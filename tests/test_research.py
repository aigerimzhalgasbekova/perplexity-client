"""Milestone 6: Deep Research -- the workflow states, and following a task by id.

The fixtures are two frames cut out of a real research stream (`research-clarify`) and
a finished research thread (`research-thread-resume`). Between them they carry every
state `ResearchTask` claims to distinguish, which is the point: PRD §5's trap is that
the entry's own `status` says `PENDING` both while research works and while it waits
for an answer nobody is going to give.
"""

import json
import pathlib
import time

import pytest

from perplexity_client import adapter, research
from perplexity_client.errors import ClarificationRequiredError, PplxError
from perplexity_client.research import ResearchTask

FIXTURES = pathlib.Path(__file__).parent.parent / "spike" / "fixtures"
CLARIFY = json.loads((FIXTURES / "research-clarify-2026-08-01.json").read_text())
THREAD = json.loads((FIXTURES / "research-thread-resume-2026-07-31.json").read_text())
DONE = THREAD["entries"][0]


def asked() -> dict:
    """An entry mid-run, holding the questions the stream had just delivered.

    The stream sends the workflow block as a diff; the thread endpoint serves it
    assembled. This rebuilds the assembled shape from the diff's own payload, which is
    the same object either way.
    """
    item = next(
        p["value"]
        for b in CLARIFY["asked"]["blocks"]
        for p in (b.get("diff_block") or {}).get("patches") or ()
        if isinstance(p.get("value"), dict) and p["value"].get("type") == adapter.ASKED
    )
    return {
        "status": "PENDING",
        "blocks": [
            {
                "intended_usage": "workflow_root",
                "workflow_block": {
                    "status": adapter.AWAITING,
                    "steps": [{"status": adapter.AWAITING, "items": [item]}],
                },
            }
        ],
    }


def answered() -> dict:
    """The same entry after the question was retired -- by an answer or by the
    server's own 60-second timeout, which look identical on the wire."""
    entry = asked()
    step = entry["blocks"][0]["workflow_block"]["steps"][0]
    step["items"] = [
        step["items"][0],
        {"id": step["items"][0]["id"], "type": adapter.ANSWERED, "payload": {}},
    ]
    entry["blocks"][0]["workflow_block"]["status"] = "WORKFLOW_AWAITING_NEXT_STEPS"
    return entry


# --- reading the workflow -----------------------------------------------------


def test_a_waiting_task_is_not_a_running_one():
    # The whole trap: `status` is PENDING in both cases, so a client keying on it
    # hangs to its timeout while research waits for an answer (PRD §5).
    assert asked()["status"] == "PENDING"
    assert adapter.task_status(asked()) == "awaiting_input"


def test_a_retired_question_stops_being_outstanding():
    assert adapter.questions_of(asked())
    assert not adapter.questions_of(answered())
    assert adapter.task_status(answered()) == "running"


def test_a_finished_thread_keeps_its_questions_but_is_done():
    # The finished entry still carries the WORKFLOW_ITEM_USER_QUESTIONS it asked an
    # hour ago. Reporting that as `awaiting_input` would strand every completed task.
    assert "WORKFLOW_ITEM_USER_QUESTIONS" in json.dumps(DONE)
    assert adapter.task_status(DONE) == "done"
    assert not adapter.questions_of(DONE)


def test_lower_case_completed_is_still_completed():
    # The thread endpoint lower-cases what the stream shouts (observed 2026-08-01).
    assert adapter.task_status({**DONE, "status": "completed"}) == "done"


def test_questions_parse_into_the_shape_the_prd_promises():
    qs = adapter.questions_of(asked())
    assert len(qs) == 4
    first = qs[0]
    assert first.text == "Which policy areas should the comparison focus on most?"
    assert "Fiscal policy (spending, taxation, deficits) (Recommended)" in first.options
    assert first.multi is False and first.free_text is True
    # The "Other" entry is a free-text affordance, not an option anyone can click.
    assert all(o for o in first.options)


def test_progress_is_per_goal_not_a_percentage():
    progress = adapter.plan_of(DONE)
    assert progress and len(progress) == 6
    assert all(state == "DONE" for _, state in progress)
    assert progress[0][0].startswith("Comparing Australia")


def test_a_goal_still_running_reads_as_in_progress():
    entry = json.loads(json.dumps(DONE))
    plan = next(b for b in entry["blocks"] if b["intended_usage"] == "plan")
    plan["plan_block"]["goals"][-1]["final"] = False
    assert adapter.plan_of(entry)[-1][1] == "IN_PROGRESS"


def test_a_search_answer_has_no_plan_at_all():
    assert adapter.plan_of({"blocks": []}) is None


# --- the live view, assembled from stream diffs -------------------------------


def stream_frames() -> list[dict]:
    """The two captured frames, as they arrived: workflow state as JSON-patch diffs."""
    return [CLARIFY["asked"], CLARIFY["released"]]


def test_a_running_task_is_only_visible_on_the_stream():
    # The thread endpoint answers with an entry that has no blocks at all while the
    # task is pending (spike/probe_poll.py), so this is not an optimisation -- it is
    # the only way progress and questions can be read before the task finishes.
    live = adapter.entry_from_frames([CLARIFY["asked"]])
    assert adapter.task_status(live) == "awaiting_input"
    assert len(adapter.questions_of(live)) == 4


def test_the_live_view_retires_a_question_the_same_way():
    live = adapter.entry_from_frames(stream_frames())
    assert not adapter.questions_of(live)
    assert adapter.task_status(live) != "awaiting_input"


def test_plan_goals_are_replayed_from_their_diffs():
    frames = [
        {
            "status": "PENDING",
            "blocks": [
                {
                    "intended_usage": "plan",
                    "diff_block": {
                        "field": "plan_block",
                        "patches": [
                            {
                                "op": "replace",
                                "path": "",
                                "value": {
                                    "goals": [{"description": "a", "final": False}]
                                },
                            }
                        ],
                    },
                }
            ],
        },
        {
            "blocks": [
                {
                    "intended_usage": "plan",
                    "diff_block": {
                        "field": "plan_block",
                        "patches": [
                            {"op": "replace", "path": "/goals/0/final", "value": True},
                            {
                                "op": "add",
                                "path": "/goals/1",
                                "value": {"description": "b", "final": False},
                            },
                        ],
                    },
                }
            ]
        },
    ]
    assert adapter.plan_of(adapter.entry_from_frames(frames)) == [
        ("a", "DONE"),
        ("b", "IN_PROGRESS"),
    ]


def test_a_goal_index_off_the_end_is_ignored_not_padded():
    # Straight off the network, like every other index this parser sees (M3).
    frames = [
        {
            "blocks": [
                {
                    "intended_usage": "plan",
                    "diff_block": {
                        "field": "plan_block",
                        "patches": [
                            {"op": "add", "path": "/goals/9", "value": {"x": 1}},
                            {"op": "replace", "path": "/goals/9/final", "value": True},
                        ],
                    },
                }
            ]
        }
    ]
    assert adapter.plan_of(adapter.entry_from_frames(frames)) == []


# --- following a task ---------------------------------------------------------


class FakePage:
    """A thread endpoint that hands out a scripted sequence of bodies."""

    def __init__(self, *bodies):
        self.bodies = list(bodies)
        self.polls = 0
        self.waits = 0
        self.radios = {}
        self.keyboard = self

    def goto(self, url, **kw):
        self.url = url

    def evaluate(self, script, arg=None):
        self.polls += 1
        return self.bodies[min(self.polls - 1, len(self.bodies) - 1)]

    def wait_for_timeout(self, ms):
        self.waits += 1

    def press(self, key):
        pass


def body(entry: dict, uuid: str = "task-1") -> dict:
    return {"entries": [{**entry, "backend_uuid": uuid}]}


class SilentCDP:
    """A CDP session that never delivers a frame -- which is what a finished thread's
    page looks like: there is no stream left to reconnect to."""

    def send(self, method, params=None):
        return {}

    def on(self, event, fn):
        pass

    def detach(self):
        pass


class FakeCtx:
    def new_cdp_session(self, page):
        return SilentCDP()


def fake_chrome(page):
    import contextlib

    @contextlib.contextmanager
    def _chrome(headless=True, url="about:blank", interval=0.0):
        yield FakeCtx(), page

    return _chrome


def task(monkeypatch, page, **kw) -> ResearchTask:
    monkeypatch.setattr(research, "chrome", fake_chrome(page))
    return ResearchTask(task_id="task-1", **kw)


def test_wait_polls_until_the_task_completes(monkeypatch):
    page = FakePage(body(asked()), body(answered()), body(DONE))
    r = task(monkeypatch, page).wait(timeout=60)
    assert r.complete is True
    assert page.polls == 3


def test_wait_returns_the_entry_it_was_asked_for(monkeypatch):
    # A thread can hold several turns; the task is one of them, by id.
    other = {**DONE, "backend_uuid": "somebody-else"}
    page = FakePage({"entries": [other, {**DONE, "backend_uuid": "task-1"}]})
    assert task(monkeypatch, page).wait(timeout=60).complete is True


def test_a_timeout_does_not_cancel_the_task(monkeypatch):
    page = FakePage(body(answered()))
    with pytest.raises(PplxError) as e:
        task(monkeypatch, page).wait(timeout=0)
    # Both halves matter: that it is still running, and how to pick it up again.
    assert "not been cancelled" in str(e.value)
    assert "pplx result task-1" in str(e.value)


def test_a_failed_task_is_not_a_timeout(monkeypatch):
    page = FakePage(body({**DONE, "status": "FAILED"}))
    with pytest.raises(PplxError, match="failed"):
        task(monkeypatch, page).wait(timeout=60)


def test_skipping_a_question_presses_nothing(monkeypatch, capsys):
    # The server retires the question by itself after `timeout_seconds` (60 observed,
    # docs/M4-M8-findings.md), so skipping is waiting -- there is no Skip to click.
    page = FakePage(body(asked()), body(answered()), body(DONE))
    task(monkeypatch, page).wait(timeout=60)
    assert "clarifying question" in capsys.readouterr().err


def test_raise_on_clarify_hands_back_the_questions(monkeypatch):
    page = FakePage(body(asked()))
    with pytest.raises(ClarificationRequiredError) as e:
        task(monkeypatch, page, on_clarify="raise").wait(timeout=60)
    assert len(e.value.questions) == 4
    assert e.value.questions[0].options


def test_a_callable_answers_through_the_wizard(monkeypatch):
    seen = []

    class Wizard(FakePage):
        def get_by_role(self, role, name=None, exact=False):
            return Radio(name)

    class Radio:
        def __init__(self, name):
            self.name = name

        def count(self):
            return 1

        @property
        def first(self):
            return self

        def get_attribute(self, attr):
            return "true"

        def click(self, timeout=None):
            seen.append(self.name)

    page = Wizard(body(asked()), body(answered()), body(DONE))
    answers = [q.options[0] for q in adapter.questions_of(asked())]
    task(monkeypatch, page, on_clarify=lambda qs: answers).wait(timeout=60)
    # One radio and one Continue per question.
    assert seen == [x for a in answers for x in (a, adapter.CONTINUE)]


def test_a_callable_that_answers_the_wrong_number_of_questions_is_refused(monkeypatch):
    page = FakePage(body(asked()))
    with pytest.raises(PplxError, match="1 answers for 4"):
        task(monkeypatch, page, on_clarify=lambda qs: ["only one"]).wait(timeout=60)


def test_an_answer_that_is_not_on_offer_is_refused(monkeypatch):
    class NoSuchOption(FakePage):
        def get_by_role(self, role, name=None, exact=False):
            return Missing()

    class Missing:
        def count(self):
            return 0

    page = NoSuchOption(body(asked()))
    with pytest.raises(PplxError, match="not one of the options"):
        task(monkeypatch, page, on_clarify=lambda qs: ["nope"] * 4).wait(timeout=60)


def test_an_unknown_on_clarify_is_refused(monkeypatch):
    page = FakePage(body(asked()))
    with pytest.raises(PplxError, match="on_clarify must be"):
        task(monkeypatch, page, on_clarify="whatever").wait(timeout=60)


def test_progress_is_reported_while_waiting(monkeypatch):
    # Only while waiting: a task that is already done has an answer to hand over, and
    # reporting its goals first would be a progress bar for something finished.
    seen = []
    page = FakePage(body({**DONE, "status": "PENDING"}), body(DONE))
    task(monkeypatch, page).wait(timeout=60, on_progress=seen.append)
    assert len(seen) == 1
    assert seen[0][0][1] in ("DONE", "IN_PROGRESS")


def test_waiting_costs_one_page_load_however_long_it_takes():
    # The reconnect stream is a live feed, so following a task is one navigation and
    # then listening -- not a reload per look.
    page = FakePage(body(answered()), body(answered()), body(DONE))
    ResearchTask(task_id="task-1")._loop(
        page, None, time.monotonic() + 60, 60, False, lambda g: None
    )
    assert not hasattr(page, "url")  # `_loop` never navigates; `wait` did that once


def test_a_replayed_question_is_not_asked_twice():
    # Every reconnect replays the questions asked so far, so the same item arrives
    # again on every look. Handing a caller two copies would be this tool's bug.
    entry = asked()
    step = entry["blocks"][0]["workflow_block"]["steps"][0]
    step["items"] = step["items"] * 3
    assert len(adapter.questions_of(entry)) == 4


def test_a_task_id_doubles_as_its_thread_id():
    assert ResearchTask(task_id="abc").thread_id == "abc"
    assert ResearchTask(task_id="abc", thread_id="thread").thread_id == "thread"
