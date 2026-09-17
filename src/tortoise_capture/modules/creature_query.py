"""SMSG_CREATURE_QUERY_RESPONSE -- creature identity (QueryHandler.cpp:186).

    uint32 entry ; CString name ; CString name2,3,4 (always empty)
    CString subname
    uint32 type_flags, type, beast_family, rank, unk, pet_spell_list_id, display_id
    uint8 civilian ; uint8 racial_leader

This is the only place a creature's *name* appears, which is what makes a
capture readable. It does not carry unit_class, scale or damage -- those are
UpdateFields only, so creature_template authoring needs both this and
SMSG_UPDATE_OBJECT.

An entry with bit 31 set is the server saying "no such creature"; there is no
body to decode after it.
"""

from __future__ import annotations

from typing import Iterator

from ..core.base import BaseModule
from ..core.contracts import Column, DecodeContext, Event, Packet, Row, SqlContext, TableSpec
from ..core.reader import ByteReader
from ..core.registry import module

UNKNOWN_ENTRY_BIT = 0x80000000

_TABLE = TableSpec(
    name="capture_creature_template",
    columns=(
        Column("capture", "VARCHAR(64)", nullable=False),
        Column("entry", "INT UNSIGNED", nullable=False),
        Column("name", "VARCHAR(100)"),
        Column("subname", "VARCHAR(100)"),
        Column("type_flags", "INT UNSIGNED"),
        Column("type", "INT UNSIGNED"),
        Column("beast_family", "INT UNSIGNED"),
        Column("rank", "INT UNSIGNED"),
        Column("pet_spell_list_id", "INT UNSIGNED"),
        Column("display_id", "INT UNSIGNED"),
        Column("civilian", "TINYINT UNSIGNED"),
        Column("racial_leader", "TINYINT UNSIGNED"),
    ),
    key=("capture", "entry"),
    comment="identity as the server answered it; stats come from SMSG_UPDATE_OBJECT",
)


@module(id="creature_query", opcodes=("SMSG_CREATURE_QUERY_RESPONSE",), order=10)
class CreatureQuery(BaseModule):
    text_section = "SMSG_CREATURE_QUERY_RESPONSE (creature identity)"
    text_templates = {
        "creature_query": "entry={entry:<7} name={name!r} subname={subname!r} "
                          "type={type} rank={rank} display={display_id}",
    }
    sql_tables = (_TABLE,)

    def decode(self, pkt: Packet, ctx: DecodeContext) -> Iterator[Event]:
        r = ByteReader(pkt.body, pkt.name or "SMSG_CREATURE_QUERY_RESPONSE")
        entry = r.u32("entry")
        if entry & UNKNOWN_ENTRY_BIT:
            ctx.log.debug("entry %d unknown to the server; no body follows", entry & ~UNKNOWN_ENTRY_BIT)
            return

        name = r.cstring("name")
        for slot in range(2, 5):
            r.cstring(f"name{slot}")          # always empty in 1.12.1
        subname = r.cstring("subname")
        type_flags = r.u32("type_flags")
        type_ = r.u32("type")
        beast_family = r.u32("beast_family")
        rank = r.u32("rank")
        r.u32("unk")
        pet_spell_list_id = r.u32("pet_spell_list_id")
        display_id = r.u32("display_id")
        yield self.event(pkt, "creature_query", entry=entry, name=name, subname=subname,
                         type_flags=type_flags, type=type_, beast_family=beast_family,
                         rank=rank, pet_spell_list_id=pet_spell_list_id, display_id=display_id,
                         civilian=r.u8("civilian"), racial_leader=r.u8("racial_leader"))

    def sql_rows(self, ev: Event, ctx: SqlContext) -> Iterator[Row]:
        yield Row(_TABLE.name, {"capture": ctx.capture_id, **ev.data})
