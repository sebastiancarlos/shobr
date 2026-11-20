"""Progress data: consumed by chart generation."""

import calendar
from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, ClassVar, cast

from ..config import Config, get_config_events, load_provider_config, parse_config
from ..core import (
    SHOBR_DATA_DIR,
    DataKind,
    ShobrError,
    cooldown_rejection_reason,
    event_config_hash,
    read_events,
    store_key,
)
from ..discovery import JobRow, discovery_rejection_reason
from ..enrichment import EnrichmentEvent, enrichment_rejection_reason
from ..providers.base import current_provider
from ..screening import ReviewDecision, screening_decision
from ..tracking import TrackStatus

SERIES_ORDER = ("Discovered", "Enriched", "Screened", "Tailored", "Tracked", "Applied")
RANGE_CHOICES = ("week", "last-week", "month", "last-month", "6months", "year")
_RANGE_MONTHS = {
    "6months": 6,
    "year": 12,
}


@dataclass(frozen=True)
class _BinRange:
    """A half-open date range [start, end)."""

    start: date
    end: date


@dataclass(frozen=True)
class _BinCount:
    """A half-open datetime range with the number of applications in it."""

    start: datetime
    end: datetime
    count: int

    def to_json(self) -> dict[str, object]:
        """Serialize the bucket for `shobr progress --json`."""
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "count": self.count,
        }


@dataclass(frozen=True)
class _ReplayResult:
    """The result of replaying the event log."""

    # The `*_passing` timelines map each timestamp to how many postings passed
    # the matching filter under the config in effect at that time.
    discovered_passing: dict[datetime, int]
    enriched_passing: dict[datetime, int]
    screened_passing: dict[datetime, int]

    # the backlog timeline: each timestamp maps to how many postings were still
    # waiting by then
    pending: dict[datetime, int]


@dataclass(frozen=True)
class _ProgressData:
    """Raw data used to generate the final chart's data.

    For each posting, the `discovered`, `enriched`, `screened`, `tailored`,
    `tracked`, and `applied` fields record the first time it reached each one.

    The `*_passing` and `pending` fields come from the `_ReplayResult` class.
    """

    discovered: dict[str, datetime]
    enriched: dict[str, datetime]
    screened: dict[str, datetime]
    tailored: dict[str, datetime]
    tracked: dict[str, datetime]
    applied: dict[str, datetime]

    discovered_passing: dict[datetime, int]
    enriched_passing: dict[datetime, int]
    screened_passing: dict[datetime, int]

    pending: dict[datetime, int]

    def times(self, series_name: str) -> list[datetime]:
        """Return the event times for one progress series."""
        return list(getattr(self, series_name).values())


@dataclass(frozen=True)
class SeriesSpec:
    """One line on a progress chart.

    `x` and `y` are the step points inside the selected range.

    `total` is the number shown in the legend.
    """

    total: int | None
    x: list[datetime]
    y: list[int]

    def to_json(self) -> dict[str, object]:
        """Serialize the series for `shobr progress --json`."""
        data: dict[str, object] = {}
        if self.total is not None:
            data["total"] = self.total
        data["x"] = [value.isoformat() for value in self.x]
        data["y"] = self.y
        return data


@dataclass(frozen=True)
class ChartSpec:
    """The full data used to render one chart."""

    provider: str
    range_key: str
    start: datetime
    end: datetime
    today: date
    series: dict[str, SeriesSpec]
    application_bins: list[_BinCount]

    def to_json(self) -> dict[str, object]:
        """Serialize the chart data for `shobr progress --json`."""
        return {
            "provider": self.provider,
            "range_key": self.range_key,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "today": self.today.isoformat(),
            "series": {key: series.to_json() for key, series in self.series.items()},
            "application_bins": [bin_.to_json() for bin_ in self.application_bins],
        }


@dataclass
class _PostingState:
    """What the replay currently knows about one posting.

    Note: The replay walks the event logs in time order and rebuilds the
    pipeline as it goes.
    """

    # the latest discovery row and the time it was fetched, used for the
    # discovery filter and the cooldown check
    discovery_row: JobRow | None = None
    discovery_fetched_at: str | None = None

    # the latest enrichment event, used for the enrichment filter.
    enrichment_event: EnrichmentEvent | None = None

    # The two scores feed the screening decision.
    human_score: int | None = None
    ai_score: int | None = None

    # a tracking event has been seen
    tracked: bool = False


