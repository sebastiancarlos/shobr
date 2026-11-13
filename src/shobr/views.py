"""Typed read-only views shared by text and JSON renderers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

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
        enrichment=enrichment,
        screening=screening,
        package=package,
        tracking=tracking,
    )
