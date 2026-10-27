"""The LinkedIn provider."""

import re
from urllib.parse import urlencode

from ..config import Config, resolve_geo_ids
from ..core import LINKEDIN_BASE_URL, ShobrError
from .base import Provider


class LinkedInProvider(Provider):
    """LinkedIn job search and detail scraping."""

    def search_url(self, filters: Config) -> str:
        """Build the LinkedIn search-results URL from the configured filters."""
        titles = " or ".join(filters["titles"])
        workplace_types = " or ".join(filters["workplace_types"])
        params = {
            "keywords": f"{titles}, {workplace_types}",
            "origin": "PREFERENCES_LANDING",  # left here for "real user" signal
        }
        if geo_ids := resolve_geo_ids(filters):
            params["geoId"] = ",".join(geo_ids)
        return f"{LINKEDIN_BASE_URL}/jobs/search-results?{urlencode(params)}"

    def nav_url(self, posting_id: str, posting_url: str) -> str:
        """Navigate through the configured base (a fake host in tests)."""
        return f"{LINKEDIN_BASE_URL}/jobs/view/{posting_id}"

    def stored_url(self, target: str, posting_id: str) -> str:
        """LinkedIn's canonical public jobs-view URL."""
        return f"https://www.linkedin.com/jobs/view/{posting_id}"

    def extract_posting_id(self, target: str) -> str:
        """Posting id from a bare id or a LinkedIn jobs URL."""
        if target.isdigit():
            return target
        for pattern in (r"/jobs/view/(?:[a-z-]*-)?(\d+)", r"currentJobId=(\d+)"):
            if match := re.search(pattern, target):
                return match.group(1)
        raise ShobrError(f"could not extract a posting id from {target!r}")


PROVIDER = LinkedInProvider(
    name="linkedin",
    display_name="LinkedIn",
    supports_tailoring=True,
    has_location=True,
    has_company=True,
    has_notifications=True,
)