@dataclass(frozen=True)
class _PassingStatus:
    """The "passing" state of one posting during the replay."""

    discovered: bool
    enriched: bool
    screening: ReviewDecision


class _ConfigResolver:
    """Resolve config hashes to effective provider configs for the replay."""

    def __init__(self, provider: str, config_events: list[dict[str, object]]) -> None:
        """Index the raw configs from the config event log."""
        self._provider = provider
        self._raw_by_hash: dict[str, dict[str, Any]] = {}
        for event in config_events:
            config_hash = cast(str, event["config_hash"])
            raw = cast(dict[str, Any], event["config"])
            self._raw_by_hash[config_hash] = raw
        self._cache: dict[str, Config] = {}

    def get(self, event: dict[str, Any]) -> Config:
        """Return the effective config for one event."""
        config_hash = event_config_hash(event)
        raw = self._raw_by_hash.get(config_hash)
        if raw is None:
            raise ShobrError(f"config hash {config_hash} is missing from config/events.jsonl")
        if config_hash not in self._cache:
            self._cache[config_hash] = parse_config(raw, provider=self._provider)
        return self._cache[config_hash]


@dataclass
class _CompanyApplications:
    """Applications grouped by company as tracking events are replayed.

    Posting keys are `store_key(provider, posting_id)` values.
    """

    # posting key -> company name
    company_by_posting: dict[str, str]

    # posting key -> first tracked timestamp
    first_tracked_by_posting: dict[str, str] = field(init=False, default_factory=dict)

    # company -> {posting key: first tracked timestamp}
    apps_by_company: dict[str, dict[str, str]] = field(init=False, default_factory=dict)

    def apply(self, event: dict[str, Any]) -> None:
        """Apply one tracking event to the company applications.

        A withdrawn event removes the posting from its company's applications.
        Any other status records it under its company, with the posting's first
        tracked time as the value. Postings with no known company are ignored.
        """
        key = store_key(event["provider"], event["posting_id"])
        self.first_tracked_by_posting.setdefault(key, event["tracked_at"])
        company = self.company_by_posting.get(key)
        if not company:
            return
        apps = self.apps_by_company.setdefault(company, {})
        if event["status"] == TrackStatus.WITHDRAWN:
            apps.pop(key, None)
            if not apps:
                self.apps_by_company.pop(company, None)
            return
        apps[key] = self.first_tracked_by_posting[key]


def _company_by_key(
    discovery_events: list[dict[str, Any]],
    enrichment_events: list[dict[str, Any]],
) -> dict[str, str]:
    """Return the company for each posting.

    Enrichment events are preferred when the same posting appears in both.
    """
    # posting key -> company name
    result: dict[str, str] = {}
    for event in discovery_events:
        for row in event["rows"]:
            result[store_key(row["provider"], row["posting_id"])] = row["company"]
    for event in enrichment_events:
        result[store_key(event["provider"], event["posting_id"])] = event["company"]
    return result


def _discovery_actionable(
    row: JobRow,
    fetched_at: str,
    filters: Config,
    apps_by_company: dict[str, dict[str, str]],
) -> bool:
    """Whether a discovery row passes the pre-filter."""
    reason = discovery_rejection_reason(row, filters)
    if reason is None:
        reason = cooldown_rejection_reason(
            apps_by_company,
            store_key(row["provider"], row["posting_id"]),
            row["company"],
            fetched_at,
            filters["reject_recent_application_days"],
        )
    return reason is None


def _enrichment_actionable(
    event: EnrichmentEvent,
    filters: Config,
    apps_by_company: dict[str, dict[str, str]],
) -> bool:
    """Whether an enrichment event passes the pre-filter."""
    reason = enrichment_rejection_reason(
        event,
        apps_by_company,
        filters,
    )
    return reason is None


def _passing_status(
    state: _PostingState,
    filters: Config,
    apps_by_company: dict[str, dict[str, str]],
) -> _PassingStatus:
    """Return which filter stages one replayed posting currently passes."""
    discovered = False
    if state.discovery_row is not None:
        assert state.discovery_fetched_at is not None
        discovered = _discovery_actionable(
            state.discovery_row,
            state.discovery_fetched_at,
            filters,
            apps_by_company,
        )
    enriched = state.enrichment_event is not None and _enrichment_actionable(
        state.enrichment_event,
        filters,
        apps_by_company,
    )
    screening = screening_decision(
        state.human_score,
        state.ai_score,
        filters["auto_skip_llm_max"],
    )
    return _PassingStatus(
        discovered=discovered,
        enriched=enriched,
        screening=screening,
    )


