#!/usr/bin/env python3
"""Turn a raw capture into committable SSE fixtures.

    python spike/make_fixtures.py spike/captures/search-<ts>.jsonl

Writes spike/fixtures/search-complete.sse (whole answer stream) and
search-truncated.sse (cut mid-answer, for the US-3 incomplete-answer test).

Account-identifying values are replaced with placeholders. Captures themselves
stay gitignored; only these redacted fixtures are committed.
"""

import base64
import json
import pathlib
import re
import sys
import time

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
# Stamped into the filename: a fixture is evidence about the site on a date, and
# nothing downstream can tell a stale one apart without it (PRD §7).
DATE = time.strftime("%Y-%m-%d")
# read_write_token grants write access to the thread -- never commit a real one.
REDACT = ("author_id", "author_username", "author_image", "read_write_token")


def redact(text: str) -> str:
    for key in REDACT:
        # SSE frames are pretty-printed, the REST body is compact -- match both.
        text = re.sub(rf'"{key}":\s*"[^"]*"', f'"{key}": "REDACTED"', text)
    return text


def thread_fixture(events, name: str) -> bool:
    """The resume path (Q5) is plain JSON, not SSE -- committed as its own fixture."""
    for e in events:
        p = e["params"]
        url = p.get("url") or ""
        if e["event"] == "body" and "/rest/thread/" in url and "list_recent" not in url:
            FIXTURES.mkdir(exist_ok=True)
            out = FIXTURES / f"{name}-{DATE}.json"
            body = redact(p["body"])
            out.write_text(body)
            print(f"{out.name}: {len(body)}B")
            check_redaction(body)
            return True
    return False


def main(path: str) -> None:
    events = [json.loads(line) for line in open(path)]
    if thread_fixture(events, "research-thread-resume"):
        return

    raw = b""
    for e in events:
        p = e["params"]
        if e["event"] in ("stream_start", "data") and "perplexity_ask" in (p.get("url") or ""):
            raw += base64.b64decode(p.get("bufferedData") or p.get("data") or "")
    text = redact(raw.decode("utf8", "replace"))
    if not text:
        sys.exit(f"no perplexity_ask stream in {path}")

    # SSE frames are CRLF-delimited on the wire; fixtures keep the bytes as sent,
    # so the adapter is tested against the real framing.
    blocks = text.split("\r\n\r\n")
    terminal = next((i for i, b in enumerate(blocks) if '"final_sse_message": true' in b), None)
    if terminal is None:
        sys.exit("no terminal frame in capture; not a complete answer")

    FIXTURES.mkdir(exist_ok=True)
    (FIXTURES / f"search-complete-{DATE}.sse").write_bytes(text.encode())
    (FIXTURES / f"search-truncated-{DATE}.sse").write_bytes(
        "\r\n\r\n".join(blocks[:terminal // 2]).encode())
    print(f"terminal frame at block {terminal}/{len(blocks)}")
    print(f"complete: {len(text)}B, truncated: {terminal // 2} blocks")
    check_redaction(text)


def check_redaction(text: str) -> None:
    leaked = [k for k in REDACT if re.search(rf'"{k}":\s*"(?!REDACTED)', text)]
    print("LEAK:" if leaked else "redaction clean:", leaked or "ok")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else sys.exit(__doc__))
