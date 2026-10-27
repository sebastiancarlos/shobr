"""User configuration (`config.toml` file setup)."""

import functools
import os
import re
import tomllib
from pathlib import Path
from typing import TypedDict, cast

from .core import (
    SHOBR_CONFIG_DIR,
    EmploymentType,
    LocationType,
    ShobrError,
    fold_accents,
    get_template,
    shobr_command,
    short_path,
)

CONFIG_PATH = SHOBR_CONFIG_DIR / "config.toml"


class Config(TypedDict):
    """Validated shobr configuration."""

    titles: list[str]
    workplace_types: list[str]
    geo: list[str]
    geo_ids: dict[str, str]
    reject_title: dict[str, re.Pattern[str]]
    presence_locations: list[str]
    reject_employment_type: list[str]
    cv_toolchain_dir: str
    profile_dir: str
    main_cv_path: str | None
    beachpatrol_profile: str | None
    beachpatrol_browser: str
    stale_after_days: int
    reject_recent_application_days: int
    auto_skip_llm_max: int
    providers: dict[str, dict[str, object]]


_KNOWN_CONFIG_KEYS = frozenset(Config.__annotations__)
_OVERRIDABLE_KEYS = _KNOWN_CONFIG_KEYS - {"providers"}


def _str_list(data: dict, key: str, *, required: bool = False, non_empty: bool = True) -> list[str]:
    """Parse a key from config.toml which should be a list of strings."""
    if key not in data:
        if required:
            raise ShobrError(f"config.toml: missing '{key}'")
        return []
    values = data[key]
    if not isinstance(values, list) or not all(
        isinstance(value, str) and value for value in values
    ):
        raise ShobrError(f"config.toml: '{key}' must be a list of non-empty strings")
    if non_empty and not values:
        raise ShobrError(f"config.toml: '{key}' must not be empty")
    return values


def _check_known(values: list[str], known: tuple[str, ...], noun: str) -> None:
    """Raise error naming the first value outside the known set (case-insensitive)."""
    for value in values:
        if value.lower() not in known:
            raise ShobrError(
                f"config.toml: unknown {noun} {value!r} (known: {', '.join(sorted(known))})."
            )


def _nonneg_int(data: dict, key: str, default: int) -> int:
    """Parse a key from config.toml which should be a non-negative integer."""
    raw = data.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise ShobrError(f"config.toml: '{key}' must be a non-negative integer")
    return raw


@functools.lru_cache(maxsize=1)
def _raw_config() -> dict:
    """Read config.toml verbatim, raising on a missing or invalid file."""
    if not CONFIG_PATH.is_file():
        raise ShobrError(
            f"profile file missing: {short_path(CONFIG_PATH)} "
            f"(run `{shobr_command('setup')}` to scaffold it)"
        )
    try:
        return tomllib.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ShobrError(f"config.toml: invalid TOML: {exc}") from exc


