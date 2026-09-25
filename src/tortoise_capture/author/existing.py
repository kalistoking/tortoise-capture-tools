"""Rows the world database already holds: reported, never proposed.

Every authored row is an INSERT, which is right for content new to the
database -- the use this was built for. Pointed at creatures the database
already has, most keys collide: 316 of 351 `creature` rows in the three test
captures, and the migration failed on the first. What should win when both
exist was the director's to decide (D3), and the choice was (a): propose only
what is new, and name the rest as gaps for a human.

A row is found by its table's key column -- the first of the primary key, or
the first column where there is none (`creature_ai_scripts`) -- read from the
table itself, not from a copy kept here. Rows sharing that value are one
thing: the points of one path, the lines of one script. If the database holds
any of it, all of it is reported, because proposing only the part it lacks
would splice new points onto a route they were never measured against.

UPDATE rows are left alone: they restate or change measured columns of a row
that must exist to be updated at all.
"""

from __future__ import annotations

from typing import Any, Iterable

from ..core.contracts import AuthoredRow
from ..emit.sql import literal

# How many key values a gap names before it only counts the rest.
_NAMED = 12


def only_new(rows: Iterable[AuthoredRow], world: Any) -> tuple[list[AuthoredRow], list[str]]:
    """(the rows to propose, gaps naming the ones the database already holds)."""
    rows = list(rows)
    if world is None:
        return rows, []

    held: dict[tuple[str, Any], bool] = {}
    for row in rows:
        key = _key(row, world)
        if key is not None and key not in held:
            table, value = key
            column = world.key_column(table)
            held[key] = world.row_exists(table, f"`{column}` = {literal(value, 'mysql')}")

    kept = [row for row in rows if not held.get(_key(row, world), False)]
    reported: dict[str, list[Any]] = {}
    for (table, value), exists in held.items():
        if exists:
            reported.setdefault(table, []).append(value)

    gaps = []
    for table, values in reported.items():
        dropped = sum(1 for row in rows if row.table == table and held.get(_key(row, world)))
        named = ", ".join(map(str, values[:_NAMED])) + (" ..." if len(values) > _NAMED else "")
        gaps.append(f"{table} -- already in the database under {world.key_column(table)} "
                    f"{named}: {dropped} row(s) not proposed, since only rows new to it are; "
                    "compare them with what the capture saw by hand")
    return kept, gaps


def _key(row: AuthoredRow, world: Any) -> tuple[str, Any] | None:
    if row.statement == "update":
        return None
    column = world.key_column(row.table)
    if column is None or column not in row.values:
        return None
    return row.table, row.values[column]
