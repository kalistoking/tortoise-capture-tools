"""SMSG_GAMEOBJECT_QUERY_RESPONSE -- gameobject identity (QueryHandler.cpp:250).

    uint32 entry ; uint32 type ; uint32 display_id
    CString name ; CString name2,3,4,5 (always empty)
    uint32 data[24]

The gameobject counterpart of SMSG_CREATURE_QUERY_RESPONSE, and the only
place a gameobject's *name* appears. `data` is `GameObjectInfo::raw`, the
union whose meaning depends on `type` (lockId for a chest or door, spellId for
a trap, ...) -- the same 24 values `gameobject_template.data0..23` holds, so
the module keeps them positional instead of naming them per type. It does not
carry faction, flags or size: those are UpdateFields (GAMEOBJECT_FACTION,
GAMEOBJECT_FLAGS, OBJECT_FIELD_SCALE_X) and need SMSG_UPDATE_OBJECT as well.

An entry with bit 31 set is the server saying "no such gameobject" (or, from
World::SendGameObjectStatsInvalidate, "forget what you cached"); there is no
body to decode after it.
"""

from __future__ import annotations

from typing import Any, Iterator

from ..core.base import BaseModule
from ..core.contracts import Column, DecodeContext, Event, Packet, Row, SqlContext, TableSpec
from ..core.reader import ByteReader
from ..core.registry import module

UNKNOWN_ENTRY_BIT = 0x80000000
DATA_COUNT = 24

_TABLE = TableSpec(
    name="capture_gameobject_template",
    columns=(
        Column("capture", "VARCHAR(64)", nullable=False),
        Column("entry", "INT UNSIGNED", nullable=False),
        Column("type", "INT UNSIGNED"),
        Column("display_id", "INT UNSIGNED"),
        Column("name", "VARCHAR(100)"),
        *(Column(f"data{i}", "INT UNSIGNED") for i in range(DATA_COUNT)),
    ),
    key=("capture", "entry"),
    comment="identity as the server answered it; faction, flags and size come from SMSG_UPDATE_OBJECT",
)


@module(id="gameobject_query", opcodes=("SMSG_GAMEOBJECT_QUERY_RESPONSE",), order=11)
class GameObjectQuery(BaseModule):
    text_section = "SMSG_GAMEOBJECT_QUERY_RESPONSE (gameobject identity)"
    text_templates = {
        "gameobject_query": "entry={entry:<7} name={name!r} type={type} display={display_id} "
                            "data={data_text}",
    }
    sql_tables = (_TABLE,)

    def decode(self, pkt: Packet, ctx: DecodeContext) -> Iterator[Event]:
        r = ByteReader(pkt.body, pkt.name or "SMSG_GAMEOBJECT_QUERY_RESPONSE")
        entry = r.u32("entry")
        if entry & UNKNOWN_ENTRY_BIT:
            ctx.log.debug("entry %d unknown to the server; no body follows", entry & ~UNKNOWN_ENTRY_BIT)
            return

        type_ = r.u32("type")
        display_id = r.u32("display_id")
        name = r.cstring("name")
        for slot in range(2, 6):
            r.cstring(f"name{slot}")          # always empty in 1.12.1
        data = [r.u32(f"data{i}") for i in range(DATA_COUNT)]
        yield self.event(pkt, "gameobject_query", entry=entry, name=name, type=type_,
                         display_id=display_id, data=data)

    def text_fields(self, ev: Event) -> dict[str, Any]:
        # Trailing zeros are the unused tail of the union for most types.
        data = list(ev.data["data"])
        while data and data[-1] == 0:
            data.pop()
        return {**ev.data, "data_text": "[" + ", ".join(map(str, data)) + "]"}

    def sql_rows(self, ev: Event, ctx: SqlContext) -> Iterator[Row]:
        values = {k: v for k, v in ev.data.items() if k != "data"}
        values.update({f"data{i}": v for i, v in enumerate(ev.data["data"])})
        yield Row(_TABLE.name, {"capture": ctx.capture_id, **values})
