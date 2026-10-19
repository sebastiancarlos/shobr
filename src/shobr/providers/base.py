"""The provider contract: one job board integration per module.

Every `shobr/providers/*.py` module exposes a module-level `PROVIDER` value.
Discovery is module-based, so a new board is one file plus its beachpatrol
commands; nothing else registers providers.
"""

import functools
import importlib
import pkgutil
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass

from ..config import Config, load_provider_config
from ..core import ShobrError

DEFAULT_PROVIDER = "linkedin"
_PROVIDERS_PACKAGE = "shobr.providers"


@dataclass(frozen=True)
class Provider(ABC):
    """One job board integration."""

    name: str
    display_name: str
    supports_tailoring: bool
    has_location: bool
    has_notifications: bool = False

    @property
    def search_command(self) -> str:
        """The beachpatrol command that parses the search-results page."""
        return f"{self.name}-job-search"

    @property
    def detail_command(self) -> str:
        """The beachpatrol command that parses the job detail page."""
        return f"{self.name}-job-detail"

    @property
    def notifications_command(self) -> str:
        """The beachpatrol command that parses the notifications page."""
        return f"{self.name}-notifications"

    @abstractmethod
    def search_url(self, filters: Config) -> str:
        """The search-results URL for one discovery fetch."""

    @abstractmethod
    def nav_url(self, posting_id: str, posting_url: str) -> str:
        """The URL to navigate to when fetching a posting's detail page."""

    @abstractmethod
    def stored_url(self, target: str, posting_id: str) -> str:
        """The canonical URL stored for a posting."""

    @abstractmethod
    def extract_posting_id(self, target: str) -> str:
        """The posting id from a bare id or a posting URL."""


@functools.cache
def all_providers() -> dict[str, Provider]:
    """Discover every provider module, keyed by provider name."""
    package = importlib.import_module(_PROVIDERS_PACKAGE)
    found: dict[str, Provider] = {}
    for info in pkgutil.iter_modules(package.__path__):
        if info.name.startswith("_") or info.name == "base":
            continue
        module = importlib.import_module(f"{_PROVIDERS_PACKAGE}.{info.name}")
        provider = getattr(module, "PROVIDER", None)
        if isinstance(provider, Provider):
            found[provider.name] = provider
    return found


def get_provider(name: str) -> Provider:
    """Look up a provider by name, raising on an unknown name."""
    providers = all_providers()
    try:
        return providers[name]
    except KeyError:
        raise ShobrError(
            f"unknown provider {name!r} (known: {', '.join(sorted(providers))})"
        ) from None


_current_provider_name = DEFAULT_PROVIDER


def set_provider(name: str) -> Provider:
    """Select the process-wide provider for the rest of the run."""
    global _current_provider_name
    provider = get_provider(name)
    _current_provider_name = provider.name
    return provider


def current_provider() -> Provider:
    """The provider selected for this process (LinkedIn by default)."""
    return get_provider(_current_provider_name)


def current_config() -> Config:
    """The config for the selected provider (top level plus overrides)."""
    return load_provider_config(current_provider().name)


def row_provider(row: Mapping[str, object]) -> str:
    """A stored row's provider; every row shobr writes carries one."""
    value = row.get("provider")
    if not isinstance(value, str) or not value:
        raise ShobrError(f"row for posting {row.get('posting_id')!r} has no provider")
    return value
