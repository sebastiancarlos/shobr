"""The tailoring stage: build per-job application packages via a CV toolchain."""

import re
import shutil
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn, TypedDict, cast

from .ai import chat_completion, parse_json_object
from .color import BLUE, BOLD, GREEN, RESET, YELLOW
from .config import resolve_cv_toolchain_dir
from .core import (
    DataKind,
    ShobrError,
    cached_projection,
    get_enriched_row_on_stage,
    paged,
    persist_event,
    print_batch_done,
    print_batch_progress,
    read_events,
    render_template,
    require_events,
    retry_llm_task,
    row_header,
    same_company_line,
    shobr_command,
    short_path,
    stage_line,
    store_key,
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
from .output import emit_json
from .providers.base import current_config, current_provider
from .screening import (
    PROFILE_FILES,
    TAILOR_FILES,
    ReviewDecision,
    ScreeningProfile,
    ScreeningRow,
    job_posting_block,
    labeled_block,
    load_files,
    profile_blocks,
    project_screening,
)
from .views import ListView, build_posting_view

TAILOR_MAX_REWRITES = 4
TAILOR_MAX_TOKENS = 4096


class TailoringRow(TypedDict):
    """A posting's tailoring state as projected into tailoring.json."""

    posting_id: str
    provider: str
    slug: str
    app_dir: str
    rewrites: int
    resume_md: str
    cover_md: str
    tailored_at: str


class TailorEvent(TailoringRow):
    """A completed tailor package build."""


class TailoredStore(TypedDict):
    """Projected packages keyed by provider/posting_id."""

    tailored_at: str | None
    rows: dict[str, TailoringRow]


class TailoringProfile(ScreeningProfile):
    """The full profile, plus the two tailor-only guides."""

    resume_guide: str
    cover_guide: str


def load_tailor_profile() -> TailoringProfile:
    """Read the CV + full profile .md files, including the tailor guides."""
    return cast(TailoringProfile, load_files(PROFILE_FILES + TAILOR_FILES))


@cached_projection
def project_tailoring(provider: str | None = None) -> TailoredStore:
    """Replay the tailoring events log."""
    if not (events := read_events(DataKind.TAILORING)):
        return {"tailored_at": None, "rows": {}}
    rows: dict[str, TailoringRow] = {}
    for raw in events:
        event = cast(TailorEvent, raw)
        if provider is not None and event["provider"] != provider:
            continue
        rows[store_key(event["provider"], event["posting_id"])] = TailoringRow(
            posting_id=event["posting_id"],
            provider=event["provider"],
            slug=event["slug"],
            app_dir=event["app_dir"],
            rewrites=event["rewrites"],
            resume_md=event["resume_md"],
            cover_md=event["cover_md"],
            tailored_at=event["tailored_at"],
        )
    return {"tailored_at": events[-1]["tailored_at"], "rows": rows}


def _tailored_rows(pending_review: bool) -> list[TailoringRow]:
    """Current provider's packages, optionally only those awaiting review."""
    require_events(DataKind.TAILORING)
    rows = list(project_tailoring(current_provider().name)["rows"].values())
    if not pending_review:
        return rows
    from .pipeline import pending_review_ids

    pending = pending_review_ids()
    return [row for row in rows if row["posting_id"] in pending]


def _tailored_list_view(pending_review: bool) -> ListView:
    """Build the tailored list view shared by text and JSON renderers."""
    from .discovery import project_discovery
    from .screening import project_screening
    from .tracking import project_tracking

    provider = current_provider().name
    rows = _tailored_rows(pending_review)
    leads = {row["posting_id"]: row for row in project_discovery(provider)["rows"]}
    enriched = project_enrichment(provider)["rows"]
    screened = project_screening(provider)["rows"]
    tracked = project_tracking(provider)["rows"]
    postings = [
        build_posting_view(
            row["posting_id"],
            after=DataKind.TAILORING,
            lead=leads.get(row["posting_id"]),
            enrichment=enriched[store_key(row["provider"], row["posting_id"])],
            screening=screened.get(store_key(row["provider"], row["posting_id"])),
            package=row,
            tracking=tracked.get(store_key(row["provider"], row["posting_id"])),
        )
        for row in rows
    ]
    return ListView(
        summary={"tailored": len(postings)},
        postings=postings,
    )


def _print_tailored_list_view(view: ListView) -> None:
    """Print a tailored list view in the human-readable form."""
    print(f"{BLUE}{view.summary['tailored']}{RESET} tailored")
    print()
    for posting in view.postings:
        enriched = posting.enrichment
        package = posting.package
        assert enriched is not None and package is not None, "tailored rows are enriched"
        print(f"{BOLD}{row_header(enriched)}{RESET}{validity_tag(enriched)}")
        print(f"  - {enriched['title']}")
        print(f"  - Slug: {package['slug']}")
        print(f"  - {BOLD}Application dir:{RESET} {short_path(package['app_dir'])}")
        rewrites = package["rewrites"]
        print(f"  - Rewrites: {GREEN if rewrites == 0 else YELLOW}{rewrites}{RESET}")
        print(stage_line(posting.stage, posting.stage_detail, DataKind.TAILORING))
        checked = datetime.fromisoformat(enriched["enriched_last_at"]).date().isoformat()
        assert posting.stale is not None, "tailored view has no staleness"
        print(f"  - Last Enriched At: {checked}{staleness_tag(posting.stale)}")
        built = datetime.fromisoformat(package["tailored_at"]).date().isoformat()
        print(f"  - Tailored At: {built}")
        if line := same_company_line(posting.same_company_application):
            print(line)

    print()


def print_tailored(pending_review: bool = False, *, json_output: bool = False) -> None:
    """Print tailored packages, optionally only those awaiting review."""
    view = _tailored_list_view(pending_review)
    if json_output:
        emit_json(view)
        return
    with paged():
        _print_tailored_list_view(view)


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
    """Deterministic application dir slug: company or provider, plus posting id."""
    company = "-".join(
        part
        for part in "".join(c if c.isalnum() else " " for c in row["company"].lower()).split()
        if part
    )
    if not company:
        return f"{row['provider']}-{row['posting_id']}"
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
        "provider": row["provider"],
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
    review_command = shobr_command(f"review {row['posting_id']}")
    lines = [
        "# SHOBR Job Description",
        "",
        "The shobr state for the job when this package was tailored "
        f"({built}). `{review_command}` shows the live state.",
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
        for label, review in (("LLM", screening["ai"]), ("Human", screening["human"])):
            if review is None:
                lines.append(f"- {label}: none")
            else:
                lines.append(f"- {label}: {review['score']}")
                lines.append(labeled_block(f"- {label} reasoning:", review["reasoning"]))
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

    slug = application_slug(row)
    app_dir: Path | None = None
    rewrites = 0
    current_md = ""
    cover_md = ""
    try:
        # Have LLM create tailored resume
        print(
            f"Sent tailor-edits request for lead {BOLD}{row_header(row)} ({row['title']}){RESET}."
        )
        print("Waiting for response...")

        def edits_task() -> str:
            """One completion plus parse for the tailored resume."""
            text = chat_completion(_tailor_edits_prompt(row, profile), max_tokens=TAILOR_MAX_TOKENS)
            md = _parse_md_reply(text, "resume_md")
            if md is None:
                unparseable_reply()
            return md

        verified_md = _wrap_markdown(retry_llm_task(edits_task))

        # create new CV application folder with the tailored resume, and commit
        app_dir = toolchain.scaffold(slug, row["posting_url"])
        commit(f"tailor {slug}: scaffold")
        (app_dir / "resume.md").write_text(verified_md, encoding="utf-8")
        commit(f"tailor {slug}: tailored resume")

        # have LLM rewrite tailored resume until it passes verification, and commit
        current_md = verified_md
        while True:
            toolchain.build(app_dir)
            is_valid, report = toolchain.page_check(app_dir)
            if is_valid:
                break
            if rewrites >= TAILOR_MAX_REWRITES:
                raise ShobrError(
                    f"resume still over one page after {rewrites} rewrites;"
                    " failed attempt rolled back"
                )
            print(f"Sent tailor-rewrite request (attempt {rewrites + 1}).")
            print("Waiting for response...")

            def rewrite_task() -> str:
                """One completion plus parse for a shortened resume."""
                text = chat_completion(
                    _tailor_rewrite_prompt(current_md, report), max_tokens=TAILOR_MAX_TOKENS
                )
                md = _parse_md_reply(text, "resume_md")
                if md is None:
                    unparseable_reply()
                return md

            current_md = _wrap_markdown(retry_llm_task(rewrite_task))
            (app_dir / "resume.md").write_text(current_md, encoding="utf-8")
            rewrites += 1
            commit(f"tailor {slug}: rewrite {rewrites}")

        # generate cover letter, and commit
        print("Sent tailor-cover request.")
        print("Waiting for response...")

        def cover_task() -> str:
            """One completion plus parse for the cover letter."""
            text = chat_completion(
                _tailor_cover_prompt(row, profile, current_md), max_tokens=TAILOR_MAX_TOKENS
            )
            md = _parse_md_reply(text, "cover_md")
            if md is None:
                unparseable_reply()
            return md

        cover_md = _wrap_markdown(retry_llm_task(cover_task))
        (app_dir / "cover-letter.md").write_text(cover_md, encoding="utf-8")
        commit(f"tailor {slug}: cover letter")

        notes_path = app_dir / "notes.md"
        existing = notes_path.read_text(encoding="utf-8") if notes_path.exists() else ""
        if existing and not existing.endswith("\n"):
            existing += "\n"
        snapshot = _notes_markdown(
            row,
            project_screening(current_provider().name)["rows"].get(
                store_key(row["provider"], row["posting_id"])
            ),
        )
        notes_path.write_text(f"{existing}\n{snapshot}", encoding="utf-8")
        commit(f"tailor {slug}: notes")

        toolchain.finalize(app_dir)
        commit(f"tailor {slug}: renamed pdfs")
    except Exception:
        if app_dir is not None:
            try:
                shutil.rmtree(app_dir)
                commit(f"tailor {slug}: rollback failed attempt")
            except (OSError, ShobrError) as cleanup_exc:
                raise ShobrError(
                    "tailoring failed and the failed attempt could not be cleaned up at"
                    f" {short_path(app_dir)}: {cleanup_exc}"
                ) from cleanup_exc
        raise

    assert app_dir is not None
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
    refuse_if_stale(row, current_config()["stale_after_days"], force)
    _run_tailor_live(row, profile)


def _require_tailoring() -> None:
    """Raise when the current board has no per-application tailoring."""
    provider = current_provider()
    if not provider.supports_tailoring:
        raise ShobrError(
            f"provider {provider.name} has no tailoring; screen and track postings directly"
        )


def tailor_posting_id(posting_id: str, print_prompt: bool, force: bool = False) -> None:
    """Build the application package for a specific pursue posting."""
    _require_tailoring()
    row = get_enriched_row_on_stage(posting_id, "tailorable")
    _run_tailor_for_row(row, print_prompt, force)


def pending_tailorable(
    enriched: list[EnrichedRow],
    screened: dict[str, ScreeningRow],
    tailored: dict[str, TailoringRow],
) -> list[EnrichedRow]:
    """Pursue rows with no package yet, still passing enrichment.

    Empty when the current board has no per-application tailoring.
    """
    if not current_provider().supports_tailoring:
        return []
    return [
        row
        for row in enriched
        if (sr := screened.get(store_key(row["provider"], row["posting_id"]))) is not None
        and sr["decision"] == ReviewDecision.PURSUE
        and store_key(row["provider"], row["posting_id"]) not in tailored
        and row["actionable"]
    ]


def tailor_next(print_prompt: bool, force: bool = False) -> None:
    """Build the package for the oldest pursue row with no package yet."""
    _require_tailoring()
    rows = get_enriched_rows()
    pending = pending_tailorable(
        list(rows.values()),
        project_screening(current_provider().name)["rows"],
        project_tailoring(current_provider().name)["rows"],
    )
    if not pending:
        print("nothing to tailor")
        return
    _run_tailor_for_row(pending[0], print_prompt, force)


def tailor_all(force: bool = False) -> None:
    """Build packages for every pursue row with no package yet.

    Skips stale rows unless forced. Stops at the first failure, and fails when
    nothing was built.
    """
    _require_tailoring()
    rows = get_enriched_rows()
    threshold = current_config()["stale_after_days"]
    screened = project_screening(current_provider().name)["rows"]
    pending = pending_tailorable(
        list(rows.values()),
        screened,
        project_tailoring(current_provider().name)["rows"],
    )
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
    total = len(ordered)
    built = 0
    for index, row in enumerate(ordered, start=1):
        print_batch_progress("Tailoring", index, total, row)
        try:
            _run_tailor_for_row(row, False, force)
        except ShobrError as exc:
            print_batch_done("Built", built, total, "packages")
            raise ShobrError(
                f"tailor-all stopped at {row_header(row)}: {exc}", code=exc.code
            ) from exc
        built += 1
    print_batch_done("Built", built, total, "packages")
    if built == 0 and skipped:
        raise ShobrError(f"All {len(skipped)} pending rows stale; re-enrich first or pass --force")