def _is_pending(state: _PostingState, passing: _PassingStatus) -> bool:
    """Whether a replayed posting still awaits a next action."""
    if state.tracked:
        return False
    if state.enrichment_event is not None:
        return passing.enriched and passing.screening != ReviewDecision.SKIP
    if state.discovery_row is not None:
        return passing.discovered and passing.screening != ReviewDecision.SKIP
    return False


def _update_membership(members: set[str], posting_id: str, present: bool) -> None:
    """Add or remove one posting from a passing set."""
    if present:
        members.add(posting_id)
    else:
        members.discard(posting_id)


def _parse_time(value: str) -> datetime:
    """Parse an ISO timestamp as a naive UTC datetime for plotting.

    A naive datetime has no `tzinfo`.
    """
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).replace(tzinfo=None)


def _to_local(value: datetime) -> datetime:
    """Convert a naive UTC datetime to naive local wall time."""
    return value.replace(tzinfo=UTC).astimezone().replace(tzinfo=None)


@dataclass(frozen=True)
class _TimedEvent:
    """One event placed on the replay timeline."""

    # Tie-breaker for events recorded at the same timestamp (admitedly a very
    # rare case currently): apply config first, then stage events in pipeline
    # order.
    _ORDER: ClassVar[dict[str, int]] = {
        "config": 0,
        "discovery": 1,
        "enrichment": 2,
        "screening": 3,
        "tracking": 4,
    }

    stamp: datetime
    kind: str
    event: dict[str, Any]

    @property
    def sort_key(self) -> tuple[datetime, int]:
        """Return the order of this event in the replay timeline."""
        return self.stamp, self._ORDER[self.kind]


