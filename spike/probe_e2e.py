#!/usr/bin/env python3
"""End-to-end check of the research path, and one open question with it.

Spends one research query. Runs the shipped code -- `Client().ask(mode="research")`
then `ResearchTask.wait()` -- rather than a hand-rolled imitation, so what passes here
is what a user gets.

The open question: `spike/probe_poll.py` saw a running task's thread document carry no
blocks at all, which is why `wait()` also tees the stream. But that probe asked for the
document *bare*, and the query string turned out to be load-bearing. So this logs, on
every poll, what the parameterised document actually contains alongside what the stream
has -- which decides whether the tee earns its keep.

Writes spike/captures/e2e-<date>.json.
"""

import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from perplexity_client import adapter  # noqa: E402
from perplexity_client.client import Client  # noqa: E402
from perplexity_client.research import ResearchTask  # noqa: E402

OUT = pathlib.Path(__file__).parent / "captures"
QUERY = (
    "what are the main exports of New Zealand, and how have they shifted since 2020"
)
log: dict[str, object] = {"query": QUERY}
polls: list[dict] = []
real_poll = ResearchTask._poll


def logged_poll(self, page, stream=None):  # type: ignore[no-untyped-def]
    body = real_poll(self, page, stream)
    entry = adapter.entry_of(body if isinstance(body, dict) else {}, self.task_id)
    live = adapter.entry_from_frames(stream.frames) if stream and stream.frames else {}
    polls.append(
        {
            "t": round(time.monotonic() - start, 1),
            "poll_status": entry.get("status"),
            "poll_blocks": [b.get("intended_usage") for b in entry.get("blocks") or ()],
            "stream_frames": len(stream.frames) if stream else 0,
            "stream_goals": len(adapter.plan_of(live) or []),
            "stream_questions": len(adapter.questions_of(live)),
            "status": self.status,
            "progress": len(self.progress or []),
        }
    )
    return body


ResearchTask._poll = logged_poll  # type: ignore[method-assign]

start = time.monotonic()
task = Client().ask(QUERY, mode="research")
assert isinstance(task, ResearchTask)
log["task_id"] = task.task_id
log["thread_id"] = task.thread_id
log["submitted_after"] = round(time.monotonic() - start, 1)
print(f"task {task.task_id} after {log['submitted_after']}s", flush=True)

try:
    r = task.wait(timeout=1500, on_progress=lambda g: print(f"  progress {g[-1]}", flush=True))
    log["answer_chars"] = len(r.text)
    log["citations"] = len(r.citations)
    log["model"] = r.model
    log["mode"] = r.mode
    log["complete"] = r.complete
    log["thread_id_returned"] = r.thread_id
    log["head"] = r.text[:200]
except Exception as e:  # keep the poll log either way -- it is the point of the run
    log["error"] = f"{type(e).__name__}: {e}"

log["waited"] = round(time.monotonic() - start, 1)
log["polls"] = polls
OUT.mkdir(exist_ok=True)
path = OUT / f"e2e-{time.strftime('%Y%m%d-%H%M%S')}.json"
path.write_text(json.dumps(log, indent=1))
print(f"-> {path}", flush=True)
