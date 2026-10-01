"""The discovery stage: Obtaining job leads in bulk."""

import re
from collections import Counter
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, TypedDict, cast
from urllib.parse import urlencode

from .browser import beachmsg_json
from .color import BOLD, GREEN, RED, RESET
from .config import Config, load_config, resolve_geo_ids
from .core import (
    LINKEDIN_BASE_URL,
    DataKind,
    LocationType,
    ShobrError,
    applications_by_company,
    cooldown_rejection_reason,
    event_log,
    paged,
    persist_event,
    read_events,
    reasons_line,
    rejected_tag,
    require_events,
    same_company_line,
    short_path,
    stage_line,
    store_path,
)

if TYPE_CHECKING:
    from .enrichment import JobDetail


class JobRow(TypedDict):
    """Job posting as parsed from a search-results card"""

    posting_id: str
    posting_url: str
    title: str
    company: str
    location: str


class DiscoveryQuery(TypedDict):
    """The search params behind one discovery fetch."""

    titles: list[str]
    workplace_types: list[str]
    geo: list[str]


class QuerySource(TypedDict):
    """A lead surfaced by a search fetch."""

    type: Literal["query"]
    query: DiscoveryQuery


class ManualSource(TypedDict):
    """A lead added directly, with no search behind it."""

    type: Literal["manual"]


DiscoverySource = QuerySource | ManualSource


class _DiscoverEvent(TypedDict):
    """One search-results fetch, or one direct add."""

    fetched_at: str
    source: DiscoverySource
    rows: list[JobRow]


class StoredJob(JobRow):
    """Job as stored, with first-sighting metadata and pre-filtering"""

    first_seen_at: str
    discovered_method: DiscoverySource
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
    last_seen: dict[str, str] = {}
    first_source: dict[str, DiscoverySource] = {}
    latest: dict[str, JobRow] = {}

    for raw in events:
        event: _DiscoverEvent = raw
        for row in event["rows"]:
            latest[row["posting_id"]] = row
            first_seen.setdefault(row["posting_id"], event["fetched_at"])
            last_seen[row["posting_id"]] = event["fetched_at"]
            first_source.setdefault(row["posting_id"], event["source"])

    filters = load_config()
    cooldown_days = filters["reject_recent_application_days"]
    companies = {pid: row["company"] for pid, row in latest.items()}
    apps_by_company = applications_by_company(companies) if cooldown_days > 0 else {}
    rows: list[StoredJob] = []
    for posting_id in latest:
        rejected_reason = _reject_job(latest[posting_id], filters)
        if rejected_reason is None:
            rejected_reason = cooldown_rejection_reason(
                apps_by_company,
                posting_id,
                latest[posting_id]["company"],
                last_seen[posting_id],
                cooldown_days,
            )
        rows.append(
            cast(
                StoredJob,
                {
                    **latest[posting_id],
                    "first_seen_at": first_seen[posting_id],
                    "discovered_method": first_source[posting_id],
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
            if line := same_company_line(row["company"], row["posting_id"]):
                print(line)
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
        "source": {
            "type": "query",
            "query": {
                "titles": filters["titles"],
                "workplace_types": filters["workplace_types"],
                "geo": filters["geo"],
            },
        },
        "rows": rows,
    }
    leads = persist_event(DataKind.DISCOVERY, new, project_discovery)

    # report the whole projected leads store, tagging rows first seen on this pass.
    _print_discovery_summary(leads, mark_new_from=leads["fetched_at"])
    print(f"wrote to {short_path(store_path(DataKind.DISCOVERY))}")


def _extract_posting_id(target: str) -> str:
    """Posting id from a bare id or a LinkedIn jobs URL."""
    if target.isdigit():
        return target
    for pattern in (r"/jobs/view/(?:[a-z-]*-)?(\d+)", r"currentJobId=(\d+)"):
        if match := re.search(pattern, target):
            return match.group(1)
    raise ShobrError(f"could not extract a posting id from {target!r}")


def discover_posting_id(target: str) -> None:
    """Fetch one posting by id or URL and record it as a manual lead + enrichment."""
    from .enrichment import build_enrichment_event, print_enriched_row, project_enrichment

    posting_id = _extract_posting_id(target)
    if event_log(DataKind.DISCOVERY).exists():
        known = {row["posting_id"] for row in project_discovery()["rows"]}
        if posting_id in known:
            raise ShobrError(
                f"posting {posting_id} is already a known lead; "
                f"fetch its detail with `shobr enrich {posting_id}`"
            )
    nav_url = f"{LINKEDIN_BASE_URL}/jobs/view/{posting_id}"
    detail = cast("JobDetail", beachmsg_json("job-detail", nav_url))
    title, company, location = (
        detail.get("title"),
        detail.get("company"),
        detail.get("location"),
    )
    if not title or not company or not location or company == title:
        raise ShobrError(
            f"parsed no discovery-stage fields for posting {posting_id}; "
            "the LinkedIn job detail page structure may have changed, "
            "refusing to record an incomplete lead"
        )
    lead: JobRow = {
        "posting_id": posting_id,
        "posting_url": f"https://www.linkedin.com/jobs/view/{posting_id}",
        "title": title,
        "company": company,
        "location": location,
    }
    fetched_at = datetime.now(UTC)
    discovery_event: _DiscoverEvent = {
        "fetched_at": fetched_at.isoformat(timespec="seconds"),
        "source": {"type": "manual"},
        "rows": [lead],
    }
    enrichment_event = build_enrichment_event(lead, detail, fetched_at)
    persist_event(DataKind.DISCOVERY, discovery_event, project_discovery)
    enriched = persist_event(DataKind.ENRICHMENT, enrichment_event, project_enrichment)

    print_enriched_row(enriched["rows"][posting_id])
    print()
    print(f"wrote to {short_path(store_path(DataKind.DISCOVERY))}")
    print(f"wrote to {short_path(store_path(DataKind.ENRICHMENT))}")
