"""Writes an authoring result as JSON: world rows, with their provenance intact.

Same contract as `MigrationWriter` -- one `AuthoredRow` per proposed change,
grouped by table in order of first appearance, gaps listed separately -- just
serialized as data instead of a commented SQL file, for a consumer like trt to
parse back into structured rows rather than a human to read.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any

from .. import log as _log
from ..core.contracts import AuthoredRow
from .authored import AuthoredCollector

_logger = _log.get_logger("emit.author_json")


def _json_value(value: Any) -> Any:
    """One JSON-safe value. The only place a Python value becomes JSON."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return f"0x{bytes(value).hex()}"
    return str(value)


def _row_dict(row: AuthoredRow) -> dict[str, Any]:
    return {
        "statement": row.statement,
        "values": {k: _json_value(v) for k, v in row.values.items()},
        "provenance": dict(row.provenance),
        "where": {k: _json_value(v) for k, v in row.where.items()},
        "notes": list(row.notes),
    }


class AuthorJsonWriter(AuthoredCollector):
    """Renders the collected rows as one JSON document (shape: module docstring)."""

    def __init__(self, path: Path, capture_id: str, entry: int) -> None:
        super().__init__(path, capture_id, entry, _logger)

    def write(self) -> Path | None:
        if self._nothing_to_write():
            return None

        document = {
            "capture_id": self._capture_id,
            "entry": self._entry,
            "generated": _dt.datetime.now().isoformat(timespec="seconds"),
            "tables": [
                {"table": table, "rows": [_row_dict(row) for row in rows]}
                for table, rows in self._sections
            ],
            "gaps": list(self._gaps),
        }

        return self._emit(json.dumps(document, indent=2) + "\n")


def author_json_name(when: _dt.datetime | None = None) -> str:
    """Filename convention mirroring `migration_name`: <YYYYMMDDHHMMSS>_world.json."""
    return f"{(when or _dt.datetime.now()).strftime('%Y%m%d%H%M%S')}_world.json"
