"""Typed read-only views shared by text and JSON renderers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from .core import (
    DataKind,
    SameCompanyApplication,
    same_company_application,
    stage_position,
)
from .enrichment import is_closed, is_stale
from .providers.base import current_config, current_provider

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .discovery import StoredJob
    from .enrichment import EnrichedRow
    from .screening import ScreeningRow
    from .tailoring import TailoringRow
    from .tracking import TrackedRow


@dataclass(frozen=True)
class PostingView:
    """One posting's stored state across every pipeline stage."""

    provider: str
    posting_id: str
    stage: DataKind
    stage_detail: str | None
    stale: bool | None
    closed: bool | None
    same_company_application: SameCompanyApplication | None
    lead: StoredJob | None
    enrichment: EnrichedRow | None
    screening: ScreeningRow | None
    package: TailoringRow | None
    tracking: TrackedRow | None


@dataclass(frozen=True)
class ListView:
    """A read-only list view with its summary counts."""

    summary: Mapping[str, object]
    postings: list[PostingView]


@dataclass(frozen=True)
class ValidityCount:
    """A count annotated with closed and filtered subcounts."""

    count: int
    closed: int
    filtered: int


@dataclass(frozen=True)
class StaleCount:
    """A count annotated with a stale subcount."""

    count: int
    stale: int


@dataclass(frozen=True)
class ScoreRow:
    """One screening score's LLM and human counts plus pending actions."""

    score: int
    llm: int
    human: int

    # the number of human scored postings for which there is a pending action
    # further on in the pipeline
    pending_action: int


@dataclass(frozen=True)
class DiscoveryStatus:
    """Counts for the discovery stage."""

    total: int
    rejected: int
    pending_enrichment: int


@dataclass(frozen=True)
class EnrichmentStatus:
    """Counts for the enrichment stage."""

    total: int
    rejected: int
    pending_screening: StaleCount


@dataclass(frozen=True)
class ScreeningStatus:
    """Counts and score histogram for the screening stage."""

    total: ValidityCount
    skipped: int
    lacking_llm_review: int
    pending_human_review: StaleCount
    pending_tailoring: StaleCount | None
    pending_review: StaleCount | None
    scores: list[ScoreRow]

    def to_json(self) -> dict[str, object]:
        """Return the JSON object, omitting the inactive pending stage."""
        data: dict[str, object] = {
            "total": self.total,
            "skipped": self.skipped,
            "lacking_llm_review": self.lacking_llm_review,
            "pending_human_review": self.pending_human_review,
        }
        if self.pending_tailoring is not None:
            data["pending_tailoring"] = self.pending_tailoring
        if self.pending_review is not None:
            data["pending_review"] = self.pending_review
        data["scores"] = self.scores
        return data


@dataclass(frozen=True)
class TailoringStatus:
    """Counts for the tailoring stage."""

    packages_built: ValidityCount
    pending_review: StaleCount


@dataclass(frozen=True)
class TrackingStatus:
    """Counts for each tracking status."""

    applied: int
    interviewing: int
    offer: int
    rejected: int
    ghosted: int
    withdrawn: int


@dataclass(frozen=True)
class StatusView:
    """The whole pipeline status overview."""

    discovery: DiscoveryStatus
    enrichment: EnrichmentStatus
    screening: ScreeningStatus
    tailoring: TailoringStatus | None
    tracking: TrackingStatus

    def to_json(self) -> dict[str, object]:
        """Return the stage-keyed JSON object, omitting unsupported tailoring."""
        data: dict[str, object] = {
            "discovery": self.discovery,
            "enrichment": self.enrichment,
            "screening": self.screening,
        }
        if self.tailoring is not None:
            data["tailoring"] = self.tailoring
        data["tracking"] = self.tracking
        return data


def _view_enrichment(row: EnrichedRow) -> EnrichedRow:
    """Return an enrichment row without raw HTML fields for view output."""
    return cast(
        "EnrichedRow",
        {key: value for key, value in row.items() if not key.endswith("_html")},
    )


def build_posting_view(
    posting_id: str,
    *,
    after: DataKind,
    lead: StoredJob | None,
    enrichment: EnrichedRow | None,
    screening: ScreeningRow | None,
    package: TailoringRow | None,
    tracking: TrackedRow | None,
) -> PostingView:
    """Assemble one posting's stage records into a typed view."""
    provider = current_provider().name
    if enrichment is not None:
        stale = is_stale(enrichment, current_config()["stale_after_days"])
        closed = is_closed(enrichment)
        company = enrichment["company"]
    else:
        stale = None
        closed = None
        company = lead["company"] if lead is not None else ""
    position = stage_position(
        after,
        enriched=enrichment is not None,
        screening_decision=str(screening["decision"]) if screening is not None else None,
        tailored=package is not None,
        tracking_status=str(tracking["status"]) if tracking is not None else None,
    )
    return PostingView(
        provider=provider,
        posting_id=posting_id,
        stage=position.stage,
        stage_detail=position.detail,
        stale=stale,
        closed=closed,
        same_company_application=same_company_application(company, provider, posting_id),
        lead=lead,
        enrichment=_view_enrichment(enrichment) if enrichment is not None else None,
        screening=screening,
        package=package,
        tracking=tracking,
    )
