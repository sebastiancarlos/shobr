"""The tailoring stage: build per-job application packages via a CV toolchain."""

import re
import sys
import textwrap
from datetime import UTC, datetime
from typing import NoReturn, TypedDict, cast

from .ai import chat_completion, parse_json_object
from .color import BLUE, BOLD, GREEN, RESET, YELLOW
from .config import load_config, resolve_cv_toolchain_dir
from .core import (
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
from .cv_toolchain import (
    CvToolchain,
    RoffumeToolchain,
    ensure_cv_toolchain_dir,
    ensure_worktree_clean,
    git_commit,
)
from .enrichment import (
    EnrichedRow,
    get_enriched_rows,
    is_stale,
    project_enrichment,
    refuse_if_stale,
    staleness_tag,
    validity_tag,
)
from .screening import (
    PROFILE_FILES,
    TAILOR_FILES,
    ReviewDecision,
    ScreeningProfile,
    ScreeningRow,
    job_posting_block,
    load_files,
    profile_blocks,
    project_screening,
)

TAILOR_MAX_REWRITES = 4
TAILOR_MAX_TOKENS = 4096


class TailoringRow(TypedDict):
    """A posting's tailoring state as projected into tailoring.json."""

    posting_id: str
    slug: str
    app_dir: str
    rewrites: int
    resume_md: str
    cover_md: str
    tailored_at: str


class TailorEvent(TailoringRow):
    """A completed tailor package build."""


class TailoredStore(TypedDict):
    """Projected packages keyed by posting id."""

    tailored_at: str | None
    rows: dict[str, TailoringRow]


class TailoringProfile(ScreeningProfile):
    """The full profile, plus the two tailor-only guides."""

    resume_guide: str
    cover_guide: str


def load_tailor_profile() -> TailoringProfile:
    """Read the CV + full profile .md files, including the tailor guides."""
    return cast(TailoringProfile, load_files(PROFILE_FILES + TAILOR_FILES))


def project_tailoring() -> TailoredStore:
    """Replay the tailoring events log."""
    if not (events := read_events(DataKind.TAILORING)):
        return {"tailored_at": None, "rows": {}}
    rows: dict[str, TailoringRow] = {}
    for event in events:
        pid = event["posting_id"]
        rows[pid] = TailoringRow(
            posting_id=pid,
            slug=event["slug"],
            app_dir=event["app_dir"],
            rewrites=event["rewrites"],
            resume_md=event["resume_md"],
            cover_md=event["cover_md"],
            tailored_at=event["tailored_at"],
        )
    return {"tailored_at": events[-1]["tailored_at"], "rows": rows}


def print_tailored() -> None:
    """Print every tailored package stored so far."""
    with paged():
        require_events(DataKind.TAILORING)
        tailored = project_tailoring()

        print(f"{BLUE}{len(tailored['rows'])}{RESET} tailored")
        print()
        assert event_log(DataKind.ENRICHMENT).exists(), "tailoring data implies enrichment data"
        enriched = project_enrichment()
        for row in tailored["rows"].values():
            eref = enriched["rows"][row["posting_id"]]
            print(f"{BOLD}[{eref['company']}] {row['posting_id']}{RESET}{validity_tag(eref)}")
            print(f"  - {eref['title']}")
            print(f"  - Slug: {row['slug']}")
            print(f"  - {BOLD}Application dir:{RESET} {short_path(row['app_dir'])}")
            print(
                f"  - Rewrites: {GREEN if row['rewrites'] == 0 else YELLOW}{row['rewrites']}{RESET}"
            )
            print(stage_line(row["posting_id"], DataKind.TAILORING))
            checked = datetime.fromisoformat(eref["enriched_last_at"]).date().isoformat()
            threshold = load_config()["stale_after_days"]
            print(f"  - Last Enriched At: {checked}{staleness_tag(eref, threshold)}")
            built = datetime.fromisoformat(row["tailored_at"]).date().isoformat()
            print(f"  - Tailored At: {built}")
            if line := same_company_line(eref["company"], row["posting_id"]):
                print(line)

        print()


def _tailor_edits_prompt(row: EnrichedRow, profile: TailoringProfile) -> str:
    """Build the resume-rewrite prompt (README draft)."""
    return render_template(
        "prompt-tailor-edits.md",
        profile_blocks=profile_blocks(profile),
        resume_guide=profile["resume_guide"],
        job_posting_block=job_posting_block(row),
        resume=profile["resume"],
    )


def _tailor_cover_prompt(row: EnrichedRow, profile: TailoringProfile, resume_md: str) -> str:
    """Build the cover-letter prompt (README draft)."""
    return render_template(
        "prompt-tailor-cover.md",
        profile_blocks=profile_blocks(profile),
        cover_guide=profile["cover_guide"],
        job_posting_block=job_posting_block(row),
        resume_md=resume_md,
    )


def _tailor_rewrite_prompt(overflowing_md: str, verify_output: str) -> str:
    """Build the single-page-shortening prompt (README draft)."""
    return render_template(
        "prompt-tailor-rewrite.md",
        overflowing_md=overflowing_md,
        verify_output=verify_output,
    )


def application_slug(row: EnrichedRow) -> str:
    """Deterministic application dir slug: slugged company + posting id."""
    company = "-".join(
        part
        for part in "".join(c if c.isalnum() else " " for c in row["company"].lower()).split()
        if part
    )
    return f"{company}-{row['posting_id']}"


def _parse_md_reply(text: str, key: str) -> str | None:
    """Parse a markdown document out of a tailor reply; None when missing, blank, or non-string."""
    data = parse_json_object(text)
    if data is None:
        return None
    value = data.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _record_package(
    row: EnrichedRow, slug: str, app_dir: str, rewrites: int, resume_md: str, cover_md: str
) -> None:
    """Persist and report a completed tailor package build (live path helper)."""
    event: TailorEvent = {
        "tailored_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "posting_id": row["posting_id"],
        "slug": slug,
        "app_dir": app_dir,
        "rewrites": rewrites,
        "resume_md": resume_md,
        "cover_md": cover_md,
    }
    persist_event(DataKind.TAILORING, event, project_tailoring)
    print("Built package:")
    print(f"  - {row['company']} ({row['posting_id']})")
    print(f"  - Slug: {slug}")
    print(f"  - Rewrites: {rewrites}")
    print()
    print(f"wrote to {short_path(store_path(DataKind.TAILORING))}")


def _wrap_markdown(text: str, width: int = 80) -> str:
    """Fold long prose lines to width; leave structure alone.

    Lines without spaces (bare URLs, long tokens) are left alone: there is
    nothing safe to split on. List markers get a hanging indent. Headings
    and the YAML front matter block are never folded: refilling them would
    break the document structure (or its YAML parse).
    """
    out = []
    in_front_matter = False
    for index, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if stripped == "---" and (index == 0 or in_front_matter):
            in_front_matter = not in_front_matter
            out.append(line)
            continue
        if in_front_matter or stripped.startswith("#"):
            out.append(line)
            continue
        if len(line) <= width or " " not in stripped:
            out.append(line)
            continue
        marker = re.match(r"^(\s*)(- |\d+\. )?", line)
        assert marker is not None
        indent, bullet = marker.group(1), marker.group(2) or ""
        subsequent = indent + ("  " if bullet else "")
        out.append(
            textwrap.fill(
                stripped[len(bullet) :],
                width=width,
                initial_indent=indent + bullet,
                subsequent_indent=subsequent,
                break_on_hyphens=False,
            )
        )
    return "\n".join(out) + "\n"


def _notes_markdown(row: EnrichedRow, screening: ScreeningRow | None) -> str:
    """Render the job description to be added to notes.md."""
    checked = datetime.fromisoformat(row["enriched_last_at"]).date().isoformat()
    built = datetime.now(UTC).date().isoformat()
    lines = [
        "# SHOBR Job Description",
        "",
        "The shobr state for the job when this package was tailored "
        f"({built}). `shobr review {row['posting_id']}` shows the live state.",
        "",
        f"- Posting: {row['posting_url']}",
        f"- Company: {row['company']}",
        f"- Location: {row['location']}",
    ]
    for label, value in (
        ("Location type", row["location_type"]),
        ("Employment type", row["employment_type"]),
        ("Salary range", row["salary_range"]),
    ):
        if value:
            lines.append(f"- {label}: {value}")
    lines.append(f"- Apply: {row['apply_method']}")
    if row["apply_url"]:
        lines.append(f"- Apply URL: {row['apply_url']}")
    lines += [
        f"- Last Enriched At: {checked}",
        f"- Tailored At: {built}",
        "",
        "## Screening",
        "",
    ]
    if screening is None:
        lines.append("- Decision: N/A")
    else:
        lines.append(f"- Decision: {str(screening['decision']).upper()}")
        for label, review in (("AI", screening["ai"]), ("Human", screening["human"])):
            if review is None:
                lines.append(f"- {label}: none")
            else:
                lines.append(f"- {label}: {review['score']}")
                lines.append(f"- {label} reasoning: {review['reasoning']}")
    lines += [
        "",
        "## Job description",
        "",
        row["job_description"],
        "",
        "## Company description",
        "",
        row["company_description"] or "none captured",
    ]
    return "\n".join(lines) + "\n"


def _run_tailor_live(
    row: EnrichedRow,
    profile: TailoringProfile,
    toolchain: CvToolchain | None = None,
) -> None:
    """Edits, then page-fit loop, then cover from the final resume.

    Note: The `toolchain` argument exists just for the test suite to check that
    another toolchain matching the interface can be used.
    """

    # init cv toolchain
    cv_toolchain_dir = resolve_cv_toolchain_dir()
    if toolchain is None:
        ensure_cv_toolchain_dir(cv_toolchain_dir)
        toolchain = RoffumeToolchain(cv_toolchain_dir)

    # Do git commits only if the cv toolchain dir is inside a git worktree.
    # Error if the git tree is currently dirty.
    ensure_worktree_clean(cv_toolchain_dir)

    def commit(message: str) -> None:
        """Record a step in the CV repo history (no-op without a worktree)."""
        git_commit(message, cv_toolchain_dir)

    def unparseable_reply() -> NoReturn:
        """Degrade path: no package recorded, controlled nonzero exit."""
        raise ShobrError("could not parse a valid reply from the LLM; no package recorded")

    # Have LLM create tailored resume
    print(
        f"Sent tailor-edits request for lead {BOLD}[{row['company']}] "
        f"{row['posting_id']} ({row['title']}){RESET}."
    )
    print("Waiting for response...")
    edits_text = chat_completion(_tailor_edits_prompt(row, profile), max_tokens=TAILOR_MAX_TOKENS)
    verified_md = _parse_md_reply(edits_text, "resume_md")
    if verified_md is None:
        unparseable_reply()
    verified_md = _wrap_markdown(verified_md)

    # create new CV application folder with the tailored resume, and commit
    slug = application_slug(row)
    app_dir = toolchain.scaffold(slug, row["posting_url"])
    commit(f"tailor {slug}: scaffold")
    (app_dir / "resume.md").write_text(verified_md, encoding="utf-8")
    commit(f"tailor {slug}: tailored resume")

    # have LLM rewrite tailored resume until it passes verification, and commit
    rewrites = 0
    current_md = verified_md
    while True:
        toolchain.build(app_dir)
        is_valid, report = toolchain.page_check(app_dir)
        if is_valid:
            break
        if rewrites >= TAILOR_MAX_REWRITES:
            raise ShobrError(
                f"resume still over one page after {rewrites} rewrites; package left at {app_dir}"
            )
        print(f"Sent tailor-rewrite request (attempt {rewrites + 1}).")
        print("Waiting for response...")
        rewrite_text = chat_completion(
            _tailor_rewrite_prompt(current_md, report), max_tokens=TAILOR_MAX_TOKENS
        )
        rewritten = _parse_md_reply(rewrite_text, "resume_md")
        if rewritten is None:
            unparseable_reply()
        current_md = _wrap_markdown(rewritten)
        (app_dir / "resume.md").write_text(current_md, encoding="utf-8")
        rewrites += 1
        commit(f"tailor {slug}: rewrite {rewrites}")

    # generate cover letter, and commit
    print("Sent tailor-cover request.")
    print("Waiting for response...")
    cover_text = chat_completion(
        _tailor_cover_prompt(row, profile, current_md), max_tokens=TAILOR_MAX_TOKENS
    )
    cover_md = _parse_md_reply(cover_text, "cover_md")
    if cover_md is None:
        raise ShobrError(
            "could not parse a valid reply from the LLM; no package recorded\n"
            f"package left at {short_path(app_dir)}"
        )
    cover_md = _wrap_markdown(cover_md)
    (app_dir / "cover-letter.md").write_text(cover_md, encoding="utf-8")
    commit(f"tailor {slug}: cover letter")

    notes_path = app_dir / "notes.md"
    existing = notes_path.read_text(encoding="utf-8") if notes_path.exists() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    snapshot = _notes_markdown(row, project_screening()["rows"].get(row["posting_id"]))
    notes_path.write_text(f"{existing}\n{snapshot}", encoding="utf-8")
    commit(f"tailor {slug}: notes")

    toolchain.finalize(app_dir)
    commit(f"tailor {slug}: renamed pdfs")

    _record_package(row, slug, str(app_dir), rewrites, current_md, cover_md)


def _run_tailor_for_row(row: EnrichedRow, print_prompt: bool, force: bool = False) -> None:
    """Print the tailoring prompts for one row, or build the package live."""
    profile = load_tailor_profile()
    if print_prompt:
        cover_placeholder = (
            "<tailored resume.md not yet generated in --print-prompt; "
            "live run inserts the TAILOR-EDITS output here>"
        )
        prompts = [
            ("TAILOR-EDITS", _tailor_edits_prompt(row, profile)),
            ("TAILOR-COVER", _tailor_cover_prompt(row, profile, cover_placeholder)),
            (
                "TAILOR-REWRITE",
                _tailor_rewrite_prompt(profile["resume"], "resume: 1 page(s)"),
            ),
        ]
        for name, prompt in prompts:
            print(f"=== PROMPT: {name} ===")
            print(prompt)
        return
    refuse_if_stale(row, load_config()["stale_after_days"], force)
    _run_tailor_live(row, profile)


def tailor_posting_id(posting_id: str, print_prompt: bool, force: bool = False) -> None:
    """Build the application package for a specific pursue posting."""
    row = get_enriched_row_on_stage(posting_id, "tailorable")
    _run_tailor_for_row(row, print_prompt, force)


def oldest_tailorable(
    enriched: list[EnrichedRow],
    screened: dict[str, ScreeningRow],
    tailored: dict[str, TailoringRow],
) -> EnrichedRow | None:
    """Oldest pursue row with no package yet, still passing enrichment."""
    tailored_ids = set(tailored)
    for row in enriched:
        sr = screened.get(row["posting_id"])
        if (
            sr is not None
            and sr["decision"] == ReviewDecision.PURSUE
            and row["posting_id"] not in tailored_ids
            and row["actionable"]
        ):
            return row
    return None


def tailor_next(print_prompt: bool, force: bool = False) -> None:
    """Build the package for the oldest pursue row with no package yet."""
    rows = get_enriched_rows()
    row = oldest_tailorable(
        list(rows.values()), project_screening()["rows"], project_tailoring()["rows"]
    )
    if row is None:
        print("nothing to tailor")
        return
    _run_tailor_for_row(row, print_prompt, force)


def tailor_all(force: bool = False) -> None:
    """Build packages for every pursue row with no package yet.

    Skips stale rows unless forced. Fails when nothing was built.
    """
    rows = get_enriched_rows()
    threshold = load_config()["stale_after_days"]
    screened = project_screening()["rows"]
    pending = [
        row
        for row in rows.values()
        if (sr := screened.get(row["posting_id"])) is not None
        and sr["decision"] == ReviewDecision.PURSUE
        and row["posting_id"] not in project_tailoring()["rows"]
        and row["actionable"]
    ]
    skipped = sorted(row["posting_id"] for row in pending if not force and is_stale(row, threshold))
    if skipped:
        print(
            f"{len(skipped)} of {len(pending)} rows to tailor were last checked over"
            f" {threshold} days ago; skipping stale rows"
            + ("" if force else " (pass --force to build them anyway)")
        )
    ordered = sorted(
        (row for row in pending if force or row["posting_id"] not in skipped),
        key=lambda row: is_stale(row, threshold),
    )
    built = 0
    failed: list[str] = []
    for row in ordered:
        try:
            _run_tailor_for_row(row, False, force)
        except ShobrError as exc:
            print(exc, file=sys.stderr)
            failed.append(row["posting_id"])
        else:
            built += 1
    print(f"Built {built} packages.")
    if failed:
        raise ShobrError(f"Failed to tailor {len(failed)}: {', '.join(failed)}")
    if built == 0 and skipped:
        raise ShobrError(f"All {len(skipped)} pending rows stale; re-enrich first or pass --force")
