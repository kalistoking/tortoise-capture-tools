"""Opcode table, parsed from the server checkout -- never hardcoded.

`Opcodes_1_12_1.h` gives SYMBOL = 0xNNN; `Opcodes.cpp` gives
StoreOpcode(SYMBOL, "NAME", ...). Cross-referencing the two yields every
opcode this fork actually knows, not just the ones a given capture happened
to contain, and it re-derives itself if the fork renumbers anything.

The parsed result is cached under .cache/ keyed by the source files' size and
mtime. It is never committed: a checked-in table would drift from the
checkout it claims to describe, which is the exact failure the live parsing
exists to prevent.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .. import log as _log

_logger = _log.get_logger("wire.opcodes")

VALUES_FILE = "src/game/Protocol/Opcodes_1_12_1.h"
NAMES_FILE = "src/game/Protocol/Opcodes.cpp"

_SYMBOL_RE = re.compile(r"\b([A-Z][A-Z0-9_]+)\s*=\s*(0x[0-9A-Fa-f]+)")
_STORE_RE = re.compile(r"StoreOpcode\(\s*([A-Z][A-Z0-9_]+)\s*,\s*\"([^\"]+)\"")


@dataclass(frozen=True, slots=True)
class OpcodeTable:
    by_value: dict[int, str] = field(default_factory=dict)
    by_name: dict[str, int] = field(default_factory=dict)
    source: str = ""

    @property
    def max_opcode(self) -> int | None:
        """Highest opcode this fork defines.

        A decrypted opcode above it is a reliable desync signal, and a cheaper
        one than waiting for a body length to run past the end of the stream.
        """
        return max(self.by_value) if self.by_value else None

    def name(self, value: int) -> str:
        return self.by_value.get(value, "")

    def __len__(self) -> int:
        return len(self.by_value)


def _stamp(paths: list[Path]) -> list[list[float]]:
    return [[p.stat().st_size, p.stat().st_mtime] for p in paths]


def load(repo: Path | None, cache_dir: Path | None = None) -> OpcodeTable:
    """Loads (or re-parses) the opcode table. Missing checkout -> empty table."""
    if repo is None:
        _logger.error("no tortoise-wow checkout configured (--repo / TCT_REPO); "
                      "opcodes stay numeric-only")
        return OpcodeTable()

    values_file, names_file = repo / VALUES_FILE, repo / NAMES_FILE
    if not values_file.exists() or not names_file.exists():
        _logger.error("opcode sources not found under %s (expected %s and %s); "
                      "opcodes stay numeric-only", repo, VALUES_FILE, NAMES_FILE)
        return OpcodeTable()

    stamp = _stamp([values_file, names_file])
    cache = (cache_dir / "opcodes.json") if cache_dir else None
    if cache and cache.exists():
        try:
            blob = json.loads(cache.read_text(encoding="utf-8"))
            if blob.get("stamp") == stamp:
                by_value = {int(k): v for k, v in blob["by_value"].items()}
                _logger.debug("opcode table from cache: %d entries", len(by_value))
                return OpcodeTable(by_value=by_value,
                                   by_name={v: k for k, v in by_value.items()},
                                   source=str(repo))
        except (ValueError, KeyError, OSError) as exc:
            _logger.debug("opcode cache unusable (%s); reparsing", exc)

    symbol_to_value = {m.group(1): int(m.group(2), 16)
                       for m in _SYMBOL_RE.finditer(values_file.read_text(encoding="utf-8"))}
    by_value: dict[int, str] = {}
    for m in _STORE_RE.finditer(names_file.read_text(encoding="utf-8")):
        symbol, name = m.group(1), m.group(2)
        if symbol in symbol_to_value:
            by_value[symbol_to_value[symbol]] = name

    table = OpcodeTable(by_value=by_value, by_name={v: k for k, v in by_value.items()},
                        source=str(repo))
    _logger.info("opcode table: %d opcodes from %s", len(table), repo)

    if cache:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps({"stamp": stamp,
                                         "by_value": {str(k): v for k, v in by_value.items()}}),
                             encoding="utf-8")
        except OSError as exc:
            _logger.debug("could not write opcode cache: %s", exc)

    return table
