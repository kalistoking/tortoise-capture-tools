"""SMSG_SPELL_START -- a cast begins: the cast bar the client shows.

`Spell::SendSpellStart` (`Spell.cpp:4613`):

    packGUID cast_item_or_caster
    packGUID caster
    uint32   spellId
    uint16   castFlags   -- CAST_FLAG_*; only AMMO (0x20, projectile visual)
                             gates anything else on the wire
    uint32   timer       -- m_timer, reset to m_casttime just before this send
                             (`Spell.h:432`) -- the cast's full duration in ms,
                             or 0 for an instant cast
    <targets>            -- SpellCastTargets; not decoded, same call as
                             spell_go.py: the head is what behaviour authoring
                             needs, and decoding less than the whole payload
                             is fine

Unlike `spell_go.py`'s head (which only proves a cast happened), `timer` here
is the one fact this opcode alone can give: how long the cast bar ran. It is
read off the wire once, at the moment casting starts -- not polled -- so it is
exact, not measured from packet gaps the way `behaviour.py`'s timers are.
"""

from __future__ import annotations

from typing import Iterator

from ..core.base import BaseModule
from ..core.contracts import Column, DecodeContext, Event, Packet, Row, SqlContext, TableSpec
from ..core.reader import ByteReader, guid_entry, guid_type
from ..core.registry import module

_TABLE = TableSpec(
    name="capture_spell_start",
    columns=(
        Column("capture", "VARCHAR(64)", nullable=False),
        Column("t", "DOUBLE"),
        Column("seq", "INT UNSIGNED", nullable=False),
        Column("guid", "BIGINT UNSIGNED", nullable=False),
        Column("entry", "INT UNSIGNED"),
        Column("spell_id", "INT UNSIGNED", nullable=False),
        Column("cast_time_ms", "INT UNSIGNED"),
    ),
    key=("capture", "seq"),
)


@module(id="spell_start", opcodes=("SMSG_SPELL_START",), order=39)
class SpellStart(BaseModule):
    text_section = "SMSG_SPELL_START (cast begins)"
    text_templates = {
        "spell_start": "entry={entry:<7} caster=0x{guid:016X} {guid_type} spell={spell_id:<6} "
                      "cast_time={cast_time_ms}ms{instant}",
    }
    sql_tables = (_TABLE,)

    def decode(self, pkt: Packet, ctx: DecodeContext) -> Iterator[Event]:
        r = ByteReader(pkt.body, pkt.name or "SMSG_SPELL_START")
        r.packguid("cast_item_or_caster")
        caster = r.packguid("caster")
        spell_id = r.u32("spellId")
        r.u16("castFlags")
        timer_ms = r.u32("timer")
        yield self.event(pkt, "spell_start", guid=caster, entry=guid_entry(caster),
                         guid_type=guid_type(caster), spell_id=spell_id,
                         cast_time_ms=timer_ms, is_instant=timer_ms == 0)

    def text_fields(self, ev: Event):
        data = dict(ev.data)
        data["instant"] = " (instant)" if data.get("is_instant") else ""
        return data

    def sql_rows(self, ev: Event, ctx: SqlContext) -> Iterator[Row]:
        d = ev.data
        yield Row(_TABLE.name, {"capture": ctx.capture_id, "t": ev.packet.t,
                                "seq": ev.packet.seq, "guid": d["guid"], "entry": d["entry"],
                                "spell_id": d["spell_id"], "cast_time_ms": d["cast_time_ms"]})
