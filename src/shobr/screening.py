"""The screening stage: Both human and AI reviews of enriched leads."""

import os
import subprocess
import sys
import tempfile
import textwrap
from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Literal, TypedDict, cast

from .ai import chat_completion, parse_json_object
from .color import BLUE, BOLD, GREEN, RED, RESET, YELLOW
from .config import CONFIG_PATH, load_config, scaffold_config
from .core import (
    SHOBR_CONFIG_DIR,
    DataKind,
    ShobrError,
    event_log,
    get_enriched_row_on_stage,
    paged,
    persist_event,
    read_events,
    render_template,
    require_events,
    same_company_line,
    short_path,
    stage_line,
    store_path,
)
from .enrichment import (
    EnrichedRow,
    get_enriched_rows,
    is_stale,
    print_enriched_row,
    project_enrichment,
    refuse_if_stale,
    staleness_tag,
    validity_tag,
)

_PROFILE_DIR = SHOBR_CONFIG_DIR / "profile"


def _resolve_cv_path() -> Path:
    """Master resume path: $SHOBR_MAIN_CV_PATH wins, else <cv_toolchain_dir>/resume.md."""
    if env_path := os.environ.get("SHOBR_MAIN_CV_PATH"):
        return Path(env_path).expanduser()
    return Path(load_config()["cv_toolchain_dir"]).expanduser() / "resume.md"


_PROFILE_TEMPLATE_SENTINEL = "SHOBR PROFILE TEMPLATE"

_PROFILE_TEMPLATES: dict[str, str] = {
    "user-detail.md": render_template(
        "profile-user-detail.md", sentinel=_PROFILE_TEMPLATE_SENTINEL
    ),
    "fit-criteria.md": render_template(
        "profile-fit-criteria.md", sentinel=_PROFILE_TEMPLATE_SENTINEL
    ),
    "deal-breakers.md": render_template(
        "profile-deal-breakers.md", sentinel=_PROFILE_TEMPLATE_SENTINEL
    ),
    "resume-guide.md": render_template(
        "profile-resume-guide.md", sentinel=_PROFILE_TEMPLATE_SENTINEL
    ),
    "cover-guide.md": render_template(
        "profile-cover-guide.md", sentinel=_PROFILE_TEMPLATE_SENTINEL
    ),
}


def init_profile() -> None:
    """Scaffold instructional profile .md templates, keeping existing files."""
    _PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    width = max(
        len(short_path(path))
        for path in [CONFIG_PATH, *(_PROFILE_DIR / f for f in _PROFILE_TEMPLATES)]
    )
    for filename, content in _PROFILE_TEMPLATES.items():
        path = _PROFILE_DIR / filename
        if path.exists():
            print(f"skip {short_path(path).ljust(width)} {GREEN}(already exists){RESET}")
            continue
        path.write_text(content, encoding="utf-8")
        print(f"{BOLD}wrote{RESET} {short_path(path)}")
    if scaffold_config():
        print(f"{BOLD}wrote{RESET} {short_path(CONFIG_PATH)}")
    else:
        print(f"skip {short_path(CONFIG_PATH).ljust(width)} {GREEN}(already exists){RESET}")


PROFILE_FILES: tuple[str, str, str] = (
    "user-detail.md",
    "fit-criteria.md",
    "deal-breakers.md",
)

TAILOR_FILES: tuple[str, str] = (
    "resume-guide.md",
    "cover-guide.md",
)


def print_profile() -> None:
    """Print the profile input paths shobr reads, flagging missing files."""
    entries = (
        [("CV", _resolve_cv_path())]
        + [(filename, _PROFILE_DIR / filename) for filename in PROFILE_FILES]
        + [(filename, _PROFILE_DIR / filename) for filename in TAILOR_FILES]
        + [("config.toml", CONFIG_PATH)]
    )
    width = max(len(label) + 1 for label, _ in entries) + 1
    for label, path in entries:
        key = f"{label}:".ljust(width)
        shown, missing = short_path(path), ""
        if not path.is_file():
            missing = f" {RED}(missing){RESET}"
        print(f"{BOLD}{key}{RESET} {shown}{missing}")
    if any(not path.is_file() for label, path in entries if label != "CV"):
        print()
        print(f"Some profile files are missing. Run {BLUE}shobr setup{RESET}")


