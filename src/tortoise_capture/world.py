"""Read-only access to the world database, for the lookups authoring needs.

Two things genuinely require the database as an *input*, not just as something
to compare against afterwards: resolving an item's display id (all the wire
carries) to the item entry a world table wants, and checking whether a row
already exists before proposing to insert it.

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
from dataclasses import dataclass
from typing import Any, Sequence

from . import log as _log

_logger = _log.get_logger("world")

ENV_PASSWORD = "TCT_DB_PASSWORD"
QUERY_TIMEOUT = 30


class WorldError(Exception):
    """The database could not answer. Authoring continues without it."""


@dataclass(frozen=True, slots=True)
class World:
    client: str                     # path to mysql.exe / mariadb.exe, or a bare name on PATH
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "mangos"
    database: str = "tw_world"

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

    def item_entry_for_display(self, display_id: int) -> int | None:
        """The item whose display id the creature was seen wearing.

        Ambiguity is possible in principle -- several items can share a display
        -- so a non-unique answer is reported rather than silently taking the
        first, which would put a plausible wrong item in a world table.
        """
        rows = self.query(f"SELECT entry FROM item_template WHERE display_id = {int(display_id)}")
        if not rows:
            _logger.warning("no item_template row has display_id %d", display_id)
            return None
        if len(rows) > 1:
            found = ", ".join(r[0] for r in rows[:5])
            _logger.warning("display_id %d matches %d items (%s...); not guessing which",
                            display_id, len(rows), found)
            return None
        return int(rows[0][0])

    def row_exists(self, table: str, where: str) -> bool:
        return (self.scalar(f"SELECT 1 FROM `{table}` WHERE {where} LIMIT 1")) is not None

    def column(self, table: str, column: str, where: str) -> str | None:
        return self.scalar(f"SELECT `{column}` FROM `{table}` WHERE {where} LIMIT 1")

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
