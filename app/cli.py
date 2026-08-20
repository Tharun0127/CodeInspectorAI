"""
Command line entry point.

    python -m app.cli owner/repo 42
    python -m app.cli owner/repo 42 --dry-run
    python -m app.cli owner repo 42 --save review.md
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any, Dict

from graph.graph import run_pr_review


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="codeinspector",
        description="Run an AI review on a GitHub pull request.",
    )
    parser.add_argument("target", help='"owner/repo" (or just the owner, with repo next).')
    parser.add_argument("rest", nargs="+", help="[repo] pr_number")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Generate the review but do not post it to GitHub.",
    )
    parser.add_argument("--save", metavar="PATH", help="Write the review markdown to a file.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging.")
    return parser.parse_args(argv)


def _resolve_target(args: argparse.Namespace) -> Dict[str, Any]:
    """Accept both `owner/repo 42` and `owner repo 42`."""
    if "/" in args.target:
        owner, repo_name = args.target.split("/", 1)
        pr_raw = args.rest[0]
    else:
        owner, repo_name = args.target, args.rest[0]
        if len(args.rest) < 2:
            raise SystemExit("Missing PR number. Usage: python -m app.cli owner/repo 42")
        pr_raw = args.rest[1]

    try:
        pr_number = int(pr_raw)
    except ValueError:
        raise SystemExit("PR number must be an integer, got: " + pr_raw)

    return {"owner": owner, "repo_name": repo_name, "pr_number": pr_number}


def _force_utf8_stdout() -> None:
    """
    The review markdown contains emoji; the default Windows console codepage
    (cp1252) cannot encode them and would raise UnicodeEncodeError on print.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover - redirected stream
                pass


def main(argv=None) -> int:
    args = _parse_args(argv)
    _force_utf8_stdout()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-8s %(name)s: %(message)s",
    )

    state = _resolve_target(args)
    if args.dry_run:
        state["post_comment"] = False

    print("Reviewing {o}/{r}#{n} ...".format(
        o=state["owner"], r=state["repo_name"], n=state["pr_number"]))

    result = run_pr_review(state)

    comment = result.get("formatted_comment", "")
    if comment:
        print("\n" + comment + "\n")
    if args.save and comment:
        Path(args.save).write_text(comment, encoding="utf-8")
        print("Saved review to " + args.save)

    print("-" * 60)
    print("Status          :", result.get("review_status"))
    print("Verdict         :", result.get("overall_verdict"))
    print("Quality score   :", result.get("code_quality_score"))
    print("Review comments :", len(result.get("review_comments") or []))
    print("Security issues :", len(result.get("security_issues") or []))
    if result.get("posted_comment_url"):
        print("Posted to       :", result["posted_comment_url"])
    for error in result.get("errors") or []:
        print("Error           :", error)

    return 0 if result.get("review_status") == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
