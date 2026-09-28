"""The shobr CLI: argument parsing and entry point."""

import argparse
import sys
from collections.abc import Callable

from .ai import smoke_test_completion
from .browser import commands_install, smoke_test_browser
from .core import ShobrError, __version__
from .discovery import discover, discover_local
from .enrichment import enrich_next, enrich_posting_id, print_enriched
from .notification import notifications, notifications_local
from .pipeline import next_posting_id, print_status, run_next
from .screening import (
    init_profile,
    print_profile,
    print_screened,
    screen_llm_all,
    screen_llm_next,
    screen_llm_posting_id,
    screen_next,
    screen_posting_id,
)
from .tailoring import print_tailored, tailor_all, tailor_next, tailor_posting_id
from .tracking import (
    TrackStatus,
    print_tracked,
    review_next,
    review_posting_id,
    track_posting_id,
)


def _print_prompt_arg(
    subparser: argparse.ArgumentParser, plural: bool = False
) -> argparse.ArgumentParser:
    """Add a --print-prompt(s) flag."""
    noun = "prompts" if plural else "prompt"
    subparser.add_argument(
        "--print-prompt",
        action="store_true",
        help=f"print the built {noun} without calling the LLM",
    )
    return subparser


def _force_arg(subparser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Add a --force flag."""
    subparser.add_argument(
        "--force",
        action="store_true",
        help="act on stale rows instead of refusing",
    )
    return subparser


def main() -> None:
    """Parse argv, setup --help, and dispatch to the requested command.

    The `argparse` content is declared in an order which makes sense when seen
    on --help.
    """
    parser = argparse.ArgumentParser(prog="shobr", suggest_on_error=True)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    for name, help_text in [
        ("setup", "scaffold profile templates and config.toml, symlink beachpatrol commands"),
        ("profile", "print the profile input paths shobr reads"),
        ("smoke-test-browser", "dump the LinkedIn homepage rendered HTML through beachpatrol"),
        ("smoke-test-llm", "get one LLM chat completion through any-llm and print it"),
    ]:
        sub.add_parser(name, help=help_text)

    sub.add_parser(
        "notifications",
        help="parse the LinkedIn notifications page into structured rows",
    ).add_argument(
        "--local",
        action="store_true",
        help="print the stored notifications queue summary without fetching",
    )

    for name, help_text in [
        ("discovered", "print the stored job leads summary"),
        ("discover", "parse the LinkedIn jobs search-results page into structured rows"),
        ("enriched", "print every enriched job detail stored so far"),
    ]:
        sub.add_parser(name, help=help_text)

    sub.add_parser(
        "enrich",
        help="fetch the detail of a specific posting id",
    ).add_argument("posting_id", help="LinkedIn posting id to enrich first")
    sub.add_parser(
        "enrich-next",
        help="fetch the detail of the oldest pre-filtered lead that is not yet enriched",
    )
    sub.add_parser(
        "screened",
        help="print the screening store with review provenance",
    ).add_argument(
        "--score",
        help="filter by score: N, N-M, N,M,K, or 'none' (no human review yet)",
    )

    screen = sub.add_parser(
        "screen",
        help="record a human screening verdict for a posting id",
    )
    screen.add_argument("posting_id", help="LinkedIn posting id to screen")
    screen.add_argument(
        "score",
        nargs="?",
        type=int,
        help="fit score 1-5; omit to review in $EDITOR",
    )
    screen.add_argument("--reason", help="free-text reasoning; must accompany a score")
    _force_arg(screen)

    screen_next_parser = sub.add_parser(
        "screen-next",
        help="print the oldest enriched, unscreened lead for review",
    )
    screen_next_parser.add_argument(
        "--score",
        type=int,
        help="fit score 1-5; record directly instead of opening $EDITOR",
    )
    screen_next_parser.add_argument(
        "--reason",
        help="free-text reasoning; must accompany --score",
    )
    _force_arg(screen_next_parser)

    screen_llm = sub.add_parser(
        "screen-llm",
        help="print the LLM scoring prompt for a posting id",
    )
    screen_llm.add_argument("posting_id", help="LinkedIn posting id to score")
    _force_arg(_print_prompt_arg(screen_llm))

    screen_llm_next_parser = sub.add_parser(
        "screen-llm-next",
        help="print the LLM scoring prompt for the oldest unscored lead",
    )
    _force_arg(_print_prompt_arg(screen_llm_next_parser))

    screen_llm_all_parser = sub.add_parser(
        "screen-llm-all", help="score every unscored lead through the LLM"
    )
    _force_arg(screen_llm_all_parser)
    sub.add_parser("tailored", help="print every tailored package stored so far")

    tailor = sub.add_parser(
        "tailor",
        help="build the application package for a posting id",
    )
    tailor.add_argument("posting_id", help="LinkedIn posting id to tailor for")
    _force_arg(_print_prompt_arg(tailor, plural=True))

    tailor_next_parser = sub.add_parser(
        "tailor-next",
        help="build the package for the oldest pursue row with no package yet",
    )
    _force_arg(_print_prompt_arg(tailor_next_parser, plural=True))

    tailor_all_parser = sub.add_parser(
        "tailor-all", help="build packages for every pursue row with no package yet"
    )
    _force_arg(tailor_all_parser)

    sub.add_parser(
        "tracked",
        help="print the tracking store with per-status breakdown",
    )

    track = sub.add_parser(
        "track",
        help="record a submission pipeline status for a packaged posting",
    )
    track.add_argument("posting_id", help="LinkedIn posting id to track")
    track.add_argument(
        "status",
        choices=[status.value for status in TrackStatus],
        help="new pipeline status",
    )
    track.add_argument("--note", help="free-text note recorded with the transition")

    sub.add_parser(
        "review",
        help="print the full package for a packaged posting",
    ).add_argument("posting_id", help="LinkedIn posting id to review")

    for name, help_text in [
        ("review-next", "print the full package for the oldest untracked posting"),
        ("status", "print the pipeline status overview"),
    ]:
        sub.add_parser(name, help=help_text)

    sub.add_parser(
        "next",
        help="prompt for the single next pipeline action and run it on confirmation",
    ).add_argument(
        "posting_id",
        nargs="?",
        help="limit to the next action for one LinkedIn posting id",
    )

    args = parser.parse_args()

    try:
        _dispatch(args, parser)
    except ShobrError as exc:
        print(exc, file=sys.stderr)
        sys.exit(exc.code)


def setup() -> None:
    """Scaffold profile/config and install beachpatrol commands."""
    init_profile()
    commands_install()


def _dispatch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Run a given SHOBR action(s) based on the CLI command."""
    commands: dict[str, Callable[[], None]] = {
        "setup": setup,
        "profile": print_profile,
        "smoke-test-browser": smoke_test_browser,
        "smoke-test-llm": smoke_test_completion,
        "notifications": lambda: notifications_local() if args.local else notifications(),
        "discovered": discover_local,
        "discover": discover,
        "enriched": print_enriched,
        "enrich": lambda: enrich_posting_id(args.posting_id),
        "enrich-next": enrich_next,
        "screened": lambda: print_screened(args.score),
        "screen": lambda: screen_posting_id(args.posting_id, args.score, args.reason, args.force),
        "screen-next": lambda: screen_next(args.score, args.reason, args.force),
        "screen-llm": lambda: screen_llm_posting_id(args.posting_id, args.print_prompt, args.force),
        "screen-llm-next": lambda: screen_llm_next(args.print_prompt, args.force),
        "screen-llm-all": lambda: screen_llm_all(args.force),
        "tailored": print_tailored,
        "tailor": lambda: tailor_posting_id(args.posting_id, args.print_prompt, args.force),
        "tailor-next": lambda: tailor_next(args.print_prompt, args.force),
        "tailor-all": lambda: tailor_all(args.force),
        "tracked": print_tracked,
        "track": lambda: track_posting_id(args.posting_id, args.status, args.note),
        "review": lambda: review_posting_id(args.posting_id),
        "review-next": review_next,
        "status": print_status,
        "next": lambda: next_posting_id(args.posting_id) if args.posting_id else run_next(),
    }
    match commands.get(args.command):
        case None:
            parser.print_help()
        case run:
            run()


if __name__ == "__main__":
    main()
