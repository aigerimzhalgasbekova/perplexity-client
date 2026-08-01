"""`pplx` CLI. Exit codes: 0 ok, 1 session not usable, 2 tool error."""

import argparse
import sys

from .chrome import session_path
from .client import Client
from .errors import PplxError

HINT = {
    "no-session": "no session yet -- run: pplx login",
    "expired": "session expired or revoked -- run: pplx login",
    "challenged": "perplexity.ai served a bot-detection challenge; this tool never "
    "bypasses one. Open Chrome yourself, then re-run: pplx login",
}


def main(argv: list[str] | None = None) -> int:
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
    args = parser.parse_args(argv)

    try:
        if args.cmd == "login":
            print(
                "Opening Chrome. Log in to Perplexity in the window that appears; "
                "this waits up to 10 minutes."
            )
            Client().login()
            print(f"logged in; session saved to {session_path()}")
            return 0
        state = Client().status()
        print(state)
        if state != "ok":
            print(HINT[state], file=sys.stderr)
            return 1
        return 0
    except PplxError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
