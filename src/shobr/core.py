"""SHOBR cross-functional core: paths, display helpers, stage gates."""

import contextlib
import importlib.resources
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from .color import BLUE, RED, RESET, YELLOW

if TYPE_CHECKING:
    from .enrichment import EnrichedRow

__version__ = "0.1.0"

ROOT = Path(__file__).resolve().parent.parent.parent

# Overridable via env for tests.
LINKEDIN_BASE_URL = os.environ.get("SHOBR_LINKEDIN_BASE_URL", "https://www.linkedin.com")

DATA_HOME = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
SHOBR_DATA_DIR = DATA_HOME / "shobr"

CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
SHOBR_CONFIG_DIR = CONFIG_HOME / "shobr"


class ShobrError(Exception):
    """An expected failure with a printable message and exit code."""

    def __init__(self, message: str, code: int = 1) -> None:
        """Store the message and exit code."""
        super().__init__(message)
        self.code = code


class LocationType(StrEnum):
    """The location types of a job."""

    REMOTE = "Remote"
    HYBRID = "Hybrid"
    ON_SITE = "On-site"


class EmploymentType(StrEnum):
    """The employment types of a job."""

    FULL_TIME = "Full-time"
    PART_TIME = "Part-time"
    CONTRACT = "Contract"
    TEMPORARY = "Temporary"
    INTERNSHIP = "Internship"


def short_path(path: Path | str) -> str:
    """Render absolute paths under $HOME with ~/ (for display only)."""
    text = str(path)
    home = str(Path.home())
    if text == home:
        return "~"
    if text.startswith(home + "/"):
        return f"~{text[len(home) :]}"
    return text


class DataKind(StrEnum):
    """Data kinds owning event logs (stages plus notifications)."""

    NOTIFICATIONS = "notifications"
    DISCOVERY = "discovery"
    ENRICHMENT = "enrichment"
    SCREENING = "screening"
    TAILORING = "tailoring"
    TRACKING = "tracking"


def data_dir(kind: DataKind) -> Path:
    """The data dir for a data kind (always the kind name)."""
    return SHOBR_DATA_DIR / kind.value


def event_log(kind: DataKind) -> Path:
    """The events.jsonl path for a data kind."""
    return data_dir(kind) / "events.jsonl"


def store_path(kind: DataKind) -> Path:
    """The projection store path for a data kind (always <kind>.json)."""
    return data_dir(kind) / f"{kind.value}.json"


def read_events(kind: DataKind) -> list[Any]:
    """Parse every event line in a data kind's log, oldest first."""
    path = event_log(kind)
    if not path.exists():
        return []
    events = [json.loads(line) for line in path.read_text().splitlines() if line]
    assert events, f"empty {path}"
    return events


def get_template(name: str) -> str:
    """Read a src/shobr/templates/ file verbatim."""
    return importlib.resources.files("shobr.templates").joinpath(name).read_text(encoding="utf-8")


def render_template(name: str, **kwargs: object) -> str:
    """Render a src/shobr/templates/ file with str.format (verbatim bytes)."""
    return get_template(name).format(**kwargs)


def require_events(kind: DataKind) -> None:
    """Raise ShobrError unless the data kind's event log exists."""
    suggested_commands = {
        DataKind.NOTIFICATIONS: "notifications",
        DataKind.DISCOVERY: "discover",
        DataKind.ENRICHMENT: "enrich-next",
        DataKind.SCREENING: "screen-next",
        DataKind.TAILORING: "tailor-next",
        DataKind.TRACKING: "track <posting_id> <status>",
    }
    if not event_log(kind).exists():
        raise ShobrError(
            f"no {kind.value} data yet; run {BLUE}shobr {suggested_commands[kind]}{RESET} first"
        )