def load_files(files: tuple[str, ...]) -> dict[str, str]:
    """Read the CV + the given profile .md files (errors raise)."""
    cv = _resolve_cv_path()
    if not cv.is_file():
        raise ShobrError(f"profile file missing: {short_path(cv)}")
    texts: dict[str, str] = {"resume": cv.read_text(encoding="utf-8")}
    for filename in files:
        key = filename.removesuffix(".md").replace("-", "_")
        path = _PROFILE_DIR / filename
        if not path.is_file():
            raise ShobrError(
                f"profile file missing: {short_path(path)} "
                f"(run {BLUE}shobr setup{RESET} to scaffold it)"
            )
        content = path.read_text(encoding="utf-8")
        if _PROFILE_TEMPLATE_SENTINEL in content:
            raise ShobrError(
                f"Profile file {BLUE}{short_path(path)}{RESET} "
                f"still {RED}has template content{RESET}\n"
                "- Fill it in and remove the marker line"
            )
        texts[key] = content
    return texts


class ScreeningProfile(TypedDict):
    """The four profile texts interpolated into the LLM scoring prompt."""

    resume: str
    user_detail: str
    fit_criteria: str
    deal_breakers: str


def _load_profile() -> ScreeningProfile:
    """Read the CV + scoring profile .md files."""
    return cast(ScreeningProfile, load_files(PROFILE_FILES))


def pill_line(row: EnrichedRow) -> str:
    """Structured facts line. Segments omitted when missing."""
    return " | ".join(
        value
        for value in (
            row["location"],
            row["location_type"],
            row["employment_type"],
            row["salary_range"],
        )
        if value
    )


def job_posting_block(row: EnrichedRow) -> str:
    """The shared JOB POSTING document block."""
    return render_template(
        "block-job-posting.md",
        title=row["title"],
        company=row["company"],
        pills=pill_line(row),
        job_description=row["job_description"],
        company_description=row["company_description"],
    )


def profile_blocks(profile: ScreeningProfile) -> str:
    """The three user-authored profile document blocks."""
    return render_template(
        "block-profiles.md",
        user_detail=profile["user_detail"],
        fit_criteria=profile["fit_criteria"],
        deal_breakers=profile["deal_breakers"],
    )


def _screen_prompt(row: EnrichedRow, profile: ScreeningProfile) -> str:
    """Build the LLM scoring prompt for one enriched row."""
    return render_template(
        "prompt-screen.md",
        resume=profile["resume"],
        profile_blocks=profile_blocks(profile),
        job_posting_block=job_posting_block(row),
    )


class _ReviewKind(StrEnum):
    """Screening step kind."""

    HUMAN = "human_review"
    AI = "ai_review"


class Score(IntEnum):
    """The 1 to 5 score for screening."""

    ONE = 1  # skip
    TWO = 2  # barely
    THREE = 3  # arguable
    FOUR = 4  # good
    FIVE = 5  # excellent


# A human score at or above this means "pursue"
_HUMAN_PURSUE_MIN = Score.TWO

# Note: This is DEAD CODE for now, but might be wired-in later on.
_AUTO_PURSUE_AI_MIN: int | None = None


class _ScreeningReview(TypedDict):
    """A screening review."""

    scored_at: str
    posting_id: str
    kind: _ReviewKind
    score: Score
    reasoning: str


class _HumanReview(_ScreeningReview):
    """A human screening review."""

    kind: Literal[_ReviewKind.HUMAN]


class _AIReview(_ScreeningReview):
    """An AI screening review."""

    kind: Literal[_ReviewKind.AI]


class ReviewDecision(StrEnum):
    """Screening verdict for a posting.

    PENDING until a human review exists, else PURSUE/SKIP.
    """

    PENDING = "pending"
    PURSUE = "pursue"
    SKIP = "skip"


DECISION_COLORS = {"pursue": GREEN, "skip": RED, "pending": YELLOW}

SCORE_COLORS = {1: RED, 2: YELLOW, 3: YELLOW, 4: GREEN, 5: GREEN}


class ScreeningRow(TypedDict):
    """A job's screening state as projected into screening.json"""

    posting_id: str
    human: _HumanReview | None
    ai: _AIReview | None
    decision: ReviewDecision


class _ScreenedStore(TypedDict):
    """Projected reviews keyed by posting id."""

    screened_at: str | None
    rows: dict[str, ScreeningRow]  # keyed by posting_id


