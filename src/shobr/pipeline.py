"""Pipeline-wide overview, and single-step dispatcher."""

import sys
from collections.abc import Callable, Iterable

from .color import BLUE, BOLD, GREEN, RED, RESET, YELLOW
from .core import ShobrError
from .discovery import StoredJob, discover, project_discovery
from .enrichment import (
    EnrichedRow,
    enrich_next,
    enrich_posting_id,
    is_closed,
    project_enrichment,
)
from .screening import (
    SCORE_COLORS,
    ReviewDecision,
    Score,
    ScreeningRow,
    project_screening,
    screen_llm_next,
    screen_llm_posting_id,
    screen_next,
    screen_posting_id,
)
from .tailoring import (
    TailoringRow,
    project_tailoring,
    tailor_next,
    tailor_posting_id,
)
from .tracking import (
    TRACK_STATUS_COLORS,
    TrackedRow,
    TrackStatus,
    project_tracking,
    review_next,
    review_posting_id,
)


def _load_stores() -> tuple[
    list[StoredJob],
    list[EnrichedRow],
    dict[str, ScreeningRow],
    dict[str, TailoringRow],
    dict[str, TrackedRow],
]:
    """Project every stage store."""
    leads = project_discovery()["rows"]
    enriched = list(project_enrichment()["rows"].values())
    screened = project_screening()["rows"]
    tailored = project_tailoring()["rows"]
    tracked = project_tracking()["rows"]
    return leads, enriched, screened, tailored, tracked


def _pending_enrichment(leads: list[StoredJob], enriched_ids: set[str]) -> int:
    """Passing leads with no enrichment attempt yet."""
    return sum(1 for row in leads if row["actionable"] and row["posting_id"] not in enriched_ids)


def _pending_screening(enriched: list[EnrichedRow], human_reviewed_ids: set[str]) -> int:
    """Passing enriched rows with no human review yet."""
    return sum(
        1 for row in enriched if row["actionable"] and row["posting_id"] not in human_reviewed_ids
    )


def _pending_ai_screening(
    enriched: list[EnrichedRow], human_reviewed_ids: set[str], ai_reviewed_ids: set[str]
) -> int:
    """Passing enriched rows with neither human nor AI review yet."""
    return sum(
        1
        for row in enriched
        if row["actionable"]
        and row["posting_id"] not in human_reviewed_ids
        and row["posting_id"] not in ai_reviewed_ids
    )


def _pending_tailoring(
    screened: dict[str, ScreeningRow], tailored_ids: set[str], enriched: list[EnrichedRow]
) -> int:
    """Pursue rows with no package yet, still passing enrichment."""
    passing = {row["posting_id"] for row in enriched if row["actionable"]}
    return sum(
        1
        for pid, sr in screened.items()
        if sr["decision"] == ReviewDecision.PURSUE and pid not in tailored_ids and pid in passing
    )


def _pending_review(
    tailored_ids: set[str], tracked_ids: set[str], enriched: list[EnrichedRow]
) -> int:
    """Packaged postings with no tracking event yet, still passing enrichment."""
    passing = {row["posting_id"] for row in enriched if row["actionable"]}
    return sum(1 for pid in tailored_ids if pid not in tracked_ids and pid in passing)


