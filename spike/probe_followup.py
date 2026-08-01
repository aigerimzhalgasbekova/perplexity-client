#!/usr/bin/env python3
"""M4/M5 recon: pick a model, then ask a follow-up in the same thread.

Spends two search queries on the account, deliberately on the cheapest model the
plan offers ("Sonar 2"). Answers three questions the M0/M3 captures cannot:

  1. Does clicking the Model menu change `model_preference` on the wire, and what
     does the terminal frame then report as `display_model`?
  2. What does a second turn's POST look like -- is there a thread-linkage field,
     and what is `query_source` when the query is not typed on the homepage?
  3. Does the second entry share the first's `thread_url_slug`, and where does
     `GET /rest/thread/<uuid>` put it in `entries`?

Writes spike/captures/followup-<date>.json.
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
Q1 = "what is the capital of Australia and why is it not Sydney"
Q2 = "and what is the capital of New Zealand"
MODEL = "Sonar 2"

FETCH = """path => fetch(path, {credentials: 'include'})
    .then(r => r.json()).catch(e => ({error: String(e)}))"""
DUMP = """() => [...document.querySelectorAll(
    'button,[role=button],[role=menuitem],[role=menuitemradio],[role=textbox],textarea'
)].map(e => ({
    role: e.getAttribute('role') || e.tagName.toLowerCase(),
    name: (e.getAttribute('aria-label') || e.innerText || e.placeholder || '')
        .trim().slice(0, 60),
    state: e.getAttribute('aria-checked') || '',
})).filter(e => e.name)"""


def main() -> None:
    log: dict[str, object] = {"model_requested": MODEL}
    posts: list[dict[str, object]] = []
    with chrome(headless=True, interval=20.0) as (ctx, page):
        page.goto(adapter.HOME, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)

        cdp = ctx.new_cdp_session(page)
        cdp.send("Network.enable")

        def on_request(params: dict) -> None:
            url = (params.get("request") or {}).get("url") or ""
            if adapter.ASK_PATH in url:
                posts.append(
                    {"t": round(time.monotonic(), 1), "url": url,
                     "postData": (params["request"].get("postData") or "")[:6000]}
                )

        cdp.on("Network.requestWillBeSent", on_request)

        def stream_frames(submit, label: str) -> list[dict]:
            """Tee one answer stream around `submit`, return its frames."""
            s = adapter.Stream()
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
                            "Network.streamResourceContent",
                            {"requestId": p.get("requestId")},
                        )
                    except Exception:
                        return
                    rid = p.get("requestId")
                    s.feed(base64.b64decode(got.get("bufferedData") or ""))

            def on_data(p: dict) -> None:
                if p.get("requestId") == rid and p.get("data"):
                    s.feed(base64.b64decode(p["data"]))

            def on_end(p: dict) -> None:
                if p.get("requestId") == rid:
                    s.close()

            cdp.on("Network.responseReceived", on_response)
            cdp.on("Network.dataReceived", on_data)
            cdp.on("Network.loadingFinished", on_end)
            cdp.on("Network.loadingFailed", on_end)
            try:
                submit()
                deadline = time.monotonic() + 180
                while not s.done and not s.ended and time.monotonic() < deadline:
                    page.wait_for_timeout(250)
            finally:
                cdp.remove_listener("Network.responseReceived", on_response)
                cdp.remove_listener("Network.dataReceived", on_data)
                cdp.remove_listener("Network.loadingFinished", on_end)
                cdp.remove_listener("Network.loadingFailed", on_end)
            log[f"{label}_frames"] = len(s.frames)
            return s.frames

        def summary(frames: list[dict]) -> dict:
            fin = adapter.terminal(frames) or {}
            first = frames[0] if frames else {}
            return {
                "display_model": fin.get("display_model"),
                "user_selected_model": fin.get("user_selected_model"),
                "search_mode": fin.get("search_mode"),
                "backend_uuid": fin.get("backend_uuid"),
                "context_uuid": fin.get("context_uuid"),
                "thread_url_slug": fin.get("thread_url_slug"),
                "status": fin.get("status"),
                "first_frame_keys": sorted(first)[:40],
                "first_backend_uuid": first.get("backend_uuid"),
            }

        # --- turn 1: select the model, then ask -------------------------------
        def open_picker() -> None:
            """The picker button is labelled with whatever model is selected, and
            only reads "Model" while that is the default -- so it has to be found
            by any of its possible names, not by one."""
            cfg = page.evaluate(FETCH, "/rest/models/config/v2") or {}
            labels = {
                m.get("label")
                for m in (cfg.get("models") or {}).values()
                if isinstance(m, dict) and m.get("mode") == "search"
            }
            page.keyboard.press("Escape")  # a menu left open would toggle shut
            page.wait_for_timeout(300)
            for name in ("Model", *sorted(filter(None, labels))):
                loc = page.get_by_role("button", name=name, exact=True)
                if loc.count():
                    loc.first.click()
                    return
            raise SystemExit("no model picker button found")

        def checked_model() -> str:
            """The label the picker currently shows as chosen, menu open."""
            open_picker()
            page.wait_for_timeout(1200)
            got = page.evaluate(
                """() => (document.querySelector(
                    '[role=menuitemradio][aria-checked=true]'
                )?.innerText || '').trim().split('\\n')[0]"""
            )
            return str(got)

        log["checked_before"] = checked_model()
        log["model_menu"] = page.evaluate(DUMP)
        item = page.get_by_role("menuitemradio", name=MODEL).first
        # press, not click: sibling items own submenus whose poppers overlap the
        # target, and a pointer click is then intercepted by whatever is on top.
        item.press("Enter")
        page.wait_for_timeout(1500)
        log["checked_after"] = checked_model()
        log["menu_after_pick"] = page.evaluate(
            """() => [...document.querySelectorAll('[role=menuitemradio],[role=menuitem]')]
                .map(e => ((e.innerText || '').trim().split('\\n')[0]) + '=' +
                     e.getAttribute('aria-checked'))"""
        )
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
        if not str(log["checked_after"]).lower().startswith(MODEL.lower()):
            print(json.dumps(log, indent=1)[:3000])
            raise SystemExit(f"model not selected: {log['checked_after']!r}")

        def submit1() -> None:
            adapter.submit(page, Q1)

        f1 = stream_frames(submit1, "turn1")
        log["turn1"] = summary(f1)
        log["url_after_turn1"] = page.url
        page.wait_for_timeout(3000)
        log["thread_dom"] = page.evaluate(DUMP)

        # --- turn 2: follow up in the same thread -----------------------------
        time.sleep(20)  # the pacing floor, which one lock hold does not re-apply

        def submit2() -> None:
            box = page.get_by_role("textbox").last
            box.wait_for(timeout=30_000)
            box.click()
            box.fill(Q2)
            box.press("Enter")

        f2 = stream_frames(submit2, "turn2")
        log["turn2"] = summary(f2)
        log["url_after_turn2"] = page.url

        slug = (log["turn1"] or {}).get("thread_url_slug") or (log["turn1"] or {}).get(
            "backend_uuid"
        )
        if slug:
            body = page.evaluate(FETCH, f"/rest/thread/{slug}")
            entries = (body or {}).get("entries") or []
            log["thread_get"] = {
                "slug_used": slug,
                "n_entries": len(entries),
                "entries": [
                    {
                        "backend_uuid": e.get("backend_uuid"),
                        "thread_url_slug": e.get("thread_url_slug"),
                        "context_uuid": e.get("context_uuid"),
                        "status": e.get("status"),
                        "display_model": e.get("display_model"),
                        "query_str": (e.get("query_str") or "")[:60],
                    }
                    for e in entries
                ],
            }
    log["asks"] = posts
    OUT.mkdir(exist_ok=True)
    path = OUT / f"followup-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(log, indent=1))
    print(f"-> {path}")


if __name__ == "__main__":
    main()
