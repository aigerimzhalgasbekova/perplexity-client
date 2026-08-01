#!/usr/bin/env python3
"""Why did the tee catch nothing on a reopened thread page?

Spends one research query. `spike/probe_reconnect.py` found the transport --
`GET /rest/sse/perplexity_ask/reconnect/<backend_uuid>`, an event-stream served ~1s
after the thread page opens -- and `adapter.ASK_PATH` is a substring of that URL, so
`adapter.tee` *should* already bind to it. It didn't in `spike/probe_e2e.py`.

Three candidates, and this tells them apart by logging what the tee's own CDP session
sees: the response never reaches the handler; `streamResourceContent` refuses the
request; or it binds and no data follows.

Writes spike/captures/tee-<date>.json.
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
    "explain the trade-offs between event sourcing and a conventional relational "
    "schema for an order management system, with examples"
)
WATCH = 300.0


def main() -> None:
    log: dict[str, object] = {"query": QUERY}
    seen: list[dict] = []
    start = time.monotonic()
    task = Client().ask(QUERY, mode="research")
    assert isinstance(task, ResearchTask)
    log["task_id"] = task.task_id
    print(f"task {task.task_id}", flush=True)

    with chrome(headless=True) as (ctx, page):
        stream = adapter.Stream()
        cdp = ctx.new_cdp_session(page)
        cdp.send("Network.enable")
        rid: str | None = None

        def on_response(p: dict) -> None:
            nonlocal rid
            r = p.get("response") or {}
            url = r.get("url") or ""
            if "/rest/sse/" not in url:
                return
            row = {
                "t": round(time.monotonic() - start, 1),
                "url": url.split("perplexity.ai")[-1].split("?")[0],
                "mime": r.get("mimeType"),
                "status": r.get("status"),
                "matches_ask_path": adapter.ASK_PATH in url,
            }
            if rid is None and row["matches_ask_path"]:
                try:
                    got = cdp.send(
                        "Network.streamResourceContent",
                        {"requestId": p.get("requestId")},
                    )
                    rid = p.get("requestId")
                    row["bound"] = True
                    import base64 as b64

                    raw = b64.b64decode(got.get("bufferedData") or "")
                    row["buffered"] = len(raw)
                    # The whole question this run exists to answer: what do the first
                    # bytes of a reconnect stream actually look like?
                    row["head"] = repr(raw[:400])
                    row["crlf_frames"] = len(raw.split(b"\r\n\r\n"))
                    row["lf_frames"] = len(raw.split(b"\n\n"))
                    row["parsed"] = len(adapter.frames(raw))
                    stream.feed(raw)
                except Exception as e:
                    row["bound"] = False
                    row["error"] = f"{type(e).__name__}: {str(e)[:120]}"
            seen.append(row)

        def on_data(p: dict) -> None:
            import base64

            if p.get("requestId") == rid and p.get("data"):
                stream.feed(base64.b64decode(p["data"]))

        cdp.on("Network.responseReceived", on_response)
        cdp.on("Network.dataReceived", on_data)

        page.goto(adapter.thread_url(task.thread_id), wait_until="domcontentloaded")
        deadline = time.monotonic() + WATCH
        while time.monotonic() < deadline:
            page.wait_for_timeout(3000)
            live = adapter.entry_from_frames(stream.frames) if stream.frames else {}
            if stream.frames:
                print(
                    f"  {round(time.monotonic() - start, 1)}s frames={len(stream.frames)}"
                    f" goals={len(adapter.plan_of(live) or [])}"
                    f" questions={len(adapter.questions_of(live))}"
                    f" status={adapter.task_status(live)}",
                    flush=True,
                )
            if stream.done:
                log["stream_completed_after"] = round(time.monotonic() - start, 1)
                break
        log["frames"] = len(stream.frames)
        live = adapter.entry_from_frames(stream.frames) if stream.frames else {}
        log["live_status"] = adapter.task_status(live)
        log["live_goals"] = adapter.plan_of(live)
        log["live_questions"] = len(adapter.questions_of(live))
    log["sse_seen"] = seen
    OUT.mkdir(exist_ok=True)
    path = OUT / f"tee-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(log, indent=1))
    print(f"-> {path} ({log['frames']} frames)", flush=True)


if __name__ == "__main__":
    main()
