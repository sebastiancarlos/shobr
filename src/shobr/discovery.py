"""The discovery stage: Obtaining job leads in bulk."""

from collections import Counter
from datetime import UTC, datetime
from typing import TypedDict, cast
from urllib.parse import urlencode

from .browser import beachmsg_json
from .color import BOLD, GREEN, RED, RESET
from .config import Config, load_config, resolve_geo_ids
from .core import (
    LINKEDIN_BASE_URL,
    DataKind,
    LocationType,
    ShobrError,
    paged,
    persist_event,
    read_events,
    reasons_line,
    rejected_tag,
    require_events,
    short_path,
    stage_line,
    store_path,
)


class JobRow(TypedDict):
    """Job posting as parsed from a search-results card"""

    posting_id: str
    posting_url: str
    title: str
    company: str
    location: str


class _DiscoverEvent(TypedDict):
    """One search-results fetch."""

    fetched_at: str
    rows: list[JobRow]


class StoredJob(JobRow):
    """Job as stored, with first-sighting metadata and pre-filtering"""

    first_seen_at: str
    actionable: bool
    rejected_reason: str | None


class DiscoveredStore(TypedDict):
    """Projected store, newest first."""

    fetched_at: str | None
    rows: list[StoredJob]


def _location_accepted(
    location: str, location_type: LocationType | None, presence_locations: list[str]
) -> bool:
    """Whether a posting passes the presence rule.

    Remote work passes anywhere, presence work (on-site and hybrid) must name a
    listed place (case-insensitive substring). Without a structured type (from
    "enrichment"), a text mention of remote stands in as the remote signal.
    """
    location = location.lower()
    if location_type is None:
        return "remote" in location or any(
            presence_location.lower() in location for presence_location in presence_locations
        )
    if location_type.lower() == "remote":
        return True
    return any(presence_location.lower() in location for presence_location in presence_locations)


def base_reject_reason(
    location: str, location_type: LocationType | None, title: str, filters: Config
) -> str | None:
    """location + title pre-filter. First matching rule provides the reason."""
    if not _location_accepted(location, location_type, filters["presence_locations"]):
        return f"location '{location}' matches no location rule"
    title_lower = title.lower()
    for label, pattern in filters["reject_title"]:
        if pattern.search(title_lower):
            return f"title contains '{label}'"
    return None


def _reject_job(row: JobRow, filters: Config) -> str | None:
    """Return a rejection reason if the job fails the configured pre-filter.

    First matching rule provides the reason.
    """
    return base_reject_reason(row["location"], None, row["title"], filters)


def project_discovery() -> DiscoveredStore:
    """Replay events.jsonl into the store shape (discovery.json)

    Walks the log, keeping, per posting_id, the first sighting and the most
    recent observed row. Repeats dedupe. Ordered newest-first by first
    sighting.
    """
    if not (events := read_events(DataKind.DISCOVERY)):
        return {"fetched_at": None, "rows": []}
    first_seen: dict[str, str] = {}
    latest: dict[str, JobRow] = {}

    for raw in events:
        event: _DiscoverEvent = raw
        for row in event["rows"]:
            latest[row["posting_id"]] = row
            first_seen.setdefault(row["posting_id"], event["fetched_at"])

    filters = load_config()
    rows: list[StoredJob] = []
    for posting_id in latest:
        rejected_reason = _reject_job(latest[posting_id], filters)
        rows.append(
            cast(
                StoredJob,
                {
                    **latest[posting_id],
                    "first_seen_at": first_seen[posting_id],
                    "actionable": rejected_reason is None,
                    "rejected_reason": rejected_reason,
                },
            )
        )
    rows.sort(key=lambda row: row["first_seen_at"], reverse=True)
    return {"fetched_at": events[-1]["fetched_at"], "rows": rows}


