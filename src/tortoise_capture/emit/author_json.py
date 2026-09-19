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
from typing import Any, Iterable

from .. import log as _log
from ..core.contracts import AuthoredRow

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


class AuthorJsonWriter:
    """Collects rows from every authoring rule and writes one JSON file.

    Mirrors `MigrationWriter`'s grouping so the two outputs stay structurally
    identical -- sections in order of first appearance, gaps listed at the
    end -- just rendered as data instead of SQL comments.
    """

    def __init__(self, path: Path, capture_id: str, entry: int) -> None:
        self._path = path
        self._capture_id = capture_id
        self._entry = entry
        self._sections: list[tuple[str, list[AuthoredRow]]] = []
        self._gaps: list[str] = []

    def add(self, rows: Iterable[AuthoredRow]) -> int:
        """Groups by the row's own table: one rule may fill several of them."""
        added = 0
        for row in rows:
            section = next((s for s in self._sections if s[0] == row.table), None)
            if section is None:
                section = (row.table, [])
                self._sections.append(section)
            section[1].append(row)
            added += 1
        return added

    def add_gaps(self, gaps: Iterable[str]) -> None:
        self._gaps.extend(gaps)

    @property
    def row_count(self) -> int:
        return sum(len(rows) for _, rows in self._sections)

    def write(self) -> Path | None:
        if not self._sections and not self._gaps:
            _logger.warning("nothing to author for entry %d; %s not written",
                            self._entry, self._path)
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

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8", newline="\n")
        _logger.info("wrote %d row(s) across %d table(s) and %d gap(s) to %s",
                     self.row_count, len(self._sections), len(self._gaps), self._path)
        return self._path


def author_json_name(when: _dt.datetime | None = None) -> str:
    """Filename convention mirroring `migration_name`: <YYYYMMDDHHMMSS>_world.json."""
    return f"{(when or _dt.datetime.now()).strftime('%Y%m%d%H%M%S')}_world.json"
