"""Pipeline-wide overview, and single-step dispatcher."""

import sys
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime

from .color import BLUE, BOLD, GREEN, RED, RESET, YELLOW
from .config import load_config
from .core import ShobrError, shobr_command, store_key
from .discovery import StoredJob, discover, project_discovery
from .enrichment import (
    EnrichedRow,
    enrich_next,
    enrich_posting_id,
    is_closed,
    is_stale,
    project_enrichment,
)
from .providers.base import current_provider
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
    provider = current_provider().name
    leads = project_discovery(provider)["rows"]
    enriched = list(project_enrichment(provider)["rows"].values())
    screened = project_screening(provider)["rows"]
    tailored = project_tailoring(provider)["rows"]
    tracked = project_tracking(provider)["rows"]
    return leads, enriched, screened, tailored, tracked


def _ids(
    rows: Mapping[str, StoredJob | EnrichedRow | ScreeningRow | TailoringRow | TrackedRow],
) -> set[str]:
    """The plain posting ids of a projected store."""
    return {row["posting_id"] for row in rows.values()}


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
    if not current_provider().supports_tailoring:
        return 0
    passing = {row["posting_id"] for row in enriched if row["actionable"]}
    return sum(
        1
        for sr in screened.values()
        if sr["decision"] == ReviewDecision.PURSUE
        and sr["posting_id"] not in tailored_ids
        and sr["posting_id"] in passing
    )


def _pending_review_ids(
    screened: dict[str, ScreeningRow],
    tailored_ids: set[str],
    tracked_ids: set[str],
    enriched: list[EnrichedRow],
) -> set[str]:
    """Actionable posting ids awaiting review or tracking."""
    passing = {row["posting_id"] for row in enriched if row["actionable"]}
    if not current_provider().supports_tailoring:
        return {
            sr["posting_id"]
            for sr in screened.values()
            if sr["decision"] == ReviewDecision.PURSUE
            and sr["posting_id"] not in tracked_ids
            and sr["posting_id"] in passing
        }
    return {pid for pid in tailored_ids if pid not in tracked_ids and pid in passing}


def _pending_review(
    screened: dict[str, ScreeningRow],
    tailored_ids: set[str],
    tracked_ids: set[str],
    enriched: list[EnrichedRow],
) -> int:
    """Packaged or directly trackable postings with no tracking event yet."""
    return len(_pending_review_ids(screened, tailored_ids, tracked_ids, enriched))


