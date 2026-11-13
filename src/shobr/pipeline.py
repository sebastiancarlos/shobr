"""Pipeline-wide overview, and single-step dispatcher."""

import sys
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

from .color import BLUE, BOLD, GREEN, RED, RESET, YELLOW
from .core import DataKind, ShobrError, paged, shobr_command, store_key
from .discovery import StoredJob, discover, print_lead, project_discovery
from .enrichment import (
    DEFAULT_ENRICH_ALL_DELAY,
    EnrichedRow,
    enrich_all,
    enrich_next,
    enrich_posting_id,
    is_closed,
    is_stale,
    project_enrichment,
)
from .output import emit_json
from .providers.base import current_config, current_provider
from .screening import (
    SCORE_COLORS,
    ReviewDecision,
    Score,
    ScreeningRow,
    project_screening,
    screen_llm_all,
    screen_llm_next,
    screen_llm_posting_id,
    screen_next,
    screen_posting_id,
)
from .tailoring import (
    TailoringRow,
    pending_tailorable,
    project_tailoring,
    tailor_all,
    tailor_next,
    tailor_posting_id,
)
from .tracking import (
    TRACK_STATUS_COLORS,
    TrackedRow,
    TrackStatus,
    print_review,
    project_tracking,
    review_next,
    review_posting_id,
)
from .views import (
    DiscoveryStatus,
    EnrichmentStatus,
    ScoreRow,
    ScreeningStatus,
    StaleCount,
    StatusView,
    TailoringStatus,
    TrackingStatus,
    ValidityCount,
    build_posting_view,
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


def _needs_human_review(sr: ScreeningRow | None) -> bool:
    """Whether a screening row still waits on a human decision."""
    return sr is None or (sr["human"] is None and sr["decision"] == ReviewDecision.PENDING)


def _pending_screening(enriched: list[EnrichedRow], screened: dict[str, ScreeningRow]) -> int:
    """Passing enriched rows awaiting a human screening decision."""
    return sum(
        1
        for row in enriched
        if row["actionable"]
        and _needs_human_review(screened.get(store_key(row["provider"], row["posting_id"])))
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
    screened: dict[str, ScreeningRow],
    tailored: dict[str, TailoringRow],
    enriched: list[EnrichedRow],
) -> int:
    """Pursue rows with no package yet, still passing enrichment."""
    return len(pending_tailorable(enriched, screened, tailored))


def pending_review_ids() -> set[str]:
    """Actionable posting ids awaiting review or tracking."""
    _, enriched, screened, tailored, tracked = _load_stores()
    passing = {row["posting_id"] for row in enriched if row["actionable"]}
    tailored_ids = _ids(tailored)
    tracked_ids = _ids(tracked)
    if not current_provider().supports_tailoring:
        return {
            sr["posting_id"]
            for sr in screened.values()
            if sr["decision"] == ReviewDecision.PURSUE
            and sr["posting_id"] not in tracked_ids
            and sr["posting_id"] in passing
        }
    return {pid for pid in tailored_ids if pid not in tracked_ids and pid in passing}


def stale_pending_rows() -> list[EnrichedRow]:
    """Stale actionable rows still pending screening, tailoring, or review."""
    _, enriched, screened, tailored, tracked = _load_stores()
    if not enriched:
        return []

    threshold = current_config()["stale_after_days"]
    tracked_ids = _ids(tracked)
    review_ids = pending_review_ids()
    active_keys: set[str] = set()

    for row in enriched:
        key = store_key(row["provider"], row["posting_id"])
        if row["actionable"] and _needs_human_review(screened.get(key)):
            active_keys.add(key)

    active_keys.update(
        store_key(row["provider"], row["posting_id"])
        for row in pending_tailorable(enriched, screened, tailored)
    )
    active_keys.update(
        store_key(row["provider"], row["posting_id"])
        for row in enriched
        if row["posting_id"] in review_ids
    )

    stale = [
        row
        for row in enriched
        if store_key(row["provider"], row["posting_id"]) in active_keys
        and row["posting_id"] not in tracked_ids
        and is_stale(row, threshold)
    ]
    stale.sort(key=lambda row: row["enriched_last_at"])
    return stale


def _status_view() -> StatusView:
    """Build the semantic pipeline status overview."""
    leads, enriched, screened, tailored, tracked = _load_stores()

    enriched_ids = {row["posting_id"] for row in enriched}
    human_reviewed = {sr["posting_id"] for sr in screened.values() if sr["human"] is not None}
    passing = {row["posting_id"] for row in enriched if row["actionable"]}
    tailored_ids = _ids(tailored)
    tracked_ids = _ids(tracked)
    tailoring_supported = current_provider().supports_tailoring
    pending_review_set = pending_review_ids()

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
    pending_ids = tailored_ids if tailoring_supported else tracked_ids
    score_rows: list[ScoreRow] = []
    for score in scores_desc:
        pending = sum(
            1
            for sr in screened.values()
            if sr["human"] is not None
            and int(sr["human"]["score"]) == score
            and sr["decision"] == ReviewDecision.PURSUE
            and sr["posting_id"] not in pending_ids
            and sr["posting_id"] in passing
        )
        score_rows.append(
            ScoreRow(
                score=int(score),
                llm=ai_scores[score],
                human=human_scores[score],
                pending_action=pending,
            )
        )

    counts = {status: 0 for status in TrackStatus}
    for row in tracked.values():
        counts[row["status"]] += 1

    closed = {row["posting_id"] for row in enriched if is_closed(row)}
    failing = {row["posting_id"] for row in enriched if not row["actionable"]}
    filtered = failing - closed
    stale: set[str] = set()
    if enriched:
        threshold = current_config()["stale_after_days"]
        stale = {row["posting_id"] for row in enriched if is_stale(row, threshold)}

    def _validity_count(count: int, pids: Iterable[str]) -> ValidityCount:
        """Count closed and filtered postings among `pids`."""
        ids = list(pids)
        return ValidityCount(
            count=count,
            closed=sum(1 for pid in ids if pid in closed),
            filtered=sum(1 for pid in ids if pid in filtered),
        )

    def _stale_count(count: int, pids: Iterable[str]) -> StaleCount:
        """Count stale postings among `pids`."""
        return StaleCount(count=count, stale=sum(1 for pid in pids if pid in stale))

    discovery = DiscoveryStatus(
        total=len(leads),
        rejected=sum(1 for row in leads if not row["actionable"]),
        pending_enrichment=_pending_enrichment(leads, enriched_ids),
    )
    enrichment = EnrichmentStatus(
        total=len(enriched),
        rejected=sum(1 for row in enriched if not row["actionable"]),
        pending_screening=_stale_count(
            _pending_screening(enriched, screened),
            (
                row["posting_id"]
                for row in enriched
                if row["actionable"] and row["posting_id"] not in human_reviewed
            ),
        ),
    )
    screening = ScreeningStatus(
        total=_validity_count(len(screened), _ids(screened)),
        skipped=skip,
        lacking_llm_review=lacking_llm,
        pending_human_review=_stale_count(
            pending_human,
            (
                sr["posting_id"]
                for sr in screened.values()
                if sr["decision"] == ReviewDecision.PENDING and sr["posting_id"] in passing
            ),
        ),
        pending_tailoring=(
            _stale_count(
                _pending_tailoring(screened, tailored, enriched),
                (
                    sr["posting_id"]
                    for sr in screened.values()
                    if sr["decision"] == ReviewDecision.PURSUE
                    and sr["posting_id"] not in tailored_ids
                    and sr["posting_id"] in passing
                ),
            )
            if tailoring_supported
            else None
        ),
        pending_review=(
            None
            if tailoring_supported
            else _stale_count(len(pending_review_set), pending_review_set)
        ),
        scores=score_rows,
    )
    tailoring = (
        TailoringStatus(
            packages_built=_validity_count(len(tailored), _ids(tailored)),
            pending_review=_stale_count(len(pending_review_set), pending_review_set),
        )
        if tailoring_supported
        else None
    )
    tracking = TrackingStatus(
        applied=counts[TrackStatus.APPLIED],
        interviewing=counts[TrackStatus.INTERVIEWING],
        offer=counts[TrackStatus.OFFER],
        rejected=counts[TrackStatus.REJECTED],
        ghosted=counts[TrackStatus.GHOSTED],
        withdrawn=counts[TrackStatus.WITHDRAWN],
    )
    return StatusView(
        discovery=discovery,
        enrichment=enrichment,
        screening=screening,
        tailoring=tailoring,
        tracking=tracking,
    )


@dataclass(frozen=True)
class _StatusRow:
    """One labeled count in the text status overview."""

    label: str
    count: int
    closed: int | None = None
    filtered: int | None = None
    stale: int | None = None


@dataclass(frozen=True)
class _StatusSection:
    """A named group of status rows for the text overview."""

    name: str
    rows: list[_StatusRow]


def _status_sections(view: StatusView) -> list[_StatusSection]:
    """Map the semantic status view to labeled display sections."""
    discovery = view.discovery
    enrichment = view.enrichment
    screening = view.screening
    screening_rows = [
        _StatusRow(
            "Total Screened:",
            screening.total.count,
            closed=screening.total.closed,
            filtered=screening.total.filtered,
        ),
        _StatusRow("Skipped:", screening.skipped),
        _StatusRow("Lacking LLM Review:", screening.lacking_llm_review),
        _StatusRow(
            "Pending Human Review:",
            screening.pending_human_review.count,
            stale=screening.pending_human_review.stale,
        ),
    ]
    if screening.pending_tailoring is not None:
        screening_rows.append(
            _StatusRow(
                "Pending Tailoring:",
                screening.pending_tailoring.count,
                stale=screening.pending_tailoring.stale,
            )
        )
    if screening.pending_review is not None:
        screening_rows.append(
            _StatusRow(
                "Pending Review:",
                screening.pending_review.count,
                stale=screening.pending_review.stale,
            )
        )
    sections = [
        _StatusSection(
            "DISCOVERY",
            [
                _StatusRow("Total Leads Found:", discovery.total),
                _StatusRow("Rejected by Filter:", discovery.rejected),
                _StatusRow("Pending Enrichment:", discovery.pending_enrichment),
            ],
        ),
        _StatusSection(
            "ENRICHMENT",
            [
                _StatusRow("Total Enriched:", enrichment.total),
                _StatusRow("Rejected by Filter:", enrichment.rejected),
                _StatusRow(
                    "Pending Screening:",
                    enrichment.pending_screening.count,
                    stale=enrichment.pending_screening.stale,
                ),
            ],
        ),
        _StatusSection("SCREENING", screening_rows),
    ]
    if view.tailoring is not None:
        sections.append(
            _StatusSection(
                "TAILORING",
                [
                    _StatusRow(
                        "Packages Built:",
                        view.tailoring.packages_built.count,
                        closed=view.tailoring.packages_built.closed,
                        filtered=view.tailoring.packages_built.filtered,
                    ),
                    _StatusRow(
                        "Pending Review:",
                        view.tailoring.pending_review.count,
                        stale=view.tailoring.pending_review.stale,
                    ),
                ],
            )
        )
    tracking = view.tracking
    sections.append(
        _StatusSection(
            "TRACKING",
            [
                _StatusRow("Applied:", tracking.applied),
                _StatusRow("Interviewing:", tracking.interviewing),
                _StatusRow("Offer:", tracking.offer),
                _StatusRow("Rejected:", tracking.rejected),
                _StatusRow("Ghosted:", tracking.ghosted),
                _StatusRow("Withdrawn:", tracking.withdrawn),
            ],
        )
    )
    return sections


def _status_suffix(row: _StatusRow) -> str:
    """The parenthesized validity and staleness notes for a status row."""
    parts = []
    if row.closed:
        parts.append(f"{row.closed} closed")
    if row.filtered:
        parts.append(f"{row.filtered} filtered")
    validity = f" ({', '.join(parts)})" if parts else ""
    stale = f" ({row.stale} stale)" if row.stale else ""
    return validity + stale


def _status_color(section: str, label: str) -> str:
    """The ANSI color for one status row."""
    if section in ("DISCOVERY", "ENRICHMENT"):
        if label in ("Total Leads Found:", "Total Enriched:"):
            return BLUE
        if label == "Rejected by Filter:":
            return RED
        return GREEN
    if section == "SCREENING":
        if label == "Total Screened:":
            return BLUE
        if label == "Skipped:":
            return RED
        if label in ("Lacking LLM Review:", "Pending Human Review:"):
            return YELLOW
        return GREEN
    if section == "TAILORING":
        if label == "Packages Built:":
            return BLUE
        return GREEN
    return TRACK_STATUS_COLORS[TrackStatus(label.removesuffix(":").lower())]


def _score_histogram(view: StatusView, pending_action: str) -> list[str]:
    """The LLM and human score histogram lines for the status output."""
    left_header, right_header = "LLM Scores:", "Human Scores:"
    left_rows = [f"{row.score}: {row.llm}" for row in view.screening.scores]
    right_rows = []
    for row in view.screening.scores:
        text = f"{row.score}: {row.human}"
        if row.pending_action:
            text += f" ({row.pending_action} to {pending_action})"
        right_rows.append(text)
    column = max(len(left_header), *(len(row) for row in left_rows)) + 4
    histogram = [f"  - {left_header.ljust(column)}{right_header}"]
    for row, left, right in zip(view.screening.scores, left_rows, right_rows):
        line = f"    {left.ljust(column)}{right}"
        histogram.append(
            line.replace(f"{row.score}:", f"{SCORE_COLORS[row.score]}{row.score}{RESET}:")
        )
    return histogram


def print_status(*, json_output: bool = False) -> None:
    """Print the pipeline status overview, sectioned per stage."""
    view = _status_view()
    if json_output:
        emit_json(view)
        return
    sections = _status_sections(view)
    width = max(len(row.label) for section in sections for row in section.rows)
    pending_action = "tailor" if current_provider().supports_tailoring else "track"
    print("SHOBR STATUS")
    print()
    for section in sections:
        print(f"{BOLD}- {section.name}{RESET}")
        for row in section.rows:
            suffix = _status_suffix(row)
            color = _status_color(section.name, row.label)
            print(f"  - {row.label:<{width}}  {color}{row.count}{RESET}{suffix}")
            if section.name == "SCREENING" and row.label == "Pending Human Review:":
                for line in _score_histogram(view, pending_action):
                    print(line)
        print()


def show_posting(posting_id: str, *, json_output: bool = False) -> None:
    """Print every stored detail for one posting, whatever stage it reached."""
    leads, enriched, screened, tailored, tracked = _load_stores()
    key = store_key(current_provider().name, posting_id)
    lead = next((row for row in leads if row["posting_id"] == posting_id), None)
    enriched_row = next((row for row in enriched if row["posting_id"] == posting_id), None)
    if lead is None and enriched_row is None:
        raise ShobrError(f"posting {posting_id} not found in leads or enrichment store")
    view = build_posting_view(
        posting_id,
        after=DataKind.DISCOVERY,
        lead=lead,
        enrichment=enriched_row,
        screening=screened.get(key),
        package=tailored.get(key),
        tracking=tracked.get(key),
    )
    if json_output:
        emit_json(view)
        return
    if view.enrichment is None:
        assert view.lead is not None
        with paged():
            print_lead(view)
        return
    print_review(view, show_stage=True)


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
        answer = (
            input(
                f"Fetch [o]ldest lead details ({BLUE}{shobr_command('enrich-next')}{RESET}), "
                f"[a]ll pending ({BLUE}{shobr_command('enrich-all')}{RESET}), "
                f"or [N]ot now? {BOLD}[N]{RESET} "
            )
            .strip()
            .lower()
        )
        if answer in ("o", "y", "yes"):
            enrich_next()
            return
        if answer in ("a", "all"):
            enrich_all(DEFAULT_ENRICH_ALL_DELAY)
            return

    human_reviewed = {sr["posting_id"] for sr in screened.values() if sr["human"] is not None}
    if pending := _pending_screening(enriched, screened):
        ai_reviewed = {sr["posting_id"] for sr in screened.values() if sr["ai"] is not None}
        lacking = _pending_ai_screening(enriched, human_reviewed, ai_reviewed)
        extra = f" ({lacking} lacking LLM screening)" if lacking else ""
        threshold = current_config()["stale_after_days"]
        stale_rows = [
            row
            for row in enriched
            if row["actionable"]
            and _needs_human_review(screened.get(store_key(row["provider"], row["posting_id"])))
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
                    f"[a]ll ({BLUE}{shobr_command('screen-llm-all')}{RESET}),\n"
                    f"or [h]uman ({BLUE}{shobr_command('screen-next')}{RESET}), "
                    f"or [N]ot now? {BOLD}[N]{RESET} "
                )
                .strip()
                .lower()
            )
            if answer == "l":
                screen_llm_next(False)
                return
            if answer in ("a", "all"):
                screen_llm_all(False)
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

    if pending := _pending_tailoring(screened, tailored, enriched):
        threshold = current_config()["stale_after_days"]
        stale_rows = [
            row
            for row in pending_tailorable(enriched, screened, tailored)
            if is_stale(row, threshold)
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
        answer = (
            input(
                f"Tailor [o]ldest lead ({BLUE}{shobr_command('tailor-next')}{RESET}), "
                f"[a]ll pending ({BLUE}{shobr_command('tailor-all')}{RESET}),\n"
                f"or [N]ot now? {BOLD}[N]{RESET} "
            )
            .strip()
            .lower()
        )
        if answer in ("o", "y", "yes"):
            tailor_next(False)
            return
        if answer in ("a", "all"):
            tailor_all(False)
            return

    if pending_review_ids():
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
    if sr is None or _needs_human_review(sr):
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