def _replay_events(
    discovery_events: list[dict[str, Any]],
    enrichment_events: list[dict[str, Any]],
    screening_events: list[dict[str, Any]],
    tracking_events: list[dict[str, Any]],
    config_events: list[dict[str, object]],
    provider: str,
) -> _ReplayResult:
    """Replay events with config and tracking state as of each event.

    Note that a config change can move the pending backlog and passing counts
    by itself (because filters may have changed).

    The final point uses the current provider config.
    """
    configs = _ConfigResolver(provider, config_events)
    cooldown = _CompanyApplications(
        company_by_posting=_company_by_key(discovery_events, enrichment_events)
    )

    # put all events in a sorted timeline
    timeline = [
        _TimedEvent(
            stamp=_parse_time(cast(str, event[timestamp_key])),
            kind=kind,
            event=cast(dict[str, Any], event),
        )
        for kind, timestamp_key, events in (
            ("config", "recorded_at", config_events),
            ("discovery", "fetched_at", discovery_events),
            ("enrichment", "fetched_at", enrichment_events),
            ("screening", "scored_at", screening_events),
            ("tracking", "tracked_at", tracking_events),
        )
        for event in events
    ]
    timeline.sort(key=lambda item: item.sort_key)

    states: dict[tuple[str, str], _PostingState] = {}
    pending: dict[tuple[str, str], bool] = {}
    pending_count = 0
    counts: dict[datetime, int] = {}
    discovered_passing: set[str] = set()
    enriched_passing: set[str] = set()
    screened_passing: set[str] = set()
    discovered_passing_counts: dict[datetime, int] = {}
    enriched_passing_counts: dict[datetime, int] = {}
    screened_passing_counts: dict[datetime, int] = {}
    company_by_key: dict[tuple[str, str], str] = {}
    keys_by_company: dict[str, set[tuple[str, str]]] = {}

    def set_company(key: tuple[str, str], company: str) -> None:
        """Move one posting to its current company index."""
        previous = company_by_key.get(key)
        if previous == company:
            return
        if previous is not None:
            keys_by_company[previous].discard(key)
        company_by_key[key] = company
        keys_by_company.setdefault(company, set()).add(key)

    def record(stamp: datetime) -> None:
        """Record the current pending and passing counts."""
        counts[stamp] = pending_count
        discovered_passing_counts[stamp] = len(discovered_passing)
        enriched_passing_counts[stamp] = len(enriched_passing)
        screened_passing_counts[stamp] = len(screened_passing)

    def refresh(keys: set[tuple[str, str]], filters: Config) -> None:
        """Recompute the pending and passing status of the given postings."""
        nonlocal pending_count
        for key in keys:
            state = states.get(key)
            if state is None:
                continue
            passing = _passing_status(state, filters, cooldown.apps_by_company)
            was = pending.get(key, False)
            now = _is_pending(state, passing)
            pending[key] = now
            if was != now:
                pending_count += 1 if now else -1
            posting_id = key[1]
            _update_membership(discovered_passing, posting_id, passing.discovered)
            _update_membership(enriched_passing, posting_id, passing.enriched)
            _update_membership(
                screened_passing,
                posting_id,
                passing.screening == ReviewDecision.PURSUE,
            )

    current_filters: Config | None = None
    for timed_event in timeline:
        stamp = timed_event.stamp
        kind = timed_event.kind
        event = timed_event.event
        affected: set[tuple[str, str]] = set()

        filters = configs.get(event)
        filters_changed = filters is not current_filters
        if filters_changed:
            current_filters = filters
            refresh(set(states), filters)

        if kind == "config":
            refresh(affected, filters)
            record(stamp)
            continue
        if kind == "discovery":
            if not any(row["provider"] == provider for row in event["rows"]):
                refresh(affected, filters)
                continue
            for row in event["rows"]:
                if row["provider"] != provider:
                    continue
                discovery_row = cast(JobRow, row)
                key = (provider, discovery_row["posting_id"])
                state = states.setdefault(key, _PostingState())
                state.discovery_row = discovery_row
                state.discovery_fetched_at = event["fetched_at"]
                set_company(key, discovery_row["company"])
                affected.add(key)
        elif kind == "enrichment":
            if event["provider"] != provider:
                refresh(affected, filters)
                continue
            enrichment_event = cast(EnrichmentEvent, event)
            key = (provider, enrichment_event["posting_id"])
            state = states.setdefault(key, _PostingState())
            state.enrichment_event = enrichment_event
            set_company(key, enrichment_event["company"])
            affected.add(key)
        elif kind == "screening":
            if event["provider"] != provider:
                refresh(affected, filters)
                continue
            key = (provider, event["posting_id"])
            state = states.setdefault(key, _PostingState())
            if event["kind"] == "human_review":
                state.human_score = int(event["score"])
            elif event["kind"] == "ai_review":
                state.ai_score = int(event["score"])
            affected.add(key)
        elif kind == "tracking":
            company = cooldown.company_by_posting.get(
                store_key(event["provider"], event["posting_id"])
            )
            cooldown.apply(event)
            if company:
                affected.update(keys_by_company.get(company, ()))
            if event["provider"] == provider:
                key = (provider, event["posting_id"])
                states.setdefault(key, _PostingState()).tracked = True
                affected.add(key)

        refresh(affected, filters)
        record(stamp)

    if states:
        present = datetime.now(UTC).replace(tzinfo=None)
        if counts and present < (latest := max(counts)):
            present = latest
        # The live config is parsed separately from the historical cache, so
        # always refresh instead of relying on object identity.
        latest_filters = load_provider_config(provider)
        refresh(set(states), latest_filters)
        record(present)

    return _ReplayResult(
        pending=counts,
        discovered_passing=discovered_passing_counts,
        enriched_passing=enriched_passing_counts,
        screened_passing=screened_passing_counts,
    )


def _for_provider(
    events: list[dict[str, Any]],
    provider: str,
) -> list[dict[str, Any]]:
    """Keep events belonging to one provider."""
    return [event for event in events if event.get("provider") == provider]


def _first_by_posting(
    events: list[dict[str, Any]],
    timestamp_key: str,
    *,
    status: str | None = None,
) -> dict[str, datetime]:
    """Return the earliest timestamp per posting, optionally filtered by status."""
    result: dict[str, datetime] = {}
    for event in events:
        if status is not None and event.get("status") != status:
            continue
        key = event["posting_id"]
        stamp = _parse_time(event[timestamp_key])
        previous = result.get(key)
        if previous is None or stamp < previous:
            result[key] = stamp
    return result


