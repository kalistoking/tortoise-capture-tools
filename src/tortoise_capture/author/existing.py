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

A row that only means something beside another is held with it: a path
belongs to its creature, a script line speaks the text its `dataint` names, an
AI event runs the script its `action1_script` names. Proposed alone, it would
bolt onto a row the database already holds and the capture was never compared
with -- waypoints on a creature keeping its own movement_type, a script saying
whatever line is stored under that text id.

UPDATE rows are left alone: they restate or change measured columns of a row
that must exist to be updated at all.
"""

from __future__ import annotations

from typing import Any, Iterable

from ..core.contracts import AuthoredRow
from ..emit.sql import literal

# How many key values a gap names before it only counts the rest.
_NAMED = 12

# table -> (its column naming the row it hangs off, that row's table); a table
# is listed after the one it hangs off, so a hold passes down the chain.
_HANGS_OFF = {
    "creature_movement": ("id", "creature"),
    "creature_ai_scripts": ("dataint", "broadcast_text"),
    "creature_ai_events": ("action1_script", "creature_ai_scripts"),
}


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

    # key -> (the table it hangs off, whether that row is stored or only held)
    with_parent: dict[tuple[str, Any], tuple[str, bool]] = {}
    for table, (column, parent) in _HANGS_OFF.items():
        for row in rows:
            key, up = _key(row, world), (parent, row.values.get(column))
            if (row.table == table and key is not None and not held.get(key)
                    and held.get(up)):
                held[key] = True
                with_parent[key] = (parent, up not in with_parent)

    kept = [row for row in rows if not held.get(_key(row, world), False)]
    reported: dict[tuple[str, tuple[str, bool] | None], list[Any]] = {}
    for (table, value), exists in held.items():
        if exists:
            reported.setdefault((table, with_parent.get((table, value))), []).append(value)

    gaps = []
    for (table, up), values in reported.items():
        dropped = sum(1 for row in rows if row.table == table and _key(row, world) in
                      {(table, v) for v in values})
        named = ", ".join(map(str, values[:_NAMED])) + (" ..." if len(values) > _NAMED else "")
        if up is None:
            gaps.append(f"{table} -- already in the database under {world.key_column(table)} "
                        f"{named}: {dropped} row(s) not proposed, since only rows new to it "
                        "are; compare them with what the capture saw by hand")
        else:
            parent, stored = up
            gaps.append(f"{table} -- {world.key_column(table)} {named}: {dropped} row(s) not "
                        f"proposed, since the {parent} row they hang off "
                        + ("is already in the database and was not compared with the capture"
                           if stored else "is not proposed either, down a chain that ends in "
                           "a row the database already holds"))
    return kept, gaps


def _key(row: AuthoredRow, world: Any) -> tuple[str, Any] | None:
    if row.statement == "update":
        return None
    column = world.key_column(row.table)
    if column is None or column not in row.values:
        return None
    return row.table, row.values[column]
