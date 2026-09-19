"""The collection half of an authoring writer, shared by every output form.

`MigrationWriter` (SQL) and `AuthorJsonWriter` (JSON) render the same thing
-- `AuthoredRow`s grouped by table in order of first appearance, gaps kept
apart -- so the grouping lives here once and each writer keeps only its
`write()`, which is where the two genuinely differ. ARCHITECTURE.md 17.1's
ordering rule (a script before the event that references it) is therefore
enforced in one place rather than mirrored by hand in two.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

from ..core.contracts import AuthoredRow


class AuthoredCollector:
    def __init__(self, path: Path, capture_id: str, entry: int, log: logging.Logger) -> None:
        self._path = path
        self._capture_id = capture_id
        self._entry = entry
        self._log = log
        self._sections: list[tuple[str, list[AuthoredRow]]] = []
        self._gaps: list[str] = []

    def add(self, rows: Iterable[AuthoredRow]) -> int:
        """Groups by the row's own table: one rule may fill several of them.

        Section order follows first appearance, so a rule that emits a script
        before the event referencing it produces output that applies in that
        order too.
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

    # -- for the subclass's write() -----------------------------------------

    def _nothing_to_write(self) -> bool:
        if self._sections or self._gaps:
            return False
        self._log.warning("nothing to author for entry %d; %s not written",
                          self._entry, self._path)
        return True

    def _emit(self, text: str) -> Path:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(text, encoding="utf-8", newline="\n")
        self._log.info("wrote %d row(s) across %d table(s) and %d gap(s) to %s",
                       self.row_count, len(self._sections), len(self._gaps), self._path)
        return self._path
