#!/usr/bin/env python3
"""Assert the shape claims in docs/M0-findings.md against the committed fixtures.

    python spike/verify_findings.py

Fails loudly if the documented protocol shape is wrong. This is the M0 evidence,
not the adapter -- v1 milestone 3 writes the real parser.
"""

import json
import pathlib
import re

FIXTURES = pathlib.Path(__file__).parent / "fixtures"
# In the filenames, not only in the prose: a fixture is evidence about the site *on a
# date*, and a green run against a year-old capture means nothing (PRD §7).
DATE = "2026-07-31"


def frames(path):
    return [json.loads(line[6:])
            for block in path.read_bytes().decode().split("\r\n\r\n")
            for line in block.split("\r\n")
            if line.startswith("data: ") and line[6:].strip().startswith("{")]


def terminal_frame(msgs):
    finals = [m for m in msgs if m.get("final_sse_message")]
    return finals[0] if finals else None


def main() -> None:
    msgs = frames(FIXTURES / f"search-complete-{DATE}.sse")
    assert len(msgs) > 100, len(msgs)

    # Q2: exactly one terminal frame, agreeing with status
    finals = [m for m in msgs if m.get("final_sse_message")]
    assert len(finals) == 1, f"expected 1 terminal frame, got {len(finals)}"
    fin = finals[0]
    assert fin["status"] == "COMPLETED", fin["status"]
    # text_completed fires early -- documented as unusable for completion
    assert sum(1 for m in msgs if m.get("text_completed")) > 1

    # Q3: terminal frame is self-contained, double-encoded
    steps = {s["step_type"]: s["content"] for s in json.loads(fin["text"])}
    answer = json.loads(steps["FINAL"]["answer"])
    text, cites = answer["answer"], steps["SEARCH_RESULTS"]["web_results"]
    assert text and cites
    assert [(w["name"], w["url"]) for w in cites] == \
           [(w["name"], w["url"]) for w in answer["web_results"]], "citation lists disagree"

    # §5 citation index contract: every marker resolves
    markers = {int(n) for n in re.findall(r"\[(\d+)\]", text)}
    unmapped = {n for n in markers if not 1 <= n <= len(cites)}
    assert not unmapped, f"unmapped markers: {unmapped} against {len(cites)} citations"

    # Q4: observed model distinguishable from requested
    assert fin["display_model"] and fin["user_selected_model"]

    # thread id == url slug
    assert fin["backend_uuid"] == fin["thread_url_slug"]

    # US-3: a stream cut mid-answer has no terminal frame
    cut = frames(FIXTURES / f"search-truncated-{DATE}.sse")
    assert cut, "truncated fixture is empty"
    assert terminal_frame(cut) is None, "truncated fixture still has a terminal frame"
    assert all(m.get("status") == "PENDING" for m in cut)

    print(f"ok: {len(msgs)} frames, terminal={fin['status']}, "
          f"{len(cites)} citations, markers {min(markers)}-{max(markers)}, "
          f"model={fin['display_model']} (requested {fin['user_selected_model']})")
    print(f"ok: truncated fixture {len(cut)} frames, no terminal signal")
    verify_resume()


def verify_resume() -> None:
    """Q5: the resume path is plain JSON with its own shape and its own done signal."""
    d = json.loads((FIXTURES / f"research-thread-resume-{DATE}.json").read_text())
    entry = d["entries"][0]
    assert entry["status"] == "COMPLETED", entry["status"]
    assert entry["backend_uuid"] == entry["thread_url_slug"]
    assert entry["search_mode"] == "RESEARCH", entry["search_mode"]
    assert entry["display_model"] and entry["user_selected_model"]

    blocks = {b["intended_usage"]: b for b in entry["blocks"]}
    text = blocks["ask_text"]["markdown_block"]["answer"]
    cites = blocks["web_results"]["web_result_block"]["web_results"]
    assert text and cites
    # not double-encoded here, unlike the SSE terminal frame
    assert isinstance(text, str) and not text.lstrip().startswith("{")

    markers = {int(n) for n in re.findall(r"\[(\d+)\]", text)}
    assert all(1 <= n <= len(cites) for n in markers), (markers, len(cites))
    assert blocks["plan"]["plan_block"]["progress"] == "DONE"

    print(f"ok: resume path {entry['search_mode']} {entry['status']}, {len(text)} chars, "
          f"{len(cites)} citations, markers {min(markers)}-{max(markers)}, "
          f"model={entry['display_model']}")


if __name__ == "__main__":
    main()
