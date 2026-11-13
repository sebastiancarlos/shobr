"""The tracking stage: user-recorded submission statuses."""

from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, TypedDict, cast

from .color import BLUE, BOLD, GREEN, RED, RESET, YELLOW
from .core import (
    DataKind,
    ShobrError,
    cached_projection,
    event_log,
    paged,
    persist_event,
    read_events,
    require_events,
    row_header,
    same_company_line,
    shobr_command,
    short_path,
    stage_line,
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
from .output import emit_json
from .providers.base import current_config, current_provider
from .screening import (
    ReviewDecision,
    pill_line,
    print_screening_status,
    project_screening,
)
from .tailoring import TailoringRow, project_tailoring

if TYPE_CHECKING:
    from .views import ListView, PostingView


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


@cached_projection
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
    warn_if_stale(enriched, current_config()["stale_after_days"])
    _record_transition(enriched, package, track_status, note)


def _tracked_list_view(tracked: TrackedStore) -> ListView:
    """Build the tracking store as a typed list view."""
    from .discovery import project_discovery
    from .views import ListView, build_posting_view

    provider = current_provider().name
    leads = {row["posting_id"]: row for row in project_discovery(provider)["rows"]}
    enriched = project_enrichment(provider)["rows"]
    screened = project_screening(provider)["rows"]
    tailored = project_tailoring(provider)["rows"]
    rows = list(tracked["rows"].values())
    postings = [
        build_posting_view(
            row["posting_id"],
            after=DataKind.TRACKING,
            lead=leads.get(row["posting_id"]),
            enrichment=enriched[store_key(row["provider"], row["posting_id"])],
            screening=screened.get(store_key(row["provider"], row["posting_id"])),
            package=tailored.get(store_key(row["provider"], row["posting_id"])),
            tracking=row,
        )
        for row in rows
    ]
    counts = {status: 0 for status in TrackStatus}
    for row in rows:
        counts[row["status"]] += 1
    return ListView(
        summary={
            "tracked": len(rows),
            "by_status": {status.value: counts[status] for status in TrackStatus},
        },
        postings=postings,
    )


def print_tracked(*, json_output: bool = False) -> None:
    """Print the tracking store summary."""
    require_events(DataKind.TRACKING)
    view = _tracked_list_view(project_tracking(current_provider().name))
    if json_output:
        emit_json(view)
        return
    with paged():
        by_status = cast("dict[str, int]", view.summary["by_status"])
        breakdown = ", ".join(
            f"{by_status[status.value]} {status.value}"
            for status in TrackStatus
            if by_status[status.value]
        )
        print(f"{view.summary['tracked']} tracked ({breakdown})")
        print()
        for posting in view.postings:
            eref = posting.enrichment
            row = posting.tracking
            assert eref is not None, "tracking view has no enrichment"
            assert row is not None, "tracking view has no tracking"
            print(f"{BOLD}{row_header(eref)}{RESET}{validity_tag(eref)}")
            print(f"  - {eref['title']}")
            color = TRACK_STATUS_COLORS[row["status"]]
            print(f"  - Status: {color}{str(row['status']).upper()}{RESET}")
            print(f"  - Tracked At: {datetime.fromisoformat(row['tracked_at']).date().isoformat()}")
            checked = datetime.fromisoformat(eref["enriched_last_at"]).date().isoformat()
            assert posting.stale is not None, "tracking view has no staleness"
            print(f"  - Last Enriched At: {checked}{staleness_tag(posting.stale)}")
            if row["note"]:
                print(f"  - Note: {row['note']}")

        print()


def print_review(view: PostingView, *, show_stage: bool = False) -> None:
    """Print the full stored detail for one enriched posting (and above)."""
    enriched = view.enrichment
    package = view.package
    lead = view.lead
    assert enriched is not None, "review view has no enrichment"
    assert view.stale is not None, "review view has no staleness"
    with paged():
        threshold = current_config()["stale_after_days"]
        print(f"{row_header(enriched)}{validity_tag(enriched)}")
        if show_stage:
            print(stage_line(view.stage, view.stage_detail, DataKind.DISCOVERY))
        print(f"  - {enriched['title']}")
        if lead is not None:
            print(f"  - Posting URL: {lead['posting_url']}")
            seen = datetime.fromisoformat(str(lead["first_seen_at"])).date().isoformat()
            print(f"  - First Discovered At: {seen}")
            method = lead["discovered_method"]
            if isinstance(method, Mapping):
                print(f"  - Discovered Via: {method['type']}")
        checked = datetime.fromisoformat(enriched["enriched_last_at"]).date().isoformat()
        print(f"  - Last Enriched At: {checked}{staleness_tag(view.stale)}")
        if package is not None:
            built = datetime.fromisoformat(package["tailored_at"]).date().isoformat()
            print(f"  - Tailored At: {built}")
        warn_if_stale(enriched, threshold, stale=view.stale)
        if pills := pill_line(enriched):
            print(f"  - {pills}")
        print_apply_block(enriched)
        if line := same_company_line(view.same_company_application):
            print(line)

        # print screening info
        sr = view.screening
        if sr is None:
            print("  - Screening: N/A")
        else:
            print("  - Screening:")
            print_screening_status(sr, "    ")

        # print tracking info
        tracked = view.tracking
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
    from .views import build_posting_view

    enriched, package = trackable_row(posting_id)
    provider = current_provider()
    key = store_key(provider.name, posting_id)
    view = build_posting_view(
        posting_id,
        after=DataKind.DISCOVERY,
        lead=None,
        enrichment=enriched,
        screening=project_screening(provider.name)["rows"].get(key),
        package=package,
        tracking=project_tracking(provider.name)["rows"].get(key),
    )
    print_review(view)


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
    from .views import build_posting_view

    for row, package in candidates:
        key = store_key(row["provider"], row["posting_id"])
        view = build_posting_view(
            row["posting_id"],
            after=DataKind.DISCOVERY,
            lead=None,
            enrichment=row,
            screening=project_screening(provider.name)["rows"].get(key),
            package=package,
            tracking=tracked.get(key),
        )
        print_review(view)
        track_command = shobr_command(f"track {row['posting_id']} applied")
        print(f"If you applied, track it: {BLUE}{track_command}{RESET}")
        return
    print("nothing to review")
