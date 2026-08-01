"""Milestone 3: the answer parser, against the dated M0 fixtures.

Green here means the parser still handles the site *as of the capture date in the
filenames* -- nothing more. Live drift is `pplx doctor`'s job, not pytest's; see the
testing posture in PRD §7.
"""

import json
import pathlib
import re

import pytest

from perplexity_client import adapter
from perplexity_client.errors import CitationError, IncompleteAnswerError

FIXTURES = pathlib.Path(__file__).parent.parent / "spike" / "fixtures"
COMPLETE = (FIXTURES / "search-complete-2026-07-31.sse").read_bytes()
TRUNCATED = (FIXTURES / "search-truncated-2026-07-31.sse").read_bytes()
THREAD = json.loads((FIXTURES / "research-thread-resume-2026-07-31.json").read_text())


def finished():
    return adapter.answer_from(adapter.terminal(adapter.frames(COMPLETE)), complete=True)


def test_answer_from_reads_the_terminal_frame():
    r = finished()
    assert r.text.startswith("The capital of Australia")
    assert len(r.citations) == 15
    assert r.model == "pplx_pro"
    assert r.mode == "search"
    assert r.thread_id and r.complete is True
    assert all(isinstance(c, adapter.Citation) and c.url and c.title for c in r.citations)


def test_citation_markers_all_resolve():
    r = finished()
    markers = {int(n) for n in re.findall(r"\[(\d+)\]", r.text)}
    assert markers
    assert all(r.citations[n - 1].url for n in markers)


def test_empty_snippet_becomes_none():
    # PRD §5 types it `str | None`; the wire sends "" for some sources (M0 Q3), and a
    # caller checking `is None` should not have to also check for the empty string.
    r = finished()
    assert any(c.snippet is None for c in r.citations)
    assert all(c.snippet != "" for c in r.citations)


def test_a_marker_past_the_citation_list_is_an_error():
    # PRD §5: an unmapped marker is surfaced, never silently dropped -- it is how a
    # real URL ends up attached to a claim it does not support.
    entry = {"blocks": [
        {"intended_usage": "ask_text", "markdown_block": {"answer": "claim [3]"}},
        {"intended_usage": "web_results",
         "web_result_block": {"web_results": [{"url": "u", "name": "t", "snippet": ""}]}}],
        "display_model": "pplx_pro", "search_mode": "SEARCH", "backend_uuid": "id"}
    with pytest.raises(CitationError):
        adapter.answer_from(entry, complete=True)


def test_zero_is_not_a_citation_marker():
    # citations[n-1] has no meaning for n == 0, and Python would happily index [-1].
    entry = {"blocks": [
        {"intended_usage": "ask_text", "markdown_block": {"answer": "claim [0]"}},
        {"intended_usage": "web_results",
         "web_result_block": {"web_results": [{"url": "u", "name": "t"}]}}]}
    with pytest.raises(CitationError):
        adapter.answer_from(entry, complete=True)


# --- the stream --------------------------------------------------------------------


def test_terminal_frame_is_the_one_that_says_so():
    fs = adapter.frames(COMPLETE)
    assert len(fs) > 100
    fin = adapter.terminal(fs)
    assert fin["final_sse_message"] is True and fin["status"] == "COMPLETED"


def test_a_final_flag_without_completed_status_is_not_terminal():
    # Both, not either: a frame claiming to be final while reporting anything else is
    # exactly the payload US-3 exists to keep out of a pipeline.
    assert adapter.terminal([{"final_sse_message": True, "status": "FAILED"}]) is None


def test_text_completed_is_never_the_completion_signal():
    # It goes true one frame *early* (M0 Q2), so keying on it would admit a payload
    # that is not yet final.
    assert adapter.terminal([{"text_completed": True, "status": "PENDING"}]) is None


def test_truncated_stream_has_no_terminal_frame():
    assert adapter.terminal(adapter.frames(TRUNCATED)) is None


def test_truncated_stream_raises_by_default():
    with pytest.raises(IncompleteAnswerError):
        adapter.parse_stream(adapter.frames(TRUNCATED), allow_incomplete=False)


def test_truncated_stream_opted_into_returns_what_arrived():
    r = adapter.parse_stream(adapter.frames(TRUNCATED), allow_incomplete=True)
    assert r.complete is False
    # Real partial text, not an empty string: the assembled markdown only ever appears
    # on the terminal frame, so the diffs have to be replayed (docs/M3-findings.md).
    assert r.text.startswith("The capital of Australia")
    assert 50 < len(r.text) < len(finished().text)
    assert r.thread_id and r.model