def print_status() -> None:
    """Print the pipeline status overview, sectioned per stage."""
    leads, enriched, screened, tailored, tracked = _load_stores()

    enriched_ids = {row["posting_id"] for row in enriched}
    human_reviewed = {pid for pid, sr in screened.items() if sr["human"] is not None}

    skip = sum(1 for sr in screened.values() if sr["decision"] == ReviewDecision.SKIP)
    pending_human = sum(1 for sr in screened.values() if sr["decision"] == ReviewDecision.PENDING)
    ai_reviewed = {pid for pid, sr in screened.items() if sr["ai"] is not None}
    lacking_llm = _pending_ai_screening(enriched, human_reviewed, ai_reviewed)

    scores_desc = tuple(sorted(Score, reverse=True))
    ai_scores: dict[int, int] = {score: 0 for score in scores_desc}
    human_scores: dict[int, int] = {score: 0 for score in scores_desc}
    for sr in screened.values():
        if sr["ai"] is not None:
            ai_scores[int(sr["ai"]["score"])] += 1
        if sr["human"] is not None:
            human_scores[int(sr["human"]["score"])] += 1
    left_header, right_header = "LLM Scores:", "Human Scores:"
    left_rows = [f"{score}: {ai_scores[score]}" for score in scores_desc]
    right_rows = []
    for score in scores_desc:
        text = f"{score}: {human_scores[score]}"
        to_tailor = sum(
            1
            for pid, sr in screened.items()
            if sr["human"] is not None
            and int(sr["human"]["score"]) == score
            and sr["decision"] == ReviewDecision.PURSUE
            and pid not in tailored
        )
        if to_tailor:
            text += f" ({to_tailor} to tailor)"
        right_rows.append(text)
    column = max(len(left_header), *(len(row) for row in left_rows)) + 4
    histogram = [f"  - {left_header.ljust(column)}{right_header}"]
    for score, left, right in zip(scores_desc, left_rows, right_rows):
        line = f"    {left.ljust(column)}{right}"
        histogram.append(line.replace(f"{score}:", f"{SCORE_COLORS[score]}{score}{RESET}:"))

    counts = {status: 0 for status in TrackStatus}
    for row in tracked.values():
        counts[row["status"]] += 1

    closed = {row["posting_id"] for row in enriched if is_closed(row)}
    failing = {row["posting_id"] for row in enriched if not row["actionable"]}
    filtered = failing - closed

    def _breakdown(pids: Iterable[str]) -> str:
        parts = []
        if closed_count := sum(1 for pid in pids if pid in closed):
            parts.append(f"{closed_count} closed")
        if filtered_count := sum(1 for pid in pids if pid in filtered):
            parts.append(f"{filtered_count} filtered")
        return f" ({', '.join(parts)})" if parts else ""

    validity_note = {
        "Total Screened:": _breakdown(screened),
        "Packages Built:": _breakdown(tailored),
        **{
            f"{status.value.capitalize()}:": _breakdown(
                pid for pid, row in tracked.items() if row["status"] == status
            )
            for status in TrackStatus
        },
    }

    sections: list[tuple[str, list[tuple[str, int, str]]]] = [
        (
            "DISCOVERY",
            [
                ("Total Leads Found:", len(leads), BLUE),
                (
                    "Rejected by Filter:",
                    sum(1 for row in leads if not row["actionable"]),
                    RED,
                ),
                ("Pending Enrichment:", _pending_enrichment(leads, enriched_ids), GREEN),
            ],
        ),
        (
            "ENRICHMENT",
            [
                ("Total Enriched:", len(enriched), BLUE),
                (
                    "Rejected by Filter:",
                    sum(1 for row in enriched if not row["actionable"]),
                    RED,
                ),
                ("Pending Screening:", _pending_screening(enriched, human_reviewed), GREEN),
            ],
        ),
        (
            "SCREENING",
            [
                ("Total Screened:", len(screened), BLUE),
                ("Skipped:", skip, RED),
                ("Lacking LLM Review:", lacking_llm, YELLOW),
                ("Pending Human Review:", pending_human, YELLOW),
                (
                    "Pending Tailoring:",
                    _pending_tailoring(screened, set(tailored), enriched),
                    GREEN,
                ),
            ],
        ),
        (
            "TAILORING",
            [
                ("Packages Built:", len(tailored), BLUE),
                ("Pending Review:", _pending_review(set(tailored), set(tracked), enriched), GREEN),
            ],
        ),
        (
            "TRACKING",
            [
                (
                    f"{status.value.capitalize()}:",
                    counts[status],
                    TRACK_STATUS_COLORS[status],
                )
                for status in TrackStatus
            ],
        ),
    ]
    width = max(len(label) for _, rows in sections for label, _, _ in rows)
    print("SHOBR STATUS")
    print()
    for name, rows in sections:
        print(f"{BOLD}- {name}{RESET}")
        for label, count, color in rows:
            print(f"  - {label:<{width}}  {color}{count}{RESET}{validity_note.get(label, '')}")
            if name == "SCREENING" and label == "Pending Human Review:":
                for line in histogram:
                    print(line)
        print()


def _confirm(prompt: str) -> bool:
    """A [y/N] prompt, defaulting to no."""
    return input(prompt).strip().lower() in ("y", "yes")


def _guarded(action: Callable[[], None]) -> None:
    """Run an interactive action; Ctrl+C exits 130, EOF (Ctrl+D) aborts."""
    try:
        action()
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)
    except EOFError:
        print("\ninterrupted", file=sys.stderr)


def run_next() -> None:
    """Prompt for the single next pipeline action; first yes runs it.

    Declining falls through to the next stage. A side-effect-free review runs
    directly without prompting.
    """
    _guarded(_run_next)


