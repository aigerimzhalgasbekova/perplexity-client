#!/usr/bin/env python3
"""M1 design probe: what distinguishes an `ok` session from `expired`? (PRD US-7)

    python spike/probe_status.py

`pplx status` must report exactly one of ok / no-session / expired / challenged from
one page load. Cookie presence alone cannot prove the *server* still accepts the
session, so this hunts for an endpoint that distinguishes the two.

Three headless arms: a copy of the logged-in profile; a fresh empty profile; and a
copy of the logged-in profile with only the auth cookie deleted, which is what an
expired session actually looks like (a fresh profile is *challenged*, not merely
logged out, so it is not the control it appears to be).

Findings, 2026-07-31, Chrome 150:

    arm         title             /rest/* status   /api/auth/session
    logged-in   "Perplexity"      200              {"expires":…, "user":{…}}
    empty       "Just a moment…"  403              {}
    deauthed    "Perplexity"      200              {}

So: HTTP status codes do not discriminate -- every /rest/ endpoint serves anonymous
visitors 200 -- but NextAuth's /api/auth/session does, and it answers 200 with `{}`
from behind a Cloudflare interstitial too, which is why the client checks for a
challenge *before* it trusts the probe.
"""

import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time

from playwright.sync_api import sync_playwright

from capture import PROFILE, attach, launch_chrome, port_open

ENDPOINTS = [
    "/api/auth/session",
    "/rest/user/settings",
    "/rest/rate-limit/status",
    "/rest/thread/list_recent?limit=1",
]
ARGS = ("--headless=new", "--window-size=1280,900", "--no-first-run",
        "--no-default-browser-check")


def probe(arm: str, port: int, profile, deauth=False) -> dict:
    r = {"arm": arm, "title": None, "url": None, "cookies": [], "endpoints": {},
         "error": None}
    launch_chrome(port=port, profile=profile, extra_args=ARGS)
    with sync_playwright() as p:
        try:
            ctx, page = attach(p, port=port)
            page.goto("https://www.perplexity.ai/", wait_until="domcontentloaded")
            page.wait_for_timeout(5000)
            if deauth:
                # Drop only the auth cookie, keeping Cloudflare's clearance: that is
                # what an expired/revoked session looks like, unlike a fresh profile.
                cdp = ctx.new_cdp_session(page)
                for name in ("__Secure-next-auth.session-token", "next-auth.session-token"):
                    cdp.send("Network.deleteCookies",
                             {"name": name, "domain": ".perplexity.ai"})
                    cdp.send("Network.deleteCookies",
                             {"name": name, "domain": "www.perplexity.ai"})
                page.goto("https://www.perplexity.ai/", wait_until="domcontentloaded")
                page.wait_for_timeout(5000)
            r["title"] = page.title()
            r["url"] = page.url
            r["cookies"] = sorted(c["name"] for c in ctx.cookies()
                                  if "session" in c["name"].lower() or "auth" in c["name"].lower())
            for ep in ENDPOINTS:
                r["endpoints"][ep] = page.evaluate(
                    """ep => fetch(ep, {credentials: 'include'})
                             .then(res => res.status).catch(e => String(e))""", ep)
            r["auth_session"] = page.evaluate(
                """() => fetch('/api/auth/session', {credentials: 'include'})
                    .then(r => r.text())
                    .then(t => t.replace(/"[^"]{0,400}"/g, s => `<str:${s.length - 2}>`))
                    .catch(e => String(e))""")
            # Status codes don't discriminate (anonymous sessions get 200 too), so
            # look at the body. Booleans/nulls only -- never log identity strings.
            r["settings"] = page.evaluate(
                """() => fetch('/rest/user/settings', {credentials: 'include'})
                    .then(r => r.json())
                    .then(j => Object.fromEntries(Object.entries(j).map(([k, v]) =>
                        [k, (v === null || typeof v === 'boolean' || typeof v === 'number')
                            ? v : (typeof v === 'string' ? `<str:${v.length}>` : typeof v)])))
                    .catch(e => String(e))""")
        except Exception as e:
            r["error"] = f"{type(e).__name__}: {e}"
        finally:
            subprocess.run(["pkill", "-f", f"remote-debugging-port={port}"],
                           capture_output=True)
            for _ in range(20):
                if not port_open(port):
                    break
                time.sleep(0.5)
    return r


def main() -> None:
    if not PROFILE.exists():
        sys.exit(f"no logged-in profile at {PROFILE}; run: capture.py login")
    tmp = tempfile.mkdtemp(prefix="pplx-status-")

    def copy(name):
        dst = f"{tmp}/{name}"
        shutil.copytree(PROFILE, dst, symlinks=True, ignore_dangling_symlinks=True)
        for lock in pathlib.Path(dst).glob("Singleton*"):
            lock.unlink()
        return dst

    try:
        results = [probe("logged-in", 9231, copy("live")),
                   probe("empty", 9232, f"{tmp}/empty"),
                   probe("deauthed", 9233, copy("deauth"), deauth=True)]
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
