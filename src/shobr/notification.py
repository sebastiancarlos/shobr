"""LinkedIn notifications.

This is not a pipeline stage. Notifications are obtainable, but they are not
currently wired up to the rest of the system.
"""

import re
import sys
from datetime import UTC, datetime, timedelta
from typing import TypedDict, cast

from .browser import beachmsg_json
from .color import GREEN, RESET
from .core import (
    LINKEDIN_BASE_URL,
    DataKind,
    ShobrError,
    paged,
    persist_event,
    read_events,
    require_events,
    short_path,
    store_path,
)


def _absolute_timestamp(relative_time: str, fetched_at: datetime) -> str | None:
    """Convert a relative time like "2d" into an ISO timestamp anchored at fetch time.

    Unparseable input yields None.
    """
    unit_seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    match = re.fullmatch(r"(\d+)([smhdw])", relative_time)
    if not match:
        print(
            f"warning: unparseable relative time {relative_time!r}; timestamp set to null",
            file=sys.stderr,
        )
        return None
    seconds = int(match.group(1)) * unit_seconds[match.group(2)]
    return (fetched_at - timedelta(seconds=seconds)).isoformat(timespec="seconds")


class NotificationLink(TypedDict):
    """A link seen in a notification row."""

    text: str
    url: str


class NotificationRow(TypedDict):
    """Notification as parsed from LinkedIn"""

    actor_url: str
    time: str
    links: list[NotificationLink]
    text: str
    plain_text: str


class NotificationEvent(TypedDict):
    """One notifications fetch."""

    fetched_at: str
    rows: list[NotificationRow]


class StoredNotification(NotificationRow):
    """Notification as stored, with extra metadata"""

    first_seen_at: str
    timestamp: str | None


class NotificationStore(TypedDict):
    """Projected store, newest first."""

    fetched_at: str | None
    rows: list[StoredNotification]


NotificationKey = tuple[str, str, tuple[str, ...]]


def notification_key(row: NotificationRow) -> NotificationKey:
    """Identity for a notification. It considers 'plain_text' (without relative time), and urls."""
    return (
        row["plain_text"],
        row["actor_url"],
        tuple(sorted(link["url"] for link in row["links"])),
    )


# Personal blocklist: notifications unrelated to the job hunt. Regex, matched
# case-insensitively.
NOTIFICATIONS_BLOCKLIST: tuple[str, ...] = (
    r"\bcongratulate\b",
    r"\bwork anniversary\b",
    r"\bbirthday\b",
    r"\bis popular with your network\b",
    r"\bare engaging with\b",
    r"\byou may know\b",
)
_BLOCKLIST_RE = re.compile("|".join(NOTIFICATIONS_BLOCKLIST), flags=re.IGNORECASE)


def is_blocked(row: NotificationRow) -> bool:
    """True when a notification is not job-hunt related."""
    return bool(_BLOCKLIST_RE.search(row["plain_text"]))


def _project_notifications() -> NotificationStore:
    """Replay events.jsonl into the projection shape (notifications.json)

    Walks events.jsonl, keeping, per content key, the first sighting
    (fetched_at), and the most recent observed row. Blocklisted notifications
    are skipped.
    """
    if not (events := read_events(DataKind.NOTIFICATIONS)):
        return {"fetched_at": None, "rows": []}
    first_seen: dict[NotificationKey, str] = {}
    timestamps: dict[NotificationKey, str | None] = {}
    latest: dict[NotificationKey, NotificationRow] = {}

    for raw in events:
        event: NotificationEvent = raw
        for row in event["rows"]:
            if is_blocked(row):
                continue
            key = notification_key(row)
            latest[key] = row

            # on first seen key: store 'fetched_at' and computed 'timestamp'
            if key not in first_seen:
                first_seen[key] = event["fetched_at"]
                fetched_at = datetime.fromisoformat(event["fetched_at"])
                timestamps[key] = _absolute_timestamp(row["time"], fetched_at)

    rows = [
        cast(
            StoredNotification,
            {
                **latest[key],
                "first_seen_at": first_seen[key],
                "timestamp": timestamps[key],
            },
        )
        for key in latest
    ]

    # sort descending, by first_seen_at/timestamp
    rows.sort(key=lambda row: (row["first_seen_at"], row["timestamp"] or ""), reverse=True)
    return {"fetched_at": events[-1]["fetched_at"], "rows": rows}


def _print_notifications_summary(
    projection: NotificationStore, mark_new_from: str | None = None
) -> None:
    """Print the projected story; rows first seen in `mark_new_from` get `(NEW)`."""
    with paged():
        if mark_new_from:
            new_count = sum(
                1 for row in projection["rows"] if row["first_seen_at"] == mark_new_from
            )
            print(f"{len(projection['rows'])} notifications ({new_count} newly seen)")
        else:
            print(f"{len(projection['rows'])} notifications")
        print()
        for row in projection["rows"]:
            tag = (
                f" {GREEN}(NEW){RESET}"
                if mark_new_from and row["first_seen_at"] == mark_new_from
                else ""
            )
            print(f"[{row['time'] or '?'}]{tag} {row['actor_url']}")
            print(f"  {row['text'][:120]}")
        print()


def notifications_local() -> None:
    """Print the stored notifications queue summary without fetching."""
    require_events(DataKind.NOTIFICATIONS)
    projection = _project_notifications()
    _print_notifications_summary(projection)


def notifications() -> None:
    """Parse the rendered notifications page into structured rows."""
    # scrape latest notifications
    url = f"{LINKEDIN_BASE_URL}/notifications"
    raw = beachmsg_json("notifications", url)
    rows: list[NotificationRow] = raw["rows"]
    if not rows:
        raise ShobrError(
            f"parsed 0 notifications from {url}; the LinkedIn notifications "
            "page structure may have changed, refusing to write an empty queue"
        )

    # append to log, reproject, and write the store
    fetched_at = datetime.now(UTC)
    new: NotificationEvent = {
        "fetched_at": fetched_at.isoformat(timespec="seconds"),
        "rows": rows,
    }
    projection = persist_event(DataKind.NOTIFICATIONS, new, _project_notifications)
    _print_notifications_summary(projection, mark_new_from=projection["fetched_at"])
    print(f"wrote to {short_path(store_path(DataKind.NOTIFICATIONS))}")
