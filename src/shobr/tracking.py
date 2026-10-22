"""The tracking stage: user-recorded submission statuses."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import TypedDict, cast

from .color import BLUE, BOLD, GREEN, RED, RESET, YELLOW
from .config import load_config
from .core import (
    DataKind,
    ShobrError,
    event_log,
    paged,
    persist_event,
    read_events,
    require_events,
    row_header,
    same_company_line,
    short_path,
    store_key,
    store_path,
)
from .enrichment import (
    EnrichedRow,
    print_apply_block,
    print_description_block,
    project_enrichment,
    staleness_tag,
    validity_tag,
    warn_if_stale,
)
from .providers.base import current_provider
from .screening import (
    ReviewDecision,
    pill_line,
    print_screening_status,
    project_screening,
)
from .tailoring import TailoringRow, project_tailoring


class TrackStatus(StrEnum):
    """Submission pipeline status for a packaged posting."""

    APPLIED = "applied"
    INTERVIEWING = "interviewing"
    OFFER = "offer"
    REJECTED = "rejected"
    GHOSTED = "ghosted"
    WITHDRAWN = "withdrawn"  # explicitly withdrawn by the user


TRACK_STATUS_COLORS = {
    TrackStatus.APPLIED: YELLOW,
    TrackStatus.INTERVIEWING: YELLOW,
    TrackStatus.OFFER: GREEN,
    TrackStatus.REJECTED: RED,
    TrackStatus.GHOSTED: RED,
    TrackStatus.WITHDRAWN: RED,
}


class TrackEvent(TypedDict):
    """A recorded status transition."""

    posting_id: str
    provider: str
    status: TrackStatus
    note: str | None
    tracked_at: str


class TrackedRow(TrackEvent):
    """A job posting's tracking state as projected into tracking.json."""

    first_tracked_at: str


class TrackedStore(TypedDict):
    """Projected statuses keyed by provider/posting_id."""

    tracked_at: str | None
    rows: dict[str, TrackedRow]


def project_tracking(provider: str | None = None) -> TrackedStore:
    """Replay the tracking events log.

    Latest-wins per posting, plus the first transition time."""
    if not (events := read_events(DataKind.TRACKING)):
        return {"tracked_at": None, "rows": {}}
    rows: dict[str, TrackedRow] = {}
    first: dict[str, str] = {}
    for raw in events:
        event = cast(TrackEvent, raw)
        if provider is not None and event["provider"] != provider:
            continue
        key = store_key(event["provider"], event["posting_id"])
        first.setdefault(key, event["tracked_at"])
        rows[key] = TrackedRow(
            posting_id=event["posting_id"],
            provider=event["provider"],
            status=TrackStatus(event["status"]),
            note=event["note"],
            tracked_at=event["tracked_at"],
            first_tracked_at=first[key],
        )
    return {"tracked_at": events[-1]["tracked_at"], "rows": rows}


def trackable_row(posting_id: str) -> tuple[EnrichedRow, TailoringRow | None]:
    """Return the enriched row plus its package, if the board tailors.

    A board without tailoring has no package; its actionable pursue rows are
    trackable directly after screening.
    """
    provider = current_provider()
    key = store_key(provider.name, posting_id)
    if provider.supports_tailoring:
        require_events(DataKind.TAILORING)
        package = project_tailoring(provider.name)["rows"].get(key)
        if package is None:
            if (
                event_log(DataKind.ENRICHMENT).exists()
                and key in project_enrichment(provider.name)["rows"]
            ):
                raise ShobrError(f"posting {posting_id} has no package yet")
            raise ShobrError(f"posting {posting_id} not found in enrichment store")
        enriched = project_enrichment(provider.name)["rows"].get(key)
        if enriched is None:
            raise ShobrError(f"posting {posting_id} not found in enrichment store")
        return enriched, package
    require_events(DataKind.ENRICHMENT)
    enriched = project_enrichment(provider.name)["rows"].get(key)
    if enriched is None:
        raise ShobrError(f"posting {posting_id} not found in enrichment store")
    if not enriched["actionable"]:
        reason = enriched["rejected_reason"] or "unknown reason"
        raise ShobrError(f"posting {posting_id} did not pass the pre-filter ({reason})")
    require_events(DataKind.SCREENING)
    screened = project_screening(provider.name)["rows"].get(key)
    if screened is None or screened["decision"] != ReviewDecision.PURSUE:
        raise ShobrError(f"posting {posting_id} is not marked pursue")
    return enriched, None


def _record_transition(
    enriched: EnrichedRow, package: TailoringRow | None, status: TrackStatus, note: str | None
) -> None:
    """Persist (as event and on store) and report a status transition."""
    previous = project_tracking(current_provider().name)["rows"].get(
        store_key(enriched["provider"], enriched["posting_id"])
    )
    was = previous["status"].upper() if previous else "UNTRACKED"
    event: TrackEvent = {
        "tracked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "posting_id": enriched["posting_id"],
        "provider": enriched["provider"],
        "status": status,
        "note": note,
    }
    persist_event(DataKind.TRACKING, event, project_tracking)
    print("Recorded tracking:")
    if package is not None:
        print(f"  - Posting: {enriched['posting_id']} ({package['slug']})")
    else:
        print(f"  - Posting: {enriched['posting_id']}")
    print(f"  - Status: {status.value.upper()} (was {was})")
    if note:
        print(f"  - Note: {note}")
    print()
    print(f"wrote to {short_path(store_path(DataKind.TRACKING))}")


