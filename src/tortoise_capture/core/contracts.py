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

    `scope` is almost always left at its default. `"entry"` means what it
    sounds like: the event is about one creature, and `--entry`/`--guid`
    filtering applies normally -- a module that cannot supply `entry`/`guid`
    (SMSG_MESSAGECHAT's emote form has no sender guid) is correctly dropped
    under a filtered run, by design. `"session"` is the deliberate exception:
    a fact that is not about any creature at all -- which map the session was
    on, say -- and so has nothing to filter by in the first place. Without
    it, such an event would silently vanish under any --entry run, which is
    not "correctly filtered", just a category error: it was never a
    per-creature fact for the filter to judge.
    """

    packet: Packet
    module_id: str
    kind: str
    data: Mapping[str, Any] = field(default_factory=dict)
    scope: str = "entry"


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


# --------------------------------------------------------------------------
# Authoring: turning observations into rows for the world database.
# --------------------------------------------------------------------------

# Where a value came from. The distinction is the whole point of authoring
# output: a reviewer must be able to see which numbers were read off the wire
# and which were inferred, before any of it reaches a world database.
WIRE = "wire"                # read directly out of a packet
DERIVED = "derived"          # inferred by correlation or reconstruction
LOOKUP = "lookup"            # resolved against the world database
CONVENTION = "convention"    # fixed by the authoring convention, not observed
CONFIRMED = "confirmed"      # matches the wire, restated as the DB's own value -- a safe no-op


@dataclass(frozen=True, slots=True)
class AuthoredRow:
    """One row destined for a world table, with its provenance attached."""

    table: str
    values: Mapping[str, Any]
    provenance: Mapping[str, str] = field(default_factory=dict)   # column -> WIRE/DERIVED/...
    statement: str = "insert"                                     # insert | update
    where: Mapping[str, Any] = field(default_factory=dict)        # update only
    notes: tuple[str, ...] = ()                                   # caveats for the reviewer


@dataclass(frozen=True, slots=True)
class AuthorContext:
    capture_id: str
    entry: int
    log: "logging.Logger"
    world: Any = None        # a read-only world database accessor, or None


@runtime_checkable
class AuthorRule(Protocol):
    """Builds one world table from the event stream.

    A rule is a sink: it sees every emitted event *and* every analyzer finding,
    in order, then produces its rows once the stream ends. That is why it needs
    no place of its own in the runner.
    """

    id: str
    table: str

    def handle(self, ev: Event, mod: Any) -> None: ...
    def close(self) -> None: ...
    def rows(self, ctx: AuthorContext) -> Iterable[AuthoredRow]: ...
    def gaps(self, ctx: AuthorContext) -> Iterable[str]: ...


@runtime_checkable
class Analyzer(Protocol):
    """Stateful consumer of the whole event stream, producing more events.

    Where a decoder answers "what does this packet say", an analyzer answers
    "what does the session as a whole say" -- the patrol behind 113 hops, the
    respawn timer behind a death and a create. It sees every decoded event in
    order, keeps whatever state it needs, and emits its findings once the
    stream ends.

    Findings are ordinary `Event`s, so an analyzer declares `text_templates`
    and `sql_tables` exactly like a module does and reaches the same sinks.
    That is the whole integration: the emit layer never learns that analysis
    exists.
    """

    id: str

    def feed(self, ev: Event) -> None: ...

    def finish(self, ctx: DecodeContext) -> Iterable[Event]: ...


def offers_text(mod: object) -> bool:
    return bool(getattr(mod, "text_templates", None))


def offers_sql(mod: object) -> bool:
    return bool(getattr(mod, "sql_tables", None))


def offers_expand(mod: object) -> bool:
    # BaseModule deliberately does not define expand(), so presence of the
    # attribute is exactly "this module is a container".
    return callable(getattr(mod, "expand", None))