def test_a_frame_cut_mid_json_is_skipped_not_fatal():
    # The wire cuts wherever it cuts. make_fixtures cuts on frame boundaries, so slice
    # the bytes to get the shape a killed stream actually leaves behind.
    cut = adapter.frames(COMPLETE[:len(COMPLETE) // 3])
    assert cut and adapter.terminal(cut) is None


def test_stream_reassembles_across_arbitrary_chunk_boundaries():
    # CDP chunks have nothing to do with SSE framing (docs/M3-findings.md): a frame
    # routinely spans two dataReceived events.
    s = adapter.Stream()
    for i in range(0, len(COMPLETE), 997):
        s.feed(COMPLETE[i:i + 997])
    assert s.done
    assert [f.get("uuid") for f in s.frames] == \
           [f.get("uuid") for f in adapter.frames(COMPLETE)]


def test_stream_is_not_done_until_the_terminal_frame_lands():
    s = adapter.Stream()
    s.feed(TRUNCATED)
    assert not s.done and len(s.frames) > 10


def test_chunk_index_is_honoured_not_appended():
    # An out-of-order or repeated frame must not shift every token after it by one.
    fs = [{"backend_uuid": "id", "display_model": "m", "search_mode": "SEARCH",
           "blocks": [{"intended_usage": "ask_text", "diff_block": {
               "field": "markdown_block",
               "patches": [{"op": "replace", "path": "",
                            "value": {"chunks": ["a", "b"]}}]}}]},
          {"blocks": [{"intended_usage": "ask_text", "diff_block": {
              "field": "markdown_block",
              "patches": [{"op": "add", "path": "/chunks/3", "value": "d"},
                          {"op": "add", "path": "/chunks/2", "value": "c"}]}}]}]
    r = adapter._partial(fs)
    assert r.text == "abcd"
    assert r.thread_id == "id" and r.model == "m" and r.complete is False


def test_unknown_patch_operations_are_ignored_not_guessed():
    # A guess here invents text that was never sent -- worse than a short answer.
    fs = [{"blocks": [{"intended_usage": "ask_text", "diff_block": {
        "field": "markdown_block",
        "patches": [{"op": "replace", "path": "", "value": {"chunks": ["a"]}},
                    {"op": "remove", "path": "/chunks/0"},
                    {"op": "copy", "from": "/x", "path": "/chunks/9"}]}}]}]
    assert adapter._partial(fs).text == "a"


def test_partial_ignores_diffs_aimed_at_other_fields():
    fs = [{"blocks": [{"intended_usage": "ask_text", "diff_block": {
        "field": "plan_block",
        "patches": [{"op": "replace", "path": "", "value": {"chunks": ["nope"]}}]}}]}]
    assert adapter._partial(fs).text == ""


def test_partial_does_not_enforce_the_citation_contract():
    # The sources for a marker may simply not have arrived yet. Raising on output the
    # caller explicitly opted into would be a false alarm (docs/M3-findings.md).
    fs = [{"blocks": [{"intended_usage": "ask_text", "diff_block": {
        "field": "markdown_block",
        "patches": [{"op": "replace", "path": "", "value": {"chunks": ["claim [9]"]}}]}}]}]
    assert adapter._partial(fs).text == "claim [9]"


def test_partial_keeps_the_citations_that_did_arrive():
    r = adapter.parse_stream(adapter.frames(TRUNCATED), allow_incomplete=True)
    assert r.citations and all(c.url for c in r.citations)


def test_stream_does_not_report_done_on_a_half_delivered_terminal_frame():
    # The terminal frame is ~400KB and will not arrive in one chunk. Reporting `done`
    # off a partial one would hand the parser a truncated payload as a complete answer.
    s = adapter.Stream()
    s.feed(COMPLETE[:-200])
    assert not s.done
    s.feed(COMPLETE[-200:])
    assert s.done


# --- the resume path ---------------------------------------------------------------


def test_resume_path_parses_with_the_same_parser():
    # PRD §9 milestone 3 budgeted a second parser for this. It is the same `blocks`
    # shape, so it is the same parser (docs/M3-findings.md).
    r = adapter.parse_thread(THREAD, allow_incomplete=False)
    assert r.complete is True and r.mode == "research"
    assert len(r.text) > 5000 and len(r.citations) == 30
    assert r.model and r.thread_id


def test_resume_path_has_no_final_sse_message_and_does_not_need_one():
    assert "final_sse_message" not in THREAD["entries"][0]


def test_resume_path_of_an_unfinished_thread_raises():
    with pytest.raises(IncompleteAnswerError):
        adapter.parse_thread({"entries": [{"status": "PENDING"}]}, allow_incomplete=False)


def test_resume_path_with_no_entries_raises():
    with pytest.raises(IncompleteAnswerError):
        adapter.parse_thread({"entries": []}, allow_incomplete=False)