def persist_event[T](kind: DataKind, event: object, project: Callable[[], T]) -> T:
    """Append one event to the log, reproject, and write the store."""
    events = event_log(kind)
    events.parent.mkdir(parents=True, exist_ok=True)
    with events.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")
    projected = project()
    store_path(kind).write_text(json.dumps(projected, indent=2))
    return projected


_STAGE_RANK = {
    DataKind.DISCOVERY: 0,
    DataKind.ENRICHMENT: 1,
    DataKind.SCREENING: 2,
    DataKind.TAILORING: 3,
    DataKind.TRACKING: 4,
}


def stage_line(posting_id: str, after: DataKind) -> str:
    """Produce a `- Stage: ...` line for CLI output, naming the furthest stage
    reached, and some information about it.

    Always returns a line: the current stage plain, any later stage in
    blue. The verdict detail (decision/status) is included unless it
    belongs to the caller's own stage, which already shows it.
    Furthest wins: tracking > tailoring > screening > enrichment. Reads the
    sibling projections, each guarded by file existence. Imports are
    local: stage modules import this package.
    """
    from .enrichment import project_enrichment
    from .screening import project_screening
    from .tailoring import project_tailoring
    from .tracking import project_tracking

    stage = after
    detail = ""
    if posting_id in project_enrichment()["rows"]:
        stage, detail = DataKind.ENRICHMENT, ""
    row = project_screening()["rows"].get(posting_id)
    if row is not None:
        stage = DataKind.SCREENING
        if after is not DataKind.SCREENING:
            detail = f" ({str(row['decision']).upper()})"
    if posting_id in project_tailoring()["rows"]:
        stage, detail = DataKind.TAILORING, ""
    row = project_tracking()["rows"].get(posting_id)
    if row is not None:
        stage = DataKind.TRACKING
        if after is not DataKind.TRACKING:
            detail = f" ({str(row['status']).upper()})"
    if _STAGE_RANK[stage] > _STAGE_RANK[after]:
        return f"  - Stage: {BLUE}{stage.value}{RESET}{detail}"
    return f"  - Stage: {stage.value}{detail}"


def same_company_line(company: str, posting_id: str) -> str | None:
    """Yellow 'Last Applied To Same Company' line.

    Latest non-withdrawn tracking event at `company`, excluding `posting_id`
    itself. None when the stores are missing or hold no such application.
    """
    from .tracking import TrackStatus, project_tracking

    if not event_log(DataKind.TRACKING).exists():
        return None
    if not event_log(DataKind.ENRICHMENT).exists():
        return None
    from .enrichment import project_enrichment

    companies = {pid: row["company"] for pid, row in project_enrichment()["rows"].items()}
    latest: tuple[str, str] | None = None
    for pid, tracked in project_tracking()["rows"].items():
        if pid == posting_id or tracked["status"] == TrackStatus.WITHDRAWN:
            continue
        if companies.get(pid) != company:
            continue
        if latest is None or tracked["tracked_at"] > latest[0]:
            latest = (tracked["tracked_at"], pid)
    if latest is None:
        return None
    date = datetime.fromisoformat(latest[0]).date().isoformat()
    return f"  - {YELLOW}Last Applied To Same Company:{RESET} {date} ({latest[1]})"


def rejected_tag(reason: str) -> str:
    """A red (REJECTED: ...) tag with quoted spans in blue."""
    inner = "".join(
        f"{BLUE}{chunk}{RED}" if chunk.startswith("'") else chunk
        for chunk in re.split(r"('[^']*')", reason)
    )
    return f" {RED}(REJECTED: {inner}){RESET}"


def closed_tag() -> str:
    """A red (CLOSED) marker for rows whose latest enrichment found them closed."""
    return f" {RED}(CLOSED){RESET}"


def reasons_line(reason: str, count: int) -> str:
    """A rejection breakdown line with the quoted span and count in red."""
    match = re.search(r"'[^']*'", reason)
    if match is None:
        return f"- {reason} {RED}x{count}{RESET}"
    return f"- {reason[: match.start()]}{RED}{reason[match.start() :]} x{count}{RESET}"


