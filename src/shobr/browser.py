"""Browser integration (via `beachpatrol`)."""

import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from .config import load_config
from .core import (
    DATA_HOME,
    LINKEDIN_BASE_URL,
    SHOBR_DATA_DIR,
    ShobrError,
    __version__,
    short_path,
)

BEACHPATROL_COMMANDS_DIR = DATA_HOME / "beachpatrol" / "commands"
BEACHPATROL_COMMANDS_LOCAL_DIR = Path(__file__).resolve().parent / "beachpatrol-commands"


def commands_install() -> None:
    """Symlink every beachpatrol-commands/*.js into the user commands home.

    Commands are prefixed with their provider (for example
    `linkedin-job-search.js`), so several providers can be installed side by
    side.
    """
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


def _condense_html(html: str) -> str:
    """Shrink a captured page to the markup a scraper could care about.

    Drops scripts, styles, noscript blocks, HTML comments, and every `<head>`
    child except `<title>`. Best-effort, not a parser: a stray `</script>`
    inside a script string can leave a tail behind.
    """
    raw_text_re = re.compile(r"(?is)<(script|style|noscript)\b[^>]*>.*?</\1\s*>")
    comment_re = re.compile(r"(?s)<!--.*?-->")
    head_re = re.compile(r"(?is)<head\b[^>]*>(.*?)</head\s*>")
    title_re = re.compile(r"(?is)<title\b[^>]*>.*?</title\s*>")
    html = raw_text_re.sub("", html)
    html = comment_re.sub("", html)
    if match := head_re.search(html):
        title = title_re.search(match.group(1))
        head = f"<head>{title.group(0)}</head>" if title else "<head></head>"
        html = f"{html[: match.start()]}{head}{html[match.end() :]}"
    return html


def capture_parse_failure(
    kind: str,
    url: str,
    payload: Mapping[str, object],
    *,
    posting_id: str | None = None,
) -> Path:
    """Write a debug bundle for a failed parse (contained a condensed HTML) and
    return its folder path.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    bundle = SHOBR_DATA_DIR / "debug" / f"{kind}-{posting_id or 'page'}-{stamp}"
    bundle.mkdir(parents=True, exist_ok=True)

    # write condensed HTML
    raw_html = payload.get("raw_html")
    page_html = _condense_html(raw_html) if isinstance(raw_html, str) else ""
    (bundle / "page.html").write_text(page_html, encoding="utf-8")

    # Write some metadata for help with debugging.
    meta = {
        "url": url,
        "shobr_version": __version__,
        "browser": _resolve_beachpatrol_browser(),
    }
    (bundle / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    print(
        f"wrote a debug bundle to {short_path(bundle)}\n"
        "review and redact personal data before sharing",
        file=sys.stderr,
    )
    return bundle