def _screening_decision(human: _HumanReview | None, ai: _AIReview | None) -> ReviewDecision:
    """Derive a posting's screening decision from its reviews."""
    if human is None:
        if (
            _AUTO_PURSUE_AI_MIN is not None
            and ai is not None
            and ai["score"] >= _AUTO_PURSUE_AI_MIN
        ):
            return ReviewDecision.PURSUE
        return ReviewDecision.PENDING
    if human["score"] >= _HUMAN_PURSUE_MIN:
        return ReviewDecision.PURSUE
    return ReviewDecision.SKIP


def project_screening() -> _ScreenedStore:
    """Replay the screening events log into the projection shape (screening.json).

    Keyed by posting_id. Latest-wins per review kind per posting: a later human
    (or AI) review replaces the earlier one of the same kind.
    """
    if not (events := read_events(DataKind.SCREENING)):
        return {"screened_at": None, "rows": {}}
    human: dict[str, _HumanReview] = {}
    ai: dict[str, _AIReview] = {}

    for event in events:
        if event["kind"] == _ReviewKind.HUMAN:
            human[event["posting_id"]] = event
        elif event["kind"] == _ReviewKind.AI:
            ai[event["posting_id"]] = event

    rows: dict[str, ScreeningRow] = {}

    def _row_order(posting_id: str) -> tuple[str, str]:
        reviews = [r for r in (human.get(posting_id), ai.get(posting_id)) if r is not None]
        return (max(review["scored_at"] for review in reviews), posting_id)

    for posting_id in sorted(human.keys() | ai.keys(), key=_row_order, reverse=True):
        h = human.get(posting_id)
        a = ai.get(posting_id)
        rows[posting_id] = ScreeningRow(
            posting_id=posting_id,
            human=h,
            ai=a,
            decision=_screening_decision(h, a),
        )
    return {"screened_at": events[-1]["scored_at"], "rows": rows}


def review_score(review: _AIReview | _HumanReview | None, missing: str) -> str:
    """A review's score in its color, or the missing placeholder plain."""
    if review is None:
        return missing
    return f"{SCORE_COLORS[int(review['score'])]}{review['score']}{RESET}"


def print_screening_status(sr: ScreeningRow | None, indent: str) -> None:
    """Print the LLM/Human/Decision lines (plus wrapped reasonings) at an indent."""
    ai = review_score(sr["ai"] if sr else None, "N/A")
    human = review_score(sr["human"] if sr else None, "PENDING")
    decision = sr["decision"] if sr else ReviewDecision.PENDING
    color = DECISION_COLORS[str(decision).lower()]
    print(f"{indent}- LLM: {ai}")
    print(f"{indent}- Human: {human}")
    print(f"{indent}- Decision: {color}{str(decision).upper()}{RESET}")
    if sr and sr["ai"]:
        print(labeled_block(f"{indent}- LLM reasoning:", sr["ai"]["reasoning"]))
    if sr and sr["human"]:
        print(labeled_block(f"{indent}- Human reasoning:", sr["human"]["reasoning"]))


def _parse_score_filter(raw: str) -> frozenset[int]:
    """Parse a --score filter (N, N-M, or N,M,K over 1-5) to accepted scores.

    Raises on anything malformed.
    """
    scores: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if "-" in part:
            bounds = part.split("-")
            if len(bounds) != 2:
                break
            try:
                low, high = int(bounds[0]), int(bounds[1])
            except ValueError:
                break
            if not 1 <= low <= high <= 5:
                break
            scores.update(range(low, high + 1))
        else:
            try:
                score = int(part)
            except ValueError:
                break
            if score not in (1, 2, 3, 4, 5):
                break
            scores.add(score)
    else:
        if scores:
            return frozenset(scores)
    raise ShobrError(f"invalid --score {raw!r}; use N, N-M, N,M,K (1-5), or 'none'")


def _effective_score(row: ScreeningRow) -> int | None:
    """The human score if present, else the AI score, else None."""
    if row["human"] is not None:
        return int(row["human"]["score"])
    if row["ai"] is not None:
        return int(row["ai"]["score"])
    return None


