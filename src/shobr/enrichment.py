"""The enrichment stage: Fetching individual job details."""

import re
import subprocess
import sys
from collections import Counter
from datetime import UTC, datetime
from typing import TypedDict, cast

from .browser import beachmsg_json
from .color import BLUE, BOLD, GREEN, RED, RESET, YELLOW
from .config import Config, load_config
from .core import (
    LINKEDIN_BASE_URL,
    DataKind,
    EmploymentType,
    LocationType,
    ShobrError,
    applications_by_company,
    closed_tag,
    cooldown_rejection_reason,
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
from .discovery import (
    DiscoveredStore,
    JobRow,
    StoredJob,
    base_reject_reason,
    project_discovery,
)


class JobDetail(TypedDict):
    """The parsed job detail page, as returned by the beachpatrol command."""

    job_description_html: str
    company_description_html: str
    location_type: LocationType | None
    employment_type: EmploymentType | None
    salary_range: str | None
    accepting_applications: bool
    apply_method: str | None
    apply_url: str | None


class EnrichmentEvent(JobRow, JobDetail):
    """One enrichment attempt, appended to the log.

    Combines the card-level snapshot from the discovered row. The description
    fields are stored in markdown, with the raw HTML kept alongside.
    """

    job_description: str
    company_description: str
    fetched_at: str


class EnrichedRow(EnrichmentEvent):
    """An enrichment as projected into enrichment.json (latest attempt wins)."""

    enriched_last_at: str
    actionable: bool
    rejected_reason: str | None


class EnrichedStore(TypedDict):
    """Projected enriched rows keyed by posting id."""

    fetched_at: str | None
    rows: dict[str, EnrichedRow]  # keyed by posting_id


def _reject_enriched(
    row: EnrichmentEvent, apps_by_company: dict[str, dict[str, str]], filters: Config
) -> str | None:
    """Return a rejection reason if the job fails the post-enrichment pre-filter."""
    reason = base_reject_reason(row["location"], row["location_type"], row["title"], filters)
    if reason is not None:
        return reason
    if (row["employment_type"] or "").lower() in {
        value.lower() for value in filters["reject_employment_type"]
    }:
        return f"employment type is {row['employment_type']}"
    if not row["accepting_applications"]:
        return "no longer accepting applications"
    return cooldown_rejection_reason(
        apps_by_company,
        row["posting_id"],
        row["company"],
        row["fetched_at"],
        filters["reject_recent_application_days"],
    )


def project_enrichment() -> EnrichedStore:
    """Replay the enrichment events log into the projection shape (enrichment.json).

    Keyed by posting_id. Latest-wins per posting_id.
    """
    if not (events := read_events(DataKind.ENRICHMENT)):
        return {"fetched_at": None, "rows": {}}
    latest: dict[str, EnrichmentEvent] = {}

    for raw in events:
        event: EnrichmentEvent = raw
        latest[event["posting_id"]] = event

    rows = {}
    filters = load_config()
    cooldown_days = filters["reject_recent_application_days"]
    companies = {pid: row["company"] for pid, row in latest.items()}
    apps_by_company = applications_by_company(companies) if cooldown_days > 0 else {}
    for posting_id, row in latest.items():
        rejected_reason = _reject_enriched(row, apps_by_company, filters)
        rows[posting_id] = cast(
            EnrichedRow,
            {
                **row,
                "enriched_last_at": row["fetched_at"],
                "actionable": rejected_reason is None,
                "rejected_reason": rejected_reason,
            },
        )
    return {"fetched_at": events[-1]["fetched_at"], "rows": rows}


def is_closed(row: EnrichedRow) -> bool:
    """Whether the latest enrichment found the posting closed."""
    return not row["accepting_applications"]


def is_stale(row: EnrichedRow, threshold_days: int) -> bool:
    """Whether the row's last enrichment check is older than the threshold."""
    checked = datetime.fromisoformat(row["enriched_last_at"])
    return (datetime.now(UTC) - checked).days > threshold_days


def warn_if_stale(row: EnrichedRow, threshold_days: int) -> None:
    """Print the re-enrich nudge when the row's last check is stale."""
    if is_stale(row, threshold_days):
        print(
            f"  - Stale: last checked over {threshold_days} days ago;"
            f" consider `shobr enrich {row['posting_id']}` first"
        )


def refuse_if_stale(row: EnrichedRow, threshold_days: int, force: bool) -> None:
    """Raise ShobrError for a stale row unless forced."""
    if force or not is_stale(row, threshold_days):
        return
    checked = datetime.fromisoformat(row["enriched_last_at"]).date().isoformat()
    raise ShobrError(
        f"posting {row['posting_id']} last checked {checked}"
        f" (> {threshold_days} days ago);"
        f" re-enrich (`shobr enrich {row['posting_id']}`) or pass --force"
    )


def staleness_tag(row: EnrichedRow, threshold_days: int) -> str:
    """A yellow (STALE) marker, or empty when freshly checked."""
    return f" {YELLOW}(STALE){RESET}" if is_stale(row, threshold_days) else ""


def validity_tag(row: EnrichedRow) -> str:
    """A (CLOSED) marker, a (REJECTED: ...) tag, or empty for passing rows."""
    if is_closed(row):
        return closed_tag()
    if not row["actionable"]:
        assert row["rejected_reason"] is not None
        return rejected_tag(row["rejected_reason"])
    return ""


def get_enriched_rows() -> dict[str, EnrichedRow]:
    """Projected enriched rows, raising the error when missing."""
    require_events(DataKind.ENRICHMENT)
    return project_enrichment()["rows"]


def _next_lead_to_enrich(leads: DiscoveredStore, enriched: EnrichedStore) -> StoredJob | None:
    """The oldest lead not yet enriched."""
    for row in reversed(leads["rows"]):
        if not row["actionable"]:
            continue
        if row["posting_id"] in enriched["rows"]:
            continue
        return row
    return None


def _strip_link_attributes(html: str) -> str:
    """Drop every attribute but href from <a> tags. Useful for better pandoc
    conversion to markdown links."""

    def keep_href(match: re.Match) -> str:
        href = re.search(r'\bhref\s*=\s*("[^"]*"|\'[^\']*\')', match.group(0))
        return f"<a href={href.group(1)}>" if href else "<a>"

    return re.sub(r"<a[^>]*>", keep_href, html, flags=re.IGNORECASE)


def _html_to_markdown(html: str) -> str | None:
    """Render a description's HTML to markdown via pandoc, or None on failure.

    `native_divs`/`native_spans` are turned off so LinkedIn's classed wrapper
    divs and spans are transparent. Output is wrapped to ~80 columns.
    """
    try:
        result = subprocess.run(
            [
                "pandoc",
                "-f",
                "html-native_divs-native_spans",
                "-t",
                "gfm",
                "--wrap=auto",
                "--columns=80",
            ],
            input=_strip_link_attributes(html),
            text=True,
            capture_output=True,
        )
    except FileNotFoundError:
        print("pandoc not found; is it installed?", file=sys.stderr)
        return None
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        return None
    markdown = result.stdout.strip()
    lines = markdown.splitlines()

    # clean up some artifacts
    cleaned: list[str] = []
    for line in lines:
        if line == "\\":
            # pandoc renders a double <br> (a paragraph break) as a lone
            # backslash line; keep it as a blank line again.
            if cleaned and cleaned[-1] != "":
                cleaned.append("")
            continue
        # a content line's trailing backslash marks a single hard break: drop
        # the marker.
        line = line[:-1] if line.endswith("\\") else line
        cleaned.append(line)

    return "\n".join(cleaned)


def print_apply_block(row: EnrichedRow) -> None:
    """Print the apply-method block with external URL + LinkedIn fallback."""
    if not row["apply_method"]:
        return
    color = YELLOW if row["apply_method"] == "external" else BLUE
    suffix = ":" if row["apply_url"] else ""
    print(f"  - Apply ({color}{row['apply_method']}{RESET}){suffix}")
    if row["apply_url"]:
        print(f"    - {row['apply_url']}")
        print("    - Or through LinkedIn:")
    else:
        print("    - Through LinkedIn:")
    print(f"      - {row['posting_url']}")


def print_description_block(label: str, text: str | None, *, truncate: bool) -> None:
    """Print one description block, optionally capped at its first line."""
    if not text:
        print(f"  - {label}: none captured")
        return
    print(f"  - {label}:")
    lines = text.splitlines()
    if truncate and len(lines) > 1:
        print(f"      {lines[0]}...")
        return
    for line in lines:
        print(f"      {line}")


def print_enriched_row(row: EnrichedRow, *, short: bool = False, truncate: bool = False) -> None:
    """Print the enriched lead fields.

    With `short`, omit job and company description blocks. With `truncate`,
    cap each description block at its first line (for multi-row listings).
    """
    tag = ""
    if not row["actionable"]:
        assert row["rejected_reason"] is not None
        tag = rejected_tag(row["rejected_reason"])
    print(f"[{row['company']}] {row['posting_id']}{tag}")
    print(f"  - {row['title']}")
    print(f"  - {row['location']}")
    for label, value in (
        ("Location type", row["location_type"]),
        ("Employment type", row["employment_type"]),
        ("Salary range", row["salary_range"]),
    ):
        if value:
            print(f"  - {label}: {value}")
    print_apply_block(row)
    if not row["accepting_applications"]:
        print("  - no longer accepting applications")
    checked = datetime.fromisoformat(row["enriched_last_at"]).date().isoformat()
    threshold = load_config()["stale_after_days"]
    print(f"  - Last Enriched At: {checked}{staleness_tag(row, threshold)}")
    if line := same_company_line(row["company"], row["posting_id"]):
        print(line)
    if short:
        return
    print_description_block("Description", row["job_description"], truncate=truncate)
    print_description_block("Company description", row["company_description"], truncate=truncate)


def _enrich_one(target: StoredJob) -> None:
    """Fetch and persist the detail for a single known lead."""
    nav_url = f"{LINKEDIN_BASE_URL}/jobs/view/{target['posting_id']}"
    raw = beachmsg_json("job-detail", nav_url)
    detail = cast(JobDetail, raw)

    job_description = _html_to_markdown(detail["job_description_html"])
    company_description = _html_to_markdown(detail["company_description_html"])
    if job_description is None or not job_description.strip():
        raise ShobrError(
            f"parsed no description for posting {target['posting_id']}; "
            "the LinkedIn job detail page structure may have changed, "
            "refusing to record an empty enrichment"
        )
    if detail["accepting_applications"] and not detail["apply_method"] and not detail["apply_url"]:
        raise ShobrError(
            f"parsed no apply info for posting {target['posting_id']} while it still "
            "appears open; the LinkedIn job detail page structure may have changed "
            "(or the posting closed with an unrecognized notice), "
            "refusing to record an incomplete enrichment"
        )
    if company_description is None:
        company_description = ""

    # persist event
    fetched_at = datetime.now(UTC)
    row: EnrichmentEvent = {
        "fetched_at": fetched_at.isoformat(timespec="seconds"),
        "posting_id": target["posting_id"],
        "posting_url": target["posting_url"],
        "title": target["title"],
        "company": target["company"],
        "location": target["location"],
        "job_description_html": detail["job_description_html"],
        "company_description_html": detail["company_description_html"],
        "job_description": job_description,
        "company_description": company_description,
        "location_type": detail["location_type"],
        "employment_type": detail["employment_type"],
        "salary_range": detail["salary_range"],
        "accepting_applications": detail["accepting_applications"],
        "apply_method": detail["apply_method"],
        "apply_url": detail["apply_url"],
    }
    # persist to store
    enriched = persist_event(DataKind.ENRICHMENT, row, project_enrichment)

    print_enriched_row(enriched["rows"][target["posting_id"]])
    print()
    print(f"wrote to {short_path(store_path(DataKind.ENRICHMENT))}")


def enrich_next() -> None:
    """Fetch the full detail of the oldest pre-filtered, non-enriched lead."""
    require_events(DataKind.DISCOVERY)
    leads = project_discovery()
    enriched = project_enrichment()

    target = _next_lead_to_enrich(leads, enriched)
    if target is None:
        print("no leads to enrich")
        return

    _enrich_one(target)


def enrich_posting_id(posting_id: str) -> None:
    """Fetch and persist the detail for a specific posting id."""
    require_events(DataKind.DISCOVERY)

    leads = project_discovery()
    target = next((row for row in leads["rows"] if row["posting_id"] == posting_id), None)
    if target is None:
        raise ShobrError(f"posting {posting_id} not found in leads store")

    _enrich_one(target)


def print_enriched() -> None:
    """Print every enriched job detail stored so far, in enrichment order."""
    with paged():
        require_events(DataKind.ENRICHMENT)
        enriched = project_enrichment()

        rows = enriched["rows"].values()
        rejected_count = sum(1 for row in rows if not row["actionable"])
        print(
            f"{GREEN}{len(enriched['rows'])}{RESET} enriched "
            f"({RED}{rejected_count}{RESET} rejected)"
        )
        print()
        for row in rows:
            print_enriched_row(row, truncate=True)
            print(stage_line(row["posting_id"], DataKind.ENRICHMENT))

        reasons = Counter(
            row["rejected_reason"] for row in rows if row["rejected_reason"] is not None
        )
        if reasons:
            print()
            print(f"{BOLD}Rejection reasons:{RESET}")
            for reason, count in reasons.items():
                print(reasons_line(reason, count))

        print()