def print_status() -> None:
    """Print the pipeline status overview, sectioned per stage."""
    leads, enriched, screened, tailored, tracked = _load_stores()

    enriched_ids = {row["posting_id"] for row in enriched}
    human_reviewed = {sr["posting_id"] for sr in screened.values() if sr["human"] is not None}
    passing = {row["posting_id"] for row in enriched if row["actionable"]}
    tailored_ids = _ids(tailored)
    tracked_ids = _ids(tracked)
    tailoring_supported = current_provider().supports_tailoring
    pending_review_ids = _pending_review_ids(screened, tailored_ids, tracked_ids, enriched)

    skip = sum(1 for sr in screened.values() if sr["decision"] == ReviewDecision.SKIP)
    pending_human = sum(
        1
        for sr in screened.values()
        if sr["decision"] == ReviewDecision.PENDING and sr["posting_id"] in passing
    )
    ai_reviewed = {sr["posting_id"] for sr in screened.values() if sr["ai"] is not None}
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
    pending_ids = tailored_ids if tailoring_supported else tracked_ids
    verb = "to tailor" if tailoring_supported else "to track"
    for score in scores_desc:
        text = f"{score}: {human_scores[score]}"
        pending = sum(
            1
            for sr in screened.values()
            if sr["human"] is not None
            and int(sr["human"]["score"]) == score
            and sr["decision"] == ReviewDecision.PURSUE
            and sr["posting_id"] not in pending_ids
            and sr["posting_id"] in passing
        )
        if pending:
            text += f" ({pending} {verb})"
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
    stale: set[str] = set()
    if enriched:
        threshold = load_config()["stale_after_days"]
        stale = {row["posting_id"] for row in enriched if is_stale(row, threshold)}

    def _stale_suffix(pids: Iterable[str]) -> str:
        n = sum(1 for pid in pids if pid in stale)
        return f" ({n} stale)" if n else ""

    stale_note = {
        "Pending Screening:": _stale_suffix(
            row["posting_id"]
            for row in enriched
            if row["actionable"] and row["posting_id"] not in human_reviewed
        ),
        "Pending Human Review:": _stale_suffix(
            sr["posting_id"]
            for sr in screened.values()
            if sr["decision"] == ReviewDecision.PENDING and sr["posting_id"] in passing
        ),
        "Pending Review:": _stale_suffix(pending_review_ids),
    }
    if tailoring_supported:
        stale_note["Pending Tailoring:"] = _stale_suffix(
            sr["posting_id"]
            for sr in screened.values()
            if sr["decision"] == ReviewDecision.PURSUE
            and sr["posting_id"] not in tailored_ids
            and sr["posting_id"] in passing
        )

    def _breakdown(pids: Iterable[str]) -> str:
        parts = []
        if closed_count := sum(1 for pid in pids if pid in closed):
            parts.append(f"{closed_count} closed")
        if filtered_count := sum(1 for pid in pids if pid in filtered):
            parts.append(f"{filtered_count} filtered")
        return f" ({', '.join(parts)})" if parts else ""

    validity_note = {
        "Total Screened:": _breakdown(_ids(screened)),
        "Packages Built:": _breakdown(_ids(tailored)),
        **{
            f"{status.value.capitalize()}:": _breakdown(
                row["posting_id"] for row in tracked.values() if row["status"] == status
            )
            for status in TrackStatus
        },
    }

    screening_rows = [
        ("Total Screened:", len(screened), BLUE),
        ("Skipped:", skip, RED),
        ("Lacking LLM Review:", lacking_llm, YELLOW),
        ("Pending Human Review:", pending_human, YELLOW),
    ]
    if tailoring_supported:
        screening_rows.append(
            ("Pending Tailoring:", _pending_tailoring(screened, tailored_ids, enriched), GREEN)
        )
    else:
        screening_rows.append(("Pending Review:", len(pending_review_ids), GREEN))
    review_rows = []
    if tailoring_supported:
        review_rows = [
            ("Packages Built:", len(tailored), BLUE),
            ("Pending Review:", len(pending_review_ids), GREEN),
        ]

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
            screening_rows,
        ),
    ]
    if tailoring_supported:
        sections.append(("TAILORING", review_rows))
    sections.append(
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
        )
    )
    width = max(len(label) for _, rows in sections for label, _, _ in rows)
    print("SHOBR STATUS")
    print()
    for name, rows in sections:
        print(f"{BOLD}- {name}{RESET}")
        for label, count, color in rows:
            suffix = validity_note.get(label, "") + stale_note.get(label, "")
            print(f"  - {label:<{width}}  {color}{count}{RESET}{suffix}")
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
            f"Fetch oldest lead details? ({BLUE}{shobr_command('enrich-next')}{RESET}) "
            f"{BOLD}[y/N]{RESET} "
        ):
            enrich_next()
            return

    human_reviewed = {sr["posting_id"] for sr in screened.values() if sr["human"] is not None}
    if pending := _pending_screening(enriched, human_reviewed):
        ai_reviewed = {sr["posting_id"] for sr in screened.values() if sr["ai"] is not None}
        lacking = _pending_ai_screening(enriched, human_reviewed, ai_reviewed)
        extra = f" ({lacking} lacking LLM screening)" if lacking else ""
        threshold = load_config()["stale_after_days"]
        stale_rows = [
            row
            for row in enriched
            if row["actionable"]
            and row["posting_id"] not in human_reviewed
            and is_stale(row, threshold)
        ]
        stale_bit = f" ({len(stale_rows)} stale)" if stale_rows else ""
        print(f"{pending} enriched leads awaiting screening{extra}{stale_bit}.")
        oldest = min(stale_rows, key=lambda row: row["enriched_last_at"], default=None)
        if oldest is not None:
            checked = datetime.fromisoformat(oldest["enriched_last_at"]).date().isoformat()
            print(f"Oldest pending row last checked {checked} (> {threshold} days ago).")
            enrich_command = shobr_command(f"enrich {oldest['posting_id']}")
            if _confirm(f"Re-enrich it first? ({BLUE}{enrich_command}{RESET}) {BOLD}[y/N]{RESET} "):
                enrich_posting_id(oldest["posting_id"])
                return
        if lacking:
            answer = (
                input(
                    f"Score with [L]LM ({BLUE}{shobr_command('screen-llm-next')}{RESET}), "
                    f"[h]uman ({BLUE}{shobr_command('screen-next')}{RESET}), "
                    f"or [N]ot now? {BOLD}[N]{RESET} "
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
            f"Review in [h]uman editor ({BLUE}{shobr_command('screen-next')}{RESET})? "
            f"{BOLD}[y/N]{RESET} "
        ).strip().lower() in (
            "y",
            "yes",
            "h",
        ):
            screen_next(None, None)
            return

    if pending := _pending_tailoring(screened, _ids(tailored), enriched):
        threshold = load_config()["stale_after_days"]
        stale_rows = [
            row
            for row in enriched
            if (sr := screened.get(store_key(row["provider"], row["posting_id"]))) is not None
            and sr["decision"] == ReviewDecision.PURSUE
            and store_key(row["provider"], row["posting_id"]) not in tailored
            and row["actionable"]
            and is_stale(row, threshold)
        ]
        stale_bit = f" ({len(stale_rows)} stale)" if stale_rows else ""
        print(f"{pending} pursue rows awaiting tailoring{stale_bit}.")
        oldest = stale_rows[0] if stale_rows else None
        if oldest is not None:
            checked = datetime.fromisoformat(oldest["enriched_last_at"]).date().isoformat()
            print(f"Oldest pending row last checked {checked} (> {threshold} days ago).")
            enrich_command = shobr_command(f"enrich {oldest['posting_id']}")
            if _confirm(f"Re-enrich it first? ({BLUE}{enrich_command}{RESET}) {BOLD}[y/N]{RESET} "):
                enrich_posting_id(oldest["posting_id"])
                return
        if _confirm(
            f"Tailor oldest screened lead? ({BLUE}{shobr_command('tailor-next')}{RESET}) "
            f"{BOLD}[y/N]{RESET} "
        ):
            tailor_next(False)
            return

    if _pending_review(screened, _ids(tailored), _ids(tracked), enriched):
        review_next()
        return

    if _confirm(f"Fetch new leads? ({BLUE}{shobr_command('discover')}{RESET}) {BOLD}[y/N]{RESET} "):
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
    key = store_key(current_provider().name, posting_id)
    enriched_rows = {row["posting_id"]: row for row in enriched}
    if posting_id not in enriched_rows:
        if _confirm(
            f"Fetch details for {posting_id}? "
            f"({BLUE}{shobr_command(f'enrich {posting_id}')}{RESET}) "
            f"{BOLD}[y/N]{RESET} "
        ):
            enrich_posting_id(posting_id)
            return
        print(f"nothing to do for {posting_id}")
        return
    if not enriched_rows[posting_id]["actionable"]:
        reason = enriched_rows[posting_id]["rejected_reason"] or "unknown reason"
        raise ShobrError(f"posting {posting_id} did not pass the pre-filter ({reason})")

    sr = screened.get(key)
    if sr is None or sr["human"] is None:
        human_reviewed = {
            review["posting_id"] for review in screened.values() if review["human"] is not None
        }
        ai_reviewed = {
            review["posting_id"] for review in screened.values() if review["ai"] is not None
        }
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
            f"Review {posting_id} in [h]uman editor "
            f"({BLUE}{shobr_command(f'screen {posting_id}')}{RESET})? "
            f"{BOLD}[y/N]{RESET} "
        ):
            screen_posting_id(posting_id, None, None)
            return
        print(f"nothing to do for {posting_id}")
        return

    if sr["decision"] != ReviewDecision.PURSUE:
        print(f"nothing to do for {posting_id}")
        return
    if current_provider().supports_tailoring and key not in tailored:
        if _confirm(
            f"Tailor {posting_id}? "
            f"({BLUE}{shobr_command(f'tailor {posting_id}')}{RESET}) "
            f"{BOLD}[y/N]{RESET} "
        ):
            tailor_posting_id(posting_id, False)
            return
        print(f"nothing to do for {posting_id}")
        return
    if key not in tracked:
        review_posting_id(posting_id)
        return
    print(f"nothing to do for {posting_id}")