def _print_discovery_summary(leads: DiscoveredStore, mark_new_from: str | None = None) -> None:
    """Print the projected leads store. First seen in `mark_new_from` get `(NEW)`."""
    with paged():
        rejected_count = sum(1 for row in leads["rows"] if not row["actionable"])
        if mark_new_from:
            new_count = sum(1 for row in leads["rows"] if row["first_seen_at"] == mark_new_from)
            print(
                f"{BOLD}All stored jobs:{RESET} {len(leads['rows'])} jobs "
                f"({RED}{rejected_count}{RESET} rejected. {GREEN}{new_count}{RESET} newly seen)"
            )
        else:
            print(
                f"{BOLD}All stored jobs:{RESET} {len(leads['rows'])} jobs "
                f"({RED}{rejected_count}{RESET} rejected)"
            )
        print()
        for row in leads["rows"]:
            tag = (
                f" {GREEN}(NEW){RESET}"
                if mark_new_from and row["first_seen_at"] == mark_new_from
                else ""
            )
            if not row["actionable"]:
                assert row["rejected_reason"] is not None
                tag += rejected_tag(row["rejected_reason"])
            print(f"[{row['company']}]{tag}")
            print(f"  - {row['title']}")
            print(f"  - {row['location']}")
            print(f"  - {row['posting_url']}")
            seen = datetime.fromisoformat(row["first_seen_at"]).date().isoformat()
            print(f"  - First Discovered At: {seen}")
            print(stage_line(row["posting_id"], DataKind.DISCOVERY))

        reasons = Counter(
            row["rejected_reason"] for row in leads["rows"] if row["rejected_reason"] is not None
        )
        if reasons:
            print()
            print(f"{BOLD}Rejection reasons:{RESET}")
            for reason, count in reasons.items():
                print(reasons_line(reason, count))

        print()


def discover_local() -> None:
    """Print the stored job leads summary without fetching."""
    require_events(DataKind.DISCOVERY)
    leads = project_discovery()
    _print_discovery_summary(leads)


def _discovery_url(filters: Config) -> str:
    """Build the search-results URL from the configured titles, types, and geos."""
    params = {
        "keywords": f"{' or '.join(filters['titles'])}, {' or '.join(filters['workplace_types'])}",
        "origin": "PREFERENCES_LANDING",  # left here for "real user" signal
    }
    if geo_ids := resolve_geo_ids(filters):
        params["geoId"] = ",".join(geo_ids)

    jobs_search_path = "jobs/search-results"
    return f"{LINKEDIN_BASE_URL}/{jobs_search_path}?{urlencode(params)}"


def _print_search_params(filters: Config) -> None:
    """Print the search parameters a fetch is about to use."""
    print(f"Performing LinkedIn job search with {BOLD}search parameters{RESET}:")
    for label, values in (
        ("Titles", filters["titles"]),
        ("Workplace types", filters["workplace_types"]),
        ("Geo", filters["geo"]),
    ):
        print(f"- {BOLD}{label}:{RESET}")
        for value in values:
            print(f"  - {value}")


def discover() -> None:
    """Fetch and store the jobs search-results page."""
    filters = load_config()
    _print_search_params(filters)
    print()
    url = _discovery_url(filters)
    raw = beachmsg_json("job-search", url)
    rows: list[JobRow] = raw["rows"]
    if not rows:
        raise ShobrError(
            f"parsed 0 jobs from {url}; the LinkedIn jobs search page structure "
            "may have changed, refusing to write an empty leads store"
        )

    # persist event, reproject, and write the store
    fetched_at = datetime.now(UTC)
    new: _DiscoverEvent = {
        "fetched_at": fetched_at.isoformat(timespec="seconds"),
        "rows": rows,
    }
    leads = persist_event(DataKind.DISCOVERY, new, project_discovery)

    # report the whole projected leads store, tagging rows first seen on this pass.
    _print_discovery_summary(leads, mark_new_from=leads["fetched_at"])
    print(f"wrote to {short_path(store_path(DataKind.DISCOVERY))}")