def print_screened(score_filter: str | None = None) -> None:
    """Print the screening store summary, optionally filtered by score."""
    with paged():
        require_events(DataKind.SCREENING)
        screened = project_screening()

        wanted = (
            _parse_score_filter(score_filter)
            if score_filter is not None and score_filter != "none"
            else frozenset()
        )

        def keep(row: ScreeningRow) -> bool:
            if score_filter is None:
                return True
            if score_filter == "none":
                return row["human"] is None
            return _effective_score(row) in wanted

        rows = [row for row in screened["rows"].values() if keep(row)]
        assert event_log(DataKind.ENRICHMENT).exists(), "screening data implies enrichment data"
        passing = {pid for pid, row in project_enrichment()["rows"].items() if row["actionable"]}
        pursue = sum(1 for row in rows if row["decision"] == ReviewDecision.PURSUE)
        skip = sum(1 for row in rows if row["decision"] == ReviewDecision.SKIP)
        pending = sum(
            1
            for row in rows
            if row["decision"] == ReviewDecision.PENDING and row["posting_id"] in passing
        )
        lacking = sum(
            1
            for row in rows
            if row["human"] is None and row["ai"] is None and row["posting_id"] in passing
        )
        print(
            f"{BLUE}{len(rows)}{RESET} screened ({GREEN}{pursue}{RESET} pursue, "
            f"{RED}{skip}{RESET} skip, {YELLOW}{lacking}{RESET} lacking LLM review, "
            f"{YELLOW}{pending}{RESET} pending human review)"
        )
        print()
        enriched = project_enrichment()
        for row in rows:
            eref = enriched["rows"][row["posting_id"]]
            print(f"[{eref['company']}] {row['posting_id']}{validity_tag(eref)}")
            pills = pill_line(eref)
            print(f"  - {eref['title']}")
            if pills:
                print(f"  - {pills}")
            print_screening_status(row, "  ")
            print(stage_line(row["posting_id"], DataKind.SCREENING))
            checked = datetime.fromisoformat(eref["enriched_last_at"]).date().isoformat()
            threshold = load_config()["stale_after_days"]
            print(f"  - Last Enriched At: {checked}{staleness_tag(eref, threshold)}")
            scored = [
                review["scored_at"] for review in (row["human"], row["ai"]) if review is not None
            ]
            reviewed = datetime.fromisoformat(max(scored)).date().isoformat()
            print(f"  - Reviewed At: {reviewed}")
            if line := same_company_line(eref["company"], row["posting_id"]):
                print(line)

        print()


def _record_review(row: EnrichedRow, score: int, reasoning: str, kind: _ReviewKind) -> None:
    """Validate, persist, and report a screening of any kind."""
    if not 1 <= score <= 5:
        raise ShobrError("score must be between 1 and 5")
    score = Score(score)
    event = {
        "scored_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "posting_id": row["posting_id"],
        "kind": kind,
        "score": score,
        "reasoning": reasoning,
    }
    screened = persist_event(DataKind.SCREENING, event, project_screening)
    sr = screened["rows"][row["posting_id"]]
    label = "human" if kind is _ReviewKind.HUMAN else "AI"
    print(f"Recorded {label} review:")
    print(f"  - {BOLD}Score:{RESET} {SCORE_COLORS[int(score)]}{score}{RESET}")
    decision = sr["decision"].upper()
    print(
        f"  - {BOLD}Decision:{RESET} "
        f"{DECISION_COLORS[str(sr['decision']).lower()]}{decision}{RESET}"
    )
    print(f"  - {BOLD}Reasoning:{RESET} {reasoning}")
    print()
    print(f"wrote to {short_path(store_path(DataKind.SCREENING))}")


def _parse_ai_reply(text: str) -> tuple[Score, str] | None:
    """Parse an AI score/reasoning pair out of a completion, tolerating fences.

    Returns None when the reply holds no valid score (int 1-5, no bools) with
    non-empty reasoning.
    """

    # parse JSON response
    data = parse_json_object(text)
    if data is None:
        return None

    # extract data
    score = data.get("score")
    reasoning = data.get("reasoning")
    if isinstance(score, bool) or not isinstance(score, int):
        return None
    if not 1 <= score <= 5:
        return None
    if not isinstance(reasoning, str) or not reasoning.strip():
        return None
    return Score(score), reasoning.strip()


def labeled_block(label: str, text: str, width: int = 80) -> str:
    """A label line plus body wrapped to width, indented one level deeper.

    The body indent derives from the label's own indent, so call sites
    never restate it.
    """
    body_indent = label[: len(label) - len(label.lstrip())] + "    "
    blocks = []
    for paragraph in text.splitlines():
        if not paragraph.strip():
            blocks.append("")
            continue
        blocks.append(
            textwrap.fill(
                paragraph, width=width, initial_indent=body_indent, subsequent_indent=body_indent
            )
        )
    return label + "\n" + "\n".join(blocks)


