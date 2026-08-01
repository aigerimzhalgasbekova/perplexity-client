#!/usr/bin/env python3
"""M6 recon: answer a Deep Research clarifying question instead of skipping it.

Spends one research query. M0 saw the questions arrive and M3 saw them expire, but
nobody has ever watched an *answer* go back, so `on_clarify=<callable>` (PRD §6) has
no observed protocol behind it. Two questions:

  1. What DOM does the question widget present, and what does clicking through it
     put on the wire? (`response_endpoint` turns out to be a handler *name*, not a
     URL, so the request shape cannot be guessed from the stream.)
  2. Does answering release the workflow the same way the 60s server-side timeout
     does -- `WORKFLOW_ITEM_USER_RESPONSE`, then `WORKFLOW_AWAITING_NEXT_STEPS`?

There is a 60-second window (`timeout_seconds` on the question payload) between the
questions arriving and the server skipping them for us, so everything after the
detection is pre-programmed rather than explored.

Writes spike/captures/clarify-<date>.json.
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
# Deliberately under-specified: M0 saw a broad query draw four questions and a narrow
# one draw none, and a first run of this probe on M0's own query drew none either, so
# the trigger is the number of unstated choices rather than the topic.
QUERY = (
    "research the best approach for building an internal data platform for my "
    "company and compare the options"
)
DEADLINE = 1800.0

DUMP = """() => [...document.querySelectorAll(
    'button,[role=button],[role=checkbox],[role=radio],[role=option],'
    + '[role=textbox],textarea,[data-testid]'
)].map(e => ({
    role: e.getAttribute('role') || e.tagName.toLowerCase(),
    name: (e.getAttribute('aria-label') || e.innerText || e.placeholder || '')
        .trim().slice(0, 70),
    testid: e.getAttribute('data-testid') || '',
    state: e.getAttribute('aria-checked') || e.getAttribute('data-state') || '',
})).filter(e => e.name || e.testid)"""


def main() -> None:
    log: dict[str, object] = {"query": QUERY}
    posts: list[dict[str, object]] = []
    start = time.monotonic()

    def t() -> float:
        return round(time.monotonic() - start, 1)

    with chrome(headless=True, interval=20.0) as (ctx, page):
        page.goto(adapter.HOME, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        cdp = ctx.new_cdp_session(page)
        cdp.send("Network.enable")
        stream = adapter.Stream()
        rid: str | None = None

        def on_request(p: dict) -> None:
            req = p.get("request") or {}
            url = req.get("url") or ""
            # Everything but the noise: analytics and autosuggest fire constantly.
            if "/rest/" in url and req.get("method") in ("POST", "PUT", "PATCH"):
                if any(x in url for x in ("/event/analytics", "/autosuggest/")):
                    return
                posts.append(
                    {"t": t(), "url": url, "method": req["method"],
                     "postData": (req.get("postData") or "")[:8000]}
                )

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
                        "Network.streamResourceContent",
                        {"requestId": p.get("requestId")},
                    )
                except Exception:
                    return
                rid = p.get("requestId")
                stream.feed(base64.b64decode(got.get("bufferedData") or ""))

        def on_data(p: dict) -> None:
            if p.get("requestId") == rid and p.get("data"):
                stream.feed(base64.b64decode(p["data"]))

        def on_end(p: dict) -> None:
            if p.get("requestId") == rid:
                stream.close()

        cdp.on("Network.requestWillBeSent", on_request)
        cdp.on("Network.responseReceived", on_response)
        cdp.on("Network.dataReceived", on_data)
        cdp.on("Network.loadingFinished", on_end)
        cdp.on("Network.loadingFailed", on_end)

        # Deep research lives behind the mode button, which is named for whatever
        # mode is currently selected (spike/capture.py found it as "Search").
        for name in ("Search", "Deep research", "Learn step by step"):
            loc = page.get_by_role("button", name=name, exact=True)
            if loc.count():
                loc.first.click()
                break
        page.wait_for_timeout(1000)
        page.get_by_role("menuitemradio", name="Deep research").first.press("Enter")
        page.wait_for_timeout(1500)
        log["mode_button"] = page.evaluate(
            """() => [...document.querySelectorAll('button')]
                .map(e => (e.innerText || '').trim()).filter(x => x && x.length < 25)"""
        )
        adapter.submit(page, QUERY)
        log["submitted_at"] = t()

        answered = False
        seen = 0
        while not stream.done and not stream.ended and time.monotonic() - start < DEADLINE:
            page.wait_for_timeout(250)
            while seen < len(stream.frames):
                frame = stream.frames[seen]
                seen += 1
                if answered or "WORKFLOW_ITEM_USER_QUESTIONS" not in json.dumps(frame):
                    continue
                answered = True
                log["questions_at"] = t()
                log["questions_frame"] = frame
                log["url"] = page.url
                # 60s from here before the server skips for us.
                page.wait_for_timeout(2000)
                log["dom_at_questions"] = page.evaluate(DUMP)
                log["html_at_questions"] = page.evaluate(
                    """() => {
                        const n = [...document.querySelectorAll('div')].find(
                            d => /Recommended/.test(d.innerText || '')
                                && d.innerText.length < 4000);
                        return n ? n.outerHTML.slice(0, 30000) : '';
                    }"""
                )
                # One question at a time: options are `role=radio`, and "Continue"
                # advances. The first run of this probe clicked the option's *text*
                # and sent `responses: []` -- the radio has to be clicked and its
                # aria-checked confirmed before advancing, or Continue reads as Skip.
                steps: list[dict] = []
                try:
                    payload = json.dumps(frame)
                    titles = [
                        t
                        for t in __import__("re").findall(
                            r'"title": "([^"]*\(Recommended\))"', payload
                        )
                    ]
                    log["recommended_titles"] = titles
                    for i, title in enumerate(titles):
                        wanted = title.replace(" (Recommended)", "")[:40]
                        step: dict[str, object] = {"i": i, "wanted": wanted}
                        radios = page.get_by_role("radio")
                        step["n_radios"] = radios.count()
                        target = page.get_by_role("radio", name=wanted).first
                        target.click(timeout=5000)
                        page.wait_for_timeout(400)
                        step["checked"] = page.evaluate(
                            """() => [...document.querySelectorAll('[role=radio]')]
                                .filter(e => e.getAttribute('aria-checked') === 'true')
                                .map(e => (e.innerText || '').trim().slice(0, 50))"""
                        )
                        step["buttons"] = page.evaluate(
                            """() => [...document.querySelectorAll('button')]
                                .map(e => (e.innerText || '').trim())
                                .filter(t => t && t.length < 24)"""
                        )
                        for label in ("Continue", "Submit", "Start research", "Done"):
                            b = page.get_by_role("button", name=label)
                            if b.count():
                                b.first.click(timeout=5000)
                                step["advanced_with"] = label
                                break
                        page.wait_for_timeout(1200)
                        steps.append(step)
                except Exception as e:
                    log["answer_error"] = f"{type(e).__name__}: {e}"
                log["steps"] = steps
                log["answered_at"] = t()

        fin = adapter.terminal(stream.frames) or {}
        log["n_frames"] = len(stream.frames)
        log["ended_at"] = t()
        log["terminal"] = {
            "status": fin.get("status"),
            "backend_uuid": fin.get("backend_uuid"),
            "thread_url_slug": fin.get("thread_url_slug"),
            "display_model": fin.get("display_model"),
            "user_selected_model": fin.get("user_selected_model"),
            "search_mode": fin.get("search_mode"),
        }
        # Every workflow frame, so the awaiting -> answered -> running transition is
        # readable afterwards without the whole 4MB stream.
        log["workflow_frames"] = [
            f
            for f in stream.frames
            if "WORKFLOW_ITEM_USER" in json.dumps(f) or "workflow_root" in json.dumps(f)
        ][:40]
    log["posts"] = posts
    OUT.mkdir(exist_ok=True)
    path = OUT / f"clarify-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(log, indent=1))
    print(f"-> {path}  ({log.get('n_frames')} frames)")


if __name__ == "__main__":
    main()
