#!/usr/bin/env python3
"""M0 protocol spike: capture perplexity.ai network traffic for one query.

Usage:
    python spike/capture.py login              # launch Chrome, wait for manual login
    python spike/capture.py search "query"
    python spike/capture.py research "query"   # Deep Research mode
    python spike/capture.py watch              # you drive the UI; this only records
    python spike/capture.py thread <uuid>      # re-open an existing thread

Drives a normally-launched Google Chrome over CDP rather than a Playwright-managed
browser: the bundled Chromium and `channel="chrome"` launches are both challenged by
Cloudflare on sight. Nothing here spoofs a fingerprint or solves a challenge (PRD §8);
it just doesn't add automation switches in the first place.

Chrome 136+ refuses --remote-debugging-port on the default profile, so this uses a
dedicated profile dir that you log into once.

Writes one JSONL of raw CDP network events to spike/captures/. Answer content only;
request/response headers and cookies are never recorded.
"""

import json
import pathlib
import socket
import subprocess
import sys
import time

from playwright.sync_api import sync_playwright

CONFIG = pathlib.Path.home() / ".config" / "perplexity-client"
PROFILE = CONFIG / "chrome-profile"
CAPTURES = pathlib.Path(__file__).parent / "captures"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 9222
SKIP = (".js", ".css", ".png", ".jpg", ".svg", ".woff", ".woff2", ".ico", ".webp")


def interesting(url: str) -> bool:
    return "perplexity.ai" in url and not url.endswith(SKIP) and "/_next/static/" not in url


