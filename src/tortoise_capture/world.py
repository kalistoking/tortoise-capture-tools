"""Read-only access to the world database, for the lookups authoring needs.

Three things genuinely require the database as an *input*, not just as
something to compare against afterwards: resolving an item's display id (all
the wire carries) to the item entry a world table wants, checking whether a
row already exists before proposing to insert it, and reading a target
table's own column defaults (`describe()`) so authoring can match its full
width without a second, hand-maintained copy of the schema
(ARCHITECTURE.md §17.3).

It shells out to a `mysql`/`mariadb` client rather than taking a driver
dependency -- the client ships with the server install this project already
depends on, and `scapy` stays the only third-party package (ARCHITECTURE.md
D9). Queries are read-only by construction: this module has no method that
writes, and the authoring output is a file a human applies.

The password is read from `TCT_DB_PASSWORD` and handed over through `MYSQL_PWD`
in the child's environment, never on a command line, where it would be visible
to anything that can list processes.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from typing import Any, Sequence

from . import log as _log

_logger = _log.get_logger("world")

ENV_PASSWORD = "TCT_DB_PASSWORD"
QUERY_TIMEOUT = 30


class WorldError(Exception):
    """The database could not answer. Authoring continues without it."""


def _coerce(text: str | None) -> Any:
    """A DESCRIBE Default cell: 'NULL' -> no usable default, else int/float/str."""
    if text is None or text == "NULL":
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


@dataclass(frozen=True, slots=True)
class World:
    client: str                     # path to mysql.exe / mariadb.exe, or a bare name on PATH
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "mangos"
    database: str = "tw_world"
    # Mutating a frozen dataclass's own attribute is blocked; mutating what
    # that attribute points at is not, so a plain dict works as a cache here.
    _schema_cache: dict[str, dict[str, Any]] = field(default_factory=dict, init=False,
                                                      repr=False, compare=False)

    # -- plumbing ----------------------------------------------------------

    def query(self, sql: str) -> list[list[str]]:
        """Rows as lists of strings. `-N -B` gives clean tab-separated output."""
        env = dict(os.environ)
        password = os.environ.get(ENV_PASSWORD)
        if password:
            env["MYSQL_PWD"] = password
        command = [self.client, f"-h{self.host}", f"-P{self.port}", f"-u{self.user}",
                   self.database, "-N", "-B", "-e", sql]
        try:
            done = subprocess.run(command, capture_output=True, text=True,
                                  env=env, timeout=QUERY_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WorldError(f"could not run {self.client}: {exc}") from exc
        if done.returncode != 0:
            raise WorldError(done.stderr.strip() or f"{self.client} exited {done.returncode}")
        return [line.split("\t") for line in done.stdout.splitlines() if line]

    def scalar(self, sql: str) -> str | None:
        rows = self.query(sql)
        return rows[0][0] if rows and rows[0] else None

    # -- the lookups authoring needs ---------------------------------------

    def items_for_display(self, display_id: int) -> list[tuple[int, int, int, int]]:
        """(entry, class, subclass, inventory_type) of every item wearing a display.

        Several items can share a display, so all of them are returned: which
        one a creature holds is the caller's to settle, against what else the
        wire says about it, or to leave unsettled.
        """
        rows = self.query("SELECT entry, class, subclass, inventory_type FROM item_template "
                          f"WHERE display_id = {int(display_id)} ORDER BY entry")
        return [tuple(int(v) for v in row) for row in rows]

    def describe(self, table: str) -> dict[str, Any]:
        """Column -> the table's own DEFAULT, from DESCRIBE. Cached per table.

        This is what lets authoring match a hand-written migration's full
        column width without a second, hand-maintained copy of the schema:
        the boilerplate zeros (`event_flags`, `x`/`y`/`z`/`o`, `emote_id*`...)
        are read from the table that will receive them, not retyped here.
        A column with no schema default (NULL, or DESCRIBE failed) is simply
        absent from the result, and stays a gap.
        """
        if table not in self._schema_cache:
            try:
                rows = self.query(f"DESCRIBE `{table}`")
            except WorldError as exc:
                _logger.warning("could not describe %s: %s", table, exc)
                self._schema_cache[table] = {}
            else:
                # DESCRIBE columns: Field, Type, Null, Key, Default, Extra.
                defaults = {r[0]: _coerce(r[4] if len(r) > 4 else None) for r in rows}
                self._schema_cache[table] = {c: d for c, d in defaults.items() if d is not None}
        return self._schema_cache[table]

    def row_exists(self, table: str, where: str) -> bool:
        return (self.scalar(f"SELECT 1 FROM `{table}` WHERE {where} LIMIT 1")) is not None

    def column(self, table: str, column: str, where: str) -> str | None:
        return self.scalar(f"SELECT `{column}` FROM `{table}` WHERE {where} LIMIT 1")

    def numeric_column(self, table: str, column: str, where: str) -> float | None:
        """Like `column`, but at full precision for a FLOAT/DOUBLE column.

        A plain `SELECT` of a FLOAT truncates to the client library's default
        display precision (MySQL shows ~6 significant digits -- "20.2119" for
        a column that actually stores 20.2118873596), which is fine for a
        coarse agreement check but would be a *worse* value to restate than
        the one it is being compared against. Casting to a wide DECIMAL makes
        the server print what is actually stored.
        """
        text = self.scalar(f"SELECT CAST(`{column}` AS DECIMAL(30,10)) "
                           f"FROM `{table}` WHERE {where} LIMIT 1")
        return float(text) if text is not None else None

    def check(self) -> bool:
        """True when the database answers. Logged once, at startup."""
        try:
            self.scalar("SELECT 1")
        except WorldError as exc:
            _logger.warning("world database unreachable (%s); authoring will run without "
                            "lookups and diffing", exc)
            return False
        _logger.info("world database %s@%s:%d/%s reachable",
                     self.user, self.host, self.port, self.database)
        return True


def from_config(settings: dict[str, Any] | None) -> World | None:
    """Builds an accessor from the `[database]` config section, if there is one."""
    if not settings or not settings.get("client"):
        return None
    world = World(client=str(settings["client"]),
                  host=str(settings.get("host", "127.0.0.1")),
                  port=int(settings.get("port", 3306)),
                  user=str(settings.get("user", "mangos")),
                  database=str(settings.get("world", "tw_world")))
    return world if world.check() else None
