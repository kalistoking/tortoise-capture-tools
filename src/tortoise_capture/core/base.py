"""BaseModule: optional convenience base for opcode modules.

The contracts are Protocols, so inheriting this is never required -- it only
supplies the no-op defaults (no events, no SQL, data-as-text-fields) so a
module that decodes one field stays three lines long.

Deliberately absent: `expand()`. Its presence is what marks a module as a
container, so the base must not define it.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from .contracts import DecodeContext, Event, Packet, Row, SqlContext, TableSpec


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

    def event(self, pkt: Packet, kind: str, **data: Any) -> Event:
        """Builds an Event stamped with this module's id."""
        return Event(packet=pkt, module_id=self.id, kind=kind, data=data)


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