def _load_progress(data_home: Path, provider: str) -> _ProgressData:
    """Load the data behind one provider's progress charts."""
    discovery_events = read_events(DataKind.DISCOVERY, data_home)
    enrichment_events = read_events(DataKind.ENRICHMENT, data_home)
    screening_events = read_events(DataKind.SCREENING, data_home)
    tailoring_events = read_events(DataKind.TAILORING, data_home)
    tracking_events = read_events(DataKind.TRACKING, data_home)
    config_events = get_config_events(data_home)

    # generate mapping of posting to 'first time' for discovery events, given
    # that this event contains many postings (unlike the others)
    discovered: dict[str, datetime] = {}
    for event in discovery_events:
        stamp = _parse_time(event["fetched_at"])
        for row in event["rows"]:
            if row["provider"] != provider:
                continue
            key = row["posting_id"]
            previous = discovered.get(key)
            if previous is None or stamp < previous:
                discovered[key] = stamp

    enrichment = _for_provider(enrichment_events, provider)
    screening = _for_provider(screening_events, provider)
    tailoring = _for_provider(tailoring_events, provider)
    tracking = _for_provider(tracking_events, provider)
    enriched = _first_by_posting(enrichment, "fetched_at")
    replay = _replay_events(
        discovery_events,
        enrichment_events,
        screening_events,
        tracking_events,
        config_events,
        provider,
    )

    return _ProgressData(
        discovered=discovered,
        enriched=enriched,
        screened=_first_by_posting(
            [event for event in screening if event["kind"] == "human_review"],
            "scored_at",
        ),
        tailored=_first_by_posting(tailoring, "tailored_at"),
        tracked=_first_by_posting(tracking, "tracked_at"),
        applied=_first_by_posting(tracking, "tracked_at", status="applied"),
        discovered_passing=replay.discovered_passing,
        enriched_passing=replay.enriched_passing,
        screened_passing=replay.screened_passing,
        pending=replay.pending,
    )


def add_months(value: date, months: int) -> date:
    """Add calendar months to a date, clamping the day."""
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def bin_granularity(range_key: str) -> str:
    """Pick the bucket size that matches the selected range."""
    if range_key in {"week", "last-week", "month", "last-month"}:
        return "day"
    return "month"


def _make_bins(start: date, end: date, granularity: str) -> list[_BinRange]:
    """Build half-open bins for the selected granularity."""
    if granularity in {"day", "week"}:
        step = timedelta(days=1 if granularity == "day" else 7)
        current = start
        result = []
        while current < end:
            result.append(_BinRange(current, min(current + step, end)))
            current += step
        return result

    current = start.replace(day=1)
    result = []
    while current < end:
        next_month = add_months(current, 1)
        result.append(_BinRange(max(current, start), min(next_month, end)))
        current = next_month
    return result


def _cumulative_series(
    times: list[datetime],
    start: datetime,
    end: datetime,
) -> tuple[list[datetime], list[int]]:
    """Build a precise step series with cumulative totals in [start, end)."""
    ordered = sorted(value for value in times if value < end)
    prior = bisect_right(ordered, start)
    in_window = [value for value in ordered if start < value < end]
    counts = Counter(in_window)
    xs = [start]
    ys = [prior]
    total = prior
    for value in sorted(counts):
        total += counts[value]
        xs.append(value)
        ys.append(total)
    if xs[-1] != end:
        xs.append(end)
        ys.append(total)
    return xs, ys


def _count_at(counts: dict[datetime, int], at: datetime) -> int:
    """The last count at or before one timestamp."""
    result = 0
    for value, count in sorted(counts.items()):
        if value > at:
            break
        result = count
    return result


def _latest_count(counts: dict[datetime, int]) -> int:
    """The most recent count in a timeline."""
    return counts[max(counts)] if counts else 0


def _state_series(
    counts: dict[datetime, int],
    start: datetime,
    end: datetime,
    *,
    precise: bool,
) -> tuple[list[datetime], list[int]]:
    """Build a step series from a count timeline in [start, end).

    `precise` keeps exact timestamps; otherwise counts collapse to end-of-day.
    """
    if not precise:
        counts = _end_of_day_counts(counts)
    initial = _count_at(counts, start)
    final = _count_at(counts, end - timedelta(microseconds=1))
    xs = [start]
    ys = [initial]
    for value, count in sorted(counts.items()):
        if start < value < end:
            xs.append(value)
            ys.append(count)
    if xs[-1] != end:
        xs.append(end)
        ys.append(final)
    return xs, ys


def _end_of_day_counts(counts: dict[datetime, int]) -> dict[datetime, int]:
    """Collapse exact-time pending counts to end-of-day counts."""
    result: dict[datetime, int] = {}
    for value, count in sorted(counts.items()):
        result[datetime.combine(value.date(), time.min)] = count
    return result


def _pending_series(
    counts: dict[datetime, int],
    start: datetime,
    end: datetime,
    precise: bool,
) -> tuple[list[datetime], list[int]]:
    """Build a step series of pending counts inside the window.

    `precise` keeps exact timestamps; otherwise counts collapse to end-of-day.
    """
    if not precise:
        counts = _end_of_day_counts(counts)
        final_at = end - timedelta(days=1)
    else:
        final_at = end - timedelta(microseconds=1)
    initial = _count_at(counts, start)
    final = _count_at(counts, final_at)
    xs = [start]
    ys = [initial]
    for value, count in sorted(counts.items()):
        if start < value < end:
            xs.append(value)
            ys.append(count)
    if xs[-1] != end:
        xs.append(end)
        ys.append(final)
    return xs, ys


