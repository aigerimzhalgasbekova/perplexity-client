#!/usr/bin/env python3
"""M6 recon: watch a research task from the thread endpoint instead of the stream.

Spends one research query. `ResearchTask.wait()` is going to poll
`GET /rest/thread/<uuid>` rather than hold a stream open for a quarter of an hour, so
the states it will actually see need to exist:

  * does a *running* entry appear there at all, and with what `status`?
  * does `plan_block` carry goals and `pct_complete` while they are still moving?
  * is `WORKFLOW_AWAITING_USER` visible to a poller, or only to the stream? This is
    the one that matters -- PRD §5's trap is that the top-level status stays PENDING
    while research waits for an answer nobody is going to give.

Records one snapshot per poll, trimmed to the fields the poller reads.

Writes spike/captures/poll-<date>.json.
"""

import base64
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from perplexity_client import adapter  # noqa: E402
from perplexity_client.chrome import chrome  # noqa: E402

OUT = pathlib.Path(__file__).parent / "captures"
QUERY = (
    "research how mid-size engineering teams should choose between a monorepo and "
    "many repositories, and compare the options"
)
EVERY = 5.0
DEADLINE = 1800.0


def snapshot(body: dict, uuid: str) -> dict:
    entries = body.get("entries") or []
    entry = next((e for e in entries if e.get("backend_uuid") == uuid), None)
    if entry is None:
        return {"n_entries": len(entries), "found": False}
    blocks = {
        b.get("intended_usage"): b for b in entry.get("blocks") or () if isinstance(b, dict)
    }
    plan = (blocks.get("plan") or {}).get("plan_block") or {}
    workflow = (blocks.get("workflow_root") or {}).get("workflow_block") or {}
    items = [
        i.get("type")
        for step in workflow.get("steps") or ()
        for i in (step.get("items") or ())
        if isinstance(i, dict)
    ]
    return {
        "found": True,
        "status": entry.get("status"),
        "blocks": sorted(blocks),
        "plan_progress": plan.get("progress"),
        "pct": plan.get("pct_complete"),
        "eta": plan.get("eta_seconds_remaining"),
        "goals": [
            (g.get("description", "")[:40], g.get("final")) for g in plan.get("goals") or ()
        ],
        "workflow_status": workflow.get("status"),
        "workflow_items": items,
        "answer_len": len(
            (
                ((blocks.get("ask_text") or {}).get("markdown_block") or {}).get("answer")
                or ""
            )
        ),
    }


def main() -> None:
    log: dict[str, object] = {"query": QUERY, "every": EVERY}
    polls: list[dict] = []
    start = time.monotonic()
    with chrome(headless=True, interval=20.0) as (ctx, page):
        page.goto(adapter.HOME, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        cdp = ctx.new_cdp_session(page)
        cdp.send("Network.enable")
        stream = adapter.Stream()
        rid: str | None = None

        def on_response(p: dict) -> None:
            nonlocal rid
            r = p.get("response") or {}
            if (
                rid is None
                and adapter.ASK_PATH in (r.get("url") or "")
                and r.get("mimeType") == "text/event-stream"
            ):
                try:
                    got = cdp.send(
                        "Network.streamResourceContent", {"requestId": p.get("requestId")}
                    )
                except Exception:
                    return
                rid = p.get("requestId")
                stream.feed(base64.b64decode(got.get("bufferedData") or ""))

        def on_data(p: dict) -> None:
            if p.get("requestId") == rid and p.get("data"):
                stream.feed(base64.b64decode(p["data"]))

        cdp.on("Network.responseReceived", on_response)
        cdp.on("Network.dataReceived", on_data)

        for name in ("Search", "Deep research", "Learn step by step"):
            loc = page.get_by_role("button", name=name, exact=True)
            if loc.count():
                loc.first.click()
                break
        page.wait_for_timeout(1000)
        page.get_by_role("menuitemradio", name="Deep research").first.press("Enter")
        page.wait_for_timeout(1500)
        adapter.submit(page, QUERY)

        # The id `--detach` would print: first frame, while status is still PENDING.
        uuid = ""
        while not uuid and time.monotonic() - start < 120:
            page.wait_for_timeout(250)
            for f in stream.frames:
                if f.get("backend_uuid"):
                    uuid = str(f["backend_uuid"])
                    break
        log["task_id"] = uuid
        log["id_after_seconds"] = round(time.monotonic() - start, 1)
        if not uuid:
            sys.exit("no backend_uuid on the stream")

        while time.monotonic() - start < DEADLINE:
            body = page.evaluate(adapter.FETCH_JSON, f"/rest/thread/{uuid}")
            shot = snapshot(body or {}, uuid)
            shot["t"] = round(time.monotonic() - start, 1)
            polls.append(shot)
            print(
                f"  {shot['t']:6}s status={shot.get('status')} "
                f"workflow={shot.get('workflow_status')} pct={shot.get('pct')}"
            )
            if shot.get("status") in ("COMPLETED", "FAILED"):
                # One extra poll's worth of the finished document, so the fixture
                # below is the same thing `wait()` will parse.
                log["final_body"] = body
                break
            time.sleep(EVERY)
    log["polls"] = polls
    OUT.mkdir(exist_ok=True)
    path = OUT / f"poll-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(log, indent=1))
    print(f"-> {path}")


if __name__ == "__main__":
    main()
