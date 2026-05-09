"""Bedtime story generator CLI."""

# === Assignment reflection ===
#
# Before submitting the assignment, describe here in a few sentences what you
# would have built next if you spent 2 more hours on this project:
#



# (1) With more time I would create a user input option to ask for a specific story length from the start.
# (2) I would also make sure to include a more robust user feedback mechanism,
# where users can type out what they would want to change.
# (3) I would add more specific review agents to support the judge and main storytelling
# agent, for example an agent to check for sentence cadence.
# (4) Build a more robust "feedback" system from the judge, with
# more specifications around the feedback scoring, and more chances
# for the user to input their feedback into the judging criteria.


from __future__ import annotations

import argparse
import logging
import sys

from client import MissingAPIKeyError
from config import DEFAULT_MAX_REVISIONS
from prompts import ENVIRONMENT_FACTS
from story_engine import StoryRequest, run_pipeline


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="good-night-earth",
        description=(
            "Good Night, Earth - Gentle bedtime stories that inspire "
            "children to care for the planet."
        ),
    )
    parser.add_argument("--name", help="Listener's first name.")
    parser.add_argument(
        "--age", type=int,
        help="Listener's age, 5 to 10.",
    )
    parser.add_argument(
        "--environment",
        choices=sorted(ENVIRONMENT_FACTS),
        help="Environmental theme for the story (one of the curated topics).",
    )
    parser.add_argument(
        "--max-revisions",
        type=int,
        default=DEFAULT_MAX_REVISIONS,
        help=f"Maximum judge-driven revision rounds (default: {DEFAULT_MAX_REVISIONS}).",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Show panel scores, judge verdict, and feedback per revision on stderr.",
    )
    return parser.parse_args(argv)


def _prompt(label: str, hint: str = "") -> str:
    suffix = f" [{hint}]" if hint else ""
    return input(f"{label}{suffix}: ").strip()


def _gather_request(args: argparse.Namespace) -> StoryRequest:
    """Collect any missing fields interactively, then build a StoryRequest.

    Each flag is independently optional; only missing fields trigger a
    prompt. All validation is centralized in `StoryRequest.__post_init__`.
    """
    name = args.name if args.name is not None else _prompt("Listener's first name")

    if args.age is not None:
        age = args.age
    else:
        age_str = _prompt("Listener's age (5-10)")
        try:
            age = int(age_str)
        except ValueError:
            raise ValueError(f"Age must be a whole number (got {age_str!r}).") from None

    if args.environment is not None:
        environment = args.environment
    else:
        choices = ", ".join(sorted(ENVIRONMENT_FACTS))
        environment = _prompt("Environmental theme", choices).lower()

    return StoryRequest(name=name, age=age, environment=environment)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    # Warnings (e.g. unparseable agent JSON) are always visible; per-revision
    # detail only shows with --verbose.
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(message)s",
        stream=sys.stderr,
    )

    try:
        request = _gather_request(args)
    except (EOFError, KeyboardInterrupt):
        print("\nNo input provided.", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"Invalid input: {exc}", file=sys.stderr)
        return 1

    try:
        result = run_pipeline(request, max_revisions=args.max_revisions)
    except MissingAPIKeyError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    logging.info("")
    logging.info(
        "Finished after %d revision(s); final pass=%s.",
        result.revisions,
        result.verdict.passed,
    )

    print(result.story)
    return 0


if __name__ == "__main__":
    sys.exit(main())
