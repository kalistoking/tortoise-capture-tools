"""The types that cross every layer boundary, and the module interfaces.

Two data types travel the whole pipeline -- `Packet` (one framed message) and
`Event` (one decoded fact) -- plus the declarative SQL descriptors. The
interfaces are `Protocol`s, so a module satisfies them structurally without
inheriting anything, and the runner can ask "does this module offer text
output?" without knowing which module it is holding.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Protocol, Sequence, runtime_checkable

if TYPE_CHECKING:  # type-only: core must not import the wire/fields layers at runtime
    import logging

    from ..fields.tables import FieldTable
    from ..wire.opcodes import OpcodeTable


class Direction(enum.StrEnum):
    S2C = "S2C"
    C2S = "C2S"


@dataclass(frozen=True, slots=True)
class Packet:
    """One framed world message, decrypted, body still raw.

    `via` is "direct" for a message read straight off the stream, or the id of
    the module that expanded it out of a container.
    """

    seq: int
    t: float | None            # capture-relative seconds, None if unknown
    direction: Direction
    opcode: int
    name: str                  # resolved opcode symbol, "" if unknown
    body: bytes
    via: str = "direct"

    def describe(self) -> str:
        return f"seq={self.seq} {self.direction} 0x{self.opcode:X} {self.name or '?'} ({len(self.body)}B)"


@dataclass(frozen=True, slots=True)
class Event:
    """One fact a module decoded out of a packet.

    `data` is a plain mapping on purpose: templates, the SQL mapper and the
    JSONL writer all consume it the same way, and no core code ever has to
    import a type a module defined.

    Conventional keys the core uses for cross-cutting filters (and nothing
    else): `entry` (creature_template.entry) and `guid` (64-bit wire GUID).
    """

    packet: Packet
    module_id: str
    kind: str
    data: Mapping[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------
# SQL descriptors: a module declares shape and mapping, emit/sql.py writes.
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Column:
    name: str
    type: str                  # dialect-level type, e.g. "INT UNSIGNED"
    nullable: bool = True


@dataclass(frozen=True, slots=True)
class TableSpec:
    """Output table a module writes into.

    managed=True  -- a table this toolkit owns; DDL is generated from here.
    managed=False -- a table that already exists in tw_world (creature_movement
                     and friends): inserts only, never DDL. Emitting CREATE
                     TABLE for a table the server owns would be actively wrong.
    """

    name: str
    columns: tuple[Column, ...]
    key: tuple[str, ...] = ()
    managed: bool = True
    conflict: str = "ignore"   # ignore | replace | plain
    comment: str = ""


@dataclass(frozen=True, slots=True)
class Row:
    table: str
    values: Mapping[str, Any]


# --------------------------------------------------------------------------
# Contexts handed to modules -- their only access to shared services.
# --------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Tables:
    """Source-derived lookup tables, parsed from the tortoise-wow checkout."""

    opcodes: "OpcodeTable"
    fields: "FieldTable"


@dataclass(frozen=True, slots=True)
class DecodeContext:
    tables: Tables
    log: "logging.Logger"
    options: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SqlContext:
    capture_id: str            # identifies the capture a row came from
    dialect: str = "mysql"


# --------------------------------------------------------------------------
# Module interfaces
# --------------------------------------------------------------------------

@runtime_checkable
class Decoder(Protocol):
    """Required of every module: turn one packet into zero or more events."""

    id: str
    opcodes: Sequence[str | int]

    def decode(self, pkt: Packet, ctx: DecodeContext) -> Iterable[Event]: ...


@runtime_checkable
class Expander(Protocol):
    """Optional: a container opcode yields the packets hidden inside it.

    This is what keeps compressed containers out of the runner -- they are
    ordinary modules that happen to produce packets instead of events.
    """

    def expand(self, pkt: Packet, ctx: DecodeContext) -> Iterable[Packet]: ...


@runtime_checkable
class TextRenderer(Protocol):
    """Optional: declare the text format; the core does the mapping."""

    text_section: str
    text_templates: Mapping[str, str]        # event kind -> str.format template

    def text_fields(self, ev: Event) -> Mapping[str, Any]: ...


@runtime_checkable
class SqlEmitter(Protocol):
    """Optional: declare the tables and map events onto rows."""

    sql_tables: Sequence[TableSpec]

    def sql_rows(self, ev: Event, ctx: SqlContext) -> Iterable[Row]: ...


def offers_text(mod: object) -> bool:
    return bool(getattr(mod, "text_templates", None))


def offers_sql(mod: object) -> bool:
    return bool(getattr(mod, "sql_tables", None))


def offers_expand(mod: object) -> bool:
    # BaseModule deliberately does not define expand(), so presence of the
    # attribute is exactly "this module is a container".
    return callable(getattr(mod, "expand", None))
