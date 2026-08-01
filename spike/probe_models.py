#!/usr/bin/env python3
"""M4 recon: where the model picker lives, and what the server says a model is.

Costs one page load, not one query. Two questions:

  1. What does `GET /rest/models/config/v2` actually enumerate? (`model_preference`
     ids, their labels, and -- the part M0 missed -- `search_config[].subscription_tier`,
     which is the only entitlement signal on the wire.)
  2. What DOM does the model picker present, and does it render a model the plan
     cannot use as disabled/locked or as freely selectable?

Writes spike/captures/models-<date>.json.
"""

import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from perplexity_client import adapter  # noqa: E402
from perplexity_client.chrome import chrome  # noqa: E402

OUT = pathlib.Path(__file__).parent / "captures"

FETCH = """path => fetch(path, {credentials: 'include'})
    .then(r => r.json()).catch(e => ({error: String(e)}))"""

# Every button/menuitem the composer offers, with the attributes that say whether it is
# selectable. Text is trimmed hard: some of these carry a whole tooltip.
DUMP = """() => [...document.querySelectorAll(
    'button,[role=button],[role=menuitem],[role=menuitemradio],[role=option],[role=tab]'
)].map(e => ({
    role: e.getAttribute('role') || e.tagName.toLowerCase(),
    name: (e.getAttribute('aria-label') || e.innerText || '').trim().slice(0, 80),
    state: e.getAttribute('aria-checked') || e.getAttribute('aria-selected') || '',
    disabled: e.disabled === true || e.getAttribute('aria-disabled') === 'true',
    testid: e.getAttribute('data-testid') || '',
})).filter(e => e.name)"""


def main() -> None:
    out: dict[str, object] = {}
    with chrome(headless=True) as (_ctx, page):
        page.goto(adapter.HOME, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        out["authed"] = page.evaluate(adapter.AUTH_PROBE)
        out["models_config"] = page.evaluate(FETCH, "/rest/models/config/v2")
        out["settings"] = page.evaluate(FETCH, "/rest/user/settings")
        out["buttons_home"] = page.evaluate(DUMP)
        # The mode menu is the known-good landmark (spike/capture.py): if the model
        # picker is a sibling of it, this is where it shows up.
        for opener in ("Search", "Best", "Model", "Choose a model"):
            try:
                page.get_by_role("button", name=opener, exact=True).first.click(
                    timeout=3000
                )
            except Exception as e:
                out[f"open_{opener}"] = f"not found: {type(e).__name__}"
                continue
            page.wait_for_timeout(1200)
            out[f"open_{opener}"] = page.evaluate(DUMP)
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
        out["html_composer"] = page.evaluate(
            """() => {
                const b = document.querySelector('[role=textbox],textarea');
                let n = b; for (let i = 0; i < 6 && n && n.parentElement; i++)
                    n = n.parentElement;
                return n ? n.outerHTML.slice(0, 40000) : '';
            }"""
        )
    OUT.mkdir(exist_ok=True)
    path = OUT / f"models-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(out, indent=1))
    print(f"-> {path}")


if __name__ == "__main__":
    main()