def _run_next() -> None:
    leads, enriched, screened, tailored, tracked = _load_stores()

    enriched_ids = {row["posting_id"] for row in enriched}
    if pending := _pending_enrichment(leads, enriched_ids):
        print(f"{pending} leads awaiting enrichment.")
        if _confirm(
            f"Fetch oldest lead details? ({BLUE}shobr enrich-next{RESET}) {BOLD}[y/N]{RESET} "
        ):
            enrich_next()
            return

    human_reviewed = {pid for pid, sr in screened.items() if sr["human"] is not None}
    if pending := _pending_screening(enriched, human_reviewed):
        ai_reviewed = {pid for pid, sr in screened.items() if sr["ai"] is not None}
        lacking = _pending_ai_screening(enriched, human_reviewed, ai_reviewed)
        extra = f" ({lacking} lacking LLM screening)" if lacking else ""
        print(f"{pending} enriched leads awaiting screening{extra}.")
        if lacking:
            answer = (
                input(
                    f"Score with [L]LM ({BLUE}shobr screen-llm-next{RESET}), [h]uman "
                    f"({BLUE}shobr screen-next{RESET}), or [N]ot now? {BOLD}[N]{RESET} "
                )
                .strip()
                .lower()
            )
            if answer == "l":
                screen_llm_next(False)
                return
            if answer == "h":
                screen_next(None, None)
                return
        elif input(
            f"Review in [h]uman editor ({BLUE}shobr screen-next{RESET})? {BOLD}[y/N]{RESET} "
        ).strip().lower() in (
            "y",
            "yes",
            "h",
        ):
            screen_next(None, None)
            return

    if pending := _pending_tailoring(screened, set(tailored), enriched):
        print(f"{pending} pursue rows awaiting tailoring.")
        if _confirm(
            f"Tailor oldest screened lead? ({BLUE}shobr tailor-next{RESET}) {BOLD}[y/N]{RESET} "
        ):
            tailor_next(False)
            return

    if _pending_review(set(tailored), set(tracked), enriched):
        review_next()
        return

    if _confirm(f"Fetch new leads? ({BLUE}shobr discover{RESET}) {BOLD}[y/N]{RESET} "):
        discover()
        return

    print("nothing to do")


def next_posting_id(posting_id: str) -> None:
    """Prompt for the single next pipeline action for one posting id."""
    _guarded(lambda: _run_next_for_id(posting_id))


def _run_next_for_id(posting_id: str) -> None:
    leads, enriched, screened, tailored, tracked = _load_stores()

    lead = next((row for row in leads if row["posting_id"] == posting_id), None)
    if lead is None:
        raise ShobrError(f"posting {posting_id} not found in leads store")
    if not lead["actionable"]:
        raise ShobrError(f"posting {posting_id} did not pass the pre-filter")
    enriched_rows = {row["posting_id"]: row for row in enriched}
    if posting_id not in enriched_rows:
        if _confirm(
            f"Fetch details for {posting_id}? ({BLUE}shobr enrich {posting_id}{RESET}) "
            f"{BOLD}[y/N]{RESET} "
        ):
            enrich_posting_id(posting_id)
            return
        print(f"nothing to do for {posting_id}")
        return
    if not enriched_rows[posting_id]["actionable"]:
        raise ShobrError(f"posting {posting_id} did not pass the pre-filter")

    sr = screened.get(posting_id)
    if sr is None or sr["human"] is None:
        human_reviewed = {pid for pid, review in screened.items() if review["human"] is not None}
        ai_reviewed = {pid for pid, review in screened.items() if review["ai"] is not None}
        if posting_id not in ai_reviewed and posting_id not in human_reviewed:
            answer = (
                input(
                    f"Score {posting_id} with [L]LM, [h]uman editor, or [N]ot now? "
                    f"{BOLD}[N]{RESET} "
                )
                .strip()
                .lower()
            )
            if answer == "l":
                screen_llm_posting_id(posting_id, False)
                return
            if answer == "h":
                screen_posting_id(posting_id, None, None)
                return
        elif _confirm(
            f"Review {posting_id} in [h]uman editor ({BLUE}shobr screen {posting_id}{RESET})? "
            f"{BOLD}[y/N]{RESET} "
        ):
            screen_posting_id(posting_id, None, None)
            return
        print(f"nothing to do for {posting_id}")
        return

    if sr["decision"] != ReviewDecision.PURSUE:
        print(f"nothing to do for {posting_id}")
        return
    if posting_id not in tailored:
        if _confirm(
            f"Tailor {posting_id}? ({BLUE}shobr tailor {posting_id}{RESET}) {BOLD}[y/N]{RESET} "
        ):
            tailor_posting_id(posting_id, False)
            return
        print(f"nothing to do for {posting_id}")
        return
    if posting_id not in tracked:
        review_posting_id(posting_id)
        return
    print(f"nothing to do for {posting_id}")
