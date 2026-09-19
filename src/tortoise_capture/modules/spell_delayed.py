"""SMSG_SPELL_DELAYED -- pushback on the recording player's own cast.

`Spell::Delayed` (`Spell.cpp:7639`):

    if (!m_caster || !m_caster->IsPlayer()) return;   -- unconditional, first line

    uint64 guid        -- m_caster->GetObjectGuid(), PLAIN (8+4 packet size confirms it)
    uint32 delaytime    -- ms added to the cast timer for this hit

Sent via `((Player*)m_caster)->SendDirectMessage(&data)` -- straight to the
casting player's own session, never broadcast. Two consequences worth being
explicit about:

- Unlike every other module here, there is no `has_entry()` case to gate:
  the caster is a player in 100% of instances, not just usually (the
  function returns before doing anything else if it is not), so no entry is
  even attempted -- the same discipline `party_kill.py` applies to its
  always-wrong `killer_entry`.
- The wire carries no `spellId` at all -- only which player, and by how much
  their current cast was pushed back. Attributing a delay to a specific
  spell needs correlating this timestamp against that player's most recent
  `SMSG_SPELL_START`, which is `analyze/`'s job, not this decoder's.
"""

from __future__ import annotations

from typing import Iterator

from ..core.base import BaseModule
from ..core.contracts import Column, DecodeContext, Event, Packet, Row, SqlContext, TableSpec
from ..core.registry import module
from ..core.reader import ByteReader

_TABLE = TableSpec(
    name="capture_spell_delayed",
    columns=(
        Column("capture", "VARCHAR(64)", nullable=False),
        Column("t", "DOUBLE"),
        Column("seq", "INT UNSIGNED", nullable=False),
        Column("guid", "BIGINT UNSIGNED", nullable=False),
        Column("delay_ms", "INT UNSIGNED", nullable=False),
    ),
    key=("capture", "seq"),
)


@module(id="spell_delayed", opcodes=("SMSG_SPELL_DELAYED",), order=42)
class SpellDelayed(BaseModule):
    text_section = "SMSG_SPELL_DELAYED (own cast pushed back by damage)"
    text_templates = {
        "spell_delayed": "guid=0x{guid:016X} pushed back {delay_ms}ms",
    }
    sql_tables = (_TABLE,)

    def decode(self, pkt: Packet, ctx: DecodeContext) -> Iterator[Event]:
        r = ByteReader(pkt.body, pkt.name or "SMSG_SPELL_DELAYED")
        guid = r.u64("guid")
        delay_ms = r.u32("delaytime")
        yield self.event(pkt, "spell_delayed", guid=guid, delay_ms=delay_ms)

    def sql_rows(self, ev: Event, ctx: SqlContext) -> Iterator[Row]:
        d = ev.data
        yield Row(_TABLE.name, {"capture": ctx.capture_id, "t": ev.packet.t,
                                "seq": ev.packet.seq, "guid": d["guid"],
                                "delay_ms": d["delay_ms"]})
