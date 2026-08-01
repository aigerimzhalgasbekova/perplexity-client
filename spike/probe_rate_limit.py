#!/usr/bin/env python3
"""M2 design probe: what does the server say about its own rate limits? (PRD milestone 2)

    python spike/probe_rate_limit.py

M0 noted that the web app polls `GET /rest/rate-limit/status` before each query, and
the PRD says milestone 2 should read that rather than guess an interval. But nothing
recorded its *shape* -- only that it answers 200. This dumps it.

Finding (2026-07-31, Pro account): it states **availability per mode**, never a rate.
`/rest/user/settings` and `/api/auth/session` were checked too and carry no query
quota either -- only per-source monthly caps and subscription tier. They are not
probed here because they carry account identity and this output gets pasted around.
See docs/M2-findings.md.

Read-only: no query is issued, so this costs one page load and no quota.

Strings longer than 40 chars are replaced by `<str:N>` -- the numbers are the point.
"""

import json
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from perplexity_client.chrome import chrome  # noqa: E402
from perplexity_client.client import HOME, is_challenge  # noqa: E402

ENDPOINTS = ("/rest/rate-limit/status",)

FETCH = """async ep => {
    try {
        const r = await fetch(ep, {credentials: 'include'});
        const t = await r.text();
        let body;
        try { body = JSON.parse(t); } catch { body = t.slice(0, 400); }
        return {status: r.status, body};
    } catch (e) { return {error: String(e)}; }
}"""


def redact(v):
    if isinstance(v, dict):
        return {k: redact(x) for k, x in v.items()}
    if isinstance(v, list):
        return [redact(x) for x in v]
    if isinstance(v, str) and len(v) > 40:
        return f"<str:{len(v)}>"
    return v


def main() -> None:
    with chrome(headless=True) as (_ctx, page):
        page.goto(HOME, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        if is_challenge(page.title(), page.url):
            sys.exit("cloudflare challenge; not bypassing (PRD §8)")
        out = {ep: redact(page.evaluate(FETCH, ep)) for ep in ENDPOINTS}
    print(json.dumps(out, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
