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
# The same query, captured after the answer moved out of `ask_text` and into the
# workflow's own text items (2026-08-04). Both dates are kept and both are parsed:
# threads answered before the move still resume.
COMPLETE_08_04 = (FIXTURES / "search-complete-2026-08-04.sse").read_bytes()
TRUNCATED_08_04 = (FIXTURES / "search-truncated-2026-08-04.sse").read_bytes()
# The only capture in the repo holding an answer split across sections, and so the only
# evidence for what separator Perplexity itself rejoins them with.
MULTITURN_08_01 = json.loads(
    (FIXTURES / "thread-multiturn-2026-08-01.json").read_text()
)


def finished():
    return adapter.answer_from(
        adapter.terminal(adapter.frames(COMPLETE)), complete=True
    )


def test_the_reconnect_streams_framing_is_read_too():
    # Byte-for-byte the head of a real `/rest/sse/perplexity_ask/reconnect/<uuid>`
    # response (2026-08-01): LF endings where the ask stream uses CRLF, `data:` with
    # no space, and an SSE comment first. Requiring either detail cost every frame --
    # invisibly, because a stream with no recognised frames looks like one that sent
    # nothing at all, which is exactly how a running research task reports itself.
    raw = b': hello\n\nevent:message\ndata:{"status": "PENDING", "uuid": "x"}\n\n'
    assert [f["status"] for f in adapter.frames(raw)] == ["PENDING"]


def test_both_framings_survive_arriving_a_byte_at_a_time():
    raw = b'event:message\ndata:{"a": 1}\n\nevent: message\r\ndata: {"a": 2}\r\n\r\n'
    s = adapter.Stream()
    for i in range(len(raw)):
        s.feed(raw[i : i + 1])
    assert [f["a"] for f in s.frames] == [1, 2]


def test_answer_from_reads_the_terminal_frame():
    r = finished()
    assert r.text.startswith("The capital of Australia")
    assert len(r.citations) == 15
    assert r.model == "pplx_pro"
    assert r.mode == "search"
    assert r.thread_id and r.complete is True
    assert all(
        isinstance(c, adapter.Citation) and c.url and c.title for c in r.citations
    )


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
    entry = {
        "blocks": [
            {"intended_usage": "ask_text", "markdown_block": {"answer": "claim [3]"}},
            {
                "intended_usage": "web_results",
                "web_result_block": {
                    "web_results": [{"url": "u", "name": "t", "snippet": ""}]
                },
            },
        ],
        "display_model": "pplx_pro",
        "search_mode": "SEARCH",
        "backend_uuid": "id",
    }
    with pytest.raises(CitationError):
        adapter.answer_from(entry, complete=True)


def test_a_complete_answer_with_no_text_is_refused():
    # The single worst outcome this tool can produce (PRD §10): every lookup into the
    # payload is `or {}`-guarded, so a renamed block collapses to "" and the marker
    # check then passes vacuously. An agent reads `complete=True, text=""` as
    # "Perplexity found nothing" -- plausible, actionable and wrong.
    fin = adapter.terminal(adapter.frames(COMPLETE))
    moved = {
        **fin,
        "blocks": [b for b in fin["blocks"] if b.get("intended_usage") != "ask_text"],
    }
    with pytest.raises(IncompleteAnswerError):
        adapter.answer_from(moved, complete=True)


def test_an_answer_block_that_only_holds_a_diff_is_refused_too():
    # The other shape of the same drift: the block is there but never assembled.
    fin = adapter.terminal(adapter.frames(COMPLETE))
    blocks = [
        {"intended_usage": "ask_text", "diff_block": {"field": "markdown_block"}}
        if b.get("intended_usage") == "ask_text"
        else b
        for b in fin["blocks"]
    ]
    with pytest.raises(IncompleteAnswerError):
        adapter.answer_from({**fin, "blocks": blocks}, complete=True)


def test_an_empty_partial_answer_is_still_allowed():
    # `allow_incomplete=True` over a stream cut before any text is legitimately empty.
    assert adapter.answer_from({}, complete=False).text == ""


# --- the answer's move out of ask_text (2026-08-04) --------------------------------


def finished_08_04():
    return adapter.parse_stream(adapter.frames(COMPLETE_08_04), allow_incomplete=False)


def test_the_answer_is_read_out_of_the_workflow_text_items():
    # What broke live: `ask_text` is simply gone from this capture, and every lookup
    # into it is `or {}`-guarded, so the parser reported a complete answer with no
    # text -- PRD §10's critical row -- rather than reading where the text went.
    fin = adapter.terminal(adapter.frames(COMPLETE_08_04))
    assert not adapter._block(fin, "ask_text", "markdown_block")
    assert fin["structured_answer_block_usages"] == ["workflow_root"]
    r = finished_08_04()
    assert r.text.startswith("The capital of Australia")
    assert r.citations and r.model == "pplx_pro" and r.mode == "search"
    assert all(1 <= n <= len(r.citations) for n in adapter.markers_in(r.text))


