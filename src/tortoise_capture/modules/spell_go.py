"""SMSG_SPELL_GO -- a spell was cast (Spell.cpp:4662).

    packGUID cast_item_or_caster ; packGUID caster ; uint32 spellId
    uint16 castFlags ; <targets>

Only the head is decoded: caster and spell id are what creature behaviour
authoring needs. The target block layout varies by cast flags and is left for
a later pass -- decoding less than the whole payload is fine, the runner does
not require a module to consume every byte.
"""

from __future__ import annotations

from typing import Iterator

from ..core.base import BaseModule
from ..core.contracts import Column, DecodeContext, Event, Packet, Row, SqlContext, TableSpec
from ..core.reader import ByteReader, guid_entry, guid_type
from ..core.registry import module

_TABLE = TableSpec(
    name="capture_spell_cast",
    columns=(
        Column("capture", "VARCHAR(64)", nullable=False),
        Column("t", "DOUBLE"),
        Column("seq", "INT UNSIGNED", nullable=False),
        Column("caster_guid", "BIGINT UNSIGNED", nullable=False),
        Column("entry", "INT UNSIGNED"),
        Column("spell_id", "INT UNSIGNED", nullable=False),
    ),
    key=("capture", "seq"),
)


@module(id="spell_go", opcodes=("SMSG_SPELL_GO",), order=40)
class SpellGo(BaseModule):
    text_section = "SMSG_SPELL_GO (spell casts)"
    text_templates = {
        "spell_go": "entry={entry:<7} caster=0x{guid:016X} {guid_type} spell={spell_id}",
    }
    sql_tables = (_TABLE,)

    def decode(self, pkt: Packet, ctx: DecodeContext) -> Iterator[Event]:
        r = ByteReader(pkt.body, pkt.name or "SMSG_SPELL_GO")
        r.packguid("cast_item_or_caster")
        caster = r.packguid("caster")
        yield self.event(pkt, "spell_go", guid=caster, entry=guid_entry(caster),
                         guid_type=guid_type(caster), spell_id=r.u32("spellId"),
                         cast_flags=r.u16("castFlags"))

    def sql_rows(self, ev: Event, ctx: SqlContext) -> Iterator[Row]:
        yield Row(_TABLE.name, {"capture": ctx.capture_id, "t": ev.packet.t,
                                "seq": ev.packet.seq, "caster_guid": ev.data["guid"],
                                "entry": ev.data["entry"], "spell_id": ev.data["spell_id"]})
