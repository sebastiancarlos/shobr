"""ANSI color constants for terminal output.

Usage: f"{GREEN}PURSUE{RESET}" (Always pair a color with RESET.)
"""

import os

_NO_COLOR = "NO_COLOR" in os.environ


def _code(sequence: str) -> str:
    """Return an ANSI color, or empty on NO_COLOR."""
    return "" if _NO_COLOR else sequence


RESET = _code("\033[0m")
GREEN = _code("\033[32m")
RED = _code("\033[31m")
YELLOW = _code("\033[33m")
BLUE = _code("\033[34m")
BOLD = _code("\033[1m")
CYAN = _code("\033[36m")
