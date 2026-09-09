# TODO: open source shobr (as a beachpatrol example)

Order: dependency first. `roffume` (nee `cv`) goes public, then shobr
references it.

## 1. Publish `roffume` (resume toolchain)

- Name: `roffume`, pronounced "rah-foo-MEH". Tagline: "Add fragrance to
  your resume with the early-UNIX energy of `roff`". Canonical spelling
  without the accent (`roffumé` lives in the tagline only).
- URL: `https://github.com/sebastiancarlos/roffume`.
- Fresh repo (no `filter-repo` surgery), done at `~/projects/roffume`:
  toolchain only (Makefile, `new-application`, `verify`, `rename-pdfs`,
  pandoc filter + ms template) + English-only skeleton `resume.md` using
  the Test Candidate persona.
- Cut for release: `sed-preview`/`sed-update`, `anonymize`, `photo.jpg`,
  `applications/`, `old-resumes/`, real resume content, existing git
  history.
- Generalized since: `resume_<language>.md` builds alongside canonical
  `resume.md` (endonym suffix map + `LANG.upper()` fallback in
  `rename-pdfs`, `--no-canonical-suffix` for single-file jobs);
  `make preview` inlined on `xdg-open`/`open` (zathura dropped);
  `./test` e2e pins a skeleton fixture + synthesizes french/chinese.
- Still open: note the `make clean` vs committed `.ms` quirk. (CI green on
  `main`, both legs, so the macOS claim stands.)
- Keep the real repo untouched as the private content fork running the same
  scripts.

## 2. CV toolchain contract (shobr-side primitives, roffume default)

Decided after three external reviews (notes in
`review-roffume-abstraction.md`): View B holds - the prompts are
input-parasitic, so a small honest contract genuinely unlocks swapping. No
registry, no plugin system, no duplicated page-check, no filename
parameterization, no JSON key renames.

Contract (4 ops; `commit` stays shobr-side, git-gated):

- `scaffold(slug, posting_url) -> Path`: app dir containing `resume.md`.
- `build(app_dir) -> None`: raises `CvToolError(output)`; linear
  orchestration instead of print-plus-return-code plumbing.
- `page_check(app_dir) -> (bool, str)`: ok flag + report fed to the rewrite
  prompt. Single source of truth stays in roffume's `verify`; shobr never
  encodes its own page number.
- `finalize(app_dir) -> None`: rename rule stays opaque inside.
- `git add -A` + commit + the dirty-tree precheck remain shobr behavior,
  skipped when the CV dir is not a worktree (intermediate rewrites survive
  only in git; events store just the final text).
- Filenames standardized shobr-wide via config `cv_path` (default
  `<cv_dir>/resume.md`, overridable with any direct master path, no
  toolchain dir required). `cover-letter.md` is pure shobr output (compiled
  and renamed by nothing), not contract.
- Backends selected by `cv_backend = "roffume" | "markdown"` (enum, default
  roffume). Markdown backend: scaffold is mkdir under the data home + copy
  of master, other three ops no-ops (rewrite loop skipped). It doubles as
  the hermetic test double and the no-toolchain answer. Roffume backend:
  today's five flagless shell-outs moved verbatim.

Order (TDD red-green, E2E via `test.py`, separate commits):

1. Prompts: generalize the front-matter wording ("keep the file's existing
   structure exactly, including any front matter, headings, indentation and
   list style"); replace "one A4 page" / "single page" prose with
   page-limit-agnostic wording (the `page_check` report carries the number).
   Done: pinned by the pre-existing `test_tailor_print_prompt`.
2. `_wrap_markdown`: skip `#` headings + the front-matter block (a long
   `## Company | Role` line was refilled into YAML-breaking breakage).
   Done: pinned by `test_tailor_wraps_long_lines` (proved load-bearing:
   folded output broke `make build` with a YAML parse error).
3. Contract: `CvToolchain` ABC (`scaffold`/`build`/`page_check`->`(bool,
   str)`/`finalize`, `CvToolError` for linear flow) + `RoffumeToolchain`
   (shell-outs moved verbatim) + injectable `toolchain` parameter on
   `_run_tailor_live` (production passes nothing). Git commits + dirty-tree
   precheck stay shobr-side, skipped outside a worktree. `cv_dir` resolved
   env > config > default; prompt profile still via `$SHOBR_CV_PATH`.
   Done: pinned by two in-process contract tests (`FakeBackend` in
   `test.py`; labeled waiver of E2E-only - interfaces are public
   contracts, not churnable internals) plus the unchanged E2E suite
   through the real backend. Deliberately NOT built: markdown backend,
   `cv_backend` enum, filename parameterization, JSON renames (all
   rejected as speculative; see review doc).
4. README honesty paragraph (the seam is for forkers; raw shell-outs remain
   the last-resort contract). Done.

## 3. Setup flow + shobr OSS hygiene

- First-run tailor with a missing toolchain dir prompts `[y/N]` to clone
  pinned roffume (`ROFFUME_PIN`, single source in `tailoring.py`) there and
  carries on; anything but `y` (including EOF) aborts nonzero. No TTY
  detection (piped answers work like `next` prompts). Done: pinned by
  decline/EOF/accept E2E (accept clones for real and packages).
- Fixtures audit: job pages are public data, but `notifications.html`
  likely contains personal network activity. Replace or re-capture what
  leaks, without breaking the verbatim-real-data parsing contract.
- README install story: no `~/projects/*` sibling assumptions, no personal
  paths; document beachpatrol + beachmsg + roffume setup, example profile,
  example config.
- Add LICENSE file (pyproject already says MIT).

## 4. Publish shobr

- Final pass: README as example-first documentation (pipeline as
  beachpatrol usage reference), AGENTS.md stays (it is part of the
  example's value).
