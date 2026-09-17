"""BaseModule: optional convenience base for opcode modules.

The contracts are Protocols, so inheriting this is never required -- it only
supplies the no-op defaults (no events, no SQL, data-as-text-fields) so a
module that decodes one field stays three lines long.

Deliberately absent: `expand()`. Its presence is what marks a module as a
container, so the base must not define it.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from .contracts import (
    CONVENTION, AuthorContext, AuthoredRow, DecodeContext, Event, Packet, Row, SqlContext,
    TableSpec,
)


class BaseModule:
    # Set by the @module decorator; declared here for readability and typing.
    id: str = ""
    opcodes: Sequence[str | int] = ()
    order: int = 100

    # Optional capabilities -- empty means "not offered".
    text_section: str = ""
    text_templates: Mapping[str, str] = {}
    sql_tables: Sequence[TableSpec] = ()

    def decode(self, pkt: Packet, ctx: DecodeContext) -> Iterable[Event]:
        return ()

    def text_fields(self, ev: Event) -> Mapping[str, Any]:
        return ev.data

    def sql_rows(self, ev: Event, ctx: SqlContext) -> Iterable[Row]:
        return ()

    # -- helper ------------------------------------------------------------

    def event(self, pkt: Packet, kind: str, *, scope: str = "entry", **data: Any) -> Event:
        """Builds an Event stamped with this module's id.

        `scope="session"` is for the rare fact that is not about any one
        creature (which map the session is on) and must reach sinks under
        any --entry filter -- see Event's own docstring.
        """
        return Event(packet=pkt, module_id=self.id, kind=kind, data=data, scope=scope)


class BaseAuthorRule:
    """Optional convenience base for authoring rules.

    A rule is a sink over the whole stream -- decoded events and analyzer
    findings alike -- that produces world rows once it ends. `close()` is the
    sink contract and is deliberately a no-op: rows are asked for afterwards,
    by `rows()`, so the caller controls when the database is consulted.
    """

    id: str = ""
    table: str = ""
    order: int = 100

    def handle(self, ev: Event, mod: Any = None) -> None:
        return None

    def close(self) -> None:
        return None

    def rows(self, ctx: "AuthorContext") -> Iterable["AuthoredRow"]:
        return ()

    def gaps(self, ctx: "AuthorContext") -> Iterable[str]:
        return ()

    # -- helpers -----------------------------------------------------------

    def row(self, values: Mapping[str, Any], provenance: Mapping[str, str],
            **kwargs: Any) -> "AuthoredRow":
        return AuthoredRow(table=self.table, values=dict(values),
                           provenance=dict(provenance), **kwargs)

    @staticmethod
    def authored_id(entry: int, index: int) -> int:
        """`entry * 100 + n` -- the id convention hand-authored content uses."""
        return entry * 100 + index

    def fill_schema_defaults(self, ctx: AuthorContext, table: str, values: dict[str, Any],
                             provenance: dict[str, str], notes: list[str],
                             skip: Iterable[str] = ()) -> None:
        """Widens a row to match the target table, using the table's own defaults.

        Only touches a column that is (a) not already set, (b) has a real
        schema default to read, and (c) not in `skip`. `skip` is for columns
        whose default is a genuine value that could be wrong rather than
        harmless boilerplate -- a map id, or the repeat delay of a spell known
        to actually repeat -- and those must stay gaps, never a silent zero.

        A no-op without a database: there is nothing to read the schema from,
        so the row stays as narrow as what was actually derived.
        """
        if ctx.world is None:
            return
        schema = ctx.world.describe(table)
        skip = set(skip)
        filled = []
        for column, default in schema.items():
            if column in values or column in skip or default is None:
                continue
            values[column] = default
            provenance[column] = CONVENTION
            filled.append(column)
        if filled:
            notes.append(f"schema default (not observed), from `{table}`: " + ", ".join(filled))

    @staticmethod
    def wire_float(value: float) -> float:
        """Shortest decimal that still lands on the same float32.

        Nine significant digits is the IEEE guarantee for a float32 round trip,
        so a value written this way stores into a FLOAT column bit-identically
        to what came off the wire. Rounding to a fixed number of decimal places
        carries no such guarantee -- it just looks tidier while quietly moving
        the value.
        """
        return float(f"{value:.9g}")


class BaseAnalyzer(BaseModule):
    """Optional convenience base for analyzers.

    Shares BaseModule's defaults and `event()` helper because a finding is an
    ordinary Event and reaches the sinks the same way. Only `feed`/`finish`
    are new; `decode` stays the inherited no-op, since an analyzer is never
    registered against an opcode.
    """

    def feed(self, ev: Event) -> None:
        return None

    def finish(self, ctx: DecodeContext) -> Iterable[Event]:
        return ()