def _screening_template(row: EnrichedRow, ai: _AIReview | None = None) -> str:
    """The $EDITOR screeing template."""
    pills = pill_line(row)
    apply_lines = f"- Apply ({row['apply_method']}):"
    if row["apply_url"]:
        apply_lines += f"\n  - <{row['apply_url']}>"
    ai_lines = ""
    if ai is not None:
        ai_lines = f"""
## AI Review

- Score: {ai["score"]}
{labeled_block("- Reasoning:", ai["reasoning"])}
"""
    checked = datetime.fromisoformat(row["enriched_last_at"]).date().isoformat()
    threshold = load_config()["stale_after_days"]
    return render_template(
        "editor-screening.md",
        company=row["company"],
        posting_id=row["posting_id"],
        title=row["title"],
        pills=pills,
        checked=checked,
        stale=staleness_tag(row, threshold),
        apply_lines=apply_lines,
        ai_lines=ai_lines,
        job_description=row["job_description"],
        company_description=row["company_description"],
    )


def _parse_screening_template(text: str) -> tuple[Score, str] | None:
    """Parse score/reasoning out of an edited screening template.

    Scans for the `SHOBR_SCORE:` and `SHOBR_REASONING:` tokens and takes their
    inline values.

    Returns None when the save is malformed.
    """
    score: Score | None = None
    reasoning = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("SHOBR_SCORE:"):
            value = stripped[len("SHOBR_SCORE:") :].strip()
            if value.isdigit() and 1 <= int(value) <= 5:
                score = Score(int(value))
            else:
                return None
        elif stripped.startswith("SHOBR_REASONING:"):
            reasoning = stripped[len("SHOBR_REASONING:") :].strip()
    if score is None or not reasoning:
        return None
    return score, reasoning


def _screen_row_via_editor(row: EnrichedRow, ai: _AIReview | None) -> None:
    """Open the enriched row in $EDITOR and record the review from the save."""
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor:
        raise ShobrError("no editor set; set $VISUAL or $EDITOR (or pass a score)")

    fd, path = tempfile.mkstemp(prefix="shobr-screen-", suffix=".md")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(_screening_template(row, ai))
        subprocess.run([editor, path], check=False)
        content = Path(path).read_text(encoding="utf-8")
    finally:
        Path(path).unlink(missing_ok=True)

    parsed = _parse_screening_template(content)
    if parsed is None:
        raise ShobrError(
            "could not parse a valid score (1-5) and reasoning from the edited file\n"
            f"- {RED}No verdict recorded{RESET}"
        )
    score, reasoning = parsed

    _record_review(row, score, reasoning, _ReviewKind.HUMAN)


def _print_screening_line(row: EnrichedRow, screened: _ScreenedStore) -> None:
    """The per-posting line shown before screening a row."""
    sr = screened["rows"].get(row["posting_id"])
    print("  - Screening:")
    print_screening_status(sr, "    ")


def _screen_row(
    row: EnrichedRow,
    screened: _ScreenedStore,
    score: int | None,
    reason: str | None,
    force: bool = False,
) -> None:
    """Screen one enriched row via argv or $EDITOR.

    An argv verdict must pass both score and reason together. Without an argv
    verdict, $EDITOR must be set or the command errors.
    """
    if (score is None) != (reason is None):
        raise ShobrError("give either both score and reason, or neither")
    print_enriched_row(row, short=True)
    _print_screening_line(row, screened)
    refuse_if_stale(row, load_config()["stale_after_days"], force)
    if score is not None and reason is not None:
        _record_review(row, score, reason, _ReviewKind.HUMAN)
        return
    sr = screened["rows"].get(row["posting_id"])
    _screen_row_via_editor(row, sr["ai"] if sr else None)


def oldest_missing_review(
    rows: dict[str, EnrichedRow],
    screened: dict[str, ScreeningRow],
    kind: Literal["human", "ai"],
    exclude: frozenset[str] = frozenset(),
) -> EnrichedRow | None:
    """Get oldest enriched row, which passes, with no review of `kind` yet.

    AI scoring is refused once a human reviewed, so it's never returned.
    """
    for row in rows.values():
        if not row["actionable"]:
            continue
        if row["posting_id"] in exclude:
            continue
        sr = screened.get(row["posting_id"])
        if sr is not None:
            if sr[kind] is not None:
                continue
            if kind == "ai" and sr["human"] is not None:
                continue
        return row
    return None


def _has_human_review(posting_id: str) -> bool:
    """Whether the posting already carries a human review."""
    sr = project_screening()["rows"].get(posting_id)
    return sr is not None and sr["human"] is not None