def test_research_narration_is_not_mistaken_for_the_answer():
    # Research has always narrated itself through this same workflow, so "an item with
    # text in it" is not the test -- `variant` is. Reading the commentary back as the
    # answer would be plausible, wrong, and indistinguishable downstream.
    entry = {
        "blocks": [
            {
                "intended_usage": adapter.WORKFLOW,
                "workflow_block": {
                    "steps": [
                        {
                            "items": [
                                {
                                    "type": "WORKFLOW_ITEM_CONTENT",
                                    "payload": {"text_payload": {"text": "Searching…"}},
                                }
                            ]
                        }
                    ]
                },
            }
        ]
    }
    assert adapter.answer_text(entry) == ""
    with pytest.raises(IncompleteAnswerError):
        adapter.answer_from(entry, complete=True)


def test_sections_split_across_items_rejoin_the_way_perplexity_joins_them():
    # Pinned to observed assembly, not to a guess: in thread-multiturn-2026-08-01 the
    # sectioned `ask_text_<n>_markdown` blocks rejoin into the assembled `ask_text`
    # answer on exactly one character each, and the boundaries read
    # "…Sydney.[1][2][3]\n## Why Canberra". A blank line here would be text the server
    # never sent, returned with complete=True and nothing downstream to catch it.
    entry = {
        "blocks": [
            {
                "intended_usage": adapter.WORKFLOW,
                "workflow_block": {
                    "steps": [
                        {
                            "items": [
                                {
                                    "type": "WORKFLOW_ITEM_TEXT",
                                    "variant": "answer",
                                    "payload": {"text_payload": {"text": t}},
                                }
                            ]
                        }
                        for t in ("Canberra.", "## Why", "It was a compromise.")
                    ]
                },
            }
        ]
    }
    assert adapter.answer_text(entry) == "Canberra.\n## Why\nIt was a compromise."


def test_the_observed_section_join_is_the_one_we_use():
    # The evidence the constant above is pinned to, read back out of the fixture rather
    # than restated: sections rejoin on SECTION_SEP to the assembled answer's length.
    # (Not byte-equality -- the assembled copy reorders some citation markers, which is
    # a separate question from where the sections meet.)
    checked = 0
    for entry in MULTITURN_08_01["entries"]:
        blocks = entry["blocks"]
        secs = [
            b["markdown_block"]["answer"]
            for b in blocks
            if re.fullmatch(r"ask_text_\d+_markdown", b["intended_usage"])
        ]
        if len(secs) < 2:
            continue
        whole = next(
            b["markdown_block"]["answer"]
            for b in blocks
            if b["intended_usage"] == "ask_text"
        )
        assert len(adapter.SECTION_SEP.join(secs)) == len(whole)
        checked += 1
    assert checked, "the fixture stopped carrying a sectioned answer"


def test_a_stream_cut_before_the_text_field_still_replays_its_chunks():
    # The assembled `text` lands on the item's last patch; before that `chunks` is the
    # only text there is, one token per patch.
    r = adapter.parse_stream(adapter.frames(TRUNCATED_08_04), allow_incomplete=True)
    assert r.complete is False
    assert r.text.startswith("The capital of Australia")
    assert finished_08_04().text.startswith(r.text)


def test_replaying_the_same_frames_twice_gives_the_same_answer():
    # A running task is polled by re-reading one growing frame list, so a replay that
    # patched those frames in place would compound: each pass would start from the last
    # pass's leftovers and the answer would grow a copy of itself every time.
    fs = adapter.frames(TRUNCATED_08_04)
    first = adapter._partial(fs).text
    assert adapter._partial(fs).text == first
    assert fs == adapter.frames(TRUNCATED_08_04)


def test_partial_text_only_ever_grows_by_appending():
    # Replayed prefix by prefix, each step must extend the last: text that shrinks or
    # rewrites itself means the patches were applied to the wrong place.
    fs = adapter.frames(COMPLETE_08_04)
    seen = ""
    for i in range(1, len(fs) + 1):
        text = adapter._partial(fs[:i]).text
        assert text.startswith(seen)
        seen = text
    assert seen == finished_08_04().text


