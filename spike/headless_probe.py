#!/usr/bin/env python3
"""Does PRD §2's headless query context survive Cloudflare? Gates milestones 1 and 3.

    python spike/headless_probe.py            # headless, then headed as control
    python spike/headless_probe.py headless   # one arm only
    python spike/headless_probe.py headed

M0 proved Cloudflare challenges Playwright-*launched* browsers but not a normally-launched
Chrome attached over CDP. Every M0 capture was headed, so PRD §2's "load session, launch
headless context" is still unproven. This runs the same query both ways against a copy of
the logged-in profile and reports which arms reach a terminal frame.

The profile is copied, not used in place: Chrome locks a user-data-dir, so probing in
place would fight the Chrome the user already has open. The copy also means a probe that
corrupts a profile costs nothing.
"""

import base64
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

from playwright.sync_api import sync_playwright

from capture import CHROME, PROFILE, attach, launch_chrome, port_open

QUERY = "what is the tallest mountain in New Zealand"
ARMS = {
    # --headless=new is the modern mode; the old one is a different browser entirely.
    "headless": ("--headless=new", "--window-size=1280,900"),
    "headed": (),
}
COMMON = ("--no-first-run", "--no-default-browser-check")


def probe(arm: str, port: int, profile) -> dict:
    """Run one query end to end. Returns what happened, never raises."""
    r = {"arm": arm, "challenged": None, "logged_in": None,
         "terminal": False, "answer_chars": 0, "error": None}
    launch_chrome(port=port, profile=profile, extra_args=(*ARMS[arm], *COMMON))
    with sync_playwright() as p:
        ctx, page = attach(p, port=port)
        try:
            cdp = ctx.new_cdp_session(page)
            cdp.send("Network.enable")
            raw, streaming = bytearray(), set()

            def on_response(params):
                if params["response"]["mimeType"] == "text/event-stream" and \
                        params["response"]["url"].endswith("perplexity_ask"):
                    rid = params["requestId"]
                    res = cdp.send("Network.streamResourceContent", {"requestId": rid})
                    streaming.add(rid)
                    raw.extend(base64.b64decode(res.get("bufferedData") or ""))

            def on_data(params):
                if params["requestId"] in streaming and params.get("data"):
                    raw.extend(base64.b64decode(params["data"]))

            cdp.on("Network.responseReceived", on_response)
            cdp.on("Network.dataReceived", on_data)

            page.goto("https://www.perplexity.ai/", wait_until="domcontentloaded")
            page.wait_for_timeout(5000)
            r["challenged"] = "Just a moment" in page.title() or "/cdn-cgi/challenge" in page.url
            r["logged_in"] = any("session-token" in c["name"] for c in ctx.cookies())
            if r["challenged"]:
                return r

            box = page.get_by_role("textbox").first
            box.wait_for(timeout=30_000)
            box.click()
            box.fill(QUERY)
            box.press("Enter")

            deadline = time.monotonic() + 120
            while time.monotonic() < deadline and b'"final_sse_message": true' not in raw:
                page.wait_for_timeout(500)
            r["terminal"] = b'"final_sse_message": true' in raw
            r["answer_chars"] = len(raw)
        except Exception as e:
            r["error"] = f"{type(e).__name__}: {e}"
        finally:
            # This Chrome is ours, not the user's -- always take it down.
            subprocess.run(["pkill", "-f", f"remote-debugging-port={port}"],
                           capture_output=True)
            for _ in range(20):
                if not port_open(port):
                    break
                time.sleep(0.5)
    return r


def main(arms) -> None:
    if not PROFILE.exists():
        sys.exit(f"no logged-in profile at {PROFILE}; run: capture.py login")
    tmp = tempfile.mkdtemp(prefix="pplx-probe-")
    copy = f"{tmp}/profile"
    print(f"copying profile -> {copy}")
    shutil.copytree(PROFILE, copy, symlinks=True, ignore_dangling_symlinks=True)
    # Singleton* point at the live Chrome; left in place the copy hands off to it and exits.
    for lock in pathlib.Path(copy).glob("Singleton*"):
        lock.unlink()
    try:
        results = [probe(a, 9223 + i, copy) for i, a in enumerate(arms)]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\nChrome {subprocess.run([CHROME, '--version'], capture_output=True, text=True).stdout.strip()}")
    print(f"{'arm':10} {'challenged':11} {'logged_in':10} {'terminal':9} bytes")
    for r in results:
        print(f"{r['arm']:10} {str(r['challenged']):11} {str(r['logged_in']):10} "
              f"{str(r['terminal']):9} {r['answer_chars']}")
        if r["error"]:
            print(f"           error: {r['error'][:160]}")

    verdict = {r["arm"]: r["terminal"] for r in results}
    if verdict.get("headless"):
        print("\nVERDICT: headless works. PRD §2's headless query context stands.")
    elif verdict.get("headed") and "headless" in verdict:
        print("\nVERDICT: headless is blocked but headed works -- headlessness is the "
              "cause, not the profile copy. PRD §2 must drop the headless context.")
    else:
        print("\nVERDICT: inconclusive -- the headed control also failed, so something "
              "other than headlessness is wrong. Check the error above.")


if __name__ == "__main__":
    main(sys.argv[1:] or ["headless", "headed"])