def _run_llm_for_row(row: EnrichedRow, print_prompt: bool, force: bool = False) -> None:
    """Print the LLM scoring prompt for one row, or score it live."""
    profile = _load_profile()
    prompt = _screen_prompt(row, profile)
    if print_prompt:
        print(prompt)
        return
    refuse_if_stale(row, load_config()["stale_after_days"], force)
    print(
        f"Sent AI review request for lead {BOLD}[{row['company']}] "
        f"{row['posting_id']} ({row['title']}){RESET}."
    )
    print("Waiting for response...")
    parsed = _parse_ai_reply(chat_completion(prompt))
    if parsed is None:
        raise ShobrError(
            "could not parse a valid score (1-5) and reasoning from the LLM reply;"
            " no review recorded"
        )
    score, reasoning = parsed
    _record_review(row, score, reasoning, _ReviewKind.AI)


def screen_next(score: int | None, reason: str | None, force: bool = False) -> None:
    """Record a human screening for the oldest enriched passing lead with no
    human review yet."""
    rows = get_enriched_rows()
    screened = project_screening()
    row = oldest_missing_review(rows, screened["rows"], "human")
    if row is None:
        print("no leads to screen")
        return
    _screen_row(row, screened, score, reason, force)


def screen_posting_id(
    posting_id: str, score: int | None, reason: str | None, force: bool = False
) -> None:
    """Record a human screening for a specific enriched posting."""
    row = get_enriched_row_on_stage(posting_id, "screenable")
    screened = project_screening()
    _screen_row(row, screened, score, reason, force)


def screen_llm_next(print_prompt: bool, force: bool = False) -> None:
    """Record an LLM screening for the oldest enriched passing lead with no
    human review yet."""
    rows = get_enriched_rows()
    screened = project_screening()
    row = oldest_missing_review(rows, screened["rows"], "ai")
    if row is None:
        print("no leads to screen")
        return
    if print_prompt:
        _run_llm_for_row(row, print_prompt)
        return
    if not _score_row_with_retries(row, force):
        raise ShobrError(f"could not score posting {row['posting_id']}")


_SCREEN_LLM_MAX_RETRIES = 2


def _score_row_with_retries(row: EnrichedRow, force: bool = False) -> bool:
    """Score one row through the LLM, retrying failures."""
    for _ in range(_SCREEN_LLM_MAX_RETRIES + 1):
        try:
            _run_llm_for_row(row, False, force)
        except ShobrError as exc:
            print(exc, file=sys.stderr)
            continue
        return True
    return False


def screen_llm_all(force: bool = False) -> None:
    """Record LLM screenings for every enriched passing lead with no AI or
    human review yet, retrying failures. Skips stale rows unless forced."""
    rows = get_enriched_rows()
    _load_profile()  # fail fast on invalid profile
    threshold = load_config()["stale_after_days"]
    screened = project_screening()["rows"]
    pending = [
        row
        for row in rows.values()
        if row["actionable"]
        and (
            (sr := screened.get(row["posting_id"])) is None
            or (sr["ai"] is None and sr["human"] is None)
        )
    ]
    skipped = sorted(row["posting_id"] for row in pending if not force and is_stale(row, threshold))
    if skipped:
        print(
            f"{len(skipped)} of {len(pending)} rows to score were last checked over"
            f" {threshold} days ago; skipping stale rows"
            + ("" if force else " (pass --force to score them anyway)")
        )
    rows = {
        pid: row
        for pid, row in sorted(rows.items(), key=lambda kv: is_stale(kv[1], threshold))
        if force or pid not in skipped
    }
    scored = 0
    failed: list[str] = []
    while True:
        screened = project_screening()["rows"]
        row = oldest_missing_review(rows, screened, "ai", exclude=frozenset(failed))
        if row is None:
            break
        if _score_row_with_retries(row, force):
            scored += 1
        else:
            failed.append(row["posting_id"])
    print(f"Scored {scored} leads.")
    if failed:
        raise ShobrError(f"Failed to score {len(failed)}: {', '.join(failed)}")
    if scored == 0 and skipped:
        raise ShobrError(f"All {len(skipped)} pending rows stale; re-enrich first or pass --force")


def screen_llm_posting_id(posting_id: str, print_prompt: bool, force: bool = False) -> None:
    """Record an LLM screening for a specific enriched posting."""
    row = get_enriched_row_on_stage(posting_id, "screenable")
    if _has_human_review(posting_id):
        raise ShobrError(f"posting {posting_id} already has a human review")
    _run_llm_for_row(row, print_prompt, force)
