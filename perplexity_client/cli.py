"""`pplx` CLI. Exit codes: 0 ok, 1 session not usable, 2 tool error.

A thin wrapper on purpose (PRD §4): every decision lives in `Client`, and this file
only turns arguments into calls and results into text. Anything it prints that is not
the answer goes to stderr, so `--json | jq` keeps working when a run has something to
warn about.
"""

import argparse
import dataclasses
import json
import sys

from .adapter import Response
from .chrome import session_path
from .client import Client
from .errors import (
    ChallengeEncounteredError,
    IncompleteAnswerError,
    PplxError,
    SessionExpiredError,
)
from .research import ResearchTask

HINT = {
    "no-session": "no session yet -- run: pplx login",
    "expired": "session expired or revoked -- run: pplx login",
    "challenged": "perplexity.ai served a bot-detection challenge; this tool never "
    "bypasses one. Open Chrome yourself, then re-run: pplx login",
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pplx", description="Automate your own Perplexity account."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="one-time manual login in a visible browser window")
    sub.add_parser(
        "status",
        help="report session state: ok | no-session | expired | "
        "challenged. Costs one page load, not one query.",
    )
    ask = sub.add_parser("ask", help="ask a question; costs one query")
    ask.add_argument("query")
    ask.add_argument("--mode", choices=("search", "research"), default="search")
    ask.add_argument(
        "--model",
        default="best",
        help="model name or id; 'best' (the default) lets Perplexity choose and "
        "accepts whatever answers",
    )
    ask.add_argument("--thread", help="continue this thread instead of starting one")
    ask.add_argument(
        "--detach",
        action="store_true",
        help="research only: print the task id and exit without waiting",
    )
    ask.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="print a cut-off answer instead of failing on it",
    )
    ask.add_argument("--json", action="store_true", help="structured output on stdout")
    result = sub.add_parser("result", help="retrieve a detached research task")
    result.add_argument("task_id")
    result.add_argument(
        "--allow-incomplete", action="store_true", help="accept an unfinished answer"
    )
    result.add_argument("--json", action="store_true")
    sub.add_parser(
        "doctor",
        help="spend one real query and check every adapter invariant against the "
        "live site. Never run in CI.",
    )
    return parser


def _as_json(r: Response) -> str:
    return json.dumps(dataclasses.asdict(r), indent=2)


def _show(r: Response, as_json: bool) -> None:
    if not r.complete:
        # stderr, always: a truncation warning that vanished into a JSON pipe is the
        # PRD §10 critical failure wearing a different hat.
        print(
            "warning: this answer is incomplete (cut off mid-stream)", file=sys.stderr
        )
    if as_json:
        print(_as_json(r))
        return
    print(r.text)
    if r.citations:
        print("\nSources:")
        for i, c in enumerate(r.citations, 1):
            print(f"  [{i}] {c.title or c.url}\n      {c.url}")
    print(f"\n({r.model}, {r.mode}, thread {r.thread_id})", file=sys.stderr)


def _progress(goals: list[tuple[str, str]]) -> None:
    done = sum(1 for _, state in goals if state == "DONE")
    current = next((d for d, state in goals if state != "DONE"), "")
    print(f"  [{done}/{len(goals)}] {current}"[:100], file=sys.stderr)


def _ask(args: argparse.Namespace) -> int:
    if args.detach and args.mode != "research":
        raise PplxError("--detach only applies to --mode research")
    out = Client().ask(
        args.query,
        mode=args.mode,
        model=args.model,
        thread_id=args.thread,
        allow_incomplete=args.allow_incomplete,
    )
    if isinstance(out, ResearchTask):
        if args.detach:
            # The id exists seconds after submitting, long before the answer does
            # (M0 Q5) -- which is the whole reason this can exit here.
            print(json.dumps({"task_id": out.task_id}) if args.json else out.task_id)
            return 0
        print(f"research task {out.task_id}; waiting...", file=sys.stderr)
        out = out.wait(allow_incomplete=args.allow_incomplete, on_progress=_progress)
    _show(out, args.json)
    return 0


def _result(args: argparse.Namespace) -> int:
    task = Client().task(args.task_id)
    try:
        # `timeout=0` polls once and gives up, which is exactly "is it ready yet".
        r = task.wait(timeout=0, allow_incomplete=args.allow_incomplete)
    except PplxError:
        if task.status == "done":
            raise  # a real parse failure, not an unfinished task
        # Not an error: "still running" is a legitimate answer to "is it ready", and
        # scripts poll on it. Exit 3 so a shell loop can tell it from a broken run.
        print(task.status, file=sys.stderr)
        if task.progress:
            _progress(task.progress)
        return 3
    _show(r, args.json)
    return 0


def _doctor() -> int:
    failed = 0
    for name, ok, detail in Client().doctor():
        print(f"{'ok  ' if ok else 'FAIL'}  {name}: {detail}")
        failed += not ok
    if failed:
        print(f"{failed} invariant(s) failed", file=sys.stderr)
    return 2 if failed else 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.cmd == "login":
            print(
                "Opening Chrome. Log in to Perplexity in the window that appears; "
                "this waits up to 10 minutes."
            )
            Client().login()
            print(f"logged in; session saved to {session_path()}")
            return 0
        if args.cmd == "ask":
            return _ask(args)
        if args.cmd == "result":
            return _result(args)
        if args.cmd == "doctor":
            return _doctor()
        state = Client().status()
        print(state)
        if state != "ok":
            print(HINT[state], file=sys.stderr)
            return 1
        return 0
    except (SessionExpiredError, ChallengeEncounteredError) as e:
        # Exit 1 is "the session is not usable", which is a different thing for a
        # caller to react to than "the tool broke" -- one is `pplx login`, the other
        # is a bug report.
        print(f"error: {e}", file=sys.stderr)
        return 1
    except IncompleteAnswerError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except PplxError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
