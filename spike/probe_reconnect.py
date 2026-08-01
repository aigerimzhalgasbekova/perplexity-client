#!/usr/bin/env python3
"""How does a *reopened* thread page learn what a running task is doing?

Spends one research query. `spike/probe_e2e.py` established two things that together
leave a hole:

  * the thread document carries only `answer_tabs` and `pending_followups` while an
    entry is PENDING -- no plan, no workflow, so no progress and no questions;
  * teeing `/rest/sse/perplexity_ask` on a freshly opened thread page catches nothing,
    so whatever the page uses to render live progress, it is not that request.

Since `ask(mode="research")` closes its browser and `wait()` opens a new one, that hole
is exactly where `on_clarify` and `progress` live. This submits a task, opens the
thread in a second browser session the way `wait()` does, and records **every** request
that page makes -- which is the only way to find what to listen to.

Writes spike/captures/reconnect-<date>.json.
"""

import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from perplexity_client import adapter  # noqa: E402
from perplexity_client.chrome import chrome  # noqa: E402
from perplexity_client.client import Client  # noqa: E402
from perplexity_client.research import ResearchTask  # noqa: E402

OUT = pathlib.Path(__file__).parent / "captures"
QUERY = (
    "compare how three different countries regulate residential rental markets, "
    "and what the evidence says about each approach"
)
WATCH = 240.0


def main() -> None:
    log: dict[str, object] = {"query": QUERY}
    start = time.monotonic()
    task = Client().ask(QUERY, mode="research")
    assert isinstance(task, ResearchTask)
    log["task_id"] = task.task_id
    print(f"task {task.task_id}", flush=True)

    requests: list[dict] = []
    sse: list[dict] = []
    with chrome(headless=True) as (ctx, page):
        cdp = ctx.new_cdp_session(page)
        cdp.send("Network.enable")

        def on_request(p: dict) -> None:
            req = p.get("request") or {}
            url = req.get("url") or ""
            if "perplexity.ai" not in url or "/event/analytics" in url:
                return
            if any(url.endswith(x) for x in (".js", ".css", ".png", ".svg", ".woff2")):
                return
            requests.append(
                {
                    "t": round(time.monotonic() - start, 1),
                    "method": req.get("method"),
                    "url": url.split("?")[0],
                    "query": url.split("?")[1][:200] if "?" in url else "",
                    "post": (req.get("postData") or "")[:300],
                }
            )

        def on_response(p: dict) -> None:
            r = p.get("response") or {}
            if r.get("mimeType") == "text/event-stream":
                sse.append(
                    {
                        "t": round(time.monotonic() - start, 1),
                        "url": (r.get("url") or "").split("?")[0],
                        "status": r.get("status"),
                    }
                )

        for name in ("webSocketCreated", "webSocketFrameReceived"):
            cdp.on(
                f"Network.{name}",
                lambda p, n=name: requests.append(
                    {
                        "t": round(time.monotonic() - start, 1),
                        "method": n,
                        "url": (p.get("url") or "")[:120],
                        "query": "",
                        "post": json.dumps(p.get("response") or {})[:200],
                    }
                ),
            )
        cdp.on("Network.requestWillBeSent", on_request)
        cdp.on("Network.responseReceived", on_response)

        page.goto(adapter.thread_url(task.thread_id), wait_until="domcontentloaded")
        log["opened_after"] = round(time.monotonic() - start, 1)
        deadline = time.monotonic() + WATCH
        while time.monotonic() < deadline:
            page.wait_for_timeout(2000)
            body = page.evaluate(adapter.FETCH_JSON, adapter.thread_path(task.task_id))
            entry = adapter.entry_of(body if isinstance(body, dict) else {}, task.task_id)
            if str(entry.get("status") or "").upper() == "COMPLETED":
                log["completed_after"] = round(time.monotonic() - start, 1)
                break
    log["sse"] = sse
    log["requests"] = requests
    OUT.mkdir(exist_ok=True)
    path = OUT / f"reconnect-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(log, indent=1))
    print(f"-> {path} ({len(requests)} requests, {len(sse)} event-streams)", flush=True)


if __name__ == "__main__":
    main()
