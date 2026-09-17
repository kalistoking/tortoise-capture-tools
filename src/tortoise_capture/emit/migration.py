"""Writes an authoring migration: world rows, with their provenance on the page.

This is not the `capture_*` observation output. It proposes rows for a real
world database, so it holds itself to a stricter standard than "here is what I
saw":

- **Every section states where its values came from** -- read off the wire,
  inferred by analysis, resolved against the database, or fixed by convention.
  A reviewer should never have to guess which numbers are measurements.
- **Nothing undecidable is defaulted silently.** A field the capture cannot
  support is left out of the statement and listed under NOT DERIVED at the end,
  with the reason. A zero that means "unknown" is how bad data enters a
  database.
- **Rows that already exist are reported, not overwritten.** The output is a
  reviewable proposal a human applies, never something that runs itself.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .. import log as _log
from ..core.contracts import CONFIRMED, CONVENTION, DERIVED, LOOKUP, WIRE, AuthoredRow
from .sql import literal

_logger = _log.get_logger("emit.migration")

_SOURCE_LABELS = {
    WIRE: "read off the wire",
    DERIVED: "inferred by analysis",
    LOOKUP: "resolved against the world database",
    CONFIRMED: "matches the wire, restated as the database's own value -- a no-op",
    CONVENTION: "authoring convention, not observed",
}
_SOURCE_ORDER = (WIRE, DERIVED, LOOKUP, CONFIRMED, CONVENTION)

RULE = "-- " + "=" * 62


def _banner(title: str) -> list[str]:
    return [RULE, f"-- {title}", RULE]


def _provenance_comment(rows: Sequence[AuthoredRow]) -> list[str]:
    """One line per source, naming the columns that came from it."""
    columns: dict[str, list[str]] = {}
    for row in rows:
        for column, source in row.provenance.items():
            names = columns.setdefault(source, [])
            if column not in names:
                names.append(column)

    out = []
    for source in _SOURCE_ORDER:
        if source in columns:
            out.append(f"--   {source:<11} ({_SOURCE_LABELS[source]}): "
                       + ", ".join(columns[source]))
    unlabelled = {c for row in rows for c in row.values if c not in row.provenance}
    if unlabelled:
        out.append(f"--   unlabelled: {', '.join(sorted(unlabelled))}")
    return out


def _insert(table: str, rows: Sequence[AuthoredRow], dialect: str) -> list[str]:
    columns = list(rows[0].values)
    body = ",\n".join(
        "(" + ", ".join(literal(row.values.get(c), dialect) for c in columns) + ")"
        for row in rows)
    header = ", ".join(f"`{c}`" for c in columns)
    return [f"INSERT INTO `{table}`", f"({header})", "VALUES", body + ";"]


def _update(table: str, row: AuthoredRow, dialect: str) -> list[str]:
    sets = ",\n".join(f"    `{c}` = {literal(v, dialect)}" for c, v in row.values.items())
    where = " AND ".join(f"`{c}` = {literal(v, dialect)}" for c, v in row.where.items())
    return [f"UPDATE `{table}`", f"SET {sets.lstrip()}", f"WHERE {where};"]


class MigrationWriter:
    """Collects rows from every authoring rule and writes one migration file."""

    def __init__(self, path: Path, capture_id: str, entry: int, dialect: str = "mysql") -> None:
        self._path = path
        self._capture_id = capture_id
        self._entry = entry
        self._dialect = dialect
        self._sections: list[tuple[str, list[AuthoredRow]]] = []
        self._gaps: list[str] = []

    def add(self, rows: Iterable[AuthoredRow]) -> int:
        """Groups by the row's own table: one rule may fill several of them.

        Section order follows first appearance, so a rule that emits a script
        before the event referencing it produces a migration that applies in
        that order too.
        """
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

        stamp = _dt.datetime.now()
        lines: list[str] = [
            *_banner(f"Authored from capture '{self._capture_id}' for entry {self._entry}"),
            f"-- Generated: {stamp.isoformat(timespec='seconds')}",
            "--",
            "-- Proposed rows, not applied. Every section states where its values came",
            "-- from; anything the capture could not support is listed at the end",
            "-- rather than defaulted to zero.",
            "",
        ]

        for table, rows in self._sections:
            lines.extend(_banner(f"{table}  ({len(rows)} row(s))"))
            lines.extend(_provenance_comment(rows))
            notes = [n for row in rows for n in row.notes]
            lines.extend(f"-- NOTE: {n}" for n in dict.fromkeys(notes))

            updates = [r for r in rows if r.statement == "update"]
            inserts = [r for r in rows if r.statement != "update"]
            if inserts:
                lines.extend(_insert(table, inserts, self._dialect))
            for row in updates:
                lines.extend(_update(table, row, self._dialect))
            lines.append("")

        if self._gaps:
            lines.extend(_banner("NOT DERIVED from this capture -- author by hand"))
            lines.extend(f"--   {gap}" for gap in self._gaps)
            lines.append("")

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
        _logger.info("wrote %d row(s) across %d table(s) and %d gap(s) to %s",
                     self.row_count, len(self._sections), len(self._gaps), self._path)
        return self._path


def migration_name(when: _dt.datetime | None = None) -> str:
    """The `tortoise-wow` migration filename convention: <YYYYMMDDHHMMSS>_world.sql."""
    return f"{(when or _dt.datetime.now()).strftime('%Y%m%d%H%M%S')}_world.sql"
