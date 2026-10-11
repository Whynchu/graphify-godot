"""Recognition helpers for CSV verification inventories."""
from __future__ import annotations

import csv
from pathlib import Path


_REQUIRED_COLUMNS = frozenset({"script", "role", "state"})
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024


def is_verification_manifest_csv(path: Path) -> bool:
    """Return whether a CSV has the small, recognizable test-inventory header.

    Ordinary CSV data is not routed into Graphify's code-only extraction path.
    The required fields are intentionally limited to test identity, runner
    role, and verification state; richer inventories can add owner/target/load
    columns without changing recognition.
    """
    if path.suffix.lower() != ".csv":
        return False
    try:
        if path.stat().st_size > _MAX_MANIFEST_BYTES:
            return False
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            header = next(csv.reader(stream), None)
    except (OSError, UnicodeError, csv.Error):
        return False
    if not header or len(header) > 64:
        return False
    columns = {value.strip().lower() for value in header if value}
    return _REQUIRED_COLUMNS.issubset(columns)