Gate = Literal["screenable", "pursuable", "tailorable"]


def _row_at_stage(posting_id: str, stage: Gate) -> tuple[EnrichedRow | None, str | None]:
    """Walk enrichment -> screening(pursue) -> tailoring(no package yet).

    Returns the row plus a failure reason (None reason means success).
    Never prints; callers decide what to do with the reason.
    """
    from .enrichment import project_enrichment

    try:
        require_events(DataKind.ENRICHMENT)
    except ShobrError as exc:
        return None, str(exc)
    rows = project_enrichment()["rows"]
    row = rows.get(posting_id)
    if row is None:
        return None, f"posting {posting_id} not found in enrichment store"
    if not row["actionable"]:
        reason = row["rejected_reason"] or "unknown reason"
        return None, f"posting {posting_id} did not pass the pre-filter ({reason})"
    if stage in ("pursuable", "tailorable"):
        from .screening import ReviewDecision, project_screening

        if not event_log(DataKind.SCREENING).exists():
            return None, f"posting {posting_id} is not marked pursue"
        sr = project_screening()["rows"].get(posting_id)
        if sr is None or sr["decision"] != ReviewDecision.PURSUE:
            return None, f"posting {posting_id} is not marked pursue"
    if stage == "tailorable":
        from .tailoring import project_tailoring

        if event_log(DataKind.TAILORING).exists() and posting_id in project_tailoring()["rows"]:
            return None, f"posting {posting_id} already has a package"
    return row, None


def get_enriched_row_on_stage(posting_id: str, stage: Gate) -> EnrichedRow:
    """Return the enriched row for `posting_id` at `stage`, raising when absent."""
    row, reason = _row_at_stage(posting_id, stage)
    if reason is not None:
        raise ShobrError(reason)
    assert row is not None
    return row


def is_at_stage(posting_id: str, stage: Gate) -> bool:
    """Whether `posting_id` is at `stage`."""
    row, _ = _row_at_stage(posting_id, stage)
    return row is not None


def _page(text: str) -> None:
    """Pipe text through the pager. Fall back to plain print on failure.

    Uses $PAGER verbatim, else less with sane flag defaults.
    """
    # get pager command
    value = os.environ.get("PAGER")
    if value and value.strip():
        cmd = shlex.split(value)
    else:
        cmd = ["less"]

    # default flags via LESS env only when the user set none
    env = {**os.environ}
    if os.path.basename(cmd[0]) == "less":
        env.setdefault("LESS", "--RAW-CONTROL-CHARS --quit-if-one-screen --no-init")

    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, env=env, text=True)
    except OSError:
        # fallback to stdout on pager fail
        sys.stdout.write(text)
        return

    try:
        assert proc.stdin is not None
        proc.stdin.write(text)
        proc.stdin.close()
    except BrokenPipeError:
        # prevent python from throwing EPIPE error on normal early exit (say,
        # 'q' on less)
        pass

    # wait for process to terminate, letting pager to handle Ctrl-C (usually by
    # ignoring it)
    while True:
        try:
            proc.wait()
            break
        except KeyboardInterrupt:
            continue


@contextlib.contextmanager
def paged() -> Iterator[None]:
    """Buffer a CLI output. Use a pager when on a tty taller than the screen.

    Piped and redirected output always prints directly (no pager).

    A mid-listing exception still flushes what was printed before it
    propagates.
    """
    # do nothing on no tty
    if not sys.stdout.isatty():
        yield
        return

    # else, redirect stdout to buffer
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            yield
    finally:
        text = buf.getvalue()
        rows = shutil.get_terminal_size(fallback=(80, 24)).lines
        if text.count("\n") < rows - 1:
            # if content fits, just print back to stdout
            sys.stdout.write(text)
        else:
            # page it
            _page(text)