def track_posting_id(posting_id: str, status: str, note: str | None) -> None:
    """Record a submission pipeline status for a trackable posting."""
    try:
        track_status = TrackStatus(status)
    except ValueError:
        raise ShobrError(
            f"invalid status {status!r}; choose from " + ", ".join(s.value for s in TrackStatus)
        ) from None
    enriched, package = trackable_row(posting_id)
    warn_if_stale(enriched, load_config()["stale_after_days"])
    _record_transition(enriched, package, track_status, note)


def print_tracked() -> None:
    """Print the tracking store summary."""
    with paged():
        require_events(DataKind.TRACKING)
        tracked = project_tracking(current_provider().name)

        counts = {status: 0 for status in TrackStatus}
        for row in tracked["rows"].values():
            counts[row["status"]] += 1
        breakdown = ", ".join(
            f"{counts[status]} {status.value}" for status in TrackStatus if counts[status]
        )
        print(f"{len(tracked['rows'])} tracked ({breakdown})")
        print()
        assert event_log(DataKind.ENRICHMENT).exists(), "tracking data implies enrichment data"
        enriched = project_enrichment(current_provider().name)
        for row in tracked["rows"].values():
            eref = enriched["rows"][store_key(row["provider"], row["posting_id"])]
            print(f"{BOLD}{row_header(eref)}{RESET}{validity_tag(eref)}")
            print(f"  - {eref['title']}")
            color = TRACK_STATUS_COLORS[row["status"]]
            print(f"  - Status: {color}{str(row['status']).upper()}{RESET}")
            print(f"  - Tracked At: {datetime.fromisoformat(row['tracked_at']).date().isoformat()}")
            checked = datetime.fromisoformat(eref["enriched_last_at"]).date().isoformat()
            threshold = load_config()["stale_after_days"]
            print(f"  - Last Enriched At: {checked}{staleness_tag(eref, threshold)}")
            if row["note"]:
                print(f"  - Note: {row['note']}")

        print()


def _print_review(enriched: EnrichedRow, package: TailoringRow | None) -> None:
    """Print the full package for one row, minus package parts when absent."""
    with paged():
        print(f"{row_header(enriched)}{validity_tag(enriched)}")
        print(f"  - {enriched['title']}")
        checked = datetime.fromisoformat(enriched["enriched_last_at"]).date().isoformat()
        threshold = load_config()["stale_after_days"]
        print(f"  - Last Enriched At: {checked}{staleness_tag(enriched, threshold)}")
        if package is not None:
            built = datetime.fromisoformat(package["tailored_at"]).date().isoformat()
            print(f"  - Tailored At: {built}")
        warn_if_stale(enriched, threshold)
        if pills := pill_line(enriched):
            print(f"  - {pills}")
        print_apply_block(enriched)
        if line := same_company_line(
            enriched["company"], enriched["provider"], enriched["posting_id"]
        ):
            print(line)

        # print screening info
        sr = project_screening(current_provider().name)["rows"].get(
            store_key(enriched["provider"], enriched["posting_id"])
        )
        if sr is None:
            print("  - Screening: N/A")
        else:
            print("  - Screening:")
            print_screening_status(sr, "    ")

        # print tracking info
        tracked = project_tracking(current_provider().name)["rows"].get(
            store_key(enriched["provider"], enriched["posting_id"])
        )
        print("  - Tracking:")
        if tracked is None:
            print("    - Status: UNTRACKED")
        else:
            print(f"    - Status: {str(tracked['status']).upper()}")
            if tracked["note"]:
                print(f"    - Note: {tracked['note']}")
        if package is not None:
            print(f"  - Slug: {package['slug']}")
            print(f"  - {BOLD}Application dir:{RESET} {short_path(package['app_dir'])}")
        print_description_block("Description", enriched["job_description"], truncate=False)
        print_description_block(
            "Company description", enriched["company_description"], truncate=False
        )
        print()
        if package is not None:
            print(f"{BOLD}--- RESUME ---{RESET}")
            print()
            print(package["resume_md"])
            print()
            print(f"{BOLD}--- COVER LETTER ---{RESET}")
            print()
            print(package["cover_md"])


def review_posting_id(posting_id: str) -> None:
    """Print the full listing for a specific trackable posting."""
    enriched, package = trackable_row(posting_id)
    _print_review(enriched, package)


def review_next() -> None:
    """Print the oldest pursue posting with no tracking event yet."""
    provider = current_provider()
    tracked = project_tracking(provider.name)["rows"]
    enriched = project_enrichment(provider.name)["rows"]
    passing = {key: row for key, row in enriched.items() if row["actionable"]}
    if provider.supports_tailoring:
        require_events(DataKind.TAILORING)
        candidates = [
            (passing[key], package)
            for key, package in project_tailoring(provider.name)["rows"].items()
            if key not in tracked and key in passing
        ]
    else:
        screened = project_screening(provider.name)["rows"]
        candidates = [
            (row, None)
            for key, row in passing.items()
            if key not in tracked
            and (sr := screened.get(key)) is not None
            and sr["decision"] == ReviewDecision.PURSUE
        ]
    for row, package in candidates:
        _print_review(row, package)
        print(f"If you applied, track it: {BLUE}shobr track {row['posting_id']} applied{RESET}")
        return
    print("nothing to review")
