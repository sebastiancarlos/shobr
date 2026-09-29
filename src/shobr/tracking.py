"""The tracking stage: user-recorded submission statuses."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import TypedDict

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
    short_path,
    store_path,
)
from .enrichment import (
    get_enriched_rows,
    print_apply_block,
    print_description_block,
    project_enrichment,
    staleness_tag,
    validity_tag,
    warn_if_stale,
)
from .screening import pill_line, print_screening_status, project_screening
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


class TrackedRow(TypedDict):
    """A job posting's tracking state as projected into tracking.json."""

    posting_id: str
    status: TrackStatus
    note: str | None
    tracked_at: str


class TrackEvent(TrackedRow):
    """A recorded status transition."""


class TrackedStore(TypedDict):
    """Projected statuses keyed by posting id."""

    tracked_at: str | None
    rows: dict[str, TrackedRow]


def project_tracking() -> TrackedStore:
    """Replay the tracking events log."""
    if not (events := read_events(DataKind.TRACKING)):
        return {"tracked_at": None, "rows": {}}
    rows: dict[str, TrackedRow] = {}
    for event in events:
        posting_id = event["posting_id"]
        rows[posting_id] = TrackedRow(
            posting_id=posting_id,
            status=TrackStatus(event["status"]),
            note=event["note"],
            tracked_at=event["tracked_at"],
        )
    return {"tracked_at": events[-1]["tracked_at"], "rows": rows}


def trackable_row(posting_id: str) -> TailoringRow:
    """Return the packaged row for `posting_id`, raising when not trackable."""
    require_events(DataKind.TAILORING)
    row = project_tailoring()["rows"].get(posting_id)
    if row is not None:
        return row
    if event_log(DataKind.ENRICHMENT).exists() and posting_id in project_enrichment()["rows"]:
        raise ShobrError(f"posting {posting_id} has no package yet")
    raise ShobrError(f"posting {posting_id} not found in enrichment store")


def _record_transition(row: TailoringRow, status: TrackStatus, note: str | None) -> None:
    """Persist (as event and on store) and report a status transition."""
    previous = project_tracking()["rows"].get(row["posting_id"])
    was = previous["status"].upper() if previous else "UNTRACKED"
    event: TrackEvent = {
        "tracked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "posting_id": row["posting_id"],
        "status": status,
        "note": note,
    }
    persist_event(DataKind.TRACKING, event, project_tracking)
    print("Recorded tracking:")
    print(f"  - Posting: {row['posting_id']} ({row['slug']})")
    print(f"  - Status: {status.value.upper()} (was {was})")
    if note:
        print(f"  - Note: {note}")
    print()
    print(f"wrote to {short_path(store_path(DataKind.TRACKING))}")


def track_posting_id(posting_id: str, status: str, note: str | None) -> None:
    """Record a submission pipeline status for a packaged posting."""
    try:
        track_status = TrackStatus(status)
    except ValueError:
        raise ShobrError(
            f"invalid status {status!r}; choose from " + ", ".join(s.value for s in TrackStatus)
        ) from None
    row = trackable_row(posting_id)
    warn_if_stale(project_enrichment()["rows"][posting_id], load_config()["stale_after_days"])
    _record_transition(row, track_status, note)


def print_tracked() -> None:
    """Print the tracking store summary."""
    with paged():
        require_events(DataKind.TRACKING)
        tracked = project_tracking()

        counts = {status: 0 for status in TrackStatus}
        for row in tracked["rows"].values():
            counts[row["status"]] += 1
        breakdown = ", ".join(
            f"{counts[status]} {status.value}" for status in TrackStatus if counts[status]
        )
        print(f"{len(tracked['rows'])} tracked ({breakdown})")
        print()
        assert event_log(DataKind.ENRICHMENT).exists(), "tracking data implies enrichment data"
        enriched = project_enrichment()
        for row in tracked["rows"].values():
            eref = enriched["rows"][row["posting_id"]]
            print(f"{BOLD}[{eref['company']}] {row['posting_id']}{RESET}{validity_tag(eref)}")
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


def _print_review(row: TailoringRow) -> None:
    """Print the full package for one packaged row."""
    with paged():
        # print enrichment info
        rows = get_enriched_rows()
        eref = rows.get(row["posting_id"])
        if eref is None:
            raise ShobrError(f"posting {row['posting_id']} not found in enrichment store")
        print(f"[{eref['company']}] {row['posting_id']}{validity_tag(eref)}")
        print(f"  - {eref['title']}")
        checked = datetime.fromisoformat(eref["enriched_last_at"]).date().isoformat()
        threshold = load_config()["stale_after_days"]
        print(f"  - Last Enriched At: {checked}{staleness_tag(eref, threshold)}")
        built = datetime.fromisoformat(row["tailored_at"]).date().isoformat()
        print(f"  - Tailored At: {built}")
        threshold = load_config()["stale_after_days"]
        warn_if_stale(eref, threshold)
        pills = pill_line(eref)
        if pills:
            print(f"  - {pills}")
        print_apply_block(eref)

        # print screening info
        sr = project_screening()["rows"].get(row["posting_id"])
        if sr is None:
            print("  - Screening: N/A")
        else:
            print("  - Screening:")
            print_screening_status(sr, "    ")

        # print tracking info
        tracked = project_tracking()["rows"].get(row["posting_id"])
        print("  - Tracking:")
        if tracked is None:
            print("    - Status: UNTRACKED")
        else:
            print(f"    - Status: {str(tracked['status']).upper()}")
            if tracked["note"]:
                print(f"    - Note: {tracked['note']}")
        print(f"  - Slug: {row['slug']}")
        print(f"  - {BOLD}Application dir:{RESET} {short_path(row['app_dir'])}")
        print_description_block("Description", eref["job_description"], truncate=False)
        print_description_block("Company description", eref["company_description"], truncate=False)
        print()
        print(f"{BOLD}--- RESUME ---{RESET}")
        print()
        print(row["resume_md"])
        print()
        print(f"{BOLD}--- COVER LETTER ---{RESET}")
        print()
        print(row["cover_md"])


def review_posting_id(posting_id: str) -> None:
    """Print the full package for a specific packaged posting."""
    row = trackable_row(posting_id)
    _print_review(row)


def review_next() -> None:
    """Print the full package for the oldest packaged posting with no
    tracking event yet."""
    require_events(DataKind.TAILORING)
    tracked = project_tracking()["rows"]
    passing = {pid for pid, row in project_enrichment()["rows"].items() if row["actionable"]}
    for posting_id, row in project_tailoring()["rows"].items():
        if posting_id in tracked or posting_id not in passing:
            continue
        _print_review(row)
        print(f"If you applied, track it: {BLUE}shobr track {posting_id} applied{RESET}")
        return
    print("nothing to review")
