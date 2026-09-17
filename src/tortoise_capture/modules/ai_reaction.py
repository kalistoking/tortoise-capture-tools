"""SMSG_AI_REACTION -- a unit reacted to a threat (Creature.cpp:2246).

    uint64 guid (raw) ; uint32 reactionType

Reaction 2 (AI_REACTION_HOSTILE) is the aggro trigger, and it coincides with
the MONSTER_MOVE STOP that marks combat engagement -- not with death.
"""

from __future__ import annotations

from typing import Iterator

from ..core.base import BaseModule
from ..core.contracts import Column, DecodeContext, Event, Packet, Row, SqlContext, TableSpec
from ..core.reader import ByteReader, guid_entry, guid_type
from ..core.registry import module

_TABLE = TableSpec(
    name="capture_ai_reaction",
    columns=(
        Column("capture", "VARCHAR(64)", nullable=False),
        Column("t", "DOUBLE"),
        Column("seq", "INT UNSIGNED", nullable=False),
        Column("guid", "BIGINT UNSIGNED", nullable=False),
        Column("entry", "INT UNSIGNED"),
        Column("reaction", "INT UNSIGNED"),
    ),
    key=("capture", "seq", "guid"),
)


@module(id="ai_reaction", opcodes=("SMSG_AI_REACTION",), order=50)
class AiReaction(BaseModule):
    text_section = "SMSG_AI_REACTION (aggro trigger)"
    text_templates = {
        "ai_reaction": "entry={entry:<7} guid=0x{guid:016X} {guid_type} reaction={reaction}",
    }
    sql_tables = (_TABLE,)

    def decode(self, pkt: Packet, ctx: DecodeContext) -> Iterator[Event]:
        r = ByteReader(pkt.body, pkt.name or "SMSG_AI_REACTION")
        guid = r.u64("guid")
        yield self.event(pkt, "ai_reaction", guid=guid, entry=guid_entry(guid),
                         guid_type=guid_type(guid), reaction=r.u32("reactionType"))

    def sql_rows(self, ev: Event, ctx: SqlContext) -> Iterator[Row]:
        yield Row(_TABLE.name, {"capture": ctx.capture_id, "t": ev.packet.t,
                                "seq": ev.packet.seq, "guid": ev.data["guid"],
                                "entry": ev.data["entry"], "reaction": ev.data["reaction"]})
