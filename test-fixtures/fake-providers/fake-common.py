"""Synthetic shobr provider copied into a temporary package by the E2E suite."""

import os
import re

from ..config import Config  # pyright: ignore[reportMissingImports]
from ..core import ShobrError  # pyright: ignore[reportMissingImports]
from .base import Provider  # pyright: ignore[reportMissingImports]


class FakeProvider(Provider):
    """A synthetic board used by the E2E suite."""

    def _base(self) -> str:
        """Return the fake board's base URL."""
        return os.environ.get("SHOBR_FAKE_BASE_URL", f"https://{self.name}.example.test")

    def search_url(self, filters: Config) -> str:
        """Return the fake board's search URL."""
        return f"{self._base()}/search"

    def nav_url(self, posting_id: str, posting_url: str) -> str:
        """Return a detail URL for a posting."""
        return f"{self._base()}/jobs/{posting_id}"

    def stored_url(self, target: str, posting_id: str) -> str:
        """Return a stable URL saved in the store."""
        return f"{self._base()}/jobs/{posting_id}"

    def extract_posting_id(self, target: str) -> str:
        """Extract a posting id from a URL or a bare id."""
        if target.isdigit():
            return target
        if match := re.search(r"/jobs/(\d+)", target):
            return match.group(1)
        raise ShobrError(f"could not extract a posting id from {target!r}")
