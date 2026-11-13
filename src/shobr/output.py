"""Render command views as machine-readable JSON."""

import json
import sys
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path


def _json_default(value: object) -> object:
    """Serialize view objects that the standard JSON encoder cannot handle."""
    to_json = getattr(value, "to_json", None)
    if callable(to_json):
        return to_json()
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return list(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def emit_json(view: object) -> None:
    """Write one view as an indented JSON document on stdout."""
    json.dump(view, sys.stdout, indent=2, default=_json_default)
    sys.stdout.write("\n")
