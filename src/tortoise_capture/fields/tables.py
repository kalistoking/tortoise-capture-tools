"""UpdateField indices, evaluated out of the server's own UpdateFields.h.

The enums are read like a C compiler would: sequential auto-increment, plus
'=' expressions that sum named constants and literals. Indices are therefore
whatever this fork uses, not whatever stock 1.12.1 used.

Object-type gating is the important part. EUnitFields names are valid only
for units and pets; gameobjects, items, corpses and dynamic objects have
entirely different layouts starting at the same OBJECT_END offset. Naming a
gameobject's field with a unit name is silent nonsense ("UNIT_FIELD_HEALTH =
329159" for a door), which is why `name_for` takes the GUID and refuses to
name anything it cannot vouch for. This was a real prototype bug and has a
regression test.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .. import log as _log
from ..core.reader import is_unit

_logger = _log.get_logger("fields.tables")

FIELDS_FILE = "src/game/Objects/UpdateFields.h"

_ENUM_RE = r"enum\s+{name}\s*\{{(.*?)\}};"
_MEMBER_RE = re.compile(r"(\w+)\s*(?:=\s*([^,]+))?\s*,")
_LITERAL_RE = re.compile(r"^(0x[0-9A-Fa-f]+|\d+)$")


@dataclass(frozen=True, slots=True)
class FieldTable:
    by_index: dict[int, str] = field(default_factory=dict)
    by_name: dict[str, int] = field(default_factory=dict)
    object_end: int = 0
    unit_end: int = 0
    source: str = ""

    def name_for(self, index: int, guid: int) -> str | None:
        """Field name, or None when this object type's layout is unknown."""
        if not is_unit(guid):
            return None          # see module docstring: other types differ entirely
        return self.by_index.get(index)

    def describe(self, index: int, guid: int) -> str:
        return self.name_for(index, guid) or f"field_{index}"

    def index_of(self, name: str) -> int | None:
        return self.by_name.get(name)

    def __len__(self) -> int:
        return len(self.by_index)


def _evaluate(text: str, enums: tuple[str, ...]) -> tuple[dict[str, int], dict[int, str]]:
    env: dict[str, int] = {}
    by_index: dict[int, str] = {}
    current = -1
    for name in enums:
        match = re.search(_ENUM_RE.format(name=name), text, re.DOTALL)
        if not match:
            _logger.warning("enum %s not found in %s", name, FIELDS_FILE)
            continue
        body = re.sub(r"//.*", "", match.group(1))
        for member in _MEMBER_RE.finditer(body):
            symbol, expr = member.group(1), member.group(2)
            if expr:
                value = 0
                for term in expr.split("+"):
                    term = term.strip()
                    value += int(term, 0) if _LITERAL_RE.match(term) else env.get(term, 0)
            else:
                value = current + 1
            env[symbol] = value
            current = value
            # First name declared for an index wins; array continuation slots
            # (second half of a GUID, AURA elements) stay unnamed, which is fine.
            by_index.setdefault(value, symbol)
    return env, by_index


def load(repo: Path | None, cache_dir: Path | None = None) -> FieldTable:
    """Loads (or re-parses) the field table. Missing checkout -> empty table."""
    if repo is None:
        _logger.warning("no tortoise-wow checkout configured (--repo / TCT_REPO); "
                        "update fields stay numeric-only")
        return FieldTable()

    path = repo / FIELDS_FILE
    if not path.exists():
        _logger.warning("%s not found; update fields stay numeric-only", path)
        return FieldTable()

    stamp = [path.stat().st_size, path.stat().st_mtime]
    cache = (cache_dir / "updatefields.json") if cache_dir else None
    if cache and cache.exists():
        try:
            blob = json.loads(cache.read_text(encoding="utf-8"))
            if blob.get("stamp") == stamp:
                by_index = {int(k): v for k, v in blob["by_index"].items()}
                _logger.debug("field table from cache: %d entries", len(by_index))
                return FieldTable(by_index=by_index,
                                  by_name={v: k for k, v in by_index.items()},
                                  object_end=blob["object_end"], unit_end=blob["unit_end"],
                                  source=str(repo))
        except (ValueError, KeyError, OSError) as exc:
            _logger.debug("field cache unusable (%s); reparsing", exc)

    env, by_index = _evaluate(path.read_text(encoding="utf-8"), ("EObjectFields", "EUnitFields"))
    table = FieldTable(by_index=by_index, by_name={v: k for k, v in by_index.items()},
                       object_end=env.get("OBJECT_END", 0), unit_end=env.get("UNIT_END", 0),
                       source=str(repo))
    _logger.info("update fields: %d named indices (OBJECT_END=%d, UNIT_END=%d) from %s",
                 len(table), table.object_end, table.unit_end, repo)

    if cache:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"stamp": stamp, "object_end": table.object_end,
                                         "unit_end": table.unit_end,
                                         "by_index": {str(k): v for k, v in by_index.items()}}),
                             encoding="utf-8")
        except OSError as exc:
            _logger.debug("could not write field cache: %s", exc)

    return table