def _parse_config(data: dict) -> Config:
    """Validate one raw config table, raising on any problem."""
    unknown_keys = sorted(key for key in data if key not in _KNOWN_CONFIG_KEYS)
    if unknown_keys:
        raise ShobrError(
            f"config.toml: unknown key {unknown_keys[0]!r} "
            f"(known: {', '.join(sorted(_KNOWN_CONFIG_KEYS))})."
        )

    titles = _str_list(data, "titles", required=True, non_empty=False)
    workplace_types = _str_list(data, "workplace_types", required=True)
    _check_known(
        workplace_types,
        tuple(e.value.lower() for e in LocationType),
        "workplace type",
    )
    geo = _str_list(data, "geo")

    # parse geo_ids map, then resolve geo names against it
    raw_geos = data.get("geo_ids", {})
    if not isinstance(raw_geos, dict):
        raise ShobrError("config.toml: 'geo_ids' must be a table")
    geo_ids: dict[str, str] = {}
    for name, gid in raw_geos.items():
        if not name:
            raise ShobrError("config.toml: 'geo_ids' names must be non-empty")
        if not isinstance(gid, str) or not gid.isdigit():
            raise ShobrError(f"config.toml: bad geo id for {name!r}: {gid!r} (digits only)")
        geo_ids[name] = gid
    unknown = [name for name in geo if name not in geo_ids]
    if unknown:
        raise ShobrError(
            f"config.toml: unknown geo {unknown[0]!r} (known: {', '.join(sorted(geo_ids))}). "
            "Find more by searching the place on LinkedIn Jobs and copying geoId= from the URL."
        )

    # parse reject_title
    raw_rejects = data.get("reject_title", {})
    if not isinstance(raw_rejects, dict):
        raise ShobrError("config.toml: 'reject_title' must be a table")
    reject_title: dict[str, re.Pattern[str]] = {}
    for label, pattern in raw_rejects.items():
        if not label:
            raise ShobrError("config.toml: 'reject_title' labels must be non-empty")
        if not isinstance(pattern, str) or not pattern:
            raise ShobrError(f"config.toml: title {label!r} needs a non-empty pattern")
        try:
            reject_title[label] = re.compile(fold_accents(pattern))
        except re.error as exc:
            raise ShobrError(f"config.toml: bad regex for title {label!r}: {exc}") from exc

    presence_locations = _str_list(data, "presence_locations")
    reject_employment_type = _str_list(data, "reject_employment_type")
    _check_known(
        reject_employment_type,
        tuple(e.value.lower() for e in EmploymentType),
        "employment type",
    )

    raw_stale = _nonneg_int(data, "stale_after_days", 2)
    raw_cooldown = _nonneg_int(data, "reject_recent_application_days", 0)
    raw_auto_skip = _nonneg_int(data, "auto_skip_llm_max", 0)
    if raw_auto_skip > 5:
        raise ShobrError("config.toml: 'auto_skip_llm_max' must be between 0 and 5")

    raw_dir = data.get("cv_toolchain_dir")
    if not isinstance(raw_dir, str) or not raw_dir:
        raise ShobrError("config.toml: 'cv_toolchain_dir' must be a non-empty string")
    raw_profile_dir = data.get("profile_dir", str(SHOBR_CONFIG_DIR / "profile"))
    if not isinstance(raw_profile_dir, str) or not raw_profile_dir:
        raise ShobrError("config.toml: 'profile_dir' must be a non-empty string")
    raw_main_cv = data.get("main_cv_path")
    if raw_main_cv is not None and (not isinstance(raw_main_cv, str) or not raw_main_cv):
        raise ShobrError("config.toml: 'main_cv_path' must be a non-empty string")
    raw_profile = data.get("beachpatrol_profile")
    if raw_profile is not None and (not isinstance(raw_profile, str) or not raw_profile):
        raise ShobrError("config.toml: 'beachpatrol_profile' must be a non-empty string")
    raw_browser = data.get("beachpatrol_browser", "chromium")
    if not isinstance(raw_browser, str) or not raw_browser:
        raise ShobrError("config.toml: 'beachpatrol_browser' must be a non-empty string")

    raw_providers = data.get("providers", {})
    if not isinstance(raw_providers, dict):
        raise ShobrError("config.toml: 'providers' must be a table")

    # verify that configured providers are valid
    from .providers.base import all_providers

    known_providers = all_providers()
    providers: dict[str, dict[str, object]] = {}
    for provider_name, section in raw_providers.items():
        if not isinstance(section, dict):
            raise ShobrError(f"config.toml: [providers.{provider_name}] must be a table")
        if provider_name not in known_providers:
            raise ShobrError(
                f"config.toml: unknown provider {provider_name!r} "
                f"(known: {', '.join(sorted(known_providers))})."
            )
        providers[provider_name] = section

    return {
        "titles": titles,
        "workplace_types": workplace_types,
        "geo": geo,
        "geo_ids": geo_ids,
        "reject_title": reject_title,
        "presence_locations": presence_locations,
        "reject_employment_type": reject_employment_type,
        "cv_toolchain_dir": raw_dir,
        "profile_dir": raw_profile_dir,
        "main_cv_path": raw_main_cv,
        "beachpatrol_profile": raw_profile,
        "beachpatrol_browser": raw_browser,
        "stale_after_days": raw_stale,
        "reject_recent_application_days": raw_cooldown,
        "auto_skip_llm_max": raw_auto_skip,
        "providers": providers,
    }


@functools.lru_cache(maxsize=1)
def load_config() -> Config:
    """Read and validate config.toml, raising on any problem."""
    return _parse_config(_raw_config())


@functools.cache
def load_provider_config(name: str) -> Config:
    """The top-level config merged with `[providers.<name>]` overrides.

    Only keys shobr knows at the top level are overridable.
    """
    raw = _raw_config()
    section = raw.get("providers", {}).get(name, {})
    if not isinstance(section, dict):
        section = {}
    overrides = {key: value for key, value in section.items() if key in _OVERRIDABLE_KEYS}
    base = {key: value for key, value in raw.items() if key != "providers"}
    return _parse_config({**base, **overrides})


def provider_options(name: str) -> dict[str, object]:
    """The raw `[providers.<name>]` section, provider-specific keys included."""
    return cast(dict[str, object], load_config()["providers"].get(name, {}))


def _resolve_path(value: str) -> Path:
    """Expand ~ and resolve relative paths against the shobr config dir."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else SHOBR_CONFIG_DIR / path


def resolve_profile_dir() -> Path:
    """Resolve the profile docs directory (provider config over the default)."""
    if not CONFIG_PATH.is_file():
        return SHOBR_CONFIG_DIR / "profile"
    from .providers.base import current_config

    return _resolve_path(current_config()["profile_dir"])


def resolve_main_cv_path() -> Path:
    """Resolve the main CV (env, provider config, then toolchain/resume.md)."""
    if env_path := os.environ.get("SHOBR_MAIN_CV_PATH"):
        return _resolve_path(env_path)
    from .providers.base import current_config

    if configured := current_config()["main_cv_path"]:
        return _resolve_path(configured)
    return resolve_cv_toolchain_dir() / "resume.md"


def resolve_cv_toolchain_dir() -> Path:
    """Resolve the CV toolchain dir (env wins, else provider-aware config)."""
    if env_dir := os.environ.get("SHOBR_CV_TOOLCHAIN_DIR"):
        return _resolve_path(env_dir)
    from .providers.base import current_config

    return _resolve_path(current_config()["cv_toolchain_dir"])


def resolve_geo_ids(filters: Config) -> list[str]:
    """Resolve the configured geo names to LinkedIn geoIds."""
    return [filters["geo_ids"][name] for name in filters["geo"]]


def scaffold_config() -> bool:
    """Write the default config.toml, keeping an existing file. Return True when wrote."""
    if CONFIG_PATH.exists():
        return False
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(get_template("config.toml"), encoding="utf-8")
    return True
