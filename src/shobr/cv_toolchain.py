"""CV toolchain contract, and roffume backend (plus git clone of roffume)."""

import subprocess
from abc import ABC, abstractmethod
from pathlib import Path

from .color import BOLD, RESET
from .core import ShobrError, short_path

ROFFUME_URL = "https://github.com/sebastiancarlos/roffume"
ROFFUME_PIN = "v0.1.0"


def _run(*args: str, cwd: Path) -> tuple[str, int]:
    """Run a subprocess. Return `(stdout + stderr, code)`."""
    try:
        result = subprocess.run(list(args), cwd=cwd, capture_output=True, text=True, timeout=180)
    except FileNotFoundError as e:
        return str(e), 127
    except subprocess.TimeoutExpired as e:
        return str(e), 124
    return result.stdout + result.stderr, result.returncode


class CvToolError(ShobrError):
    """A CV Toolchain failure."""

    def __init__(self, output: str) -> None:
        """Store the backend step output."""
        super().__init__(output)
        self.output = output


class CvToolchain(ABC):
    """The "CV toolchain" contract."""

    @abstractmethod
    def scaffold(self, slug: str, posting_url: str) -> Path:
        """Create a per-job application dir and return it."""

    @abstractmethod
    def build(self, app_dir: Path) -> None:
        """Compile the package artifacts (usually PDFs)."""

    @abstractmethod
    def page_check(self, app_dir: Path) -> tuple[bool, str]:
        """Check build has expected number of pages. -> `(is_valid, report)`."""

    @abstractmethod
    def finalize(self, app_dir: Path) -> None:
        """Post-process outputs (for example rename PDFs to a final form)."""


class RoffumeToolchain(CvToolchain):
    """The actual CV Toolchain in use. Using the `roffume` project."""

    def __init__(self, cv_toolchain_dir: Path) -> None:
        """Point at an existing toolchain checkout."""
        self.cv_toolchain_dir = cv_toolchain_dir

    def scaffold(self, slug: str, posting_url: str) -> Path:
        """Create the application dir via `./new-application`."""
        out, code = _run("./new-application", slug, posting_url, cwd=self.cv_toolchain_dir)
        if code != 0:
            raise CvToolError(out)
        return self.cv_toolchain_dir / "applications" / slug

    def build(self, app_dir: Path) -> None:
        """Compile the package via make build."""
        out, code = _run("make", "build", cwd=app_dir)
        if code != 0:
            raise CvToolError(out)

    def page_check(self, app_dir: Path) -> tuple[bool, str]:
        """Run verify, returning its pass/fail plus output."""
        out, code = _run("./verify", cwd=app_dir)
        return code == 0, out

    def finalize(self, app_dir: Path) -> None:
        """Rename the PDFs via rename-pdfs."""
        out, code = _run("./rename-pdfs", cwd=app_dir)
        if code != 0:
            raise CvToolError(out)


def _is_worktree(path: Path) -> bool:
    """Whether the path is inside a git worktree."""
    _, code = _run("git", "rev-parse", "--is-inside-work-tree", cwd=path)
    return code == 0


def git_commit(message: str, cwd: Path) -> None:
    """Stage everything at CWD, and commit. Noop on non-git cwd."""
    if not _is_worktree(cwd):
        return
    steps = [
        ["git", "add", "-A"],
        [
            "git",
            "-c",
            "user.email=shobr@localhost",
            "-c",
            "user.name=shobr",
            "commit",
            "-m",
            f"SHOBR automated commit: {message}",
            "--quiet",
        ],
    ]
    for args in steps:
        out, code = _run(*args, cwd=cwd)
        if code != 0:
            raise ShobrError(out)


def ensure_worktree_clean(cwd: Path) -> None:
    """Raise if worktrees is dirty. Noop when no git."""
    if not _is_worktree(cwd):
        return
    out, code = _run("git", "status", "--porcelain", cwd=cwd)
    if code != 0:
        raise CvToolError(out)
    if out.strip():
        raise CvToolError(
            f"cv project at {short_path(cwd)} has uncommitted "
            "changes; commit or stash them before tailoring"
        )


def ensure_cv_toolchain_dir(cv_toolchain_dir: Path) -> None:
    """If cv toolchain is not present, offer to install `roffume`. Raise when declined."""
    if cv_toolchain_dir.is_dir():
        return
    try:
        install = input(
            f"cv toolchain not found at {short_path(cv_toolchain_dir)}; "
            f"install roffume {ROFFUME_PIN} there? {BOLD}[y/N]{RESET} "
        ).strip().lower() in ("y", "yes")
    except EOFError:
        install = False
    if not install:
        raise ShobrError("cv toolchain setup declined")
    cv_toolchain_dir.parent.mkdir(parents=True, exist_ok=True)
    out, code = _run(
        "git",
        "clone",
        "--branch",
        ROFFUME_PIN,
        "--depth",
        "1",
        ROFFUME_URL,
        str(cv_toolchain_dir),
        cwd=cv_toolchain_dir.parent,
    )
    if code != 0:
        raise ShobrError(f"git clone failed: {out}")