def port_open(port: int = PORT) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def launch_chrome(port: int = PORT, profile=PROFILE, extra_args=()) -> None:
    if port_open(port):
        return
    CONFIG.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [CHROME, f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
         *extra_args, "https://www.perplexity.ai/"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(60):
        if port_open(port):
            return
        time.sleep(0.5)
    sys.exit("Chrome did not open a debugging port")


def attach(p, port: int = PORT):
    """Attach to the running Chrome. Never closes it -- it is the user's browser."""
    browser = p.chromium.connect_over_cdp(f"http://localhost:{port}")
    ctx = browser.contexts[0]
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    return ctx, page


def logged_in(ctx) -> bool:
    return any("session-token" in c["name"] for c in ctx.cookies())


def cmd_login() -> None:
    launch_chrome()
    with sync_playwright() as p:
        ctx, _ = attach(p)
        print("Log in to Perplexity in the Chrome window that just opened.")
        for _ in range(300):  # 10 min
            if logged_in(ctx):
                time.sleep(2)  # not page-bound: login redirects may close the tab
                print("Leave this Chrome running; capture attaches to it.")
                return
            time.sleep(2)
        sys.exit("timed out waiting for login")


def cmd_capture(mode: str, query: str, idle: float, hard_timeout: float) -> None:
    launch_chrome()
    CAPTURES.mkdir(exist_ok=True)
    out = CAPTURES / f"{mode}-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    events, last = [], time.monotonic()
    start = time.monotonic()

    def rec(name, params):
        nonlocal last
        events.append({"t": round(time.monotonic() - start, 3), "event": name, "params": params})
        last = time.monotonic()

    with sync_playwright() as p:
        ctx, page = attach(p)
        if not logged_in(ctx):
            sys.exit("Chrome profile is not logged in; run: capture.py login")

        cdp = ctx.new_cdp_session(page)
        cdp.send("Network.enable")
        urls = {}  # requestId -> url, so body/frame events can be attributed

        def on_request(params):
            url = params["request"]["url"]
            if interesting(url):
                urls[params["requestId"]] = url
                rec("request", {"requestId": params["requestId"], "url": url,
                                "method": params["request"]["method"],
                                "postData": params["request"].get("postData")})

        streaming = set()

        def on_response(params):
            url, rid = params["response"]["url"], params["requestId"]
            if not interesting(url):
                return
            urls[rid] = url
            mime = params["response"]["mimeType"]
            rec("response", {"requestId": rid, "url": url,
                             "status": params["response"]["status"], "mimeType": mime})
            if mime == "text/event-stream":
                # getResponseBody never returns a streaming body; tee it instead.
                try:
                    r = cdp.send("Network.streamResourceContent", {"requestId": rid})
                    streaming.add(rid)
                    rec("stream_start", {"requestId": rid, "url": url,
                                         "bufferedData": r.get("bufferedData", "")})
                except Exception as e:
                    rec("stream_failed", {"requestId": rid, "url": url, "error": str(e)})

        def on_data(params):
            rid = params["requestId"]
            if rid in streaming and params.get("data"):
                rec("data", {"requestId": rid, "url": urls.get(rid), "data": params["data"]})

        def on_finished(params):
            rid = params["requestId"]
            if rid not in urls:
                return
            try:
                body = cdp.send("Network.getResponseBody", {"requestId": rid})["body"]
            except Exception as e:  # streaming bodies are often unavailable
                body = f"<unavailable: {e}>"
            rec("body", {"requestId": rid, "url": urls[rid], "body": body[:200_000]})

        cdp.on("Network.requestWillBeSent", on_request)
        cdp.on("Network.responseReceived", on_response)
        cdp.on("Network.dataReceived", on_data)
        cdp.on("Network.loadingFinished", on_finished)
        for name in ("webSocketCreated", "webSocketFrameSent", "webSocketFrameReceived",
                     "webSocketClosed", "eventSourceMessageReceived"):
            cdp.on(f"Network.{name}", lambda params, n=name: rec(n, params))

        try:
            if mode == "watch":
                # You drive the UI; this only records.
                print(f"Recording for up to {hard_timeout:.0f}s. Run your query in Chrome now.")
            elif mode == "thread":
                # Q5: re-navigate an existing thread from a fresh process.
                page.goto(f"https://www.perplexity.ai/search/{query}")
            else:
                page.goto("https://www.perplexity.ai/")
                if "Just a moment" in page.title():
                    rec("challenged", {"url": page.url})
                    raise RuntimeError("cloudflare challenge; not bypassing")
                box = page.get_by_role("textbox").first
                box.wait_for(timeout=30_000)
                if mode == "research":
                    # Mode lives behind the "Search" button: Search / Deep research /
                    # Model council / Learn step by step.
                    page.get_by_role("button", name="Search", exact=True).first.click()
                    page.get_by_role("menuitemradio", name="Deep research").first.click()
                    page.wait_for_timeout(1000)
                    rec("mode_selected", {"mode": "Deep research"})
                box.click()
                box.fill(query)
                box.press("Enter")
                if mode == "research":
                    # Deep Research asks clarifying questions first and waits. The
                    # visible "Skip" is not clickable as a text node; the keyboard
                    # shortcut is. Harmless if no questions were asked.
                    page.wait_for_timeout(20_000)
                    page.keyboard.press("Meta+Enter")
                    rec("skipped_clarifiers", {"url": page.url})

            # Must yield via playwright, not time.sleep -- CDP events only dispatch
            # while the greenlet yields. Re-resolve the page if its tab is closed.
            deadline = start + hard_timeout
            while time.monotonic() < deadline and time.monotonic() - last < idle:
                try:
                    page.wait_for_timeout(500)
                except Exception:
                    if not ctx.pages:
                        raise
                    page = ctx.pages[0]
            rec("final_url", {"url": page.url})
            rec("final_title", {"title": page.title()})
        except Exception as e:  # keep whatever was captured
            rec("aborted", {"error": f"{type(e).__name__}: {e}"})

    out.write_text("\n".join(json.dumps(e) for e in events))
    print(f"{len(events)} events -> {out}")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("login", "search", "research", "watch", "thread"):
        sys.exit(__doc__)
    if sys.argv[1] == "login":
        cmd_login()
    elif sys.argv[1] == "thread":
        cmd_capture("thread", sys.argv[2], idle=15, hard_timeout=120)
    elif sys.argv[1] == "watch":
        cmd_capture("watch", "", idle=300, hard_timeout=1800)
    else:
        mode = sys.argv[1]
        q = sys.argv[2] if len(sys.argv) > 2 else "what is a quokka"
        cmd_capture(mode, q, idle=15 if mode == "search" else 120,
                    hard_timeout=120 if mode == "search" else 1800)
