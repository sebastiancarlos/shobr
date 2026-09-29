#!/usr/bin/env -S uv run python
"""End-to-end test suite for shobr."""

import fcntl
import http.server
import json
import os
import pty
import re
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from shobr.browser import BEACHPATROL_COMMANDS_LOCAL_DIR
from shobr.core import get_template
from shobr.cv_toolchain import ROFFUME_PIN, ROFFUME_URL
from shobr.notification import is_blocked, notification_key

CONFIG_DEFAULT = get_template("config.toml")

_KEEP_OUTPUTS = "--keep-outputs" in sys.argv or os.environ.get("SHOBR_KEEP_OUTPUTS") == "1"

ROOT = Path(__file__).resolve().parent
FIXTURES_DIR = ROOT / "test-fixtures"


def _read_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


def _search_page(*cards: tuple[str, str, str, str]) -> str:
    """Minimal synthetic search-results page for one-off enrich tests.

    Each card is (posting_id, title, company, location); the page carries
    only the hooks job-search.js depends on.
    """
    rows = "".join(
        f'<div componentkey="job-card-component-ref-{pid}">'
        f"<div>{title}</div><div>{company}</div><div>{location}</div></div>"
        for pid, title, company, location in cards
    )
    return f"<html><body><main>{rows}</main></body></html>"


def _flat(text: str) -> str:
    """Collapse all whitespace so prose assertions tolerate line wrapping."""
    return " ".join(text.split())


def _cross_second_boundary() -> None:
    """Sleep past the wall-clock second rollover.

    fetched_at timestamps truncate to seconds, so two fetches landing in the
    same second are indistinguishable to newly-seen accounting.
    """
    time.sleep(1.05)


def _cmd(*args: str) -> list[str]:
    # Prefer the project's own console script installed by `uv sync`.
    local = ROOT / ".venv" / "bin" / "shobr"
    if local.is_file():
        return [str(local), *args]
    return ["uv", "run", "shobr", *args]


_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _strip_ansi(text: str) -> str:
    """Remove ANSI color codes so assertions read the plain text."""
    return _ANSI_RE.sub("", text)


def shobr(
    *args: str,
    cwd: str | Path = ROOT,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    strip_ansi: bool = True,
) -> subprocess.CompletedProcess:
    """Run the shobr CLI in a subprocess, capturing output."""
    result = subprocess.run(
        _cmd(*args),
        cwd=cwd,
        env=env,
        input=input_text,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if strip_ansi:
        result.stdout = _strip_ansi(result.stdout)
        result.stderr = _strip_ansi(result.stderr)
    return result


def add_editor_to_env(
    env: dict[str, str],
    save_output: str,
    *,
    capture_to: Path | None = None,
) -> dict[str, str]:
    """Point $VISUAL at a stub editor that writes `save_output` into the file.

    When `capture_to` is set, the stub first copies the template it received
    there, so tests can assert on what the editor was shown.
    """
    home = Path(tempfile.mkdtemp(prefix="shobr-stub-editor-"))
    payload = home / "payload.txt"
    payload.write_text(save_output)
    editor = home / "editor.sh"
    script = ["#!/bin/sh"]
    if capture_to is not None:
        script.append(f'cp "$1" {capture_to}')
    script.append(f'cp {payload} "$1"')
    editor.write_text("\n".join(script) + "\n")
    editor.chmod(0o755)
    return {**env, "VISUAL": str(editor)}


class TestDataHome:
    """Temp XDG_DATA_HOME + XDG_CONFIG_HOME for the duration of a test.

    With `name` set, the temp dir's `shobr/` folder is copied to
    ./test-output/<name>/ on exit (before deletion) when test.py was run with
    the --keep-outputs flag, so results can be inspected manually.
    """

    def __init__(self, name: str = "") -> None:
        """Remember the kept-outputs name (empty keeps nothing)."""
        self.name = name

    def __enter__(self) -> TestDataHome:
        """Create the isolated temp XDG homes."""
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name)
        # fake server renders instantly, no need for the browsers settle wait
        self.env = {
            **os.environ,
            "XDG_DATA_HOME": str(self.path),
            "XDG_CONFIG_HOME": str(self.path / "config"),
            "SHOBR_BROWSER_WAIT_MS": "0",
        }
        return self

    @property
    def shobr_data(self) -> Path:
        """The shobr data dir inside this temp XDG_DATA_HOME."""
        return self.path / "shobr"

    @property
    def shobr_config(self) -> Path:
        """The shobr config dir inside this temp XDG_CONFIG_HOME."""
        return self.path / "config" / "shobr"

    def __exit__(self, *exc: object) -> None:
        """Optionally keep outputs, then clean up."""
        if self.name and _KEEP_OUTPUTS:
            dest = Path.cwd() / "test-output" / self.name
            shutil.rmtree(dest, ignore_errors=True)
            shutil.copytree(self.path / "shobr", dest)
        self._tmp.cleanup()


class FakeLinkedInServer:
    """Serves a mocked LinkedIn-looking host from 127.0.0.1, pages keyed by path. (Context manager)

    Defaults mirror the handful of pages SHOBR targets. Tests can override or
    extend the mapping with custom path -> html pairs.

    A path can map to a single html string (served on every request) or to a
    sequence of html strings (served in order, the last one repeating), so a
    single instance can fake the live page changing between fetches.
    """

    DEFAULT_PAGES: dict[str, str] = {
        "/": "<html><head><title>Fake LinkedIn</title></head><body>Welcome home</body></html>",
        "/notifications": _read_fixture("notifications.html"),
        "/jobs/search-results": _read_fixture("job-search.html"),
    }

    def __init__(self, pages: dict[str, str | list[str]] | None = None) -> None:
        """Merge page overrides over the defaults."""
        merged = {**self.DEFAULT_PAGES, **(pages or {})}
        self._pages = {
            path: [body] if isinstance(body, str) else list(body) for path, body in merged.items()
        }
        self._counts: dict[str, int] = {}
        self._server: http.server.ThreadingHTTPServer
        self._thread: threading.Thread

    def __enter__(self) -> FakeLinkedInServer:
        """Start the server thread."""
        pages = self._pages
        counts = self._counts

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                # for the fake server, we ignore query strings
                path = self.path.split("?", 1)[0]

                bodies = pages.get(path, pages["/"])
                index = counts.get(path, 0)
                counts[path] = index + 1
                body = bodies[min(index, len(bodies) - 1)].encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        """Shut the server down."""
        self._server.shutdown()
        self._server.server_close()

    @property
    def url(self) -> str:
        """The server base URL."""
        port = self._server.server_address[1]
        return f"http://127.0.0.1:{port}"


class FakeLLMServer:
    """Speaks the OpenAI /v1/chat/completions wire protocol from 127.0.0.1.
    (Context manager)

    Routes on the last message's content, so each prompt
    shape gets its own canned answer.
    """

    SMOKE_REPLY = "hello from the fake llm"
    SCREEN_SCORE_REPLY = '```json\n{"reasoning": "fake llm says good fit", "score": 4}\n```'
    TAILOR_EDITS_REPLY = json.dumps(
        {
            "resume_md": (
                "---\nname: Test Candidate\n---\n\n# Test Candidate\n\n"
                "## FakeCo | Senior Engineer\n"
                "    Testville\n    2024 - 2026\n\n- tailored bullet for testing\n"
            ),
        }
    )
    TAILOR_COVER_REPLY = json.dumps(
        {
            "cover_md": (
                "# Cover Letter\n\nDear hiring manager, fake cover for testing with a "
                "deliberately long second sentence so the wrapping pass must fold it.\n"
            )
        }
    )
    TAILOR_REWRITE_REPLY = json.dumps(
        {
            "resume_md": (
                "---\nname: Test Candidate\n---\n\n# Test Candidate\n\n"
                "- cut-down bullet for testing\n"
            ),
        }
    )

    def __init__(self) -> None:
        """Reset server handles and call tracking."""
        self._server: http.server.ThreadingHTTPServer
        self._thread: threading.Thread
        self.calls: list[str] = []

    def __enter__(self) -> FakeLLMServer:
        """Start the fake OpenAI endpoint."""
        smoke_reply = self.SMOKE_REPLY
        score_reply = self.SCREEN_SCORE_REPLY
        edits_reply = self.TAILOR_EDITS_REPLY
        cover_reply = self.TAILOR_COVER_REPLY
        rewrite_reply = self.TAILOR_REWRITE_REPLY
        calls: list[str] = []
        self.calls = calls

        class Handler(http.server.BaseHTTPRequestHandler):
            def _send(self, status: int, payload: dict) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if self.path.startswith("/v1/models"):
                    self._send(
                        200,
                        {"object": "list", "data": [{"id": "gpt-fake"}]},
                    )
                else:
                    self._send(404, {"error": "not found"})

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(length))
                content = req["messages"][-1]["content"]
                calls.append(content)

                filler = "- filler bullet line to overflow the page/company stuff\n"
                bloat_md = "# Test Candidate\n\n" + filler * 300

                # mock reply logic
                if "shobr llm smoke test" in content:
                    reply = smoke_reply
                elif "SHOBR-FAKE-GARBAGE" in content:
                    reply = "this is not json"
                elif "BEGIN VERIFY" in content:
                    reply = rewrite_reply
                elif '"cover_md"' in content:
                    if "cut-down bullet for testing" in content:
                        reply = json.dumps(
                            {
                                "cover_md": (
                                    "# Cover Letter\n\nDear hiring manager, "
                                    "final cover for testing.\n"
                                )
                            }
                        )
                    else:
                        reply = cover_reply
                elif "SHOBR-FAKE-BLOAT" in content:
                    reply = json.dumps({"resume_md": bloat_md})
                elif '"resume_md"' in content:
                    if "SHOBR-FAKE-LONGLINE" in content:
                        reply = json.dumps(
                            {
                                "resume_md": (
                                    "---\nname: Test Candidate\n"
                                    "title: A Deliberately Very Long Job Title That Exceeds "
                                    "Eighty Columns Even After The Key Prefix\n"
                                    "---\n\n# Test Candidate\n\n"
                                    "## FakeCo With An Extremely Long Employer Name Beyond "
                                    "Eighty Columns | Senior Engineer\n\n"
                                    "- this is a deliberately very long bullet line meant to "
                                    "exceed eighty columns so the wrapping pass must fold it, "
                                    "including cloud-native compounds\n"
                                ),
                            }
                        )
                    else:
                        reply = edits_reply
                elif "SHOBR-FAKE-LONG" in content:
                    reply = json.dumps(
                        {
                            "reasoning": (
                                "Alpha point about stack overlap and seniority scope. "
                                "Beta point about location policy and remote flexibility. "
                                "Gamma point weighing tradeoffs and growth areas. "
                                "Delta verdict stating the final call with clear conviction."
                            ),
                            "score": 4,
                        }
                    )
                elif '"reasoning"' in content:
                    reply = score_reply
                else:
                    reply = "unexpected prompt"
                self._send(
                    200,
                    {
                        "id": "chatcmpl-fake",
                        "object": "chat.completion",
                        "created": 0,
                        "model": req.get("model", "gpt-fake"),
                        "choices": [
                            {
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": reply,
                                },
                                "finish_reason": "stop",
                            }
                        ],
                        "usage": {
                            "prompt_tokens": 1,
                            "completion_tokens": 1,
                            "total_tokens": 2,
                        },
                    },
                )

            def log_message(self, *args: object) -> None:
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        """Shut the server down."""
        self._server.shutdown()
        self._server.server_close()

    @property
    def env(self) -> dict[str, str]:
        """Env pointing the OpenAI-provider any-llm client at this fake."""
        port = self._server.server_address[1]
        return {
            "OPENAI_API_KEY": "sk-test-fake",
            "OPENAI_BASE_URL": f"http://127.0.0.1:{port}/v1",
        }


_SNAPSHOTS: dict[str, Path] = {}


def _build_snapshot(
    name: str,
    pages: dict[str, str | list[str]],
    commands: list[list[str]],
    expect_enriched: dict[str, bool] | None = None,
) -> Path:
    """Run a setup flow once per suite run (or by worker test runner); it
    snapshot the shobr data dir.

    This is basically a way to speed up tests. It's based on the assumption
    that many tests need one of a small set of "base states", and that the
    state of the application is encapsulated by what SHOBR stores on its data
    folder.

    So, when a snapshot is built, it returns the generated data folder, to then
    be copied into a given test's data folder, and then carry on.

    At snapshot build-time, some assertions are performed, making the behavior
    encompased by the snapshot be testable (even if it runs only once, by the
    first test that needs it.

    At snapshot build-time, ephemeral data folders and beachpatrol instances
    are created. These are not reused by consuming tests.

    This function's arguments act as a declarative way of stating how the "base
    state" must be constructed.
    """
    if name in _SNAPSHOTS:
        return _SNAPSHOTS[name]
    profile = f"snapshot-{os.getpid()}-{name}"
    with TestDataHome() as data_home:
        env = data_home.env
        seed_test_config(data_home)
        with FakeLinkedInServer(pages) as server:
            with TestBeachpatrolInstance(env, profile):
                env = {
                    **env,
                    "SHOBR_BEACHPATROL_PROFILE": profile,
                    "SHOBR_LINKEDIN_BASE_URL": server.url,
                }
                for argv in commands:
                    result = shobr(*argv, env=env)
                    if result.returncode != 0:
                        raise AssertionError(f"snapshot {name} {argv}: {result.stderr}")
        snapshot = Path(tempfile.mkdtemp(prefix="shobr-snapshot-")) / "shobr"
        shutil.copytree(data_home.shobr_data, snapshot)
        if expect_enriched is not None:
            rows = json.loads((snapshot / "enrichment" / "enrichment.json").read_text())["rows"]
            for posting_id, passing in expect_enriched.items():
                row = rows.get(posting_id)
                if row is None or row["actionable"] != passing:
                    raise AssertionError(
                        f"snapshot {name} missing "
                        f"{'passing' if passing else 'rejected'} {posting_id}"
                    )
        _SNAPSHOTS[name] = snapshot
    return snapshot


def snapshot_screen_base() -> Path:
    """discover + enrich-next on the standard passing fixture."""
    return _build_snapshot(
        "screen-base",
        {"/jobs/view/5550000001": _read_fixture("job-detail.html")},
        [["setup"], ["discover"], ["enrich-next"]],
        expect_enriched={"5550000001": True},
    )


def snapshot_closed_enriched() -> Path:
    """discover x2 + enrich on the closed posting (rejected pre-filter)."""
    return _build_snapshot(
        "closed-enriched",
        {
            "/jobs/search-results": [
                _read_fixture("job-search.html"),
                _read_fixture("job-search-new.html"),
            ],
            "/jobs/view/5550000002": _read_fixture("job-detail-closed.html"),
        },
        [["setup"], ["discover"], ["discover"], ["enrich", "5550000002"]],
        expect_enriched={"5550000002": False},
    )


def restore_snapshot(data_home: TestDataHome, snapshot: Path) -> None:
    """Copy a snapshot's data tree and seed config it was built with."""
    shutil.copytree(snapshot, data_home.shobr_data, dirs_exist_ok=True)
    seed_test_config(data_home)


TEST_RESUME_MD = """\
# Test Candidate

Staff software engineer with ten years building scrapers and browser
automations in Python and TypeScript. Remote-first, Testville based.
"""

TEST_USER_DETAIL_MD = """\
Builds scrapers and browser automations in Python and TypeScript; staff-level
scope across product teams; prefers remote-first companies.
"""

TEST_FIT_CRITERIA_MD = """\
Remote-first product teams; staff-level scope; Python or TypeScript stacks;
pays in USD.
"""

TEST_DEAL_BREAKERS_MD = """\
No on-call rotations; no agencies or staff-augmentation shops; no on-site
outside Testville.
"""

TEST_RESUME_GUIDE_MD = """\
Keep AI and RAG experience prominent; condense early roles instead of
dropping employers; never invent collaborators, tools, or qualifiers.
"""

TEST_COVER_GUIDE_MD = """\
Direct tone, three paragraphs, lead with the stack match, never claim
unlisted skills.
"""


def seed_test_config(data_home: TestDataHome, text: str = CONFIG_DEFAULT) -> None:
    """Write config.toml (defaults unless overridden)."""
    config_dir = data_home.shobr_config
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.toml").write_text(text)


def seed_test_profile(data_home: TestDataHome) -> dict[str, str]:
    """Write the canonical test profile .md files. Return extra env overrides.

    Content lives here as constants until it moves to test-fixtures/profile/.
    """
    profile_dir = data_home.shobr_config / "profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "user-detail.md").write_text(TEST_USER_DETAIL_MD)
    (profile_dir / "fit-criteria.md").write_text(TEST_FIT_CRITERIA_MD)
    (profile_dir / "deal-breakers.md").write_text(TEST_DEAL_BREAKERS_MD)
    (profile_dir / "resume-guide.md").write_text(TEST_RESUME_GUIDE_MD)
    (profile_dir / "cover-guide.md").write_text(TEST_COVER_GUIDE_MD)
    cv_path = data_home.path / "resume.md"
    cv_path.write_text(TEST_RESUME_MD)
    return {"SHOBR_MAIN_CV_PATH": str(cv_path)}


def _ensure_roffume_cache() -> Path:
    """Clone pinned roffume into the pin-addressed cache dir (once); return it.

    Fail loud when the cache is absent and unreachable (offline bare clone).
    """
    cache = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    cache = cache / "shobr" / "roffume" / ROFFUME_PIN
    cache.parent.mkdir(parents=True, exist_ok=True)
    with open(cache.parent / f"{ROFFUME_PIN}.lock", "w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (cache / "new-application").is_file():
            return cache
        shutil.rmtree(cache, ignore_errors=True)
        try:
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--branch",
                    ROFFUME_PIN,
                    "--depth",
                    "1",
                    ROFFUME_URL,
                    str(cache),
                ],
                capture_output=True,
                check=True,
                timeout=180,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
            raise AssertionError(
                "cannot fetch pinned roffume for tests (offline?): git clone "
                f"{ROFFUME_URL} into {cache} manually ({e})"
            )
    return cache


def seed_test_cv(data_home: TestDataHome) -> dict[str, str]:
    """Copy the pinned-roffume toolchain into the temp home; git-init it with
    a test-suite identity. Return env overrides.

    Note that the cv toolchain is not expected to live on the system's data
    home. Indeed, that would be non-standard. But nothing prevents to do it for
    convenience here.

    Tests can mutate their own clone freely.
    """
    dest = data_home.path / "cv"
    shutil.copytree(
        _ensure_roffume_cache(),
        dest,
        ignore=shutil.ignore_patterns(".git", "applications"),
    )
    subprocess.run(["git", "init"], cwd=dest, capture_output=True, check=True)
    subprocess.run(["git", "add", "-A"], cwd=dest, capture_output=True, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@test",
            "-c",
            "user.name=test",
            "commit",
            "-m",
            "seed",
            "--quiet",
        ],
        cwd=dest,
        capture_output=True,
        check=True,
    )
    return {"SHOBR_CV_TOOLCHAIN_DIR": str(dest)}


