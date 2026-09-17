"""SMSG_PLAY_SOUND -- a sound played, with no sender at all.

`WorldObject::PlayDirectSound` (`Object.cpp`) writes exactly `uint32
sound_id` and nothing else -- no caster guid, no position, no target. This is
what `DoScriptText` calls when a `broadcast_text` row has a nonzero
`sound_id` (`ScriptMgr.cpp`), so a sound that accompanies a creature's line
is genuinely unattributable from this packet alone: only correlation with a
`monster_say`/`monster_yell` at (nearly) the same timestamp can connect the
two, and only when that correlation is unambiguous -- see
`analyze/behaviour.py`'s `_sound_for`.

Distinct from `SMSG_PLAY_OBJECT_SOUND`, which *does* carry a raw source guid
(`Object.cpp`'s `PlayDistanceSound`) and is not decoded here.
"""

from __future__ import annotations

from typing import Iterator

from ..core.base import BaseModule
from ..core.contracts import Column, DecodeContext, Event, Packet, Row, SqlContext, TableSpec
from ..core.reader import ByteReader
from ..core.registry import module

_TABLE = TableSpec(
    name="capture_play_sound",
    columns=(
        Column("capture", "VARCHAR(64)", nullable=False),
        Column("t", "DOUBLE"),
        Column("seq", "INT UNSIGNED", nullable=False),
        Column("sound_id", "INT UNSIGNED", nullable=False),
    ),
    key=("capture", "seq"),
)


@module(id="play_sound", opcodes=("SMSG_PLAY_SOUND",), order=25)
class PlaySound(BaseModule):
    text_section = "SMSG_PLAY_SOUND (unattributed)"
    text_templates = {"play_sound": "sound_id={sound_id}"}
    sql_tables = (_TABLE,)

    def decode(self, pkt: Packet, ctx: DecodeContext) -> Iterator[Event]:
        r = ByteReader(pkt.body, pkt.name or "SMSG_PLAY_SOUND")
        # No guid on the wire: no entry/guid key, so an --entry/--guid run
        # correctly drops this at the filter, same as MONSTER_EMOTE.
        yield self.event(pkt, "play_sound", sound_id=r.u32("soundId"))

    def sql_rows(self, ev: Event, ctx: SqlContext) -> Iterator[Row]:
        yield Row(_TABLE.name, {"capture": ctx.capture_id, "t": ev.packet.t,
                                "seq": ev.packet.seq, "sound_id": ev.data["sound_id"]})
