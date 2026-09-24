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
- **Rows that already exist are not overwritten.** Every row is proposed as an
  INSERT, so one the database already holds fails on its key rather than
  replacing what is there. Checking for them first, and proposing an UPDATE
  instead, is not done yet: content new to the database -- the use this was
  built for -- never meets one, and re-authoring what it already holds needs
  a decision on what should win. The output is a reviewable proposal a human
  applies, never something that runs itself.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Mapping, Sequence

from .. import log as _log
from ..core.contracts import CONFIRMED, CONVENTION, DERIVED, LOOKUP, WIRE, AuthoredRow
from .authored import AuthoredCollector
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
    """One INSERT per distinct set of columns, in first-appearance order.

    Rows of one table need not carry the same columns -- one spawn measured a
    respawn, another did not -- and one column list for all of them either
    drops a later row's extra value or writes NULL where a row has none, which
    a NOT NULL column refuses. A row that leaves a column out gets the table's
    own default, exactly as it would inserted alone.
    """
    groups: dict[frozenset[str], list[AuthoredRow]] = {}
    for row in rows:
        groups.setdefault(frozenset(row.values), []).append(row)
    lines: list[str] = []
    for grouped in groups.values():
        columns = list(grouped[0].values)
        body = ",\n".join(
            "(" + ", ".join(literal(row.values[c], dialect) for c in columns) + ")"
            for row in grouped)
        header = ", ".join(f"`{c}`" for c in columns)
        lines += [f"INSERT INTO `{table}`", f"({header})", "VALUES", body + ";"]
    return lines


def _update(table: str, row: AuthoredRow, dialect: str) -> list[str]:
    sets = ",\n".join(f"    `{c}` = {literal(v, dialect)}" for c, v in row.values.items())
    where = " AND ".join(f"`{c}` = {literal(v, dialect)}" for c, v in row.where.items())
    return [f"UPDATE `{table}`", f"SET {sets.lstrip()}", f"WHERE {where};"]


class MigrationWriter(AuthoredCollector):
    """Renders the collected rows as one commented SQL migration file."""

    def __init__(self, path: Path, capture_id: str, entry: int, dialect: str = "mysql") -> None:
        super().__init__(path, capture_id, entry, _logger)
        self._dialect = dialect

    def write(self) -> Path | None:
        if self._nothing_to_write():
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

        return self._emit("\n".join(lines))


def migration_name(when: _dt.datetime | None = None) -> str:
    """The `tortoise-wow` migration filename convention: <YYYYMMDDHHMMSS>_world.sql."""
    return f"{(when or _dt.datetime.now()).strftime('%Y%m%d%H%M%S')}_world.sql"