def _append_event(path: Path, event: dict) -> None:
    """Append one JSON event line, creating parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")


def seed_human_review(
    data_home: TestDataHome, posting_id: str = "5550000001", score: int = 4
) -> None:
    """Append one hand-written human review to the screening log."""
    _append_event(
        data_home.shobr_data / "screening" / "events.jsonl",
        {
            "scored_at": "2026-09-13T00:00:00+00:00",
            "posting_id": posting_id,
            "kind": "human_review",
            "score": score,
            "reasoning": "hand-written human review",
        },
    )


def seed_tracking(
    data_home: TestDataHome, posting_id: str = "5550000001", status: str = "applied"
) -> None:
    """Write one status-transition event to the tracking log."""
    tracking_dir = data_home.shobr_data / "tracking"
    tracking_dir.mkdir(parents=True, exist_ok=True)
    (tracking_dir / "events.jsonl").write_text(
        json.dumps(
            {
                "tracked_at": "2026-09-13T00:00:00+00:00",
                "posting_id": posting_id,
                "status": status,
                "note": None,
            }
        )
        + "\n"
    )


def seed_tailored_package(data_home: TestDataHome, posting_id: str = "5550000001") -> None:
    """Write one packaged-posting event to the tailoring log."""
    tailoring_dir = data_home.shobr_data / "tailoring"
    tailoring_dir.mkdir(parents=True, exist_ok=True)
    (tailoring_dir / "events.jsonl").write_text(
        json.dumps(
            {
                "tailored_at": "2026-09-13T00:00:00+00:00",
                "posting_id": posting_id,
                "slug": f"testco-{posting_id}",
                "app_dir": f"/tmp/{posting_id}",
                "rewrites": 0,
                "resume_md": "seeded resume",
                "cover_md": "seeded cover",
            }
        )
        + "\n"
    )


def seed_minimal_lead(data_home: TestDataHome, posting_id: str = "1234567890") -> None:
    """Seed one passing lead plus its passing enrichment (no browser).

    Also seeds the default filters, since every projection loads them.
    """
    seed_test_config(data_home)
    posting_url = f"https://www.linkedin.com/jobs/view/{posting_id}"
    _append_event(
        data_home.shobr_data / "discovery" / "events.jsonl",
        {
            "fetched_at": "2026-09-13T00:00:00+00:00",
            "rows": [
                {
                    "posting_id": posting_id,
                    "posting_url": posting_url,
                    "title": "Test Engineer",
                    "company": "TestCo",
                    "location": "Testville",
                }
            ],
        },
    )
    seed_enrichment_event(data_home, posting_id)


def seed_enrichment_event(
    data_home: TestDataHome, posting_id: str = "1234567890", **overrides: str | bool | None
) -> None:
    """Seed one enrichment event, overriding any fields (no browser)."""
    posting_url = f"https://www.linkedin.com/jobs/view/{posting_id}"
    event = {
        "posting_id": posting_id,
        "posting_url": posting_url,
        "title": "Test Engineer",
        "company": "TestCo",
        "location": "Testville",
        "job_description_html": "<p>Test job.</p>",
        "company_description_html": "<p>Test company.</p>",
        "location_type": "Remote",
        "employment_type": "Full-time",
        "salary_range": None,
        "accepting_applications": True,
        "apply_method": "external",
        "apply_url": "https://example.com/apply",
        "job_description": "Test job.",
        "company_description": "Test company.",
        "fetched_at": "2026-09-13T00:00:00+00:00",
    }
    event.update(overrides)
    _append_event(data_home.shobr_data / "enrichment" / "events.jsonl", event)


def refresh_enrichment(data_home: TestDataHome, posting_id: str = "1234567890") -> None:
    """Append a fresh passing enrichment event (un-stales the row)."""
    seed_enrichment_event(
        data_home,
        posting_id=posting_id,
        fetched_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )


_READY_MARKER = "beachpatrol listening on"


class TestBeachpatrolInstance:
    """Launches a throwaway headless beachpatrol instance (Context manager)"""

    def __init__(self, env: dict[str, str], profile: str) -> None:
        """Store env and profile."""
        self._env = env
        self._profile = profile
        self._proc: subprocess.Popen

    def __enter__(self) -> TestBeachpatrolInstance:
        """Launch beachpatrol, waiting for readiness."""
        self._proc = subprocess.Popen(
            ["beachpatrol", "--headless", "--browser", "chromium", "--profile", self._profile],
            env=self._env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert self._proc.stdout is not None
        assert self._proc.stderr is not None
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            line = self._proc.stdout.readline()
            if _READY_MARKER in line:
                return self
            if self._proc.poll() is not None:
                stderr = self._proc.stderr.read()
                raise AssertionError(f"beachpatrol exited early: {stderr.strip()}")
            if not line:
                time.sleep(0.05)
        self._cleanup()
        raise AssertionError("beachpatrol never announced it is listening")

    def __exit__(self, *exc: object) -> None:
        """Terminate the instance and discard its profile."""
        self._cleanup()

    def _cleanup(self) -> None:
        """SIGTERM the instance (it removes its own socket) and discard its profile dir."""
        self._proc.terminate()
        try:
            self._proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=10)
        shutil.rmtree(
            Path.home() / ".config" / "beachpatrol" / "profiles" / "chromium" / self._profile,
            ignore_errors=True,
        )
        if self._proc.stdout is not None:
            self._proc.stdout.close()
        if self._proc.stderr is not None:
            self._proc.stderr.close()


class TestCLI(unittest.TestCase):
    """The E2E suite."""

    def test_version(self) -> None:
        """The --version flag exits zero and prints a version."""
        result = shobr("--version")
        self.assertEqual(result.returncode, 0)

    def test_setup_installs_commands(self) -> None:
        """setup symlinks the JS commands into a temporary XDG commands
        home (and scaffolds the profile); a second run keeps everything."""
        with TestDataHome() as data_home:
            env = data_home.env
            commands_home = data_home.path / "beachpatrol" / "commands"
            link = commands_home / "dump-page.js"

            result = shobr("setup", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(link.is_symlink())
            self.assertTrue(os.path.realpath(link).startswith(str(BEACHPATROL_COMMANDS_LOCAL_DIR)))
            self.assertTrue((data_home.shobr_config / "config.toml").is_file())

            result = shobr("setup", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("already exists", result.stdout)
            self.assertTrue(link.is_symlink())

    def test_setup_repairs_stale_command_links(self) -> None:
        """setup replaces a dangling command symlink instead of crashing."""
        with TestDataHome() as data_home:
            env = data_home.env
            commands_home = data_home.path / "beachpatrol" / "commands"
            commands_home.mkdir(parents=True)
            link = commands_home / "dump-page.js"
            link.symlink_to(data_home.path / "gone" / "dump-page.js")

            result = shobr("setup", env=env)
            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("relink dump-page.js", result.stdout)
            self.assertTrue(link.is_symlink())
            self.assertTrue(os.path.realpath(link).startswith(str(BEACHPATROL_COMMANDS_LOCAL_DIR)))

    def test_setup_refuses_blocking_command_file(self) -> None:
        """setup fails loudly when a real file blocks a command link."""
        with TestDataHome() as data_home:
            env = data_home.env
            commands_home = data_home.path / "beachpatrol" / "commands"
            commands_home.mkdir(parents=True)
            (commands_home / "dump-page.js").write_text("// mine\n")

            result = shobr("setup", env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("dump-page.js", result.stderr)
            self.assertIn("not a symlink", result.stderr)

    def test_setup_scaffolds_templates(self) -> None:
        """setup writes instructional profile .md scaffolds plus the
        default config.toml, keeping files that already exist."""
        with TestDataHome(name="setup") as data_home:
            env = data_home.env
            result = shobr("setup", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            profile_dir = data_home.shobr_config / "profile"
            for name in (
                "user-detail.md",
                "fit-criteria.md",
                "deal-breakers.md",
                "resume-guide.md",
                "cover-guide.md",
            ):
                path = profile_dir / name
                self.assertTrue(path.is_file())
                self.assertIn("SHOBR PROFILE TEMPLATE", path.read_text())
            self.assertIn(f"wrote {profile_dir / 'user-detail.md'}", result.stdout)
            filters_path = data_home.shobr_config / "config.toml"
            self.assertTrue(filters_path.is_file())
            self.assertIn("presence_locations", filters_path.read_text())
            self.assertIn("testville-city", filters_path.read_text())
            self.assertIn(f"wrote {filters_path}", result.stdout)

            custom = profile_dir / "user-detail.md"
            custom.write_text("my real detail")
            custom_filters = filters_path.read_text().replace("testville-city", "my-town")
            filters_path.write_text(custom_filters)
            result = shobr("setup", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("already exists", result.stdout)
            self.assertEqual(custom.read_text(), "my real detail")
            self.assertEqual(filters_path.read_text(), custom_filters)

    def test_beachpatrol_profile_missing_fails_loudly(self) -> None:
        """A browser command with no profile configured (env nor config)
        errors naming beachpatrol_profile, without touching a browser."""
        with TestDataHome() as data_home:
            seed_test_config(data_home)
            result = shobr("smoke-test-browser", env=data_home.env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("beachpatrol_profile", result.stderr)

    def test_beachpatrol_browser_from_config(self) -> None:
        """browser commands use config beachpatrol_browser (stub beachmsg
        logs argv; no browser touched)."""
        with TestDataHome() as data_home:
            seed_test_config(
                data_home,
                CONFIG_DEFAULT.replace(
                    'beachpatrol_browser = "chromium"', 'beachpatrol_browser = "firefox"'
                ),
            )
            stub = data_home.path / "bin"
            stub.mkdir()
            log = data_home.path / "argv.txt"
            (stub / "beachmsg").write_text(f'#!/bin/sh\necho "$@" > "{log}"\nexit 1\n')
            (stub / "beachmsg").chmod(0o755)
            env = {
                **data_home.env,
                "SHOBR_BEACHPATROL_PROFILE": "test-profile",
                "PATH": f"{stub}:{data_home.env['PATH']}",
            }
            result = shobr("smoke-test-browser", env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("--browser firefox", log.read_text())
            self.assertIn("--profile test-profile", log.read_text())

    def test_profile_prints_paths(self) -> None:
        """profile prints the CV + profile .md paths, flagging missing ones."""
        with TestDataHome(name="profile") as data_home:
            env = data_home.env
            result = shobr("profile", env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("config.toml", result.stderr)

            seed_test_config(data_home)
            result = shobr("profile", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("user-detail.md", result.stdout)
            self.assertIn("(missing)", result.stdout)
            self.assertIn("~/shobr-resumes/resume.md", result.stdout)
            self.assertIn("Some profile files are missing. Run shobr setup", result.stdout)

            env = {**env, **seed_test_profile(data_home)}
            seed_test_config(data_home)
            result = shobr("profile", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("(missing)", result.stdout)
            self.assertNotIn("Some profile files are missing", result.stdout)
            self.assertIn(
                str(data_home.shobr_config / "profile" / "user-detail.md"),
                result.stdout,
            )

            # Paths are column-aligned like `column -t`.
            starts = set()
            count = 0
            for line in result.stdout.splitlines():
                tokens = [
                    token
                    for token in line.split(" ")
                    if token.startswith("/") or token.startswith("~")
                ]
                if not tokens:
                    continue
                count += 1
                starts.add(line.index(tokens[0]))
            self.assertEqual(count, 7)
            self.assertEqual(len(starts), 1)
            self.assertIn("config.toml", result.stdout)

    def test_smoke_test_browser_via_fake_linkedin(self) -> None:
        """smoke-test-browser fetches through the real beachpatrol against a
        localhost stand-in for LinkedIn, prints a summary, and saves the HTML."""
        profile = f"test-{os.getpid()}-shobr-smoke"
        with TestDataHome() as data_home:
            env = data_home.env
            result = shobr("setup", env=env)
            seed_test_config(data_home)

            with FakeLinkedInServer() as server:
                with TestBeachpatrolInstance(env, profile):
                    env = {
                        **env,
                        "SHOBR_BEACHPATROL_PROFILE": profile,
                        "SHOBR_LINKEDIN_BASE_URL": server.url,
                    }
                    result = shobr("smoke-test-browser", env=env)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("Fake LinkedIn", result.stdout)
                    self.assertNotIn("html=", result.stdout)
                    self.assertIn(
                        f"wrote {data_home.path / 'shobr' / 'smoke' / 'linkedin-homepage.html'}",
                        result.stdout,
                    )

                    html_path = data_home.shobr_data / "smoke" / "linkedin-homepage.html"
                    self.assertIn("Welcome home", html_path.read_text())

                    list_tabs = subprocess.run(
                        ["beachmsg", "--browser", "chromium", "--profile", profile, "list-tabs"],
                        capture_output=True,
                        text=True,
                        env=env,
                    )
                    self.assertEqual(list_tabs.returncode, 0, list_tabs.stderr)
                    self.assertIn(
                        "Fake LinkedIn",
                        list_tabs.stdout,
                        "dump-page should leave the tab open, like a real session",
                    )

    def test_smoke_test_llm_via_fake_llm(self) -> None:
        """smoke-test-llm gets one completion through the real any-llm client
        against a localhost OpenAI-wire fake, and prints model + completion."""
        with TestDataHome() as data_home:
            with FakeLLMServer() as server:
                env = {
                    **data_home.env,
                    **server.env,
                    "SHOBR_AI_MODEL": "openai:gpt-fake",
                }
                result = shobr("smoke-test-llm", env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("openai:gpt-fake", result.stdout)
                self.assertIn(FakeLLMServer.SMOKE_REPLY, result.stdout)

    def test_smoke_test_llm_without_api_key(self) -> None:
        """smoke-test-llm works with no OPENAI_API_KEY anywhere: shobr
        defaults a dummy key (local endpoints rarely check auth)."""
        with TestDataHome() as data_home:
            with FakeLLMServer() as server:
                port = server.env["OPENAI_BASE_URL"]
                env = {
                    k: v
                    for k, v in {
                        **data_home.env,
                        "OPENAI_BASE_URL": port,
                        "SHOBR_AI_MODEL": "openai:gpt-fake",
                    }.items()
                    if k != "OPENAI_API_KEY"
                }
                self.assertNotIn("OPENAI_API_KEY", env)
                result = shobr("smoke-test-llm", env=env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(FakeLLMServer.SMOKE_REPLY, result.stdout)

    def test_notifications_via_fake_linkedin(self) -> None:
        """notifications parses structured rows from the notifications
        page (synthetic fixture) through the real beachpatrol, appends one
        event line to events.jsonl, and writes the projection to
        notifications.json."""
        profile = f"test-{os.getpid()}-shobr-notifs"
        with TestDataHome(name="notifications") as data_home:
            env = data_home.env
            result = shobr("setup", env=env)
            seed_test_config(data_home)

            with FakeLinkedInServer() as server:
                with TestBeachpatrolInstance(env, profile):
                    env = {
                        **env,
                        "SHOBR_BEACHPATROL_PROFILE": profile,
                        "SHOBR_LINKEDIN_BASE_URL": server.url,
                    }
                    result = shobr("notifications", env=env)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("notifications", result.stdout.lower())
                    self.assertIn("rita-recruiter", result.stdout)
                    self.assertIn("http", result.stdout)

                    notif_dir = data_home.shobr_data / "notifications"
                    events_path = notif_dir / "events.jsonl"
                    queue_path = notif_dir / "notifications.json"
                    self.assertIn(f"wrote to {queue_path}", result.stdout)

                    # events.jsonl: one line per fetch, the full observed page.
                    events = [json.loads(line) for line in events_path.read_text().splitlines()]
                    self.assertEqual(len(events), 1)
                    event = events[0]
                    fetched_at = datetime.fromisoformat(event["fetched_at"])
                    self.assertNotIn(".", event["fetched_at"], "timestamps truncated to seconds")
                    raw_keys = {"actor_url", "links", "plain_text", "text", "time"}
                    self.assertEqual(len(event["rows"]), 6)
                    self.assertTrue(all(set(r) == raw_keys for r in event["rows"]))
                    all_text = " ".join(r["text"] for r in event["rows"])
                    # The synthetic fixture renders 6 notifications. all must be captured.
                    self.assertIn("Rita Recruiter", all_text)
                    self.assertIn("viewed your profile", all_text.lower())
                    self.assertIn("appeared in 15 searches", all_text.lower())
                    # plain_text drops the trailing relative-time token.
                    self.assertTrue(
                        any(
                            r["time"]
                            and r["text"].endswith(r["time"])
                            and r["plain_text"] != r["text"]
                            for r in event["rows"]
                        )
                    )

                    # notifications.json: the projection, current visible rows in
                    # order, each annotated with a first-sighting timestamp,
                    # minus the rows the blocklist filters out.
                    queued_rows = [r for r in event["rows"] if not is_blocked(r)]
                    self.assertIn(
                        f"{len(queued_rows)} notifications ({len(queued_rows)} newly seen)\n",
                        result.stdout,
                        "queue count excludes blocklisted notifications",
                    )
                    saved = json.loads(queue_path.read_text())
                    self.assertEqual(saved["fetched_at"], event["fetched_at"])
                    self.assertEqual(len(saved["rows"]), len(queued_rows))
                    projected_keys = raw_keys | {"first_seen_at", "timestamp"}
                    self.assertTrue(all(set(r) == projected_keys for r in saved["rows"]))
                    for row in saved["rows"]:
                        # On the baseline run every row was seen for the first time.
                        self.assertEqual(row["first_seen_at"], event["fetched_at"])
                        ts = datetime.fromisoformat(row["timestamp"])
                        self.assertNotIn(".", row["timestamp"], "timestamps truncated to seconds")
                        self.assertLess(ts, fetched_at)
                        self.assertGreater(ts, fetched_at.replace(year=fetched_at.year - 1))

                    # Blocklisted notifications keep their slot in events.jsonl
                    # but never enter the queue.
                    blocked_rows = [r for r in event["rows"] if is_blocked(r)]
                    self.assertTrue(blocked_rows, "fixture must exercise the blocklist")
                    self.assertEqual(len(blocked_rows) + len(queued_rows), len(event["rows"]))
                    queue_plain = {r["plain_text"].lower() for r in saved["rows"]}
                    for blocked in blocked_rows:
                        self.assertIn(blocked["text"][:40], all_text, "still logged")
                        self.assertNotIn(blocked["plain_text"].lower(), queue_plain)
                    # Clearly job-hunt rows must survive the blocklist.
                    self.assertTrue(
                        any("new opportunities" in r["plain_text"] for r in saved["rows"])
                    )
                    self.assertTrue(
                        any(
                            "web development request" in r["plain_text"].lower()
                            for r in saved["rows"]
                        )
                    )

                    # Local snapshot: same queue, no fetch, no NEW markers,
                    # read-only on the event log.
                    result = shobr("notifications", "--local", env=env)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertNotIn("(NEW)", result.stdout)
                    self.assertNotIn("newly seen", result.stdout)
                    self.assertIn(f"{len(queued_rows)} notifications\n", result.stdout)
                    self.assertNotIn("wrote to", result.stdout)
                    self.assertEqual(len(events_path.read_text().splitlines()), 1)
                    self.assertEqual(json.loads(queue_path.read_text()), saved)

    def test_notifications_second_run_stable_under_drift(self) -> None:
        """Test doing a first notification fetch, and then a second one
        returning additonal notifications.
        """
        new_html = _read_fixture("notifications-new.html")
        baseline_html = _read_fixture("notifications.html")
        profile = f"test-{os.getpid()}-shobr-notifs-cycle2"
        with TestDataHome(name="notifications-cycle2") as data_home:
            env = data_home.env
            result = shobr("setup", env=env)
            seed_test_config(data_home)

            # The notifications endpoint serves a baseline page on the first
            # fetch, and a new page on the second, simulating the site
            # returning different content over time.
            with FakeLinkedInServer({"/notifications": [baseline_html, new_html]}) as server:
                with TestBeachpatrolInstance(env, profile):
                    env_run = {
                        **env,
                        "SHOBR_BEACHPATROL_PROFILE": profile,
                        "SHOBR_LINKEDIN_BASE_URL": server.url,
                    }

                    # Run 1: the baseline fixture.
                    result = shobr("notifications", env=env_run)
                    self.assertEqual(result.returncode, 0, result.stderr)

                    notif_dir = data_home.shobr_data / "notifications"
                    events_path = notif_dir / "events.jsonl"
                    queue_path = notif_dir / "notifications.json"
                    event_1 = json.loads(events_path.read_text().splitlines()[0])
                    queue_1 = json.loads(queue_path.read_text())

                    # Run 2: with new items, aged-out items, and pre-existing
                    # items whose relative time drifted.
                    # (Past the second boundary: fetched_at truncates to seconds.)
                    _cross_second_boundary()
                    result = shobr("notifications", env=env_run)
                    self.assertEqual(result.returncode, 0, result.stderr)

                    events = [json.loads(line) for line in events_path.read_text().splitlines()]
                    self.assertEqual(len(events), 2)
                    queue_2 = json.loads(queue_path.read_text())

                    rows_1 = {notification_key(r): r for r in queue_1["rows"]}
                    rows_2 = {notification_key(r): r for r in queue_2["rows"]}

                    # A row that drifts in time (10h -> 19h) keeps being identified as the same.
                    drifted_key = next(
                        k for k in rows_1 if "Senior Widget Engineer: new opportunities" in k[0]
                    )
                    drifted_event_2 = next(
                        r for r in events[1]["rows"] if notification_key(r) == drifted_key
                    )
                    self.assertIn(drifted_key, rows_2)
                    drifted_event_1 = next(
                        r for r in events[0]["rows"] if notification_key(r) == drifted_key
                    )
                    self.assertNotEqual(drifted_event_1["time"], drifted_event_2["time"])
                    # The event copy carries the drifted token; the queue copy must not
                    # re-guess the timestamp: it keeps the baseline sighting.
                    self.assertEqual(rows_2[drifted_key]["first_seen_at"], event_1["fetched_at"])
                    self.assertEqual(
                        rows_2[drifted_key]["timestamp"], rows_1[drifted_key]["timestamp"]
                    )

                    # A row genuinely new in run 2 is timestamped from run 2.
                    new_key = next(
                        k for k in rows_2 if "Still job hunting after six months" in k[0]
                    )
                    self.assertNotIn(new_key, rows_1)
                    self.assertEqual(rows_2[new_key]["first_seen_at"], events[1]["fetched_at"])

                    # A row present in run 1 but gone in run 2 stays queued:
                    # the projection never evicts rows on its own.
                    aged_key = next(
                        k for k in rows_1 if "Job hunting feels a lot like fishing" in k[0]
                    )
                    self.assertIn(aged_key, rows_2)
                    self.assertEqual(rows_2[aged_key]["first_seen_at"], event_1["fetched_at"])
                    self.assertEqual(rows_2[aged_key]["timestamp"], rows_1[aged_key]["timestamp"])

                    # The projection holds one entry per content key ever seen,
                    # including rows that aged out of the live page.
                    ever_seen = set(rows_1) | set(rows_2)
                    self.assertEqual(len(queue_2["rows"]), len(ever_seen))

                    # The queue shows newest-first, with timestamp breaking ties
                    # within the same fetch. Rows with an unparseable timestamp
                    # sort at the end of their first_seen_at group.
                    def queue_order(row: dict) -> tuple[str, str]:
                        return (row["first_seen_at"], row["timestamp"] or "")

                    expected_order = sorted(queue_2["rows"], key=queue_order, reverse=True)
                    projected_keys = [notification_key(r) for r in queue_2["rows"]]
                    self.assertEqual(projected_keys, [notification_key(r) for r in expected_order])

    def test_notifications_fails_loudly_on_empty(self) -> None:
        """A successful fetch that yields zero rows must fail loudly instead of
        writing a normal-looking empty queue (likely a LinkedIn DOM change)."""
        html = (
            "<html><body><main>"
            "<div><button aria-label='More options'>x</button></div>"
            "</main></body></html>"
        )
        profile = f"test-{os.getpid()}-shobr-notifs-empty"
        with TestDataHome() as data_home:
            env = data_home.env
            result = shobr("setup", env=env)
            seed_test_config(data_home)

            with FakeLinkedInServer({"/notifications": html}) as server:
                with TestBeachpatrolInstance(env, profile):
                    env = {
                        **env,
                        "SHOBR_BEACHPATROL_PROFILE": profile,
                        "SHOBR_LINKEDIN_BASE_URL": server.url,
                    }
                    result = shobr("notifications", env=env)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("may have changed", result.stderr)
                    notif_dir = data_home.shobr_data / "notifications"
                    self.assertFalse((notif_dir / "notifications.json").exists())
                    self.assertFalse((notif_dir / "events.jsonl").exists())

    def test_notifications_waits_for_slow_render(self) -> None:
        """A notifications page that hydrates after load is captured once the
        browser wait covers the delay (client-rendered content simulation)."""
        html = _read_fixture("notifications-delayed.html")
        profile = f"test-{os.getpid()}-shobr-notifs-slow"
        with TestDataHome() as data_home:
            env = data_home.env
            result = shobr("setup", env=env)
            seed_test_config(data_home)

            with FakeLinkedInServer({"/notifications": html}) as server:
                with TestBeachpatrolInstance(env, profile):
                    env = {
                        **env,
                        "SHOBR_BEACHPATROL_PROFILE": profile,
                        "SHOBR_LINKEDIN_BASE_URL": server.url,
                        "SHOBR_BROWSER_WAIT_MS": "3000",
                    }
                    result = shobr("notifications", env=env)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("rita-recruiter", result.stdout)

                    notif_dir = data_home.shobr_data / "notifications"
                    events = [
                        json.loads(line)
                        for line in (notif_dir / "events.jsonl").read_text().splitlines()
                    ]
                    self.assertEqual(len(events), 1)
                    self.assertEqual(len(events[0]["rows"]), 1)

    def test_discover_via_fake_linkedin(self) -> None:
        """job-search parses the search-results page into structured rows,
        appends them to events.jsonl, and accumulates deduped leads
        (newest-first) in discovery.json. Re-scraping the same page finds nothing
        new."""
        profile = f"test-{os.getpid()}-shobr-jobs"
        with TestDataHome(name="linkedin-jobs-cycle1") as data_home:
            env = data_home.env
            result = shobr("setup", env=env)
            seed_test_config(data_home)

            with FakeLinkedInServer() as server:
                with TestBeachpatrolInstance(env, profile):
                    env = {
                        **env,
                        "SHOBR_BEACHPATROL_PROFILE": profile,
                        "SHOBR_LINKEDIN_BASE_URL": server.url,
                    }

                    result = shobr("discover", env=env)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("jobs", result.stdout.lower())
                    self.assertIn("Performing LinkedIn job search with", result.stdout)
                    self.assertIn("search parameters:", result.stdout)
                    self.assertIn("All stored jobs: 12 jobs", result.stdout)
                    self.assertNotIn("Result:", result.stdout)
                    self.assertIn("FakeCo", result.stdout)
                    self.assertIn("jobs/view/", result.stdout)

                    discovery_dir = data_home.shobr_data / "discovery"
                    events_path = discovery_dir / "events.jsonl"
                    discovery_path = discovery_dir / "discovery.json"
                    self.assertIn("12 jobs (8 rejected. 12 newly seen)\n", result.stdout)
                    self.assertEqual(result.stdout.count("All stored jobs: 12 jobs"), 1)
                    self.assertIn(
                        "[Globex] (NEW)\n"
                        "  - Staff Platform Engineer\n"
                        "  - Testville\n"
                        "  - https://www.linkedin.com/jobs/view/5550000011\n",
                        result.stdout,
                    )
                    self.assertIn(
                        "[Hooli] (NEW) (REJECTED: title contains 'golang')\n"
                        "  - Senior Golang Engineer\n",
                        result.stdout,
                    )
                    self.assertIn(
                        "Rejection reasons:\n"
                        "- title contains 'golang' x1\n"
                        "- title contains 'devops' x1\n"
                        "- title contains 'java' x1\n"
                        "- title contains 'robotics' x1\n"
                        "- title contains 'c++' x1\n"
                        "- title contains 'architect' x1\n"
                        "- title contains 'reliability' x1\n"
                        "- title contains 'data engineer' x1\n",
                        result.stdout,
                    )
                    self.assertIn(f"wrote to {discovery_path}", result.stdout)

                    events = [json.loads(line) for line in events_path.read_text().splitlines()]
                    self.assertEqual(len(events), 1)
                    event = events[0]
                    self.assertNotIn(".", event["fetched_at"])
                    rows = event["rows"]
                    raw_keys = {"company", "location", "posting_id", "posting_url", "title"}
                    self.assertEqual(len(rows), 12)
                    self.assertTrue(all(set(row) == raw_keys for row in rows))
                    self.assertTrue(
                        all(row["title"] and row["company"] and row["location"] for row in rows),
                        "\n".join(str(row) for row in rows),
                    )
                    canonical = next(row for row in rows if row["posting_id"] == "5550000001")
                    self.assertEqual(canonical["company"], "FakeCo")
                    self.assertIn("Widget Platform", canonical["title"])
                    self.assertEqual(canonical["location"], "Testville (Remote)")
                    self.assertEqual(
                        canonical["posting_url"],
                        "https://www.linkedin.com/jobs/view/5550000001",
                    )
                    self.assertEqual(len({row["posting_url"] for row in rows}), 12)

                    leads = json.loads(discovery_path.read_text())
                    self.assertEqual(leads["fetched_at"], event["fetched_at"])
                    self.assertEqual(len(leads["rows"]), 12)
                    self.assertTrue(
                        all(
                            set(row)
                            == raw_keys | {"first_seen_at", "actionable", "rejected_reason"}
                            for row in leads["rows"]
                        )
                    )
                    self.assertIn("12 jobs (8 rejected. 12 newly seen)\n", result.stdout)
                    for row in leads["rows"]:
                        self.assertEqual(row["first_seen_at"], event["fetched_at"])

                    rejected = {
                        row["posting_id"]: row for row in leads["rows"] if not row["actionable"]
                    }
                    passing = [row for row in leads["rows"] if row["actionable"]]
                    self.assertTrue(rejected, "expected some jobs to fail the pre-filter")
                    self.assertTrue(passing, "expected some jobs to pass the pre-filter")
                    self.assertTrue(all(row["rejected_reason"] for row in rejected.values()))
                    self.assertTrue(all(row["rejected_reason"] is None for row in passing))
                    self.assertTrue(
                        all(
                            "neither in Testville nor Remote" not in row["rejected_reason"]
                            for row in rejected.values()
                        ),
                        "expected every baseline location (Testville/Remote) "
                        "to pass the location rule",
                    )
                    self.assertTrue(
                        any("Testville" == row["location"] for row in leads["rows"]),
                        "expected the fixture to include a bare 'Testville' location",
                    )
                    self.assertTrue(
                        any(
                            row["rejected_reason"] == "title contains 'golang'"
                            for row in rejected.values()
                        ),
                        "expected at least one title-keyword rejection",
                    )

                    # Run 2: identical, already-seen page -> nothing new.
                    # (Past the second boundary: fetched_at truncates to seconds.)
                    _cross_second_boundary()
                    result = shobr("discover", env=env)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("12 jobs (8 rejected. 0 newly seen)\n", result.stdout)
                    self.assertIn("All stored jobs:", result.stdout)
                    self.assertIn("Staff Platform Engineer", result.stdout)
                    self.assertNotIn("(NEW)", result.stdout)
                    self.assertEqual(len(events_path.read_text().splitlines()), 2)
                    leads2 = json.loads(discovery_path.read_text())
                    self.assertEqual(len(leads2["rows"]), 12)
                    for row in leads2["rows"]:
                        self.assertEqual(row["first_seen_at"], events[0]["fetched_at"])

                    # Local snapshot: same projection, no fetch, no NEW markers,
                    # read-only on the event log.
                    result = shobr("discovered", env=env)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertNotIn("(NEW)", result.stdout)
                    self.assertNotIn("newly seen", result.stdout)
                    self.assertIn("12 jobs (8 rejected)\n", result.stdout)
                    self.assertIn("[Hooli] (REJECTED: title contains 'golang')\n", result.stdout)
                    self.assertIn("Rejection reasons:", result.stdout)
                    self.assertNotIn("wrote to", result.stdout)
                    self.assertEqual(len(events_path.read_text().splitlines()), 2)
                    self.assertEqual(json.loads(discovery_path.read_text()), leads2)

    def test_discover_second_snapshot_via_fake_linkedin(self) -> None:
        """A refreshed snapshot of the search page adds only
        genuinely new postings to the leads store; already-seen ones keep
        their first sighting."""
        baseline = _read_fixture("job-search.html")
        updated = _read_fixture("job-search-new.html")

        def posting_ids(html: str) -> set[str]:
            return set(re.findall(r'componentkey="job-card-component-ref-([^"]+)"', html))

        first_batch = sorted(posting_ids(baseline))
        new_ids = sorted(posting_ids(updated) - posting_ids(baseline))
        profile = f"test-{os.getpid()}-shobr-jobs-cycle2"
        with TestDataHome(name="linkedin-jobs-cycle2") as data_home:
            env = data_home.env
            result = shobr("setup", env=env)
            seed_test_config(data_home)

            # The jobs endpoint serves the original snapshot on the first
            # fetch, and a newer snapshot on the second, simulating the site
            # returning a refreshed results page over time.
            with FakeLinkedInServer({"/jobs/search-results": [baseline, updated]}) as server:
                with TestBeachpatrolInstance(env, profile):
                    env_run = {
                        **env,
                        "SHOBR_BEACHPATROL_PROFILE": profile,
                        "SHOBR_LINKEDIN_BASE_URL": server.url,
                    }

                    # Run 1: the baseline snapshot.
                    result = shobr("discover", env=env_run)
                    self.assertEqual(result.returncode, 0, result.stderr)

                    discovery_dir = data_home.shobr_data / "discovery"
                    events_path = discovery_dir / "events.jsonl"
                    discovery_path = discovery_dir / "discovery.json"
                    event_1 = json.loads(events_path.read_text().splitlines()[0])

                    # Run 2: the refreshed snapshot.
                    # (Past the second boundary: fetched_at truncates to seconds.)
                    _cross_second_boundary()
                    result = shobr("discover", env=env_run)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(f"{len(new_ids)} newly seen", result.stdout)

                    events = [json.loads(line) for line in events_path.read_text().splitlines()]
                    self.assertEqual(len(events), 2)
                    self.assertEqual(
                        sorted({row["posting_id"] for row in events[1]["rows"]}),
                        sorted(posting_ids(updated)),
                    )

                    leads_2 = json.loads(discovery_path.read_text())
                    self.assertEqual(len(leads_2["rows"]), len(first_batch) + len(new_ids))
                    self.assertTrue(
                        all(
                            "actionable" in row and "rejected_reason" in row
                            for row in leads_2["rows"]
                        )
                    )
                    new_rows = [
                        row
                        for row in leads_2["rows"]
                        if row["first_seen_at"] == events[1]["fetched_at"]
                    ]
                    old_rows = [
                        row
                        for row in leads_2["rows"]
                        if row["first_seen_at"] == event_1["fetched_at"]
                    ]
                    self.assertEqual(sorted({row["posting_id"] for row in new_rows}), new_ids)
                    self.assertEqual(
                        sorted({row["posting_id"] for row in old_rows}),
                        sorted(set(first_batch) - set(new_ids)),
                    )

    def test_discover_fails_loudly_on_empty(self) -> None:
        """A search page that renders zero job cards must fail loudly (likely
        a LinkedIn DOM change) instead of writing an empty store."""
        html = "<html><body><main>no job cards here</main></body></html>"
        profile = f"test-{os.getpid()}-shobr-jobs-empty"
        with TestDataHome() as data_home:
            env = data_home.env
            result = shobr("setup", env=env)
            seed_test_config(data_home)

            with FakeLinkedInServer({"/jobs/search-results": html}) as server:
                with TestBeachpatrolInstance(env, profile):
                    env = {
                        **env,
                        "SHOBR_BEACHPATROL_PROFILE": profile,
                        "SHOBR_LINKEDIN_BASE_URL": server.url,
                    }
                    result = shobr("discover", env=env)
                    self.assertEqual(result.returncode, 1, result.stdout)
                    self.assertIn("refusing to write", result.stderr)
                    discovery_dir = data_home.shobr_data / "discovery"
                    self.assertFalse((discovery_dir / "events.jsonl").exists())
                    self.assertFalse((discovery_dir / "discovery.json").exists())

    def test_discover_waits_for_slow_render(self) -> None:
        """A search page that hydrates after load is captured once the browser
        wait covers the delay (client-rendered content simulation)."""
        html = _read_fixture("job-search-delayed.html")
        profile = f"test-{os.getpid()}-shobr-jobs-slow"
        with TestDataHome() as data_home:
            env = data_home.env
            result = shobr("setup", env=env)
            seed_test_config(data_home)

            with FakeLinkedInServer({"/jobs/search-results": html}) as server:
                with TestBeachpatrolInstance(env, profile):
                    env = {
                        **env,
                        "SHOBR_BEACHPATROL_PROFILE": profile,
                        "SHOBR_LINKEDIN_BASE_URL": server.url,
                        "SHOBR_BROWSER_WAIT_MS": "3000",
                    }
                    result = shobr("discover", env=env)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("2 jobs (0 rejected. 2 newly seen)\n", result.stdout)
                    self.assertIn("[Acme]", result.stdout)

    def test_discovered_with_no_data(self) -> None:
        """discovered on a fresh data home fails offline, writes nothing."""
        with TestDataHome() as data_home:
            result = shobr("discovered", env=data_home.env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("no discovery data yet", result.stderr)
            self.assertIn("run shobr discover first", result.stderr)
            self.assertNotIn("jobs-search", result.stderr)
            discovery_dir = data_home.shobr_data / "discovery"
            self.assertFalse((discovery_dir / "events.jsonl").exists())
            self.assertFalse((discovery_dir / "discovery.json").exists())

    def test_discovered_never_pages_when_piped(self) -> None:
        """Piped output never invokes the pager, even with PAGER set."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            marker = data_home.path / "pager-marker.txt"
            pager = data_home.path / "pager.sh"
            pager.write_text(f"#!/bin/sh\ntouch {marker}\ncat > /dev/null\n")
            pager.chmod(0o755)
            env = {**data_home.env, "PAGER": str(pager)}
            result = shobr("discovered", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(marker.exists())
            self.assertIn("All stored jobs: 12 jobs", result.stdout)

    def test_discovered_pages_on_tty(self) -> None:
        """A long listing on a tty invokes the pager with full content."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            captured = data_home.path / "paged.txt"
            marker = data_home.path / "pager-marker.txt"
            pager = data_home.path / "pager.sh"
            pager.write_text(f"#!/bin/sh\ntouch {marker}\ncat > {captured}\n")
            pager.chmod(0o755)
            env = {**data_home.env, "PAGER": str(pager), "TERM": "xterm"}
            argv = _cmd("discovered")

            pid, fd = pty.fork()
            if pid == 0:
                try:
                    fcntl.ioctl(1, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
                    os.execvpe(argv[0], argv, env)
                except OSError:
                    os._exit(127)
            try:
                out = b""
                eof = False
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError:
                        eof = True
                        break
                    if not chunk:
                        eof = True
                        break
                    out += chunk
                if not eof:
                    os.kill(pid, signal.SIGKILL)
                    os.waitpid(pid, 0)
                    self.fail("timed out waiting for paged output")
                _, status = os.waitpid(pid, 0)
            finally:
                os.close(fd)
            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
            self.assertTrue(marker.exists())
            self.assertIn("All stored jobs: 12 jobs", _strip_ansi(captured.read_text()))

    def test_discovered_missing_filters(self) -> None:
        """discovered fails loudly naming config.toml when it is absent."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            (data_home.shobr_config / "config.toml").unlink()
            result = shobr("discovered", env=data_home.env)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("Traceback", result.stderr)
            self.assertIn("config.toml", result.stderr)
            self.assertIn("setup", result.stderr)

    def test_discovered_bad_regex(self) -> None:
        """discovered fails loudly naming the label of an invalid pattern."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            seed_test_config(
                data_home,
                CONFIG_DEFAULT.replace(r'golang = "\\bgolang\\b"', 'golang = "(unclosed"'),
            )
            result = shobr("discovered", env=data_home.env)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("Traceback", result.stderr)
            self.assertIn("golang", result.stderr)

    def test_discovered_unknown_geo(self) -> None:
        """discovered fails loudly naming the unknown geo and the known ones."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            seed_test_config(
                data_home,
                CONFIG_DEFAULT.replace(
                    'geo = ["testville-city", "testville-dept"]', 'geo = ["atlantis"]'
                ),
            )
            result = shobr("discovered", env=data_home.env)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("Traceback", result.stderr)
            self.assertIn("atlantis", result.stderr)
            self.assertIn("testville-city", result.stderr)
            self.assertIn("geoId=", result.stderr)

    def test_discovered_unknown_workplace_type(self) -> None:
        """discovered fails loudly naming the unknown workplace type."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            seed_test_config(
                data_home,
                CONFIG_DEFAULT.replace(
                    'workplace_types = ["on-site", "hybrid", "remote"]',
                    'workplace_types = ["on-site", "teleport"]',
                ),
            )
            result = shobr("discovered", env=data_home.env)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("Traceback", result.stderr)
            self.assertIn("teleport", result.stderr)

    def test_discovered_unknown_employment_type(self) -> None:
        """discovered fails loudly naming the unknown employment type."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            seed_test_config(
                data_home,
                CONFIG_DEFAULT.replace(
                    'reject_employment_type = ["Internship"]',
                    'reject_employment_type = ["Boss"]',
                ),
            )
            result = shobr("discovered", env=data_home.env)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("Traceback", result.stderr)
            self.assertIn("Boss", result.stderr)

    def test_discovered_bad_geo_id(self) -> None:
        """discovered fails loudly on a non-numeric geo id."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            seed_test_config(
                data_home,
                CONFIG_DEFAULT.replace('testville-city = "111111111"', 'testville-city = "bogus"'),
            )
            result = shobr("discovered", env=data_home.env)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("Traceback", result.stderr)
            self.assertIn("bogus", result.stderr)

    def test_discovered_optional_filter_keys_default_to_empty(self) -> None:
        """discovered accepts a config without geo, presence_locations, or
        reject_employment_type: missing lists default to empty (and the
        empty presence list re-screens on-site leads as rejected)."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            seed_test_config(
                data_home,
                CONFIG_DEFAULT.replace('geo = ["testville-city", "testville-dept"]\n', "")
                .replace('presence_locations = ["testville"]\n', "")
                .replace('reject_employment_type = ["Internship"]\n', ""),
            )
            result = shobr("discovered", env=data_home.env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("12 jobs (10 rejected)", result.stdout)

    def test_discovered_unknown_key(self) -> None:
        """discovered fails loudly naming an unrecognized config key, so
        typos never silently fall back to defaults."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            seed_test_config(
                data_home,
                CONFIG_DEFAULT.replace("\n[geo_ids]\n", '\ngoe = ["testville-city"]\n[geo_ids]\n'),
            )
            result = shobr("discovered", env=data_home.env)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("Traceback", result.stderr)
            self.assertIn("unknown key", result.stderr)
            self.assertIn("goe", result.stderr)

    def test_discovered_rescreens_history(self) -> None:
        """Tightening the places re-judges old leads without touching events:
        place-only rows get rejected while remote-text rows still pass."""
        with TestDataHome(name="discover-rescreen") as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            events_path = data_home.shobr_data / "discovery" / "events.jsonl"
            self.assertEqual(len(events_path.read_text().splitlines()), 1)

            seed_test_config(
                data_home,
                CONFIG_DEFAULT.replace(
                    'presence_locations = ["testville"]',
                    'presence_locations = ["antarctica"]',
                ),
            )
            result = shobr("discovered", env=data_home.env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("12 jobs (10 rejected)", result.stdout)
            self.assertIn(
                "[FakeCo] (REJECTED: location 'Testville' matches no location rule)",
                result.stdout,
            )
            self.assertIn(
                "[FakeCo]\n  - Senior Software Engineer, Widget Platform",
                result.stdout,
            )
            self.assertEqual(len(events_path.read_text().splitlines()), 1)

    def test_discovered_shows_later_stage(self) -> None:
        """discovered shows a Stage line per row: the current stage plain,
        any later stage in blue."""
        with TestDataHome() as data_home:
            seed_minimal_lead(data_home)
            env = data_home.env
            result = shobr("discovered", env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("  - Stage: enrichment", result.stdout)
            self.assertIn("First Discovered At: 2026-09-13", result.stdout)

            seed_human_review(data_home, posting_id="1234567890")
            result = shobr("discovered", env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("  - Stage: screening (PURSUE)", result.stdout)

    def test_discovered_shows_current_stage(self) -> None:
        """A lead that never left discovery shows a plain Stage: discovery."""
        with TestDataHome() as data_home:
            seed_test_config(data_home)
            posting_id = "1234567890"
            discovery_dir = data_home.shobr_data / "discovery"
            discovery_dir.mkdir(parents=True, exist_ok=True)
            (discovery_dir / "events.jsonl").write_text(
                json.dumps(
                    {
                        "fetched_at": "2026-09-13T00:00:00+00:00",
                        "rows": [
                            {
                                "posting_id": posting_id,
                                "posting_url": (f"https://www.linkedin.com/jobs/view/{posting_id}"),
                                "title": "Test Engineer",
                                "company": "TestCo",
                                "location": "Testville",
                            }
                        ],
                    }
                )
                + "\n"
            )
            result = shobr("discovered", env=data_home.env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("  - Stage: discovery", result.stdout)

    def test_enriched_shows_later_stage(self) -> None:
        """enriched appends the furthest stage past enrichment per row."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            seed_human_review(data_home)
            result = shobr("enriched", env=data_home.env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("  - Stage: screening (PURSUE)", result.stdout)

    def test_screened_shows_later_stage(self) -> None:
        """screened appends the furthest stage past screening per row."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            seed_human_review(data_home)
            seed_tailored_package(data_home)
            result = shobr("screened", env=data_home.env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("  - Stage: tailoring", result.stdout)

    def test_screened_stage_omits_own_decision(self) -> None:
        """The screened Stage line names no decision: the Decision line
        right above already shows it."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            seed_human_review(data_home)
            result = shobr("screened", env=data_home.env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("  - Stage: screening\n", result.stdout)
            self.assertIn("  - Decision: PURSUE", result.stdout)
            self.assertNotIn("(PURSUE)", result.stdout)

    def test_screened_order_stable_across_runs(self) -> None:
        """screened lists rows newest-first regardless of hash seed."""
        with TestDataHome() as data_home:
            for posting_id in ("1234567890", "2222222222", "3333333333"):
                seed_minimal_lead(data_home, posting_id=posting_id)
                seed_human_review(data_home, posting_id=posting_id)
            outputs = set()
            for hash_seed in ("0", "1", "42"):
                env = {**data_home.env, "PYTHONHASHSEED": hash_seed}
                result = shobr("screened", env=env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                outputs.add(result.stdout)
            self.assertEqual(len(outputs), 1)
            only = outputs.pop()
            self.assertLess(only.index("3333333333"), only.index("2222222222"))
            self.assertLess(only.index("2222222222"), only.index("1234567890"))

    def test_tailored_shows_tracking(self) -> None:
        """tailored appends the tracking status per tracked row."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            seed_human_review(data_home)
            seed_tailored_package(data_home)
            seed_tracking(data_home)
            result = shobr("tailored", env=data_home.env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("  - Stage: tracking (APPLIED)", result.stdout)

    def test_enrich_next(self) -> None:
        """enrich-next test with one fetch"""
        detail = _read_fixture("job-detail.html")
        fakeco_url = "https://www.linkedin.com/jobs/view/5550000001"

        profile = f"test-{os.getpid()}-shobr-enrich"
        with TestDataHome(name="enrich-cycle-one") as data_home:
            env = data_home.env
            result = shobr("setup", env=env)
            seed_test_config(data_home)

            with FakeLinkedInServer({"/jobs/view/5550000001": detail}) as server:
                with TestBeachpatrolInstance(env, profile):
                    env = {
                        **env,
                        "SHOBR_BEACHPATROL_PROFILE": profile,
                        "SHOBR_LINKEDIN_BASE_URL": server.url,
                    }

                    result = shobr("discover", env=env)
                    self.assertEqual(result.returncode, 0, result.stderr)

                    result = shobr("enrich-next", env=env)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("FakeCo", result.stdout)

                    enrichment_dir = data_home.shobr_data / "enrichment"
                    events_path = enrichment_dir / "events.jsonl"
                    enriched_path = enrichment_dir / "enrichment.json"

                    events = [
                        json.loads(line) for line in events_path.read_text().splitlines() if line
                    ]
                    self.assertEqual(len(events), 1)
                    event = events[0]
                    self.assertEqual(event["posting_url"], fakeco_url)
                    self.assertEqual(event["apply_method"], "external")
                    self.assertTrue(
                        event["apply_url"].startswith("https://careers.fakeco.example/"),
                        event["apply_url"],
                    )
                    self.assertIn("The mission of FakeCo", event["job_description"])
                    self.assertNotIn(
                        "leading provider of widget infrastructure", event["job_description"]
                    )
                    self.assertIn("<strong>", event["job_description_html"])
                    self.assertIn("<ul", event["job_description_html"])
                    self.assertIn("The mission of FakeCo", event["job_description_html"])
                    self.assertIn(
                        "leading provider of widget infrastructure",
                        event["company_description_html"],
                    )
                    self.assertEqual(event["location_type"], "Remote")
                    self.assertEqual(event["employment_type"], "Full-time")
                    self.assertIsNone(event.get("salary_range"))
                    self.assertIn(
                        "leading provider of widget infrastructure", event["company_description"]
                    )

                    enriched = json.loads(enriched_path.read_text())
                    self.assertIn("5550000001", enriched["rows"])
                    row = enriched["rows"]["5550000001"]
                    self.assertEqual(row["apply_method"], "external")
                    self.assertIn("enriched_last_at", row)

    def test_enrich_posting_id_handles_agency_page(self) -> None:
        """'shobr enrich <posting_id>' on a synthetic multi-box agency page:
        the job/company split must hold when several expandable boxes exist."""
        detail = _read_fixture("job-detail-agency.html")

        profile = f"test-{os.getpid()}-shobr-enrich-agency"
        with TestDataHome(name="enrich-agency") as data_home:
            env = data_home.env
            result = shobr("setup", env=env)
            seed_test_config(data_home)

            search = _search_page(
                ("5550000003", "Project Engineer", "ConsultCo", "Testville (Remote)")
            )
            with FakeLinkedInServer(
                {"/jobs/search-results": search, "/jobs/view/5550000003": detail}
            ) as server:
                with TestBeachpatrolInstance(env, profile):
                    env = {
                        **env,
                        "SHOBR_BEACHPATROL_PROFILE": profile,
                        "SHOBR_LINKEDIN_BASE_URL": server.url,
                    }

                    result = shobr("discover", env=env)
                    self.assertEqual(result.returncode, 0, result.stderr)

                    result = shobr("enrich", "5550000003", env=env)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("ConsultCo", result.stdout)

                    events_path = data_home.shobr_data / "enrichment" / "events.jsonl"
                    events = [
                        json.loads(line) for line in events_path.read_text().splitlines() if line
                    ]
                    self.assertEqual(len(events), 1)
                    event = events[0]
                    self.assertIn(
                        "ConsultCo helps product teams ship widget platforms",
                        _flat(event["job_description"]),
                    )
                    self.assertIn(
                        "ConsultCo is your expert partner",
                        _flat(event["company_description"]),
                    )
                    self.assertNotIn(
                        "ConsultCo is your expert partner",
                        _flat(event["job_description"]),
                    )

    def test_enrich_posting_id_extracts_easy_apply_page(self) -> None:
        """'shobr enrich <posting_id>' on a synthetic Easy Apply page with
        several expandable boxes records the split plus the apply method."""
        detail = _read_fixture("job-detail-services.html")

        profile = f"test-{os.getpid()}-shobr-enrich-services"
        with TestDataHome(name="enrich-services") as data_home:
            env = data_home.env
            result = shobr("setup", env=env)
            seed_test_config(data_home)

            search = _search_page(("5550000004", "Mobile Developer", "ServicesCo", "Remote"))
            with FakeLinkedInServer(
                {"/jobs/search-results": search, "/jobs/view/5550000004": detail}
            ) as server:
                with TestBeachpatrolInstance(env, profile):
                    env = {
                        **env,
                        "SHOBR_BEACHPATROL_PROFILE": profile,
                        "SHOBR_LINKEDIN_BASE_URL": server.url,
                    }

                    result = shobr("discover", env=env)
                    self.assertEqual(result.returncode, 0, result.stderr)

                    result = shobr("enrich", "5550000004", env=env)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("ServicesCo", result.stdout)

                    events_path = data_home.shobr_data / "enrichment" / "events.jsonl"
                    events = [
                        json.loads(line) for line in events_path.read_text().splitlines() if line
                    ]
                    self.assertEqual(len(events), 1)
                    event = events[0]
                    self.assertIn(
                        "ServicesCo is seeking an experienced **Mobile Developer**",
                        _flat(event["job_description"]),
                    )
                    self.assertIn(
                        "designing, developing, and leading high-quality widget solutions",
                        _flat(event["job_description"]),
                    )
                    self.assertIn(
                        "ServicesCo is the technology partner of choice",
                        _flat(event["company_description"]),
                    )
                    self.assertNotIn(
                        "ServicesCo is the technology partner of choice",
                        _flat(event["job_description"]),
                    )
                    self.assertEqual(event["apply_method"], "easy_apply")
                    self.assertIsNone(event["apply_url"])

    def test_enrich_posting_id_missing_company_module(self) -> None:
        """'shobr enrich' on a page without an 'About the company' module
        records with a blank company description instead of refusing."""
        detail = _read_fixture("job-detail-no-company.html")

        profile = f"test-{os.getpid()}-shobr-enrich-no-company"
        with TestDataHome(name="enrich-no-company") as data_home:
            env = data_home.env
            result = shobr("setup", env=env)
            seed_test_config(data_home)

            search = _search_page(("5550000007", "Backend Engineer", "PayCo", "Remote"))
            with FakeLinkedInServer(
                {"/jobs/search-results": search, "/jobs/view/5550000007": detail}
            ) as server:
                with TestBeachpatrolInstance(env, profile):
                    env = {
                        **env,
                        "SHOBR_BEACHPATROL_PROFILE": profile,
                        "SHOBR_LINKEDIN_BASE_URL": server.url,
                    }

                    result = shobr("discover", env=env)
                    self.assertEqual(result.returncode, 0, result.stderr)

                    result = shobr("enrich", "5550000007", env=env)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("[PayCo] 5550000007", result.stdout)
                    self.assertNotIn("REJECTED", result.stdout)

                    events_path = data_home.shobr_data / "enrichment" / "events.jsonl"
                    events = [
                        json.loads(line) for line in events_path.read_text().splitlines() if line
                    ]
                    self.assertEqual(len(events), 1)
                    self.assertEqual(events[0]["company_description"], "")

    def test_enrich_posting_id_extracts_salary(self) -> None:
        """'shobr enrich <posting_id>' on a synthetic pay-bearing page records
        the salary range plus the workplace/employment pills."""
        detail = _read_fixture("job-detail-salary.html")

        profile = f"test-{os.getpid()}-shobr-enrich-salary"
        with TestDataHome(name="enrich-salary") as data_home:
            env = data_home.env
            result = shobr("setup", env=env)
            seed_test_config(data_home)

            search = _search_page(("5550000005", "Backend Engineer", "PayCo", "Remote"))
            with FakeLinkedInServer(
                {"/jobs/search-results": search, "/jobs/view/5550000005": detail}
            ) as server:
                with TestBeachpatrolInstance(env, profile):
                    env = {
                        **env,
                        "SHOBR_BEACHPATROL_PROFILE": profile,
                        "SHOBR_LINKEDIN_BASE_URL": server.url,
                    }

                    result = shobr("discover", env=env)
                    self.assertEqual(result.returncode, 0, result.stderr)

                    result = shobr("enrich", "5550000005", env=env)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("PayCo", result.stdout)

                    events_path = data_home.shobr_data / "enrichment" / "events.jsonl"
                    events = [
                        json.loads(line) for line in events_path.read_text().splitlines() if line
                    ]
                    self.assertEqual(len(events), 1)
                    event = events[0]
                    self.assertEqual(event["salary_range"], "$100K/yr - $150K/yr")
                    self.assertEqual(event["location_type"], "Remote")
                    self.assertEqual(event["employment_type"], "Full-time")
                    self.assertIn(
                        "builds payroll widgets",
                        _flat(event["job_description"]),
                    )

    def test_enrich_waits_for_slow_render(self) -> None:
        """A detail page that hydrates after load is captured through the
        command's readiness wait (client-rendered content simulation)."""
        search = _search_page(("5550000031", "Backend Engineer", "SlowCo", "Remote"))
        detail = _read_fixture("job-detail-delayed.html")

        profile = f"test-{os.getpid()}-shobr-enrich-slow"
        with TestDataHome() as data_home:
            env = data_home.env
            result = shobr("setup", env=env)
            seed_test_config(data_home)

            with FakeLinkedInServer(
                {
                    "/jobs/search-results": search,
                    "/jobs/view/5550000031": detail,
                }
            ) as server:
                with TestBeachpatrolInstance(env, profile):
                    env = {
                        **env,
                        "SHOBR_BEACHPATROL_PROFILE": profile,
                        "SHOBR_LINKEDIN_BASE_URL": server.url,
                    }
                    result = shobr("discover", env=env)
                    self.assertEqual(result.returncode, 0, result.stderr)

                    result = shobr("enrich", "5550000031", env=env)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

                    events_path = data_home.shobr_data / "enrichment" / "events.jsonl"
                    events = [
                        json.loads(line) for line in events_path.read_text().splitlines() if line
                    ]
                    self.assertEqual(len(events), 1)
                    self.assertIn(
                        "hydrated this role after the client render settled",
                        _flat(events[0]["job_description"]),
                    )

    def test_enrich_refuses_empty_descriptions(self) -> None:
        """'shobr enrich' on a page that renders no description content fails
        loudly (likely a LinkedIn DOM change) instead of recording vapor."""
        empty = (
            "<html><body><main>"
            '<div id="JobDetails_AboutTheJob_x"><h2>About the job</h2><p></p></div>'
            "</main></body></html>"
        )
        profile = f"test-{os.getpid()}-shobr-enrich-empty"
        with TestDataHome() as data_home:
            env = data_home.env
            result = shobr("setup", env=env)
            seed_test_config(data_home)

            with FakeLinkedInServer({"/jobs/view/5550000001": empty}) as server:
                with TestBeachpatrolInstance(env, profile):
                    env = {
                        **env,
                        "SHOBR_BEACHPATROL_PROFILE": profile,
                        "SHOBR_LINKEDIN_BASE_URL": server.url,
                    }
                    result = shobr("discover", env=env)
                    self.assertEqual(result.returncode, 0, result.stderr)

                    result = shobr("enrich", "5550000001", env=env)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertIn("may have changed", result.stderr)
                    enrichment_dir = data_home.shobr_data / "enrichment"
                    self.assertFalse((enrichment_dir / "events.jsonl").exists())
                    self.assertFalse((enrichment_dir / "enrichment.json").exists())

    def test_enrich_posting_id_closed_posting(self) -> None:
        """'shobr enrich <posting_id>' on a closed posting."""
        search = _read_fixture("job-search.html")
        search_new = _read_fixture("job-search-new.html")
        detail = _read_fixture("job-detail-closed.html")

        profile = f"test-{os.getpid()}-shobr-enrich-closed"
        with TestDataHome(name="enrich-closed") as data_home:
            env = data_home.env
            result = shobr("setup", env=env)
            seed_test_config(data_home)

            with FakeLinkedInServer(
                {
                    "/jobs/search-results": [search, search_new],
                    "/jobs/view/5550000002": detail,
                }
            ) as server:
                with TestBeachpatrolInstance(env, profile):
                    env = {
                        **env,
                        "SHOBR_BEACHPATROL_PROFILE": profile,
                        "SHOBR_LINKEDIN_BASE_URL": server.url,
                    }

                    result = shobr("discover", env=env)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    result = shobr("discover", env=env)
                    self.assertEqual(result.returncode, 0, result.stderr)

                    result = shobr("enrich", "5550000002", env=env)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn(
                        "[FakeCo] 5550000002 (REJECTED: no longer accepting applications)",
                        result.stdout,
                    )

                    events_path = data_home.shobr_data / "enrichment" / "events.jsonl"
                    events = [
                        json.loads(line) for line in events_path.read_text().splitlines() if line
                    ]
                    self.assertEqual(len(events), 1)
                    event = events[0]
                    self.assertFalse(event["accepting_applications"])
                    self.assertIsNone(event["apply_method"])
                    self.assertIsNone(event["apply_url"])
                    self.assertIn("The mission of FakeCo", event["job_description"])

                    # ensure it's rejected
                    enriched_path = data_home.shobr_data / "enrichment" / "enrichment.json"
                    projected = json.loads(enriched_path.read_text())
                    row = projected["rows"]["5550000002"]
                    self.assertFalse(row["actionable"])
                    self.assertEqual(row["rejected_reason"], "no longer accepting applications")

                    result = shobr("enriched", env=env)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn("1 enriched (1 rejected)", result.stdout)
                    self.assertIn(
                        "[FakeCo] 5550000002 (REJECTED: no longer accepting applications)",
                        result.stdout,
                    )
                    self.assertIn("- no longer accepting applications x1", result.stdout)

    def test_enrich_posting_id_closed_variant(self) -> None:
        """'shobr enrich <posting_id>' on the 'Not currently...' closed variant."""
        search = _read_fixture("job-search.html")
        detail = _read_fixture("job-detail-closed-variant.html")

        profile = f"test-{os.getpid()}-shobr-enrich-closed-variant"
        with TestDataHome(name="enrich-closed-variant") as data_home:
            env = data_home.env
            result = shobr("setup", env=env)
            seed_test_config(data_home)

            with FakeLinkedInServer(
                {"/jobs/search-results": search, "/jobs/view/5550000002": detail}
            ) as server:
                with TestBeachpatrolInstance(env, profile):
                    env = {
                        **env,
                        "SHOBR_BEACHPATROL_PROFILE": profile,
                        "SHOBR_LINKEDIN_BASE_URL": server.url,
                    }

                    result = shobr("discover", env=env)
                    self.assertEqual(result.returncode, 0, result.stderr)

                    result = shobr("enrich", "5550000002", env=env)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn(
                        "[FakeCo] 5550000002 (REJECTED: no longer accepting applications)",
                        result.stdout,
                    )

                    events_path = data_home.shobr_data / "enrichment" / "events.jsonl"
                    events = [
                        json.loads(line) for line in events_path.read_text().splitlines() if line
                    ]
                    self.assertEqual(len(events), 1)
                    event = events[0]
                    self.assertFalse(event["accepting_applications"])
                    self.assertIsNone(event["apply_method"])
                    self.assertIsNone(event["apply_url"])

    def test_enriched_prints_stored_rows(self) -> None:
        """'shobr enriched' prints the projected enrichment store, one row per
        enriched lead, reusing the enrich-next output format."""
        with TestDataHome(name="enrich-print") as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = data_home.env

            result = shobr("enriched", env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("1 enriched (0 rejected)", result.stdout)
            self.assertEqual(result.stdout.count("Apply (external)"), 1)
            self.assertIn("[FakeCo] 5550000001", result.stdout)
            self.assertIn("**Mission**", result.stdout)
            self.assertNotIn("(STALE)", result.stdout)
            self.assertIn("  - Location type: Remote", result.stdout)
            self.assertIn("  - Employment type: Full-time", result.stdout)
            self.assertIn("  - Company description:", result.stdout)
            self.assertIn("leading provider of widget infrastructure", result.stdout)
            self.assertIn("Or through LinkedIn:", result.stdout)
            self.assertIn("https://www.linkedin.com/jobs/view/5550000001", result.stdout)
            self.assertEqual(result.stdout.count("..."), 2)
            self.assertNotIn("from its Testville office.", result.stdout)

    def test_enriched_hybrid_needs_presence(self) -> None:
        """A Hybrid-typed posting with foreign text is rejected: hybrid needs
        presence like on-site; only remote passes anywhere."""
        with TestDataHome() as data_home:
            seed_test_config(data_home)
            seed_enrichment_event(data_home, location="Berlin, Germany", location_type="Hybrid")
            result = shobr("enriched", env=data_home.env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("1 enriched (1 rejected)", result.stdout)
            self.assertIn("(REJECTED: location", result.stdout)

    def test_enriched_remote_mention_is_not_remote_work(self) -> None:
        """Location text mentioning "remote" does not pass a non-remote
        posting: only the structured Remote type passes anywhere."""
        with TestDataHome() as data_home:
            seed_test_config(data_home)
            seed_enrichment_event(data_home, location="Remote, Oregon", location_type="On-site")
            result = shobr("enriched", env=data_home.env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("1 enriched (1 rejected)", result.stdout)
            self.assertIn("(REJECTED: location", result.stdout)

    def test_enriched_with_no_data(self) -> None:
        """'shobr enriched' on a fresh data home fails offline, writes nothing."""
        with TestDataHome() as data_home:
            result = shobr("enriched", env=data_home.env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("no enrichment data yet", result.stderr)
            enrichment_dir = data_home.shobr_data / "enrichment"
            self.assertFalse((enrichment_dir / "events.jsonl").exists())
            self.assertFalse((enrichment_dir / "enrichment.json").exists())

    def test_screened_score_filter(self) -> None:
        """screened --score filters by human score, else AI score; 'none'
        shows rows with no human review."""
        with TestDataHome() as data_home:
            seed_minimal_lead(data_home, posting_id="1111111111")
            seed_minimal_lead(data_home, posting_id="2222222222")
            seed_minimal_lead(data_home, posting_id="3333333333")
            seed_minimal_lead(data_home, posting_id="5550000041")
            seed_human_review(data_home, posting_id="1111111111", score=5)
            seed_human_review(data_home, posting_id="2222222222", score=3)
            seed_human_review(data_home, posting_id="3333333333", score=1)
            screening_dir = data_home.shobr_data / "screening"
            with (screening_dir / "events.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "scored_at": "2026-09-13T00:00:00+00:00",
                            "posting_id": "5550000041",
                            "kind": "ai_review",
                            "score": 4,
                            "reasoning": "fake ai review",
                        }
                    )
                    + "\n"
                )
            env = data_home.env

            result = shobr("screened", "--score", "5", env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("1111111111", result.stdout)
            self.assertNotIn("2222222222", result.stdout)
            self.assertNotIn("5550000041", result.stdout)

            result = shobr("screened", "--score", "2-4", env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("2222222222", result.stdout)
            self.assertIn("5550000041", result.stdout)
            self.assertNotIn("1111111111", result.stdout)
            self.assertNotIn("3333333333", result.stdout)

            result = shobr("screened", "--score", "1,5", env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("1111111111", result.stdout)
            self.assertIn("3333333333", result.stdout)
            self.assertNotIn("2222222222", result.stdout)

            result = shobr("screened", "--score", "none", env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("5550000041", result.stdout)
            self.assertNotIn("1111111111", result.stdout)

    def test_screened_score_filter_invalid(self) -> None:
        """screened --score with a bad value fails loudly."""
        with TestDataHome() as data_home:
            seed_minimal_lead(data_home)
            seed_human_review(data_home)
            for bad in ("foo", "0", "6", "5-3", ""):
                result = shobr("screened", "--score", bad, env=data_home.env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("score", result.stderr.lower())

    def test_screened_with_no_data(self) -> None:
        """'shobr screened' on a fresh data home fails."""
        with TestDataHome() as data_home:
            result = shobr("screened", env=data_home.env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("no screening data yet", result.stderr)
            self.assertIn("run shobr screen-next first", result.stderr)
            self.assertNotIn("`shobr screen-next`", result.stderr)
            screening_dir = data_home.shobr_data / "screening"
            self.assertFalse((screening_dir / "events.jsonl").exists())
            self.assertFalse((screening_dir / "screening.json").exists())

    def test_screen_next_with_no_data(self) -> None:
        """'shobr screen-next' on a fresh data home fails."""
        with TestDataHome() as data_home:
            result = shobr("screen-next", env=data_home.env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("no enrichment data yet", result.stderr)
            screening_dir = data_home.shobr_data / "screening"
            self.assertFalse(screening_dir.exists())

    def test_screen_flow_on_fakeco(self) -> None:
        """screen-next without a score or $EDITOR errors while still printing
        the informative row; screen records a human review."""
        with TestDataHome(name="screen-flow") as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = data_home.env

            # Keep any inherited $EDITOR out: screen-next must hit the editor
            # requirement.
            env = {k: v for k, v in env.items() if k not in ("VISUAL", "EDITOR")}

            # screen-next without a score and without $EDITOR: the editor is
            # required.
            result = shobr("screen-next", env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("no editor set; set $VISUAL or $EDITOR (or pass a score)", result.stderr)
            self.assertIn("FakeCo", result.stdout)
            self.assertIn("5550000001", result.stdout)
            self.assertIn("Human: PENDING", result.stdout)
            self.assertNotIn("Description:", result.stdout)
            self.assertNotIn("The mission of FakeCo", result.stdout)
            self.assertFalse((data_home.shobr_data / "screening").exists())

            # screen: record a human review via argv.
            result = shobr("screen", "5550000001", "4", "--reason", "good fit", env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Recorded human review:", result.stdout)
            self.assertIn("  - Score: 4", result.stdout)
            self.assertIn("  - Decision: PURSUE", result.stdout)
            self.assertIn("  - Reasoning: good fit", result.stdout)
            self.assertIn("wrote to", result.stdout)

            events_path = data_home.shobr_data / "screening" / "events.jsonl"
            events = [json.loads(line) for line in events_path.read_text().splitlines() if line]
            self.assertEqual(len(events), 1)
            event = events[0]
            self.assertEqual(event["kind"], "human_review")
            self.assertEqual(event["posting_id"], "5550000001")
            self.assertEqual(event["score"], 4)
            self.assertEqual(event["reasoning"], "good fit")
            self.assertNotIn(".", event["scored_at"], "timestamps truncated to seconds")

            screened = json.loads(
                (data_home.shobr_data / "screening" / "screening.json").read_text()
            )
            row = screened["rows"]["5550000001"]
            self.assertEqual(row["human"]["score"], 4)
            self.assertEqual(row["ai"], None)
            self.assertEqual(row["decision"], "pursue")

            # screened: summary with provenance.
            result = shobr("screened", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                "1 screened (1 pursue, 0 skip, 0 lacking LLM review, 0 pending)", result.stdout
            )
            self.assertIn("[FakeCo] 5550000001", result.stdout)
            self.assertIn(
                "  - Senior Software Engineer, Widget Platform",
                result.stdout,
            )
            self.assertIn(
                "  - Testville (Remote) | Remote | Full-time",
                result.stdout,
            )
            self.assertIn("  - LLM: N/A", result.stdout)
            self.assertIn("  - Human: 4", result.stdout)
            self.assertIn("  - Decision: PURSUE", result.stdout)
            self.assertIn("  - Human reasoning:\n      good fit", result.stdout)
            self.assertNotIn("LLM reasoning", result.stdout)

    def test_screen_editor_path_records_and_aborts(self) -> None:
        """'shobr screen <id>' without a score opens $EDITOR on a template;
        a valid save records the review, a malformed one aborts and writes
        nothing."""
        with TestDataHome(name="screen-editor") as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = data_home.env

            events_path = data_home.shobr_data / "screening" / "events.jsonl"

            # No score, no editor: the editor requirement is enforced, after
            # printing the informative row.
            env_no_editor = {k: v for k, v in env.items() if k not in ("VISUAL", "EDITOR")}
            result = shobr("screen", "5550000001", env=env_no_editor)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("no editor set; set $VISUAL or $EDITOR (or pass a score)", result.stderr)
            self.assertIn("FakeCo", result.stdout)
            self.assertFalse(events_path.exists())

            # A stub editor that writes a valid review, capturing the
            # template it received.
            template_capture = data_home.path / "template-captured.md"
            env_good = add_editor_to_env(
                env,
                "SHOBR_SCORE: 3\nSHOBR_REASONING: editor ok\nnot on the token line\n",
                capture_to=template_capture,
            )
            result = shobr("screen", "5550000001", env=env_good)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            template_text = template_capture.read_text()
            self.assertIn("## SHOBR SCORING", template_text)
            self.assertIn('- Score 1 means "skip," 2-5 mean "pursue."', template_text)
            self.assertIn("## Job Description", template_text)

            events = [json.loads(line) for line in events_path.read_text().splitlines() if line]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["score"], 3)
            self.assertEqual(events[0]["reasoning"], "editor ok")

            # A stub editor that writes a malformed review: abort.
            env_bad = add_editor_to_env(env, "SHOBR_SCORE: 9\nSHOBR_REASONING: nope\n")
            result = shobr("screen", "5550000001", env=env_bad)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("score", result.stderr.lower())
            events = [json.loads(line) for line in events_path.read_text().splitlines() if line]
            self.assertEqual(len(events), 1, "malformed save must not write an event")

            # A stub editor that writes a score-4 review with no
            # reasoning: abort, since reasoning is always required.
            env_no_reason = add_editor_to_env(env, "SHOBR_SCORE: 4\n")
            result = shobr("screen", "5550000001", env=env_no_reason)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("reason", result.stderr.lower())
            events = [json.loads(line) for line in events_path.read_text().splitlines() if line]
            self.assertEqual(len(events), 1, "reason-less save must not write an event")

    def test_screen_editor_template_includes_ai_review(self) -> None:
        """When a posting already has an AI review, the human screening
        prompt (terminal pre-print + $EDITOR template) surfaces the AI
        score and reasoning."""
        with TestDataHome(name="screen-editor-ai") as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {**data_home.env, **seed_test_profile(data_home)}

            with FakeLLMServer() as llm_server:
                result = shobr("screen-llm", "5550000001", env={**env, **llm_server.env})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            template_capture = data_home.path / "template-ai-captured.md"
            env_editor = add_editor_to_env(
                env,
                "SHOBR_SCORE: 3\nSHOBR_REASONING: human agrees\n",
                capture_to=template_capture,
            )
            result = shobr("screen", "5550000001", env=env_editor)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("fake llm says good fit", result.stdout)
            template_text = template_capture.read_text()
            self.assertIn("4", template_text)
            self.assertIn("fake llm says good fit", template_text)
            self.assertLess(
                template_text.index("## AI Review"),
                template_text.index("## Job Description"),
            )

    def test_screen_editor_template_wraps_ai_reasoning(self) -> None:
        """A long AI reasoning is wrapped to 80 columns in the $EDITOR
        template instead of one long blob."""
        with TestDataHome(name="screen-editor-wrap") as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {**data_home.env, **seed_test_profile(data_home)}
            with (data_home.shobr_config / "profile" / "user-detail.md").open(
                "a", encoding="utf-8"
            ) as fh:
                fh.write("\nSHOBR-FAKE-LONG\n")

            with FakeLLMServer() as llm_server:
                result = shobr("screen-llm", "5550000001", env={**env, **llm_server.env})
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            template_capture = data_home.path / "template-wrap-captured.md"
            env_editor = add_editor_to_env(
                env,
                "SHOBR_SCORE: 3\nSHOBR_REASONING: human agrees\n",
                capture_to=template_capture,
            )
            result = shobr("screen", "5550000001", env=env_editor)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            section = template_capture.read_text().split("## AI Review")[1]
            section = section.split("## Job Description")[0]
            lines = [line for line in section.splitlines() if line.strip()]
            self.assertGreater(len(lines), 3)
            self.assertTrue(all(len(line) <= 80 for line in lines))
            self.assertIn("Alpha point", section)

    def test_screen_skip_and_rescreen_latest_wins(self) -> None:
        """A score 1 (with reason) yields a skip; re-screening the same
        posting replaces the human review (latest wins); screen-next then has
        nothing left to show."""
        with TestDataHome(name="screen-latest") as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = data_home.env
            env = {k: v for k, v in env.items() if k not in ("VISUAL", "EDITOR")}

            result = shobr("screen", "5550000001", "1", "--reason", "tech stack", env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            result = shobr("screened", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                "1 screened (0 pursue, 1 skip, 0 lacking LLM review, 0 pending)", result.stdout
            )
            self.assertIn("  - Human: 1", result.stdout)
            self.assertIn("  - Decision: SKIP", result.stdout)

            # Re-screen the same posting: latest human review wins.
            result = shobr("screen", "5550000001", "5", "--reason", "changed mind", env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            events_path = data_home.shobr_data / "screening" / "events.jsonl"
            events = [json.loads(line) for line in events_path.read_text().splitlines() if line]
            self.assertEqual(len(events), 2)
            self.assertEqual(events[-1]["score"], 5)

            result = shobr("screened", env=env)
            self.assertIn(
                "1 screened (1 pursue, 0 skip, 0 lacking LLM review, 0 pending)", result.stdout
            )
            self.assertIn("  - Human: 5", result.stdout)
            self.assertIn("  - Decision: PURSUE", result.stdout)

            # Nothing left: screen-next reports no candidate.
            result = shobr("screen-next", env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("no leads to screen", result.stdout)

    def test_screen_argv_paths(self) -> None:
        """screen-next --score N --reason TEXT records directly (no $EDITOR);
        one-sided argv verdicts are rejected and write nothing; after
        recording, screen-next has nothing left to show."""
        with TestDataHome(name="screen-argv") as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = data_home.env
            env = {k: v for k, v in env.items() if k not in ("VISUAL", "EDITOR")}

            events_path = data_home.shobr_data / "screening" / "events.jsonl"

            # screen-next: --score without --reason.
            result = shobr("screen-next", "--score", "4", env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("both", result.stderr)

            # screen-next: --reason without --score.
            result = shobr("screen-next", "--reason", "reason alone", env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("both", result.stderr)

            # screen: positional score without --reason.
            result = shobr("screen", "5550000001", "4", env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("both", result.stderr)

            # screen: --reason without a score.
            result = shobr("screen", "5550000001", "--reason", "reason alone", env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("both", result.stderr)

            self.assertFalse(events_path.exists())

            # screen-next records directly via argv, no $EDITOR.
            result = shobr(
                "screen-next",
                "--score",
                "3",
                "--reason",
                "argv direct",
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            events = [json.loads(line) for line in events_path.read_text().splitlines() if line]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["score"], 3)
            self.assertEqual(events[0]["reasoning"], "argv direct")

            result = shobr("screened", env=env)
            self.assertIn(
                "1 screened (1 pursue, 0 skip, 0 lacking LLM review, 0 pending)", result.stdout
            )

            result = shobr("screen-next", env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("no leads to screen", result.stdout)

    def test_screen_llm_print_prompt(self) -> None:
        """screen-llm --print-prompt prints the built scoring prompt."""
        with TestDataHome(name="screen-llm-prompt") as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {**data_home.env, **seed_test_profile(data_home)}
            # Prove hermeticity: even a bogus LLM endpoint must not matter,
            # since --print-prompt never calls out.
            env = {**env, "OPENAI_BASE_URL": "http://127.0.0.1:1/v1"}

            result = shobr("screen-llm", "5550000001", "--print-prompt", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                "Senior Software Engineer, Widget Platform",
                result.stdout,
            )
            self.assertIn(
                "Testville (Remote) | Remote | Full-time)",
                result.stdout,
            )
            self.assertIn("Full-time)\n\n**Mission**", result.stdout)
            self.assertIn("## Company Description\n\nFakeCo is", result.stdout)
            self.assertIn("FakeCo", result.stdout)
            self.assertIn("The mission of FakeCo", result.stdout)
            self.assertIn("Test Candidate", result.stdout)
            self.assertIn("browser automations", result.stdout)
            self.assertIn("Remote-first product teams", result.stdout)
            self.assertIn("No on-call", result.stdout)
            self.assertIn('{"reasoning"', result.stdout)
            self.assertIn("You are an expert", result.stdout)
            self.assertIn("Do not paste verbatim quotes", result.stdout)
            self.assertIn("at most ~30 words", result.stdout)
            self.assertIn("Deal Breakers", result.stdout)
            self.assertIn("--- BEGIN CV ---", result.stdout)
            self.assertIn("--- END JOB POSTING ---", result.stdout)

    def test_screen_llm_print_prompt_missing_profile_file(self) -> None:
        """screen-llm --print-prompt errors naming the missing profile file."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {**data_home.env, **seed_test_profile(data_home)}

            (data_home.shobr_config / "profile" / "deal-breakers.md").unlink()
            result = shobr("screen-llm", "5550000001", "--print-prompt", env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("deal-breakers.md", result.stderr)
            self.assertIn("setup", result.stderr)

    def test_screen_llm_ignores_tailor_docs(self) -> None:
        """screen-llm works without the tailor-only profile docs."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {**data_home.env, **seed_test_profile(data_home)}

            (data_home.shobr_config / "profile" / "resume-guide.md").unlink()
            (data_home.shobr_config / "profile" / "cover-guide.md").unlink()
            result = shobr("screen-llm", "5550000001", "--print-prompt", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Senior Software Engineer", result.stdout)

    def test_tailor_print_prompt_missing_guide(self) -> None:
        """tailor --print-prompt errors naming the missing tailor guide."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {**data_home.env, **seed_test_profile(data_home)}
            seed_human_review(data_home)

            (data_home.shobr_config / "profile" / "resume-guide.md").unlink()
            result = shobr("tailor", "5550000001", "--print-prompt", env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("resume-guide.md", result.stderr)
            self.assertIn("setup", result.stderr)

    def test_screen_llm_print_prompt_unknown_posting(self) -> None:
        """screen-llm --print-prompt on a fresh home errors with no data."""
        with TestDataHome() as data_home:
            env = {**data_home.env, **seed_test_profile(data_home)}
            result = shobr("screen-llm", "0000000", "--print-prompt", env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("no enrichment data yet", result.stderr)

    def test_screen_llm_print_prompt_rejected_posting(self) -> None:
        """screen-llm --print-prompt refuses a posting that failed the pre-filter."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_closed_enriched())
            env = {**data_home.env, **seed_test_profile(data_home)}

            result = shobr("screen-llm", "5550000002", "--print-prompt", env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("did not pass the pre-filter", result.stderr)

    def test_screen_llm_scores_posting(self) -> None:
        """screen-llm (live) scores a posting through the fake LLM, records an
        ai_review event, and re-projects."""
        with TestDataHome(name="screen-llm-live") as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {**data_home.env, **seed_test_profile(data_home)}

            with FakeLLMServer() as llm_server:
                env = {**env, **llm_server.env}

                result = shobr("screen-llm", "5550000001", env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Sent AI review request", result.stdout)
                self.assertIn("for lead [FakeCo] 5550000001", result.stdout)
                self.assertIn(
                    "Senior Software Engineer, Widget Platform",
                    result.stdout,
                )
                self.assertIn("Waiting for response...", result.stdout)
                self.assertIn("Recorded AI review:", result.stdout)
                self.assertIn("  - Score: 4", result.stdout)
                self.assertIn("  - Decision: PENDING", result.stdout)
                self.assertIn("fake llm says good fit", result.stdout)
                self.assertIn("wrote to", result.stdout)

                events_path = data_home.shobr_data / "screening" / "events.jsonl"
                events = [json.loads(line) for line in events_path.read_text().splitlines() if line]
                self.assertEqual(len(events), 1)
                event = events[0]
                self.assertEqual(event["kind"], "ai_review")
                self.assertEqual(event["posting_id"], "5550000001")
                self.assertEqual(event["score"], 4)
                self.assertEqual(event["reasoning"], "fake llm says good fit")
                self.assertNotIn(".", event["scored_at"], "timestamps truncated")

                screened = json.loads(
                    (data_home.shobr_data / "screening" / "screening.json").read_text()
                )
                row = screened["rows"]["5550000001"]
                self.assertEqual(row["ai"]["score"], 4)
                self.assertEqual(row["human"], None)
                self.assertEqual(row["decision"], "pending")

    def test_screen_llm_next_scores_oldest_unscored(self) -> None:
        """screen-llm-next records an AI review for the oldest enriched
        passing lead with no AI review yet; a second invocation finds
        nothing left to score."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {**data_home.env, **seed_test_profile(data_home)}

            with FakeLLMServer() as llm_server:
                env = {**env, **llm_server.env}

                result = shobr("screen-llm-next", env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Recorded AI review:", result.stdout)

                events_path = data_home.shobr_data / "screening" / "events.jsonl"
                events = [json.loads(line) for line in events_path.read_text().splitlines() if line]
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["posting_id"], "5550000001")
                self.assertEqual(events[0]["score"], 4)

                result = shobr("screen-llm-next", "--print-prompt", env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("no leads to screen", result.stdout)

                result = shobr("screened", env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("  - LLM: 4", result.stdout)
                self.assertIn("  - LLM reasoning:\n      fake llm says good fit", result.stdout)
                self.assertNotIn("Human reasoning", result.stdout)

    def test_screen_llm_garbage_reply_records_nothing(self) -> None:
        """screen-llm (live) on an unparseable reply writes nothing and exits
        nonzero."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {**data_home.env, **seed_test_profile(data_home)}
            with (data_home.shobr_config / "profile" / "user-detail.md").open(
                "a", encoding="utf-8"
            ) as fh:
                fh.write("\nSHOBR-FAKE-GARBAGE\n")

            with FakeLLMServer() as llm_server:
                env = {**env, **llm_server.env}

                result = shobr("screen-llm", "5550000001", env=env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("could not parse", result.stderr)
                self.assertFalse((data_home.shobr_data / "screening").exists())

    def test_screen_llm_next_garbage_reply_reports_failure(self) -> None:
        """screen-llm-next (live) on an unparseable reply writes nothing and
        exits nonzero (total scoring failure is not silent success)."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {**data_home.env, **seed_test_profile(data_home)}
            with (data_home.shobr_config / "profile" / "user-detail.md").open(
                "a", encoding="utf-8"
            ) as fh:
                fh.write("\nSHOBR-FAKE-GARBAGE\n")

            with FakeLLMServer() as llm_server:
                env = {**env, **llm_server.env}

                result = shobr("screen-llm-next", env=env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("could not score posting 5550000001", result.stderr)
                self.assertFalse((data_home.shobr_data / "screening").exists())

    def test_screen_llm_refuses_human_reviewed_posting(self) -> None:
        """screen-llm refuses a posting that already has a human review."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {**data_home.env, **seed_test_profile(data_home)}

            seed_human_review(data_home)

            result = shobr("screen-llm", "5550000001", env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("already has a human review", result.stderr)

            events = [
                json.loads(line)
                for line in (data_home.shobr_data / "screening" / "events.jsonl")
                .read_text()
                .splitlines()
                if line
            ]
            self.assertEqual(len(events), 1)

    def test_screen_llm_next_skips_human_reviewed(self) -> None:
        """screen-llm-next reports no leads when the only passing lead already
        has a human review."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {**data_home.env, **seed_test_profile(data_home)}

            seed_human_review(data_home)

            result = shobr("screen-llm-next", "--print-prompt", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("no leads to screen", result.stdout)

    def test_screen_llm_print_prompt_unfilled_template(self) -> None:
        """screen-llm --print-prompt refuses profile files that still carry
        the scaffold marker instead of scoring template boilerplate."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = data_home.env
            result = shobr("setup", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            cv_path = data_home.path / "resume.md"
            cv_path.write_text(TEST_RESUME_MD)
            env = {**env, "SHOBR_MAIN_CV_PATH": str(cv_path)}

            result = shobr("screen-llm", "5550000001", "--print-prompt", env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("template", result.stderr)
            self.assertIn("user-detail.md", result.stderr)

    def test_screen_llm_all_scores_everything(self) -> None:
        """screen-llm-all scores every enriched passing lead with no AI
        review yet, then reports nothing left."""
        with TestDataHome(name="screen-llm-all") as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {**data_home.env, **seed_test_profile(data_home)}

            with FakeLLMServer() as llm_server:
                env = {**env, **llm_server.env}

                result = shobr("screen-llm-all", env=env)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("Scored 1", result.stdout)

                events_path = data_home.shobr_data / "screening" / "events.jsonl"
                events = [json.loads(line) for line in events_path.read_text().splitlines() if line]
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["kind"], "ai_review")

                result = shobr("screen-llm-all", env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Scored 0", result.stdout)

    def test_screen_llm_all_retries_then_fails(self) -> None:
        """screen-llm-all retries an unparseable reply and reports the
        posting it could not score."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {**data_home.env, **seed_test_profile(data_home)}
            with (data_home.shobr_config / "profile" / "user-detail.md").open(
                "a", encoding="utf-8"
            ) as fh:
                fh.write("\nSHOBR-FAKE-GARBAGE\n")

            with FakeLLMServer() as llm_server:
                env = {**env, **llm_server.env}

                result = shobr("screen-llm-all", env=env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Failed to score 1", result.stderr)
                self.assertIn("5550000001", result.stdout)
                self.assertEqual(len(llm_server.calls), 3)
                self.assertFalse((data_home.shobr_data / "screening").exists())

    def test_screen_llm_next_print_prompt(self) -> None:
        """screen-llm-next --print-prompt prints the prompt for the oldest
        enriched passing lead with no AI review yet, without touching any LLM."""
        with TestDataHome(name="screen-llm-next") as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {**data_home.env, **seed_test_profile(data_home)}
            # Prove hermeticity: even a bogus LLM endpoint must not matter,
            # since --print-prompt never calls out.
            env = {**env, "OPENAI_BASE_URL": "http://127.0.0.1:1/v1"}

            result = shobr("screen-llm-next", "--print-prompt", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                "Senior Software Engineer, Widget Platform",
                result.stdout,
            )
            self.assertIn("Test Candidate", result.stdout)
            self.assertIn('{"reasoning"', result.stdout)

    def test_tailor_print_prompt(self) -> None:
        """tailor --print-prompt prints all three stage prompts (profile +
        enriched posting) without touching any LLM or the cv project."""
        with TestDataHome(name="tailor-prompt") as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {**data_home.env, **seed_test_profile(data_home)}

            # Bogus LLM endpoint and missing cv dir must not matter, since
            # --print-prompt never calls out.
            env = {
                **env,
                "OPENAI_BASE_URL": "http://127.0.0.1:1/v1",
                "SHOBR_CV_TOOLCHAIN_DIR": "/nonexistent-cv-dir",
            }

            seed_human_review(data_home)

            result = shobr("tailor", "5550000001", "--print-prompt", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Senior Software Engineer, Widget Platform", result.stdout)
            self.assertIn("Test Candidate", result.stdout)
            self.assertIn("=== PROMPT: TAILOR-EDITS ===", result.stdout)
            self.assertNotIn("--- PROMPT:", result.stdout)
            self.assertIn("TAILOR-EDITS", result.stdout)
            self.assertNotIn("TAILOR-VERIFY", result.stdout)
            self.assertIn("=== PROMPT: TAILOR-COVER ===", result.stdout)
            self.assertIn("=== PROMPT: TAILOR-REWRITE ===", result.stdout)
            self.assertIn('"resume_md"', result.stdout)
            self.assertNotIn('"notes"', result.stdout)
            self.assertNotIn('"dropped"', result.stdout)
            self.assertIn("Emphasize experience which is relevant", result.stdout)
            self.assertIn("Keep the file's existing structure exactly", result.stdout)
            self.assertIn("already fills its page limit", result.stdout)
            self.assertNotIn("most relevant experience leads", result.stdout)
            self.assertIn("Keep it no longer than 3 paragraphs", result.stdout)
            self.assertIn("already tailored to this specific job posting", result.stdout)
            self.assertNotIn("in the candidate's voice as shown", result.stdout)
            self.assertIn("tailored resume.md not yet generated", result.stdout)
            self.assertIn('"cover_md"', result.stdout)
            self.assertIn("--- BEGIN RESUME GUIDE ---", result.stdout)
            self.assertIn("Keep AI and RAG", result.stdout)
            self.assertIn("--- BEGIN COVER GUIDE ---", result.stdout)
            self.assertIn("Direct tone", result.stdout)
            self.assertIn("very close to fitting, so a small", result.stdout)
            self.assertIn("renders past its page limit", result.stdout)
            self.assertIn("earliest employer", result.stdout)
            self.assertNotIn("keep all employers", result.stdout)
            self.assertNotIn('"cut"', result.stdout)

    def test_tailor_next_print_prompt(self) -> None:
        """tailor-next --print-prompt prints the prompts for the oldest pursue
        row with no package yet, without touching any LLM."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {**data_home.env, **seed_test_profile(data_home)}
            env = {
                **env,
                "OPENAI_BASE_URL": "http://127.0.0.1:1/v1",
                "SHOBR_CV_TOOLCHAIN_DIR": "/nonexistent-cv-dir",
            }
            seed_human_review(data_home)

            result = shobr("tailor-next", "--print-prompt", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("TAILOR-EDITS", result.stdout)
            self.assertIn("Test Candidate", result.stdout)

    def test_tailor_wraps_long_lines(self) -> None:
        """tailor-next folds long LLM prose lines to 80 columns in resume.md,
        leaving front matter alone."""
        with TestDataHome(name="tailor-wrap") as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {
                **data_home.env,
                **seed_test_profile(data_home),
                **seed_test_cv(data_home),
            }
            with (data_home.shobr_config / "profile" / "user-detail.md").open(
                "a", encoding="utf-8"
            ) as fh:
                fh.write("\nSHOBR-FAKE-LONGLINE\n")
            seed_human_review(data_home)

            with FakeLLMServer() as llm_server:
                env = {**env, **llm_server.env}

                result = shobr("tailor-next", env=env)
                self.assertEqual(result.returncode, 0, result.stderr)

                resume = (
                    data_home.path / "cv" / "applications" / "fakeco-5550000001" / "resume.md"
                ).read_text()
                self.assertIn("deliberately very long bullet line", resume)
                self.assertIn("cloud-native compounds", resume)
                self.assertNotIn("- - ", resume)
                self.assertIn(
                    "## FakeCo With An Extremely Long Employer Name Beyond "
                    "Eighty Columns | Senior Engineer",
                    resume,
                )
                self.assertIn(
                    "title: A Deliberately Very Long Job Title That Exceeds "
                    "Eighty Columns Even After The Key Prefix",
                    resume,
                )
                in_front_matter = False
                for line in resume.splitlines():
                    if line == "---":
                        in_front_matter = not in_front_matter
                        continue
                    if in_front_matter or line.startswith("#") or "http" in line or " " not in line:
                        continue
                    self.assertLessEqual(len(line), 80, line)

    def test_tailor_missing_toolchain_dir_fails_loudly(self) -> None:
        """tailor-next on a config without cv_toolchain_dir errors naming it."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            seed_test_config(
                data_home,
                CONFIG_DEFAULT.replace(
                    '# CV Toolchain home\ncv_toolchain_dir = "~/shobr-resumes"\n', ""
                ),
            )
            env = {
                **data_home.env,
                **seed_test_profile(data_home),
                **seed_test_cv(data_home),
            }
            seed_human_review(data_home)

            with FakeLLMServer() as llm_server:
                env = {**env, **llm_server.env}

                result = shobr("tailor-next", env=env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("config.toml: 'cv_toolchain_dir'", result.stderr)

    def test_tailor_setup_declined_aborts(self) -> None:
        """tailor-next without a toolchain dir prompts; answering no aborts
        nonzero with nothing created."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            seed_test_config(data_home)
            missing = data_home.path / "no-such-cv"
            env = {
                **data_home.env,
                **seed_test_profile(data_home),
                "SHOBR_CV_TOOLCHAIN_DIR": str(missing),
            }
            seed_human_review(data_home)

            with FakeLLMServer() as llm_server:
                env = {**env, **llm_server.env}

                result = shobr("tailor-next", env=env, input_text="n\n")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("install roffume", result.stdout + result.stderr)
                self.assertFalse(missing.exists())
                self.assertFalse((data_home.shobr_data / "tailoring").exists())

    def test_tailor_setup_eof_aborts(self) -> None:
        """tailor-next without a toolchain dir and no stdin answer aborts
        nonzero (EOF counts as no)."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            seed_test_config(data_home)
            missing = data_home.path / "no-such-cv"
            env = {
                **data_home.env,
                **seed_test_profile(data_home),
                "SHOBR_CV_TOOLCHAIN_DIR": str(missing),
            }
            seed_human_review(data_home)

            with FakeLLMServer() as llm_server:
                env = {**env, **llm_server.env}

                result = shobr("tailor-next", env=env, input_text="")
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(missing.exists())
                self.assertFalse((data_home.shobr_data / "tailoring").exists())

    def test_tailor_setup_accepted_clones_and_carries_on(self) -> None:
        """tailor-next without a toolchain dir prompts; answering yes clones
        the pinned roffume and carries on with the package."""
        with TestDataHome(name="tailor-setup") as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            seed_test_config(data_home)
            missing = data_home.path / "no-such-cv"
            env = {
                **data_home.env,
                **seed_test_profile(data_home),
                "SHOBR_CV_TOOLCHAIN_DIR": str(missing),
            }
            seed_human_review(data_home)

            with FakeLLMServer() as llm_server:
                env = {**env, **llm_server.env}

                result = shobr("tailor-next", env=env, input_text="y\n")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Built package:", result.stdout)
                self.assertTrue((missing / "new-application").is_file())
                app_dir = missing / "applications" / "fakeco-5550000001"
                self.assertIn("tailored bullet for testing", (app_dir / "resume.md").read_text())

    def test_tailor_next_builds_package(self) -> None:
        """tailor-next (live) builds the application package through the fake
        LLM and the real cv toolchain, then records a tailor_package event."""
        with TestDataHome(name="tailor-live") as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {
                **data_home.env,
                **seed_test_profile(data_home),
                **seed_test_cv(data_home),
            }
            seed_human_review(data_home)

            with FakeLLMServer() as llm_server:
                env = {**env, **llm_server.env}

                result = shobr("tailor-next", env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Built package:", result.stdout)
                self.assertIn(
                    "Sent tailor-edits request for lead [FakeCo] 5550000001",
                    result.stdout,
                )
                self.assertIn("Sent tailor-cover request.", result.stdout)
                self.assertIn("fakeco-5550000001", result.stdout)
                self.assertIn("Rewrites: 0", result.stdout)
                self.assertIn("wrote to", result.stdout)

                cv_dir = data_home.path / "cv"
                app_dir = cv_dir / "applications" / "fakeco-5550000001"
                self.assertTrue((app_dir / "resume.md").is_file())
                self.assertIn("tailored bullet for testing", (app_dir / "resume.md").read_text())
                self.assertTrue((app_dir / "cover-letter.md").is_file())
                self.assertIn("fake cover for testing", (app_dir / "cover-letter.md").read_text())
                for line in (app_dir / "cover-letter.md").read_text().splitlines():
                    self.assertLessEqual(len(line), 80, line)
                notes = (app_dir / "notes.md").read_text()
                self.assertIn("https://www.linkedin.com/jobs/view/5550000001", notes)
                self.assertIn("Decision: PURSUE", notes)
                self.assertIn("hand-written human review", notes)
                self.assertIn("**Mission**", notes)
                self.assertIn("leading provider of widget infrastructure", notes)
                self.assertNotIn("UNTRACKED", notes)
                self.assertTrue((app_dir / "Test_Candidate_ENGLISH.pdf").is_file())
                self.assertFalse((app_dir / "resume_spanish.md").exists())

                events_path = data_home.shobr_data / "tailoring" / "events.jsonl"
                events = [json.loads(line) for line in events_path.read_text().splitlines() if line]
                self.assertEqual(len(events), 1)
                event = events[0]
                self.assertEqual(event["posting_id"], "5550000001")
                self.assertEqual(event["slug"], "fakeco-5550000001")
                self.assertEqual(event["rewrites"], 0)
                self.assertNotIn(".", event["tailored_at"], "timestamps truncated")

                tailored = json.loads(
                    (data_home.shobr_data / "tailoring" / "tailoring.json").read_text()
                )
                row = tailored["rows"]["5550000001"]
                self.assertEqual(row["slug"], "fakeco-5550000001")
                self.assertEqual(row["resume_md"], (app_dir / "resume.md").read_text())
                self.assertEqual(row["cover_md"], (app_dir / "cover-letter.md").read_text())
                self.assertIn("tailored bullet for testing", row["resume_md"])
                self.assertIn("fake cover for testing", row["cover_md"])

                log = subprocess.run(
                    ["git", "log", "--oneline"],
                    cwd=cv_dir,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0)
                self.assertGreaterEqual(len(log.stdout.splitlines()), 3)
                tailor_lines = [
                    line for line in log.stdout.splitlines() if "tailor fakeco-5550000001" in line
                ]
                self.assertGreaterEqual(len(tailor_lines), 3)
                for line in tailor_lines:
                    self.assertIn("SHOBR automated commit:", line)

                status = subprocess.run(
                    ["git", "status", "--porcelain"],
                    cwd=cv_dir,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(status.stdout.strip(), "")

    def test_tailor_all_builds_packages(self) -> None:
        """tailor-all builds every pursue package with no package yet."""
        with TestDataHome(name="tailor-all") as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {
                **data_home.env,
                **seed_test_profile(data_home),
                **seed_test_cv(data_home),
            }
            seed_human_review(data_home)
            seed_minimal_lead(data_home, posting_id="5550000011")
            seed_human_review(data_home, posting_id="5550000011", score=5)
            refresh_enrichment(data_home, posting_id="5550000011")
            with FakeLLMServer() as llm_server:
                result = shobr("tailor-all", env={**env, **llm_server.env})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Built 2 packages.", result.stdout)
            tailored = json.loads(
                (data_home.shobr_data / "tailoring" / "tailoring.json").read_text()
            )
            self.assertEqual(set(tailored["rows"]), {"5550000001", "5550000011"})

    def test_tailor_all_skips_stale(self) -> None:
        """tailor-all skips stale rows with a preflight line."""
        with TestDataHome(name="tailor-all-stale") as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {
                **data_home.env,
                **seed_test_profile(data_home),
                **seed_test_cv(data_home),
            }
            seed_human_review(data_home)
            seed_minimal_lead(data_home, posting_id="5550000011")
            seed_human_review(data_home, posting_id="5550000011", score=5)
            with FakeLLMServer() as llm_server:
                result = shobr("tailor-all", env={**env, **llm_server.env})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("1 of 2 rows", result.stdout)
                self.assertIn("Built 1 packages.", result.stdout)
            tailored = json.loads(
                (data_home.shobr_data / "tailoring" / "tailoring.json").read_text()
            )
            self.assertEqual(set(tailored["rows"]), {"5550000001"})

    def test_tailor_all_force_builds_stale(self) -> None:
        """tailor-all --force builds stale packages too."""
        with TestDataHome(name="tailor-all-force") as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {
                **data_home.env,
                **seed_test_profile(data_home),
                **seed_test_cv(data_home),
            }
            seed_human_review(data_home)
            seed_enrichment_event(
                data_home,
                posting_id="5550000001",
                fetched_at="2020-01-01T00:00:00+00:00",
            )
            with FakeLLMServer() as llm_server:
                result = shobr("tailor-all", "--force", env={**env, **llm_server.env})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Built 1 packages.", result.stdout)

    def test_tailor_garbage_reply_builds_nothing(self) -> None:
        """tailor (live) on an unparseable reply writes nothing: no app dir,
        no events."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {
                **data_home.env,
                **seed_test_profile(data_home),
                **seed_test_cv(data_home),
            }
            with (data_home.shobr_config / "profile" / "user-detail.md").open(
                "a", encoding="utf-8"
            ) as fh:
                fh.write("\nSHOBR-FAKE-GARBAGE\n")
            seed_human_review(data_home)

            with FakeLLMServer() as llm_server:
                env = {**env, **llm_server.env}

                result = shobr("tailor", "5550000001", env=env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("could not parse", result.stderr)
                app_dir = data_home.path / "cv" / "applications" / "fakeco-5550000001"
                self.assertFalse(app_dir.exists())
                self.assertFalse((data_home.shobr_data / "tailoring").exists())

    def test_tailor_refuses_dirty_cv(self) -> None:
        """tailor-next refuses when the cv worktree has uncommitted changes,
        leaving no app dir and no tailoring events."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {
                **data_home.env,
                **seed_test_profile(data_home),
                **seed_test_cv(data_home),
                "OPENAI_BASE_URL": "http://127.0.0.1:1/v1",
            }
            with (data_home.path / "cv" / "resume.md").open("a", encoding="utf-8") as fh:
                fh.write("\nUncommitted test edit.\n")
            seed_human_review(data_home)

            result = shobr("tailor-next", env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("uncommitted changes", result.stderr)
            app_dir = data_home.path / "cv" / "applications" / "fakeco-5550000001"
            self.assertFalse(app_dir.exists())
            self.assertFalse((data_home.shobr_data / "tailoring").exists())

    def test_tailor_bloat_rewrites_to_fit(self) -> None:
        """tailor-next (live) on an overflowing first draft loops through a
        real verify failure into a rewrite and succeeds."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {
                **data_home.env,
                **seed_test_profile(data_home),
                **seed_test_cv(data_home),
            }
            with (data_home.shobr_config / "profile" / "user-detail.md").open(
                "a", encoding="utf-8"
            ) as fh:
                fh.write("\nSHOBR-FAKE-BLOAT\n")
            seed_human_review(data_home)

            with FakeLLMServer() as llm_server:
                env = {**env, **llm_server.env}

                result = shobr("tailor-next", env=env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Rewrites: 1", result.stdout)
                self.assertIn("Sent tailor-rewrite request (attempt 1).", result.stdout)

                cv_dir = data_home.path / "cv"
                app_dir = cv_dir / "applications" / "fakeco-5550000001"
                self.assertIn(
                    "final cover for testing",
                    (app_dir / "cover-letter.md").read_text(),
                )

                events_path = data_home.shobr_data / "tailoring" / "events.jsonl"
                events = [json.loads(line) for line in events_path.read_text().splitlines() if line]
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["rewrites"], 1)

    def test_tailor_guards(self) -> None:
        """tailor refuses unknown ids, non-pursue rows, and packaged rows."""
        with TestDataHome() as data_home:
            env = {**data_home.env, **seed_test_profile(data_home)}

            result = shobr("tailor", "0000000", env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("no enrichment data yet", result.stderr)

            restore_snapshot(data_home, snapshot_screen_base())

            result = shobr("tailor", "0000000", env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not found in enrichment store", result.stderr)

            result = shobr("tailor", "5550000001", env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not marked pursue", result.stderr)

            seed_human_review(data_home)
            seed_tailored_package(data_home)

            result = shobr("tailor", "5550000001", env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("already has a package", result.stderr)

    def test_tailored_prints_stored_rows(self) -> None:
        """'shobr tailored' prints the projected tailoring store, one row per
        packaged posting."""
        with TestDataHome(name="tailored-print") as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {
                **data_home.env,
                **seed_test_profile(data_home),
                **seed_test_cv(data_home),
            }
            seed_human_review(data_home)

            with FakeLLMServer() as llm_server:
                env = {**env, **llm_server.env}

                result = shobr("tailor-next", env=env)
                self.assertEqual(result.returncode, 0, result.stderr)

            result = shobr("tailored", env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("1 tailored", result.stdout)
            self.assertIn("[FakeCo] 5550000001", result.stdout)
            self.assertIn("fakeco-5550000001", result.stdout)
            self.assertIn("Rewrites: 0", result.stdout)

    def test_tailored_with_no_data(self) -> None:
        """'shobr tailored' on a fresh data home fails, writes nothing."""
        with TestDataHome() as data_home:
            result = shobr("tailored", env=data_home.env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("no tailoring data yet", result.stderr)
            tailoring_dir = data_home.shobr_data / "tailoring"
            self.assertFalse((tailoring_dir / "events.jsonl").exists())
            self.assertFalse((tailoring_dir / "tailoring.json").exists())

    def test_notifications_local_with_no_data(self) -> None:
        """notifications --local on a fresh data home fails offline, writes nothing."""
        with TestDataHome() as data_home:
            result = shobr("notifications", "--local", env=data_home.env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("no notifications data yet", result.stderr)
            notif_dir = data_home.shobr_data / "notifications"
            self.assertFalse((notif_dir / "events.jsonl").exists())
            self.assertFalse((notif_dir / "notifications.json").exists())

    def test_shobr_fails_without_instance(self) -> None:
        """With no beachpatrol instance on the target profile, exits nonzero."""
        with TestDataHome() as data_home:
            env = {
                **data_home.env,
                "SHOBR_BEACHPATROL_PROFILE": f"test-{os.getpid()}-shobr-missing",
            }
            result = shobr("smoke-test-browser", env=env)
            self.assertNotEqual(result.returncode, 0)

    def test_track_records_status_changes(self) -> None:
        """'shobr track' records status transitions with notes; latest wins
        in the projection."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = data_home.env
            seed_tailored_package(data_home)

            result = shobr("track", "5550000001", "applied", "--note", "via portal", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("UNTRACKED", result.stdout)
            self.assertIn("APPLIED", result.stdout)

            result = shobr("track", "5550000001", "interviewing", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("APPLIED", result.stdout)
            self.assertIn("INTERVIEWING", result.stdout)

            events_path = data_home.shobr_data / "tracking" / "events.jsonl"
            events = [json.loads(line) for line in events_path.read_text().splitlines() if line]
            self.assertEqual(len(events), 2)
            self.assertEqual(events[0]["posting_id"], "5550000001")
            self.assertEqual(events[0]["status"], "applied")
            self.assertEqual(events[0]["note"], "via portal")
            self.assertIsNone(events[1]["note"])
            self.assertNotIn(".", events[0]["tracked_at"], "timestamps truncated")

            tracked = json.loads((data_home.shobr_data / "tracking" / "tracking.json").read_text())
            row = tracked["rows"]["5550000001"]
            self.assertEqual(row["status"], "interviewing")
            self.assertIsNone(row["note"])

    def test_track_guards(self) -> None:
        """track refuses missing tailoring data, unknown ids, unpackaged rows,
        and invalid statuses."""
        with TestDataHome() as data_home:
            result = shobr("track", "5550000001", "applied", env=data_home.env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("no tailoring data yet", result.stderr)

            restore_snapshot(data_home, snapshot_closed_enriched())
            env = data_home.env
            seed_tailored_package(data_home)

            result = shobr("track", "0000000", "applied", env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not found in enrichment store", result.stderr)

            result = shobr("track", "5550000002", "applied", env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("has no package yet", result.stderr)

            result = shobr("track", "5550000001", "bogus", env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("invalid choice", result.stderr)

    def test_tracked_prints_stored_rows(self) -> None:
        """'shobr tracked' prints the projected tracking store."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = data_home.env
            seed_tailored_package(data_home)

            result = shobr("track", "5550000001", "applied", "--note", "via portal", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)

            result = shobr("tracked", env=env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("1 tracked (1 applied)", result.stdout)
            self.assertIn("[FakeCo] 5550000001", result.stdout)
            self.assertIn("APPLIED", result.stdout)
            self.assertIn("via portal", result.stdout)

    def test_tracked_shows_date(self) -> None:
        """tracked shows the calendar date of each transition."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            seed_tailored_package(data_home)
            seed_tracking(data_home)
            result = shobr("tracked", env=data_home.env)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Tracked At: 2026-09-13", result.stdout)

    def test_tracked_with_no_data(self) -> None:
        """'shobr tracked' on a fresh data home fails, writes nothing."""
        with TestDataHome() as data_home:
            result = shobr("tracked", env=data_home.env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("no tracking data yet", result.stderr)
            tracking_dir = data_home.shobr_data / "tracking"
            self.assertFalse((tracking_dir / "events.jsonl").exists())
            self.assertFalse((tracking_dir / "tracking.json").exists())

    def test_review_prints_package(self) -> None:
        """'shobr review' prints screening, posting, tracking, and the stored
        resume/cover texts for a packaged posting."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = data_home.env
            seed_human_review(data_home)
            seed_tailored_package(data_home)

            result = shobr("review", "5550000001", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("[FakeCo] 5550000001", result.stdout)
            self.assertIn("Senior Software Engineer, Widget Platform", result.stdout)
            self.assertIn("Human: 4", result.stdout)
            self.assertIn("hand-written human review", result.stdout)
            self.assertIn("Decision: PURSUE", result.stdout)
            self.assertIn("UNTRACKED", result.stdout)
            self.assertIn("seeded resume", result.stdout)
            self.assertIn("seeded cover", result.stdout)
            self.assertIn("  - Description:", result.stdout)
            self.assertIn("**Mission**", result.stdout)
            self.assertIn("  - Company description:", result.stdout)
            self.assertIn("leading provider of widget infrastructure", result.stdout)

            result = shobr("track", "5550000001", "applied", "--note", "via portal", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)

            result = shobr("review", "5550000001", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("APPLIED", result.stdout)
            self.assertIn("via portal", result.stdout)
            self.assertNotIn("UNTRACKED", result.stdout)

    def test_review_next_picks_oldest_untracked(self) -> None:
        """'shobr review-next' prints the oldest packaged posting with no
        tracking event, then reports nothing once all are tracked."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = data_home.env
            seed_human_review(data_home)
            seed_tailored_package(data_home)

            result = shobr("review-next", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("[FakeCo] 5550000001", result.stdout)

            result = shobr("track", "5550000001", "applied", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)

            result = shobr("review-next", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("nothing to review", result.stdout)

    def test_review_guards(self) -> None:
        """review refuses missing tailoring data, unknown ids, and unpackaged
        rows."""
        with TestDataHome() as data_home:
            result = shobr("review", "5550000001", env=data_home.env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("no tailoring data yet", result.stderr)

            restore_snapshot(data_home, snapshot_closed_enriched())
            env = data_home.env
            seed_tailored_package(data_home)

            result = shobr("review", "0000000", env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not found in enrichment store", result.stderr)

            result = shobr("review", "5550000002", env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("has no package yet", result.stderr)

    def test_next_empty_home(self) -> None:
        """'shobr next' on a fresh data home offers a fetch, then idles."""
        with TestDataHome() as data_home:
            result = shobr("next", env=data_home.env, input_text="n\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Fetch new leads?", result.stdout)
            self.assertIn("(shobr discover) [y/N]", result.stdout)
            self.assertNotIn("jobs-search", result.stdout)
            self.assertIn("nothing to do", result.stdout)
            self.assertFalse((data_home.shobr_data / "discovery").exists())

            # Closed stdin (EOF) aborts like Ctrl+C, but exits 0 since
            # quitting via EOF is deliberate.
            result = shobr("next", env=data_home.env, input_text="")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertIn("interrupted", result.stderr)
            self.assertNotIn("nothing to do", result.stdout)

            # Ctrl+C at the prompt exits 130 with no traceback.
            proc = subprocess.Popen(
                _cmd("next"),
                cwd=ROOT,
                env=data_home.env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert proc.stdout is not None
            # Wait for the prompt itself instead of sleeping: the sleep
            # either wastes time or races startup.
            seen = ""
            deadline = time.monotonic() + 30
            while "Fetch new leads?" not in seen:
                if time.monotonic() > deadline:
                    proc.kill()
                    self.fail("timed out waiting for the next prompt")
                char = proc.stdout.read(1)
                if not char:
                    self.fail("shobr exited before prompting")
                seen += char
            proc.send_signal(signal.SIGINT)
            # Wait for the signal death before reaping output:
            # communicate() closes stdin, and that EOF could otherwise win
            # the race against SIGINT and exit 0 via the EOF path.
            try:
                self.assertEqual(proc.wait(timeout=60), 130)
            except subprocess.TimeoutExpired:
                proc.kill()
                raise AssertionError("shobr ignored SIGINT") from None
            stdout, stderr = proc.communicate(timeout=60)
            self.assertNotIn("Traceback", stderr)
            self.assertIn("interrupted", stderr)

    def test_next_reports_enrichment(self) -> None:
        """'shobr next' with unenriched passing leads reports the enrich
        step without fetching anything."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = data_home.env

            result = shobr("next", env=env, input_text="n\n" * 10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("awaiting enrichment", result.stdout)
            self.assertIn("enrich-next", result.stdout)

            enriched = json.loads(
                (data_home.shobr_data / "enrichment" / "enrichment.json").read_text()
            )
            self.assertEqual(len(enriched["rows"]), 1)

    def test_next_reports_screening(self) -> None:
        """'shobr next' with an unscreened enriched lead reports the screen
        step without scoring anything."""
        with TestDataHome() as data_home:
            seed_minimal_lead(data_home)
            refresh_enrichment(data_home)
            env = data_home.env

            result = shobr("next", env=env, input_text="n\n" * 10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("awaiting screening", result.stdout)
            self.assertIn(
                "1 enriched leads awaiting screening (1 lacking LLM screening).", result.stdout
            )
            self.assertIn("screen-next", result.stdout)
            self.assertFalse((data_home.shobr_data / "screening").exists())

    def test_next_reports_tailoring(self) -> None:
        """'shobr next' with an unpackaged pursue row reports the tailor step
        without building anything."""
        with TestDataHome() as data_home:
            seed_minimal_lead(data_home)
            seed_human_review(data_home, posting_id="1234567890")
            env = data_home.env

            result = shobr("next", env=env, input_text="n\n" * 10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("awaiting tailoring", result.stdout)
            self.assertIn("tailor-next", result.stdout)
            self.assertFalse((data_home.shobr_data / "tailoring").exists())

    def test_next_reviews_oldest_untracked(self) -> None:
        """'shobr next' with an untracked package prints it via review-next."""
        with TestDataHome() as data_home:
            seed_minimal_lead(data_home)
            seed_human_review(data_home, posting_id="1234567890")
            seed_tailored_package(data_home, posting_id="1234567890")
            env = data_home.env

            result = shobr("next", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("[TestCo] 1234567890", result.stdout)

    def test_next_nothing_left(self) -> None:
        """'shobr next' with everything tracked reports nothing to do."""
        with TestDataHome() as data_home:
            seed_minimal_lead(data_home)
            seed_human_review(data_home, posting_id="1234567890")
            seed_tailored_package(data_home, posting_id="1234567890")
            env = data_home.env

            result = shobr("track", "1234567890", "applied", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)

            result = shobr("next", env=env, input_text="n\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Fetch new leads?", result.stdout)
            self.assertIn("nothing to do", result.stdout)

    def test_next_fetches_leads_on_yes(self) -> None:
        """'shobr next' answering yes with nothing pending fetches new leads."""
        with TestDataHome(name="next-fetch") as data_home:
            env = data_home.env
            with FakeLinkedInServer() as server:
                profile = f"test-next-fetch-{os.getpid()}"
                with TestBeachpatrolInstance(env, profile):
                    env = {
                        **env,
                        "SHOBR_BEACHPATROL_PROFILE": profile,
                        "SHOBR_LINKEDIN_BASE_URL": server.url,
                    }
                    result = shobr("setup", env=env)
                    seed_test_config(data_home)
                    self.assertEqual(result.returncode, 0, result.stderr)

                    result = shobr("next", env=env, input_text="y\n")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    leads = json.loads(
                        (data_home.shobr_data / "discovery" / "discovery.json").read_text()
                    )
                    self.assertEqual(len(leads["rows"]), 12)

    def test_next_runs_enrich_on_yes(self) -> None:
        """'shobr next' answering yes at enrichment fetches the oldest lead."""
        with TestDataHome(name="next-enrich") as data_home:
            env = data_home.env
            pages: dict[str, str | list[str]] = {
                "/jobs/view/5550000001": _read_fixture("job-detail.html")
            }
            with FakeLinkedInServer(pages) as server:
                profile = f"test-next-enrich-{os.getpid()}"
                with TestBeachpatrolInstance(env, profile):
                    env = {
                        **env,
                        "SHOBR_BEACHPATROL_PROFILE": profile,
                        "SHOBR_LINKEDIN_BASE_URL": server.url,
                    }
                    result = shobr("setup", env=env)
                    seed_test_config(data_home)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    result = shobr("discover", env=env)
                    self.assertEqual(result.returncode, 0, result.stderr)

                    result = shobr("next", env=env, input_text="y\n")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    enriched = json.loads(
                        (data_home.shobr_data / "enrichment" / "enrichment.json").read_text()
                    )
                    self.assertIn("5550000001", enriched["rows"])
                    self.assertTrue(enriched["rows"]["5550000001"]["actionable"])

    def test_next_scores_with_llm_on_l(self) -> None:
        """'shobr next' answering l at screening records an AI review."""
        with TestDataHome() as data_home:
            seed_minimal_lead(data_home)
            refresh_enrichment(data_home)
            env = {**data_home.env, **seed_test_profile(data_home)}
            with FakeLLMServer() as llm_server:
                env = {**env, **llm_server.env}

                result = shobr("next", env=env, input_text="l\n")
                self.assertEqual(result.returncode, 0, result.stderr)

                events_path = data_home.shobr_data / "screening" / "events.jsonl"
                events = [json.loads(line) for line in events_path.read_text().splitlines() if line]
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["score"], 4)

    def test_next_screens_human_on_h(self) -> None:
        """'shobr next' answering h at screening records a human review."""
        with TestDataHome() as data_home:
            seed_minimal_lead(data_home)
            refresh_enrichment(data_home)
            env = add_editor_to_env(
                {**data_home.env, **seed_test_profile(data_home)},
                "SHOBR_SCORE: 4\nSHOBR_REASONING: editor ok\n",
            )

            result = shobr("next", env=env, input_text="h\n")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            events_path = data_home.shobr_data / "screening" / "events.jsonl"
            events = [json.loads(line) for line in events_path.read_text().splitlines() if line]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["score"], 4)
            self.assertEqual(events[0]["reasoning"], "editor ok")

    def test_next_human_only_when_llm_done(self) -> None:
        """'shobr next' hides the LLM option once every pending lead already
        has an AI review."""
        with TestDataHome() as data_home:
            seed_minimal_lead(data_home)
            refresh_enrichment(data_home)
            env = add_editor_to_env(
                {**data_home.env, **seed_test_profile(data_home)},
                "SHOBR_SCORE: 4\nSHOBR_REASONING: editor ok\n",
            )
            screening_dir = data_home.shobr_data / "screening"
            screening_dir.mkdir(parents=True, exist_ok=True)
            (screening_dir / "events.jsonl").write_text(
                json.dumps(
                    {
                        "scored_at": "2026-09-13T00:00:00+00:00",
                        "posting_id": "1234567890",
                        "kind": "ai_review",
                        "score": 4,
                        "reasoning": "fake llm says good fit",
                    }
                )
                + "\n"
            )

            result = shobr("next", env=env, input_text="h\n")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotIn("screen-llm-next", result.stdout)
            self.assertIn("1 enriched leads awaiting screening.", result.stdout)
            self.assertNotIn("lacking LLM screening", result.stdout)

            events_path = data_home.shobr_data / "screening" / "events.jsonl"
            events = [json.loads(line) for line in events_path.read_text().splitlines() if line]
            self.assertEqual(len(events), 2)
            self.assertEqual(events[1]["score"], 4)
            self.assertEqual(events[1]["reasoning"], "editor ok")

    def test_next_builds_package_on_yes(self) -> None:
        """'shobr next' answering yes at tailoring builds the package."""
        with TestDataHome(name="next-tailor") as data_home:
            seed_minimal_lead(data_home)
            env = {
                **data_home.env,
                **seed_test_profile(data_home),
                **seed_test_cv(data_home),
            }
            seed_human_review(data_home, posting_id="1234567890")
            refresh_enrichment(data_home, posting_id="1234567890")
            with FakeLLMServer() as llm_server:
                env = {**env, **llm_server.env}

                result = shobr("next", env=env, input_text="y\n")
                self.assertEqual(result.returncode, 0, result.stderr)

                events_path = data_home.shobr_data / "tailoring" / "events.jsonl"
                events = [json.loads(line) for line in events_path.read_text().splitlines() if line]
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["posting_id"], "1234567890")

    def test_next_declined_falls_through(self) -> None:
        """'shobr next' declining the only pending stage reports nothing done."""
        with TestDataHome() as data_home:
            seed_minimal_lead(data_home)
            env = data_home.env

            result = shobr("next", env=env, input_text="n\n" * 10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("nothing to do", result.stdout)
            self.assertFalse((data_home.shobr_data / "screening").exists())

    def test_next_id_unknown(self) -> None:
        """'shobr next <id>' on an unknown posting id fails loudly."""
        with TestDataHome() as data_home:
            result = shobr("next", "0000000", env=data_home.env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not found", result.stderr)

    def test_next_id_rejected(self) -> None:
        """'shobr next <id>' on a filtered-out posting fails loudly."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_closed_enriched())
            result = shobr("next", "5550000002", env=data_home.env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("did not pass the pre-filter", result.stderr)

    def test_next_id_prefers_enrichment_verdict(self) -> None:
        """'shobr next <id>' proceeds when enrichment passes, even though the
        discovery card failed (unstructured location, structured Remote)."""
        with TestDataHome() as data_home:
            seed_test_config(data_home)
            _append_event(
                data_home.shobr_data / "discovery" / "events.jsonl",
                {
                    "fetched_at": "2026-09-13T00:00:00+00:00",
                    "rows": [
                        {
                            "posting_id": "5550000031",
                            "posting_url": "https://www.linkedin.com/jobs/view/5550000031",
                            "title": "Test Engineer",
                            "company": "TestCo",
                            "location": "Nowhere",
                        }
                    ],
                },
            )
            seed_enrichment_event(data_home, posting_id="5550000031")
            refresh_enrichment(data_home, posting_id="5550000031")
            env = {**data_home.env, **seed_test_profile(data_home)}
            with FakeLLMServer() as llm_server:
                result = shobr(
                    "next", "5550000031", env={**env, **llm_server.env}, input_text="l\n"
                )
                self.assertEqual(result.returncode, 0, result.stderr)
            events_path = data_home.shobr_data / "screening" / "events.jsonl"
            events = [json.loads(line) for line in events_path.read_text().splitlines() if line]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["score"], 4)

    def test_next_id_declined_enrich(self) -> None:
        """'shobr next <id>' declining the enrich prompt does nothing."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = data_home.env

            result = shobr("next", "5550000012", env=env, input_text="n\n")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("nothing to do for 5550000012", result.stdout)
            enriched = json.loads(
                (data_home.shobr_data / "enrichment" / "enrichment.json").read_text()
            )
            self.assertEqual(len(enriched["rows"]), 1)

    def test_next_id_enriches_on_yes(self) -> None:
        """'shobr next <id>' answering yes enriches that posting."""
        detail = _read_fixture("job-detail.html")
        profile = f"test-{os.getpid()}-shobr-next-id-enrich"
        with TestDataHome(name="next-id-enrich") as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = data_home.env
            result = shobr("setup", env=env)
            seed_test_config(data_home)

            with FakeLinkedInServer({"/jobs/view/5550000012": detail}) as server:
                with TestBeachpatrolInstance(env, profile):
                    env = {
                        **env,
                        "SHOBR_BEACHPATROL_PROFILE": profile,
                        "SHOBR_LINKEDIN_BASE_URL": server.url,
                    }
                    result = shobr("next", "5550000012", env=env, input_text="y\n")
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    enriched = json.loads(
                        (data_home.shobr_data / "enrichment" / "enrichment.json").read_text()
                    )
                    self.assertIn("5550000012", enriched["rows"])

    def test_next_id_screens_human_on_h(self) -> None:
        """'shobr next <id>' answering h records a human review for it."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = add_editor_to_env(
                data_home.env,
                "SHOBR_SCORE: 4\nSHOBR_REASONING: editor ok\n",
            )

            result = shobr("next", "5550000001", env=env, input_text="h\n")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            events_path = data_home.shobr_data / "screening" / "events.jsonl"
            events = [json.loads(line) for line in events_path.read_text().splitlines() if line]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["posting_id"], "5550000001")
            self.assertEqual(events[0]["score"], 4)

    def test_next_id_scores_llm_on_l(self) -> None:
        """'shobr next <id>' answering l records an AI review for it."""
        with TestDataHome() as data_home:
            seed_minimal_lead(data_home)
            refresh_enrichment(data_home)
            env = {**data_home.env, **seed_test_profile(data_home)}

            with FakeLLMServer() as llm_server:
                env = {**env, **llm_server.env}

                result = shobr("next", "1234567890", env=env, input_text="l\n")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

                events_path = data_home.shobr_data / "screening" / "events.jsonl"
                events = [json.loads(line) for line in events_path.read_text().splitlines() if line]
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["posting_id"], "1234567890")
                self.assertEqual(events[0]["kind"], "ai_review")

    def test_next_id_tailors_on_yes(self) -> None:
        """'shobr next <id>' answering yes builds the package for it."""
        with TestDataHome(name="next-id-tailor") as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {
                **data_home.env,
                **seed_test_profile(data_home),
                **seed_test_cv(data_home),
            }
            seed_human_review(data_home)

            with FakeLLMServer() as llm_server:
                env = {**env, **llm_server.env}

                result = shobr("next", "5550000001", env=env, input_text="y\n")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

                app_dir = data_home.path / "cv" / "applications" / "fakeco-5550000001"
                self.assertTrue((app_dir / "resume.md").is_file())

    def test_next_id_reviews_untracked(self) -> None:
        """'shobr next <id>' on a packaged untracked posting reviews it
        without prompting."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = data_home.env
            seed_human_review(data_home)
            seed_tailored_package(data_home)

            result = shobr("next", "5550000001", env=env, input_text="")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("FakeCo", result.stdout)

    def test_next_id_nothing_left(self) -> None:
        """'shobr next <id>' on a fully tracked posting reports nothing."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = data_home.env
            seed_human_review(data_home)
            seed_tailored_package(data_home)
            seed_tracking(data_home)

            result = shobr("next", "5550000001", env=env, input_text="")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("nothing to do for 5550000001", result.stdout)

    def test_next_id_skipped(self) -> None:
        """'shobr next <id>' on a skipped posting reports nothing."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = data_home.env
            seed_human_review(data_home, score=1)

            result = shobr("next", "5550000001", env=env, input_text="")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("nothing to do for 5550000001", result.stdout)

    def test_status_empty_home(self) -> None:
        """'shobr status' on a fresh data home prints zeros and writes nothing."""
        with TestDataHome() as data_home:
            result = shobr("status", env=data_home.env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("SHOBR STATUS", result.stdout)
            self.assertIn("- DISCOVERY", result.stdout)
            self.assertIn("- ENRICHMENT", result.stdout)
            self.assertIn("- SCREENING", result.stdout)
            self.assertIn("- TAILORING", result.stdout)
            self.assertIn("- TRACKING", result.stdout)
            self.assertIn("Total Leads Found:", result.stdout)
            self.assertIn("Packages Built:", result.stdout)
            self.assertIn("Applied:", result.stdout)
            self.assertFalse((data_home.shobr_data / "tracking").exists())

    def test_status_funnel(self) -> None:
        """'shobr status' aggregates every stage store into one funnel."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = data_home.env
            seed_human_review(data_home)
            seed_tailored_package(data_home)

            result = shobr("track", "5550000001", "applied", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)

            result = shobr("status", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("SHOBR STATUS", result.stdout)
            self.assertIn("  - Total Leads Found:     12", result.stdout)
            self.assertIn("Pending Enrichment:", result.stdout)
            self.assertIn("  - Total Enriched:        1", result.stdout)
            self.assertIn("  - Total Screened:        1", result.stdout)
            self.assertIn("  - Pending Tailoring:     0", result.stdout)
            self.assertIn("  - Packages Built:        1", result.stdout)
            self.assertIn("  - Pending Review:        0", result.stdout)
            self.assertIn("  - Applied:               1", result.stdout)
            self.assertIn("Lacking LLM Review:", result.stdout)
            self.assertIn("LLM Scores:", result.stdout)
            self.assertIn("Human Scores:", result.stdout)
            self.assertIn("4: 1", result.stdout)

    def test_status_histogram_marks_untailored(self) -> None:
        """The human histogram notes pursue rows still awaiting tailoring."""
        with TestDataHome() as data_home:
            seed_minimal_lead(data_home, posting_id="1111111111")
            seed_minimal_lead(data_home, posting_id="2222222222")
            seed_human_review(data_home, posting_id="1111111111", score=5)
            seed_human_review(data_home, posting_id="2222222222", score=3)
            seed_tailored_package(data_home, posting_id="2222222222")
            result = shobr("status", env=data_home.env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("5: 1 (1 to tailor)", result.stdout)
            self.assertIn("3: 1\n", result.stdout)

    def test_status_omits_invalidated_tailoring(self) -> None:
        """status stops counting a pursue row closed by a later enrichment."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {**data_home.env, **seed_test_profile(data_home)}
            seed_human_review(data_home)
            result = shobr("status", env=env)
            self.assertRegex(_strip_ansi(result.stdout), r"Pending Tailoring:\s+1")

            seed_enrichment_event(
                data_home,
                posting_id="5550000001",
                accepting_applications=False,
                fetched_at="2026-09-14T00:00:00+00:00",
            )
            result = shobr("status", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertRegex(_strip_ansi(result.stdout), r"Pending Tailoring:\s+0")

    def test_next_skips_invalidated_tailoring(self) -> None:
        """next stops offering tailor for a pursue row closed by re-enrichment."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {**data_home.env, **seed_test_profile(data_home)}
            seed_human_review(data_home)
            result = shobr("next", env=env, input_text="n\n" * 10)
            self.assertIn("Tailor oldest", result.stdout)

            seed_enrichment_event(
                data_home,
                posting_id="5550000001",
                accepting_applications=False,
                fetched_at="2026-09-14T00:00:00+00:00",
            )
            result = shobr("next", env=env, input_text="n\n" * 10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("Tailor oldest", result.stdout)

    def test_tailor_next_reports_invalidated(self) -> None:
        """tailor-next finds nothing to do once the pursue row is closed."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {**data_home.env, **seed_test_profile(data_home)}
            seed_human_review(data_home)
            seed_enrichment_event(
                data_home,
                posting_id="5550000001",
                accepting_applications=False,
                fetched_at="2026-09-14T00:00:00+00:00",
            )
            result = shobr("tailor-next", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("nothing to tailor", result.stdout)

    def test_review_next_skips_invalidated(self) -> None:
        """review-next skips a packaged row closed by a later enrichment."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {**data_home.env, **seed_test_profile(data_home)}
            seed_human_review(data_home)
            seed_tailored_package(data_home)
            result = shobr("review-next", env=env)
            self.assertIn("FakeCo", result.stdout)

            seed_enrichment_event(
                data_home,
                posting_id="5550000001",
                accepting_applications=False,
                fetched_at="2026-09-14T00:00:00+00:00",
            )
            result = shobr("review-next", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("nothing to review", result.stdout)

    def test_screen_refuses_invalidated_with_reason(self) -> None:
        """screening a closed row names the closure reason, not just the gate."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {**data_home.env, **seed_test_profile(data_home)}
            seed_enrichment_event(
                data_home,
                posting_id="5550000001",
                accepting_applications=False,
                fetched_at="2026-09-14T00:00:00+00:00",
            )
            result = shobr("screen", "5550000001", "5", "--reason", "x", env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(
                "did not pass the pre-filter (no longer accepting applications)",
                result.stderr,
            )

    def test_screened_flags_closed(self) -> None:
        """screened tags a row closed by a later enrichment with (CLOSED)."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {**data_home.env, **seed_test_profile(data_home)}
            seed_human_review(data_home)
            result = shobr("screened", env=env)
            self.assertNotIn("(CLOSED)", result.stdout)

            seed_enrichment_event(
                data_home,
                posting_id="5550000001",
                accepting_applications=False,
                fetched_at="2026-09-14T00:00:00+00:00",
            )
            result = shobr("screened", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("(CLOSED)", result.stdout)

    def test_screened_closed_takes_precedence_over_filtered(self) -> None:
        """a both-closed-and-filtered row shows (CLOSED), never (REJECTED:)."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {**data_home.env, **seed_test_profile(data_home)}
            seed_human_review(data_home)
            seed_enrichment_event(
                data_home,
                posting_id="5550000001",
                accepting_applications=False,
                fetched_at="2026-09-14T00:00:00+00:00",
            )
            filters_path = data_home.shobr_config / "config.toml"
            with filters_path.open("a", encoding="utf-8") as fh:
                fh.write('\nengineer = "engineer"\n')
            result = shobr("screened", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("(CLOSED)", result.stdout)
            self.assertNotIn("REJECTED", result.stdout)

    def test_tailored_flags_closed(self) -> None:
        """tailored tags a packaged row closed by a later enrichment."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {**data_home.env, **seed_test_profile(data_home)}
            seed_human_review(data_home)
            seed_tailored_package(data_home)
            seed_enrichment_event(
                data_home,
                posting_id="5550000001",
                accepting_applications=False,
                fetched_at="2026-09-14T00:00:00+00:00",
            )
            result = shobr("tailored", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("(CLOSED)", result.stdout)

    def test_tracked_flags_closed(self) -> None:
        """tracked tags a tracked row closed by a later enrichment, keeping Status."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {**data_home.env, **seed_test_profile(data_home)}
            seed_human_review(data_home)
            seed_tailored_package(data_home)
            result = shobr("track", "5550000001", "applied", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            seed_enrichment_event(
                data_home,
                posting_id="5550000001",
                accepting_applications=False,
                fetched_at="2026-09-14T00:00:00+00:00",
            )
            result = shobr("tracked", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("(CLOSED)", result.stdout)
            self.assertIn("Status:", result.stdout)
            self.assertNotIn("REJECTED", result.stdout)

    def test_screened_flags_filtered(self) -> None:
        """screened shows the rejection reason for a filter-flipped row."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {**data_home.env, **seed_test_profile(data_home)}
            seed_human_review(data_home)
            result = shobr("screened", env=env)
            self.assertNotIn("REJECTED", result.stdout)

            filters_path = data_home.shobr_config / "config.toml"
            with filters_path.open("a", encoding="utf-8") as fh:
                fh.write('\nengineer = "engineer"\n')
            result = shobr("screened", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("(REJECTED: title contains 'engineer')", _strip_ansi(result.stdout))

    def test_tailored_flags_filtered(self) -> None:
        """tailored shows the rejection reason for a filter-flipped package."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {**data_home.env, **seed_test_profile(data_home)}
            seed_human_review(data_home)
            seed_tailored_package(data_home)
            filters_path = data_home.shobr_config / "config.toml"
            with filters_path.open("a", encoding="utf-8") as fh:
                fh.write('\nengineer = "engineer"\n')
            result = shobr("tailored", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("(REJECTED: title contains 'engineer')", _strip_ansi(result.stdout))

    def test_tracked_flags_filtered(self) -> None:
        """tracked shows the rejection reason, keeping Status, for flipped rows."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {**data_home.env, **seed_test_profile(data_home)}
            seed_human_review(data_home)
            seed_tailored_package(data_home)
            result = shobr("track", "5550000001", "applied", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            filters_path = data_home.shobr_config / "config.toml"
            with filters_path.open("a", encoding="utf-8") as fh:
                fh.write('\nengineer = "engineer"\n')
            result = shobr("tracked", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            out = _strip_ansi(result.stdout)
            self.assertIn("(REJECTED: title contains 'engineer')", out)
            self.assertIn("Status:", out)

    def test_status_shows_closed_counts(self) -> None:
        """status marks closed totals inline, once per reached stage."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {**data_home.env, **seed_test_profile(data_home)}
            seed_human_review(data_home)
            seed_tailored_package(data_home)
            result = shobr("track", "5550000001", "applied", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = shobr("status", env=env)
            self.assertNotIn("closed", _strip_ansi(result.stdout))

            seed_enrichment_event(
                data_home,
                posting_id="5550000001",
                accepting_applications=False,
                fetched_at="2026-09-14T00:00:00+00:00",
            )
            result = shobr("status", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            out = _strip_ansi(result.stdout)
            self.assertEqual(out.count("(1 closed)"), 3)
            self.assertRegex(out, r"Total Screened:\s+1 \(1 closed\)")
            self.assertRegex(out, r"Packages Built:\s+1 \(1 closed\)")
            self.assertRegex(out, r"Applied:\s+1 \(1 closed\)")

    def test_status_counts_each_row_once_closed_first(self) -> None:
        """status splits un-actionable rows into closed vs filtered, no doubles."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {**data_home.env, **seed_test_profile(data_home)}
            seed_human_review(data_home)
            seed_minimal_lead(data_home, posting_id="5550000011")
            seed_human_review(data_home, posting_id="5550000011", score=5)
            seed_enrichment_event(
                data_home,
                posting_id="5550000001",
                accepting_applications=False,
                fetched_at="2026-09-14T00:00:00+00:00",
            )
            filters_path = data_home.shobr_config / "config.toml"
            with filters_path.open("a", encoding="utf-8") as fh:
                fh.write('\nengineer = "engineer"\n')

            result = shobr("status", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            out = _strip_ansi(result.stdout)
            self.assertRegex(out, r"Total Screened:\s+2 \(1 closed, 1 filtered\)")
            self.assertRegex(out, r"Pending Tailoring:\s+0")

    def test_enriched_shows_checked_date(self) -> None:
        """enriched rows show the last-check date from the latest event."""
        with TestDataHome() as data_home:
            seed_test_config(data_home)
            env = data_home.env
            seed_enrichment_event(
                data_home,
                posting_id="5550000001",
                fetched_at="2026-09-14T00:00:00+00:00",
            )
            result = shobr("enriched", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Last Enriched At: 2026-09-14", result.stdout)
            self.assertIn("(STALE)", result.stdout)

    def test_screened_shows_checked_date(self) -> None:
        """screened rows show the last-check date, refreshed by re-enrichment."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {**data_home.env, **seed_test_profile(data_home)}
            seed_human_review(data_home)
            seed_enrichment_event(
                data_home,
                posting_id="5550000001",
                fetched_at="2026-09-14T00:00:00+00:00",
            )
            result = shobr("screened", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Last Enriched At: 2026-09-14", result.stdout)
            self.assertIn("(STALE)", result.stdout)
            self.assertIn("Reviewed At: 2026-09-13", result.stdout)

    def test_tailored_shows_checked_date(self) -> None:
        """tailored rows show the last-check date from the enrichment."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {**data_home.env, **seed_test_profile(data_home)}
            seed_human_review(data_home)
            seed_tailored_package(data_home)
            seed_enrichment_event(
                data_home,
                posting_id="5550000001",
                fetched_at="2026-09-14T00:00:00+00:00",
            )
            result = shobr("tailored", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Last Enriched At: 2026-09-14", result.stdout)
            self.assertIn("(STALE)", result.stdout)
            self.assertIn("Tailored At: 2026-09-13", result.stdout)

    def test_editor_template_shows_checked_date(self) -> None:
        """the $EDITOR template carries the last-check date in Job Details."""
        with TestDataHome() as data_home:
            seed_test_config(data_home)
            env = {**data_home.env, **seed_test_profile(data_home)}
            seed_enrichment_event(
                data_home,
                posting_id="5550000001",
                fetched_at="2026-09-14T00:00:00+00:00",
            )
            template_capture = data_home.path / "template-captured.md"
            env_editor = add_editor_to_env(
                env,
                "SHOBR_SCORE: 3\nSHOBR_REASONING: human agrees\n",
                capture_to=template_capture,
            )
            result = shobr("screen", "5550000001", "--force", env=env_editor)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("Last Enriched At: 2026-09-14", template_capture.read_text())
            self.assertIn("(STALE)", template_capture.read_text())

    def test_tracked_shows_action_and_check_dates(self) -> None:
        """tracked keeps the action Date and adds Last Enriched At."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {**data_home.env, **seed_test_profile(data_home)}
            seed_human_review(data_home)
            seed_tailored_package(data_home)
            result = shobr("track", "5550000001", "applied", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            seed_enrichment_event(
                data_home,
                posting_id="5550000001",
                fetched_at="2020-01-01T00:00:00+00:00",
            )
            result = shobr("tracked", env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertRegex(result.stdout, r"Tracked At: \d{4}-\d{2}-\d{2}")
            self.assertIn("Last Enriched At: 2020-01-01", result.stdout)
            self.assertIn("(STALE)", result.stdout)

    def test_review_warns_stale_package(self) -> None:
        """review warns with a re-enrich command for a stale package."""
        with TestDataHome() as data_home:
            seed_minimal_lead(data_home, posting_id="5550000001")
            seed_tailored_package(data_home)
            result = shobr("review", "5550000001", env=data_home.env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Stale:", result.stdout)
            self.assertIn("shobr enrich 5550000001", result.stdout)

    def test_review_custom_threshold_quiets_old(self) -> None:
        """a huge stale_after_days keeps an old package quiet."""
        with TestDataHome() as data_home:
            seed_minimal_lead(data_home, posting_id="5550000001")
            seed_tailored_package(data_home)
            filters_path = data_home.shobr_config / "config.toml"
            filters_path.write_text(
                filters_path.read_text(encoding="utf-8").replace(
                    "stale_after_days = 2",
                    "stale_after_days = 36500",
                ),
                encoding="utf-8",
            )
            result = shobr("review", "5550000001", env=data_home.env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("Stale:", result.stdout)

    def test_review_bad_threshold_fails_loudly(self) -> None:
        """a non-integer stale_after_days fails naming the key."""
        with TestDataHome() as data_home:
            seed_minimal_lead(data_home, posting_id="5550000001")
            seed_tailored_package(data_home)
            filters_path = data_home.shobr_config / "config.toml"
            filters_path.write_text(
                filters_path.read_text(encoding="utf-8").replace(
                    "stale_after_days = 2",
                    'stale_after_days = "soon"',
                ),
                encoding="utf-8",
            )
            result = shobr("review", "5550000001", env=data_home.env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("stale_after_days", result.stderr)

    def test_stale_threshold_boundary(self) -> None:
        """2 days old is fresh, 3 days old is stale (default threshold)."""
        with TestDataHome() as data_home:
            seed_test_config(data_home)
            now = datetime.now(UTC)
            old = (now - timedelta(days=3)).isoformat(timespec="seconds")
            edge = (now - timedelta(days=2)).isoformat(timespec="seconds")
            seed_enrichment_event(data_home, posting_id="5550000011", fetched_at=edge)
            seed_tailored_package(data_home, posting_id="5550000011")
            seed_enrichment_event(data_home, posting_id="5550000022", fetched_at=old)
            _append_event(
                data_home.shobr_data / "tailoring" / "events.jsonl",
                {
                    "tailored_at": "2026-09-13T00:00:00+00:00",
                    "posting_id": "5550000022",
                    "slug": "testco-5550000022",
                    "app_dir": "/tmp/5550000022",
                    "rewrites": 0,
                    "resume_md": "seeded resume",
                    "cover_md": "seeded cover",
                },
            )

            result = shobr("review", "5550000011", env=data_home.env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("Stale:", result.stdout)

            result = shobr("review", "5550000022", env=data_home.env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Stale:", result.stdout)

    def test_next_offers_reenrich_for_stale(self) -> None:
        """next offers (and on yes runs) re-enrichment for a stale pending row."""
        with TestDataHome() as data_home:
            seed_minimal_lead(data_home)
            result = shobr("setup", env=data_home.env)
            self.assertEqual(result.returncode, 0, result.stderr)
            profile = f"test-{os.getpid()}-shobr-next-reenrich"
            detail = _read_fixture("job-detail.html")
            with FakeLinkedInServer({"/jobs/view/1234567890": detail}) as server:
                with TestBeachpatrolInstance(data_home.env, profile):
                    env = {
                        **data_home.env,
                        "SHOBR_BEACHPATROL_PROFILE": profile,
                        "SHOBR_LINKEDIN_BASE_URL": server.url,
                    }
                    result = shobr("next", env=env, input_text="y\n")
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("Re-enrich it first?", result.stdout)
                    self.assertIn("1234567890", result.stdout)
            rows = json.loads(
                (data_home.shobr_data / "enrichment" / "enrichment.json").read_text()
            )["rows"]
            self.assertNotEqual(rows["1234567890"]["enriched_last_at"], "2026-09-13T00:00:00+00:00")

    def test_next_declines_reenrich(self) -> None:
        """declining the re-enrich offer leaves the stale row untouched."""
        with TestDataHome() as data_home:
            seed_minimal_lead(data_home)
            env = data_home.env
            result = shobr("next", env=env, input_text="n\n" * 10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Re-enrich it first?", result.stdout)
            self.assertIn("(1 stale)", result.stdout)
            events = (data_home.shobr_data / "enrichment" / "events.jsonl").read_text()
            self.assertEqual(len([line for line in events.splitlines() if line]), 1)

    def test_next_offers_reenrich_before_tailoring(self) -> None:
        """next offers re-enrichment for a stale tailorable row too."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {**data_home.env, **seed_test_profile(data_home)}
            seed_human_review(data_home)
            seed_enrichment_event(
                data_home,
                posting_id="5550000001",
                fetched_at="2020-01-01T00:00:00+00:00",
            )
            result = shobr("next", env=env, input_text="n\n" * 10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Re-enrich it first?", result.stdout)

    def test_next_refreshes_stale_before_fresh_tailor(self) -> None:
        """next offers the stale tailorable row even when the oldest is fresh."""
        with TestDataHome() as data_home:
            seed_minimal_lead(data_home, posting_id="5550000012")
            refresh_enrichment(data_home, posting_id="5550000012")
            seed_minimal_lead(data_home, posting_id="5550000011")
            seed_enrichment_event(
                data_home,
                posting_id="5550000011",
                fetched_at="2020-01-01T00:00:00+00:00",
            )
            seed_human_review(data_home, posting_id="5550000012")
            seed_human_review(data_home, posting_id="5550000011")
            result = shobr("next", env=data_home.env, input_text="n\n" * 10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Re-enrich it first?", result.stdout)
            self.assertIn("shobr enrich 5550000011", result.stdout)

    def test_next_refreshes_stale_before_fresh_screen(self) -> None:
        """next offers the stale unscreened row even when the oldest is fresh."""
        with TestDataHome() as data_home:
            seed_minimal_lead(data_home, posting_id="5550000012")
            refresh_enrichment(data_home, posting_id="5550000012")
            seed_minimal_lead(data_home, posting_id="5550000011")
            seed_enrichment_event(
                data_home,
                posting_id="5550000011",
                fetched_at="2020-01-01T00:00:00+00:00",
            )
            screening_dir = data_home.shobr_data / "screening"
            screening_dir.mkdir(parents=True, exist_ok=True)
            with (screening_dir / "events.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "scored_at": "2026-09-13T00:00:00+00:00",
                            "posting_id": "5550000011",
                            "kind": "ai_review",
                            "score": 4,
                            "reasoning": "fake ai review",
                        }
                    )
                    + "\n"
                )
            result = shobr("next", env=data_home.env, input_text="n\n" * 10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Re-enrich it first?", result.stdout)
            self.assertIn("shobr enrich 5550000011", result.stdout)

    def test_screen_llm_next_refuses_stale(self) -> None:
        """screen-llm-next refuses a stale row, naming re-enrich and --force."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {**data_home.env, **seed_test_profile(data_home)}
            seed_enrichment_event(
                data_home,
                posting_id="5550000001",
                fetched_at="2020-01-01T00:00:00+00:00",
            )
            with FakeLLMServer() as llm_server:
                result = shobr("screen-llm-next", env={**env, **llm_server.env})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("shobr enrich 5550000001", result.stderr)
                self.assertIn("--force", result.stderr)

    def test_screen_llm_next_force_scores_stale(self) -> None:
        """screen-llm-next --force scores a stale row quietly."""
        with TestDataHome() as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {**data_home.env, **seed_test_profile(data_home)}
            seed_enrichment_event(
                data_home,
                posting_id="5550000001",
                fetched_at="2020-01-01T00:00:00+00:00",
            )
            with FakeLLMServer() as llm_server:
                result = shobr("screen-llm-next", "--force", env={**env, **llm_server.env})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Recorded AI review:", result.stdout)
                self.assertNotIn("Stale:", result.stdout)

    def test_screen_next_refuses_stale(self) -> None:
        """screen-next refuses a stale row, naming re-enrich and --force."""
        with TestDataHome() as data_home:
            seed_minimal_lead(data_home)
            env = data_home.env
            result = shobr("screen-next", env=env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("shobr enrich 1234567890", result.stderr)
            self.assertIn("--force", result.stderr)

    def test_screen_next_force_reviews_stale(self) -> None:
        """screen-next --force records a human review on a stale row."""
        with TestDataHome() as data_home:
            seed_minimal_lead(data_home)
            env_editor = add_editor_to_env(
                data_home.env,
                "SHOBR_SCORE: 4\nSHOBR_REASONING: editor ok\n",
            )
            result = shobr("screen-next", "--force", env=env_editor)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            events_path = data_home.shobr_data / "screening" / "events.jsonl"
            events = [json.loads(line) for line in events_path.read_text().splitlines() if line]
            self.assertEqual(len(events), 1)

    def test_track_warns_stale(self) -> None:
        """track warns but records on a stale package."""
        with TestDataHome() as data_home:
            seed_minimal_lead(data_home)
            seed_tailored_package(data_home, posting_id="1234567890")
            result = shobr("track", "1234567890", "applied", env=data_home.env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Stale:", result.stdout)
            self.assertIn("shobr enrich 1234567890", result.stdout)

    def test_status_marks_stale_pendings(self) -> None:
        """pending lines flag their own stale counts, each different."""
        with TestDataHome() as data_home:
            seed_minimal_lead(data_home, posting_id="5550000011")
            seed_human_review(data_home, posting_id="5550000011", score=5)
            seed_minimal_lead(data_home, posting_id="5550000022")
            for pid in ("5550000031", "5550000032"):
                seed_minimal_lead(data_home, posting_id=pid)
                seed_human_review(data_home, posting_id=pid, score=5)
                refresh_enrichment(data_home, posting_id=pid)
            for pid in ("5550000033", "5550000034"):
                seed_minimal_lead(data_home, posting_id=pid)
                seed_human_review(data_home, posting_id=pid, score=5)
                refresh_enrichment(data_home, posting_id=pid)
            seed_tailored_package(data_home, posting_id="5550000033")
            _append_event(
                data_home.shobr_data / "tailoring" / "events.jsonl",
                {
                    "tailored_at": "2026-09-13T00:00:00+00:00",
                    "posting_id": "5550000034",
                    "slug": "testco-5550000034",
                    "app_dir": "/tmp/5550000034",
                    "rewrites": 0,
                    "resume_md": "seeded resume",
                    "cover_md": "seeded cover",
                },
            )
            result = shobr("status", env=data_home.env)
            self.assertEqual(result.returncode, 0, result.stderr)
            out = _strip_ansi(result.stdout)
            self.assertRegex(out, r"Pending Screening:\s+1 \(1 stale\)")
            self.assertRegex(out, r"Pending Tailoring:\s+3 \(1 stale\)")
            self.assertRegex(out, r"Pending Review:\s+2\n")

    def test_tailor_next_refuses_stale(self) -> None:
        """tailor-next refuses a stale row instead of building."""
        with TestDataHome(name="tailor-stale") as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {
                **data_home.env,
                **seed_test_profile(data_home),
                **seed_test_cv(data_home),
            }
            seed_human_review(data_home)
            seed_enrichment_event(
                data_home,
                posting_id="5550000001",
                fetched_at="2020-01-01T00:00:00+00:00",
            )
            with FakeLLMServer() as llm_server:
                result = shobr("tailor-next", env={**env, **llm_server.env})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("shobr enrich 5550000001", result.stderr)
                self.assertNotIn("Built package:", result.stdout)

    def test_tailor_next_force_builds_stale(self) -> None:
        """tailor-next --force builds a stale package quietly."""
        with TestDataHome(name="tailor-force") as data_home:
            restore_snapshot(data_home, snapshot_screen_base())
            env = {
                **data_home.env,
                **seed_test_profile(data_home),
                **seed_test_cv(data_home),
            }
            seed_human_review(data_home)
            seed_enrichment_event(
                data_home,
                posting_id="5550000001",
                fetched_at="2020-01-01T00:00:00+00:00",
            )
            with FakeLLMServer() as llm_server:
                result = shobr("tailor-next", "--force", env={**env, **llm_server.env})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Built package:", result.stdout)
                self.assertNotIn("Stale:", result.stdout)

    def test_screen_llm_all_skips_stale(self) -> None:
        """screen-llm-all skips stale rows with a preflight line."""
        with TestDataHome() as data_home:
            seed_test_config(data_home)
            env = {**data_home.env, **seed_test_profile(data_home)}
            seed_enrichment_event(
                data_home,
                posting_id="5550000011",
                company="StaleCo",
                fetched_at="2020-01-01T00:00:00+00:00",
            )
            seed_enrichment_event(
                data_home,
                posting_id="5550000022",
                company="FreshCo",
                fetched_at="2099-01-01T00:00:00+00:00",
            )
            with FakeLLMServer() as llm_server:
                result = shobr("screen-llm-all", env={**env, **llm_server.env})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("1 of 2 rows to score", result.stdout)
                self.assertIn("Scored 1 leads.", result.stdout)
                self.assertIn("FreshCo", result.stdout)

    def test_screen_llm_all_force_scores_stale(self) -> None:
        """screen-llm-all --force scores stale rows too, fresh first."""
        with TestDataHome() as data_home:
            seed_test_config(data_home)
            env = {**data_home.env, **seed_test_profile(data_home)}
            seed_enrichment_event(
                data_home,
                posting_id="5550000011",
                company="StaleCo",
                fetched_at="2020-01-01T00:00:00+00:00",
            )
            seed_enrichment_event(
                data_home,
                posting_id="5550000022",
                company="FreshCo",
                fetched_at="2099-01-01T00:00:00+00:00",
            )
            with FakeLLMServer() as llm_server:
                result = shobr("screen-llm-all", "--force", env={**env, **llm_server.env})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Scored 2 leads.", result.stdout)
                self.assertLess(result.stdout.index("FreshCo"), result.stdout.index("StaleCo"))


def _test_method_names() -> list[str]:
    """TestCLI method names in unittest's default (alphabetical) order."""
    return sorted(name for name in dir(TestCLI) if name.startswith("test_"))


def _run_test_ids(ids: list[str], keep_outputs: bool) -> tuple[int, str]:
    """Run test ids in one subprocess; return (returncode, stderr)."""
    env = dict(os.environ)
    if keep_outputs:
        env["SHOBR_KEEP_OUTPUTS"] = "1"
    try:
        result = subprocess.run(
            [sys.executable, "-m", "unittest", *(f"test.TestCLI.{name}" for name in ids)],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
        )
        return result.returncode, result.stderr
    except Exception as e:
        return 1, f"harness error: {e!r}"


def _run_parallel(workers: int, keep_outputs: bool) -> int:
    """Run the suite in fixed round-robin chunks across a worker pool.

    One subprocess per chunk (not per test): snapshots build once per worker
    and are reused by its tests, which per-test subprocesses would defeat.
    """
    import concurrent.futures

    names = _test_method_names()
    # One chunk per worker: snapshots build once per chunk process and are
    # reused by its tests. Empty chunks are dropped (a bare unittest
    # invocation would discover the whole suite).
    chunks = [names[i::workers] for i in range(workers)]
    chunks = [chunk for chunk in chunks if chunk]
    failures = 0
    started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_run_test_ids, chunk, keep_outputs): chunk for chunk in chunks}
        for future in concurrent.futures.as_completed(futures):
            returncode, stderr = future.result()
            chunk = futures[future]
            print(
                f"{'ok' if returncode == 0 else 'FAIL'}: {len(chunk)} tests "
                f"({chunk[0]} ... {chunk[-1]})",
                flush=True,
            )
            if returncode != 0:
                failures += 1
                print(stderr)
    elapsed = time.monotonic() - started
    summary = "OK" if failures == 0 else f"{failures} FAILED"
    print(f"ran {len(names)} tests in {elapsed:.1f}s with {workers} workers: {summary}")
    return 1 if failures else 0


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="End-to-end test suite for shobr.")
    parser.add_argument(
        "--keep-outputs",
        action="store_true",
        help="keep per-test data homes under ./test-output/",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=min(4, os.cpu_count() or 1),
        help="parallel workers (1 runs the suite sequentially in-process)",
    )
    parser.add_argument("tests", nargs="*", help="optional test ids (forces sequential)")
    _args = parser.parse_args()

    if _args.workers <= 1 or _args.tests:
        sys.argv = [sys.argv[0], *_args.tests]
        unittest.main()
    else:
        sys.exit(_run_parallel(_args.workers, _args.keep_outputs))
