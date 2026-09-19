"""SMSG_SPELL_FAILED_OTHER -- a cast in progress was cancelled.

`Spell::SendInterrupted` (`Spell.cpp:4926`):

    uint64 guid      -- GetObjectGuid(), a PLAIN 64-bit value, not a packGUID
                        (the 8-byte-plus-4 packet size at Spell.cpp:4931 confirms it)
    uint32 spellId

Despite the opcode's name, this is narrower than a general cast failure: it
fires from `Spell::cancel()` whenever a preparing, delayed or in-progress
cast is stopped -- interrupted by damage, a stun, movement, a manual cancel,
whatever -- and carries no reason code at all, only "this cast stopped".

That narrowness is exactly what makes it useful here. `SMSG_CAST_RESULT`
(`modules/cast_result.py`) is player-only (`Spell.cpp:4569` returns early for
any non-player caster) -- for a *creature's* interrupted cast, this broadcast
is the only wire signal there ever is. A player's own interruption shows up
on both opcodes together (`Spell.cpp:3706`: `SendInterrupted()` then
`SendCastResult(SPELL_FAILED_INTERRUPTED)`); a creature's shows up only here.
"""

from __future__ import annotations

from typing import Iterator

from ..core.base import BaseModule
from ..core.contracts import Column, DecodeContext, Event, Packet, Row, SqlContext, TableSpec
from ..core.reader import ByteReader, guid_entry, guid_type, has_entry
from ..core.registry import module

_TABLE = TableSpec(
    name="capture_spell_interrupted",
    columns=(
        Column("capture", "VARCHAR(64)", nullable=False),
        Column("t", "DOUBLE"),
        Column("seq", "INT UNSIGNED", nullable=False),
        Column("guid", "BIGINT UNSIGNED", nullable=False),
        Column("entry", "INT UNSIGNED"),
        Column("spell_id", "INT UNSIGNED", nullable=False),
    ),
    key=("capture", "seq"),
)


@module(id="spell_interrupted", opcodes=("SMSG_SPELL_FAILED_OTHER",), order=41)
class SpellInterrupted(BaseModule):
    text_section = "SMSG_SPELL_FAILED_OTHER (cast interrupted, no reason given)"
    text_templates = {
        "spell_interrupted": "entry={entry:<7} guid=0x{guid:016X} {guid_type} spell={spell_id}",
    }
    sql_tables = (_TABLE,)

    def decode(self, pkt: Packet, ctx: DecodeContext) -> Iterator[Event]:
        r = ByteReader(pkt.body, pkt.name or "SMSG_SPELL_FAILED_OTHER")
        guid = r.u64("guid")
        spell_id = r.u32("spellId")
        data = {"guid": guid, "guid_type": guid_type(guid), "spell_id": spell_id}
        # Any caster type can have a cast interrupted -- a player's guid
        # carries no creature_template entry.
        if has_entry(guid):
            data["entry"] = guid_entry(guid)
        yield self.event(pkt, "spell_interrupted", **data)

    def text_fields(self, ev: Event):
        data = dict(ev.data)
        data.setdefault("entry", "-")
        return data

    def sql_rows(self, ev: Event, ctx: SqlContext) -> Iterator[Row]:
        d = ev.data
        yield Row(_TABLE.name, {"capture": ctx.capture_id, "t": ev.packet.t,
                                "seq": ev.packet.seq, "guid": d["guid"],
                                "entry": d.get("entry"), "spell_id": d["spell_id"]})
