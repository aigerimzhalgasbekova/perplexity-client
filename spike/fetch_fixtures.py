#!/usr/bin/env python3
"""Cut the M4-M6 fixtures out of the 2026-08-01 recon captures.

    python spike/fetch_fixtures.py <thread_slug>

Three of the four are extracted from captures already on disk. The fourth -- the
multi-turn thread, which is also the only place the observed model mismatch survives
with its blocks attached -- is fetched live from `GET /rest/thread/<slug>`, because
`spike/probe_followup.py` kept only a summary of the terminal frames. That fetch costs
a page load, not a query.

Account-identifying values are redacted here, exactly as in make_fixtures.py; captures
stay gitignored and only these files are committed.
"""

import glob
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from make_fixtures import DATE, FIXTURES, check_redaction, redact  # noqa: E402

CAPTURES = pathlib.Path(__file__).parent / "captures"


def newest(pattern: str) -> dict:
    paths = sorted(glob.glob(str(CAPTURES / pattern)))
    if not paths:
        sys.exit(f"no capture matching {pattern}")
    return json.loads(pathlib.Path(paths[-1]).read_text())


def write(name: str, obj: object) -> None:
    FIXTURES.mkdir(exist_ok=True)
    body = redact(json.dumps(obj, indent=1))
    out = FIXTURES / f"{name}-{DATE}.json"
    out.write_text(body)
    print(f"{out.name}: {len(body)}B")
    check_redaction(body)


def models_fixture() -> None:
    """What the site says a model *is*: id -> label/mode, and who may pick it."""
    cfg = newest("models-*.json")["models_config"]
    write(
        "models-config",
        {
            "models": cfg["models"],
            "search_config": cfg["search_config"],
            "default_models": cfg["default_models"],
        },
    )


def clarify_fixture() -> None:
    """The two frames that bracket a clarifying question: asked, then released.

    Kept as frames rather than a whole stream -- the research capture they come out
    of is 4MB, and every byte between them is answer text that proves nothing about
    the workflow protocol.
    """
    import base64

    from perplexity_client import adapter

    # Only some research runs draw questions at all (PRD §5), so this looks through
    # every research capture rather than assuming the newest one has them.
    for src in sorted(glob.glob(str(CAPTURES / "research-2026*.jsonl"))):
        raw = b""
        for line in pathlib.Path(src).read_text().splitlines():
            e = json.loads(line)
            if e["event"] == "data" and "perplexity_ask" in (
                e["params"].get("url") or ""
            ):
                raw += base64.b64decode(e["params"]["data"])
        frames = adapter.frames(raw)
        asked = next(
            (f for f in frames if "WORKFLOW_ITEM_USER_QUESTIONS" in json.dumps(f)), None
        )
        released = next(
            (f for f in frames if "WORKFLOW_ITEM_USER_RESPONSE" in json.dumps(f)), None
        )
        if asked and released:
            print(f"  clarifiers from {pathlib.Path(src).name}")
            write("research-clarify", {"asked": asked, "released": released})
            return
    sys.exit("no capture contains a clarifying-question exchange")


def thread_fixture(slug: str) -> None:
    from perplexity_client import adapter
    from perplexity_client.chrome import chrome

    with chrome(headless=True) as (_ctx, page):
        page.goto(adapter.HOME, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        body = page.evaluate(
            """path => fetch(path, {credentials: 'include'}).then(r => r.json())""",
            adapter.thread_path(slug),
        )
    entries = body.get("entries") or []
    print(f"  {len(entries)} entries")
    for e in entries:
        print(
            f"   {e.get('backend_uuid')} slug={e.get('thread_url_slug')} "
            f"selected={e.get('user_selected_model')} served={e.get('display_model')}"
        )
    write("thread-multiturn", body)


if __name__ == "__main__":
    models_fixture()
    clarify_fixture()
    if len(sys.argv) > 1:
        thread_fixture(sys.argv[1])