def _count_bins(dates: list[date], bins: list[_BinRange]) -> list[int]:
    """Count dates in each half-open bin."""
    ordered = sorted(dates)
    return [bisect_left(ordered, bin_.end) - bisect_left(ordered, bin_.start) for bin_ in bins]


def _range_window(range_key: str, today: date) -> tuple[date, date]:
    """Resolve one range key to a half-open local date window."""
    if range_key == "week":
        start = today - timedelta(days=today.weekday())
        return start, start + timedelta(days=7)
    if range_key == "last-week":
        end = today - timedelta(days=today.weekday())
        return end - timedelta(days=7), end
    if range_key == "month":
        start = today.replace(day=1)
        return start, add_months(start, 1)
    if range_key == "last-month":
        end = today.replace(day=1)
        return add_months(end, -1), end
    end = add_months(today.replace(day=1), 1)
    return add_months(end, -_RANGE_MONTHS[range_key]), end


def _build_chart_spec(
    data: _ProgressData,
    provider: str,
    range_key: str,
    *,
    today: date | None = None,
) -> ChartSpec:
    """Build the raw chart data for one provider and range in local time."""
    if range_key not in RANGE_CHOICES:
        raise ShobrError(f"unknown range {range_key!r}; choose from " + ", ".join(RANGE_CHOICES))
    series_times = {
        name: [_to_local(value) for value in data.times(name.lower())] for name in SERIES_ORDER
    }
    all_times = [value for times in series_times.values() for value in times]
    if not all_times:
        raise ShobrError(f"no events found for provider {provider!r}; run `shobr discover` first")

    current_day = today or date.today()
    start, end = _range_window(range_key, current_day)
    granularity = bin_granularity(range_key)
    bins = _make_bins(start, end, granularity)
    start_at = datetime.combine(start, time.min)
    end_at = datetime.combine(end, time.min)
    line_end = min(end_at, datetime.combine(current_day + timedelta(days=1), time.min))
    precise = range_key in {"week", "last-week"}

    series: dict[str, SeriesSpec] = {}
    passing_counts = {
        "Discovered": data.discovered_passing,
        "Enriched": data.enriched_passing,
        "Screened": data.screened_passing,
    }
    for name, times in series_times.items():
        xs, ys = _cumulative_series(times, start_at, line_end)
        series[name.lower()] = SeriesSpec(len(times), xs, ys)
        if (counts := passing_counts.get(name)) is not None:
            local_counts = {_to_local(value): count for value, count in counts.items()}
            passing_xs, passing_ys = _state_series(
                local_counts,
                start_at,
                line_end,
                precise=precise,
            )
            series[f"{name.lower()}_passing"] = SeriesSpec(
                _latest_count(local_counts),
                passing_xs,
                passing_ys,
            )

    pending = {_to_local(value): count for value, count in data.pending.items()}
    pending_xs, pending_ys = _pending_series(pending, start_at, line_end, precise)
    series["pending"] = SeriesSpec(None, pending_xs, pending_ys)

    applications = _count_bins([value.date() for value in series_times["Applied"]], bins)
    application_bins = [
        _BinCount(
            start=datetime.combine(bin_.start, time.min),
            end=datetime.combine(bin_.end, time.min),
            count=count,
        )
        for bin_, count in zip(bins, applications, strict=True)
    ]
    return ChartSpec(
        provider=provider,
        range_key=range_key,
        start=start_at,
        end=end_at,
        today=current_day,
        series=series,
        application_bins=application_bins,
    )


def progress_specs(
    ranges: list[str] | None = None,
    *,
    data_home: Path = SHOBR_DATA_DIR,
    today: date | None = None,
) -> list[ChartSpec]:
    """Build local-time progress chart data for the current provider."""
    keys = ranges or ["week"]
    if unknown := [key for key in keys if key not in RANGE_CHOICES]:
        raise ShobrError(f"unknown range {unknown[0]!r}; choose from " + ", ".join(RANGE_CHOICES))
    provider = current_provider().name
    data = _load_progress(data_home, provider)
    return [_build_chart_spec(data, provider, key, today=today) for key in keys]
