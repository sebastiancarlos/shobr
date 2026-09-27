"""Browser integration (via `beachpatrol`)."""

import json
import os
import subprocess
from pathlib import Path

from .config import load_config
from .core import DATA_HOME, LINKEDIN_BASE_URL, SHOBR_DATA_DIR, ShobrError, short_path

BEACHPATROL_COMMANDS_DIR = DATA_HOME / "beachpatrol" / "commands"
BEACHPATROL_COMMANDS_LOCAL_DIR = Path(__file__).resolve().parent / "beachpatrol-commands"


def commands_install() -> None:
    """Symlink beachpatrol-commands/*.js into the beachpatrol user commands home."""
    BEACHPATROL_COMMANDS_DIR.mkdir(parents=True, exist_ok=True)
    sources = sorted(BEACHPATROL_COMMANDS_LOCAL_DIR.glob("*.js"))
    if not sources:
        raise ShobrError(
            f"no beachpatrol commands found in {short_path(BEACHPATROL_COMMANDS_LOCAL_DIR)}"
        )
    for src in sources:
        link = BEACHPATROL_COMMANDS_DIR / src.name
        if link.is_symlink():
            if link.resolve() == src:
                print(f"skip {link.name} (already exists)")
                continue
            link.unlink()
            print(f"relink {link.name}")
        elif link.exists():
            raise ShobrError(
                f"cannot link {link.name}: {short_path(link)} exists and is not a symlink"
            )
        link.symlink_to(src)
        print(f"linked {link.name}")


def _resolve_beachpatrol_browser() -> str:
    """Get beachpatrol browser to use. Taken from env or config."""
    if env_browser := os.environ.get("SHOBR_BEACHPATROL_BROWSER"):
        return env_browser
    return load_config()["beachpatrol_browser"]


def _resolve_beachpatrol_profile() -> str:
    """Get beachpatrol profile to use. Taken from env or config."""
    if env_profile := os.environ.get("SHOBR_BEACHPATROL_PROFILE"):
        return env_profile
    if profile := load_config()["beachpatrol_profile"]:
        return profile
    raise ShobrError(
        "no beachpatrol profile: set $SHOBR_BEACHPATROL_PROFILE or config beachpatrol_profile"
    )


def beachmsg_json(command: str, *args: str) -> dict:
    """Run a beachpatrol command expecting JSON output, returning the data.

    The trailing optional `wait` arg controls the wait time of some beachpatrol
    commands. This is settable via the env SHOBR_BROWSER_WAIT_MS (used mostly
    on the test suite, to speed it up on synthetic tests).
    """
    if wait := os.environ.get("SHOBR_BROWSER_WAIT_MS"):
        args = (*args, wait)
    cmd = [
        "beachmsg",
        "--browser",
        _resolve_beachpatrol_browser(),
        "--profile",
        _resolve_beachpatrol_profile(),
        command,
        *args,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        raise ShobrError("beachmsg not found; is beachpatrol installed?") from None
    if result.returncode != 0:
        raise ShobrError(result.stderr, code=result.returncode)

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise ShobrError(f"unexpected beachmsg output (expected {command} JSON)") from None
    return data


def smoke_test_browser() -> None:
    """Dump the LinkedIn homepage through beachpatrol, print a summary, and save the HTML."""
    dump = beachmsg_json("dump-page", LINKEDIN_BASE_URL)

    out = SHOBR_DATA_DIR / "smoke" / "linkedin-homepage.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(dump["html"])

    print(dump["url"])
    print(dump["title"])
    print(f"wrote {short_path(out)}")