def test_zero_is_not_a_citation_marker():
    # citations[n-1] has no meaning for n == 0, and Python would happily index [-1].
    entry = {
        "blocks": [
            {"intended_usage": "ask_text", "markdown_block": {"answer": "claim [0]"}},
            {
                "intended_usage": "web_results",
                "web_result_block": {"web_results": [{"url": "u", "name": "t"}]},
            },
        ]
    }
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
    cut = adapter.frames(COMPLETE[: len(COMPLETE) // 3])
    assert cut and adapter.terminal(cut) is None


def test_stream_reassembles_across_arbitrary_chunk_boundaries():
    # CDP chunks have nothing to do with SSE framing (docs/M3-findings.md): a frame
    # routinely spans two dataReceived events.
    s = adapter.Stream()
    for i in range(0, len(COMPLETE), 997):
        s.feed(COMPLETE[i : i + 997])
    assert s.done
    # Structural equality, not a count and not a key: `uuid` is the message id and is
    # the same on every frame, so comparing it would pass on a stream whose frames were
    # reordered or duplicated in place -- which is precisely what the diff replay would
    # then assemble wrongly.
    assert s.frames == adapter.frames(COMPLETE)


def test_stream_is_not_done_until_the_terminal_frame_lands():
    s = adapter.Stream()
    s.feed(TRUNCATED)
    assert not s.done and len(s.frames) > 10


def patched(*patches, **top):
    fs = [
        {
            "backend_uuid": "id",
            "display_model": "m",
            "search_mode": "SEARCH",
            **top,
            "blocks": [
                {
                    "intended_usage": "ask_text",
                    "diff_block": {"field": "markdown_block", "patches": list(patches)},
                }
            ],
        }
    ]
    return adapter._partial(fs)


def test_chunks_append_in_order():
    r = patched(
        {"op": "replace", "path": "", "value": {"chunks": ["a", "b"]}},
        {"op": "add", "path": "/chunks/2", "value": "c"},
        {"op": "add", "path": "/chunks/3", "value": "d"},
    )
    assert r.text == "abcd"
    assert r.thread_id == "id" and r.model == "m" and r.complete is False


def test_a_patch_before_the_start_never_rewrites_delivered_text():
    # int() takes "-1" happily, the padding is then a no-op, and chunks[-1] = value
    # overwrites the last token that really arrived -- producing text the server never
    # sent, which is exactly what this replay exists not to do.
    r = patched(
        {
            "op": "replace",
            "path": "",
            "value": {"chunks": ["Canberra ", "is the capital."]},
        },
        {"op": "add", "path": "/chunks/-1", "value": "is NOT the capital."},
    )
    assert r.text == "Canberra is the capital."


def test_a_patch_past_the_end_is_refused():
    # Beyond the array's length is an error per RFC 6902, and the observed wire never
    # does it -- every index in both captures is the next one. Honouring it would mean
    # padding with tokens nobody sent, and a hostile index would allocate without bound.
    r = patched(
        {"op": "replace", "path": "", "value": {"chunks": ["a"]}},
        {"op": "add", "path": "/chunks/20000000", "value": "x"},
    )
    assert r.text == "a"


def test_an_index_too_long_to_convert_is_ignored_not_raised():
    # `isdecimal()` says yes to any number of digits; CPython's int() raises ValueError
    # past 4300 of them. parse_stream runs outside the PplxError contract, so a path of
    # nines off the network would surface as a traceback rather than a diagnosis --
    # after the query was already spent.
    r = patched(
        {"op": "replace", "path": "", "value": {"chunks": ["a"]}},
        {"op": "add", "path": "/chunks/" + "9" * 5000, "value": "x"},
        {"op": "replace", "path": "/" + "9" * 5000 + "/chunks/0", "value": "x"},
    )
    assert r.text == "a"


def test_a_frame_delivered_twice_does_not_double_the_answer():
    # Replay is idempotent by construction: `add` over an index that already holds a
    # token assigns rather than shifting. The stream only ever names the next index, so
    # a repeat is a re-delivery -- inserting would hand back an answer containing itself
    # twice, well-formed and complete-looking, with nothing downstream to catch it.
    fs = adapter.frames(TRUNCATED_08_04)
    once = adapter._partial(fs).text
    assert adapter._partial(fs + fs[1:]).text == once


def test_unknown_patch_operations_are_ignored_not_guessed():
    # A guess here invents text that was never sent -- worse than a short answer.
    fs = [
        {
            "blocks": [
                {
                    "intended_usage": "ask_text",
                    "diff_block": {
                        "field": "markdown_block",
                        "patches": [
                            {"op": "replace", "path": "", "value": {"chunks": ["a"]}},
                            {"op": "remove", "path": "/chunks/0"},
                            {"op": "copy", "from": "/x", "path": "/chunks/9"},
                        ],
                    },
                }
            ]
        }
    ]
    assert adapter._partial(fs).text == "a"


def test_partial_ignores_diffs_aimed_at_other_fields():
    fs = [
        {
            "blocks": [
                {
                    "intended_usage": "ask_text",
                    "diff_block": {
                        "field": "plan_block",
                        "patches": [
                            {"op": "replace", "path": "", "value": {"chunks": ["nope"]}}
                        ],
                    },
                }
            ]
        }
    ]
    assert adapter._partial(fs).text == ""


def test_partial_does_not_enforce_the_citation_contract():
    # The sources for a marker may simply not have arrived yet. Raising on output the
    # caller explicitly opted into would be a false alarm (docs/M3-findings.md).
    fs = [
        {
            "blocks": [
                {
                    "intended_usage": "ask_text",
                    "diff_block": {
                        "field": "markdown_block",
                        "patches": [
                            {
                                "op": "replace",
                                "path": "",
                                "value": {"chunks": ["claim [9]"]},
                            }
                        ],
                    },
                }
            ]
        }
    ]
    assert adapter._partial(fs).text == "claim [9]"


def test_partial_keeps_the_citations_that_did_arrive():
    r = adapter.parse_stream(adapter.frames(TRUNCATED), allow_incomplete=True)
    assert r.citations and all(c.url for c in r.citations)


def test_stream_flushes_a_terminal_frame_the_connection_ended_on():
    # `feed` keeps the trailing block back because it is usually half-written. If the
    # connection closes right after the terminal frame and before its separator, that
    # held-back block *is* the whole answer -- and refusing it burns the query that
    # bought a complete answer.
    blocks = COMPLETE.split(adapter.FRAME_SEP)
    term = next(i for i, b in enumerate(blocks) if b'"final_sse_message": true' in b)
    s = adapter.Stream()
    s.feed(adapter.FRAME_SEP.join(blocks[: term + 1]))
    assert not s.done  # still held back: more bytes could always follow
    s.close()
    assert s.done and s.ended


def test_closing_a_stream_cut_mid_frame_admits_nothing():
    s = adapter.Stream()
    s.feed(COMPLETE[: len(COMPLETE) // 3])
    before = len(s.frames)
    s.close()
    # The tail is half-written JSON, not a frame. Flushing must not invent one.
    assert not s.done and len(s.frames) == before


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
        adapter.parse_thread(
            {"entries": [{"status": "PENDING"}]}, allow_incomplete=False
        )


def test_resume_path_with_no_entries_raises():
    with pytest.raises(IncompleteAnswerError):
        adapter.parse_thread({"entries": []}, allow_incomplete=False)


def test_a_bracketed_number_inside_code_is_not_a_citation_marker():
    # Perplexity is used for programming questions, and `nums[0]` is not a citation.
    # Reading markers out of the raw markdown throws away a complete, correct answer
    # -- after the query is already spent, with a message blaming the frontend.
    entry = {
        "blocks": [
            {
                "intended_usage": "ask_text",
                "markdown_block": {
                    "answer": "Index from zero.[1]\n\n"
                    "```python\nprint(nums[0], nums[10])\n```\n\n"
                    "Or use `arr[3]` directly.[2]"
                },
            },
            {
                "intended_usage": "web_results",
                "web_result_block": {
                    "web_results": [
                        {"url": "u1", "name": "t1"},
                        {"url": "u2", "name": "t2"},
                    ]
                },
            },
        ]
    }
    r = adapter.answer_from(entry, complete=True)
    assert r.text.count("nums[0]") == 1  # left in the answer, just not read as a marker


def test_stripping_code_does_not_disable_the_citation_contract():
    # The other half of the fix: an unmapped marker in prose still raises, code or no
    # code. Without this the fix above could pass by never checking anything.
    entry = {
        "blocks": [
            {
                "intended_usage": "ask_text",
                "markdown_block": {
                    "answer": "```python\nnums[0]\n```\n\nSee the spec.[7]"
                },
            },
            {
                "intended_usage": "web_results",
                "web_result_block": {"web_results": [{"url": "u", "name": "t"}]},
            },
        ]
    }
    with pytest.raises(CitationError, match=r"\[7\]"):
        adapter.answer_from(entry, complete=True)


def test_an_unrecognised_mode_is_not_reported_as_search():
    # `else "search"` is a guess in the flattering direction: a renamed or new mode
    # would be reported as the one mode this milestone claims to drive.
    odd = adapter.answer_from({"search_mode": "COPILOT"}, complete=False)
    assert odd.mode == "copilot"
    assert adapter.answer_from({}, complete=False).mode == "unknown"
