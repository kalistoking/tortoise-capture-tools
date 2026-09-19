"""SMSG_EMOTE -- a visual (non-text) emote.

`Unit::HandleEmoteCommand` (`Unit.cpp:1987`):

    uint32 emote_id
    uint64 guid     -- GetObjectGuid(), PLAIN (the 4+8 packet size confirms it)

Fired overwhelmingly from creature AI scripts (`m_creature->
HandleEmoteCommand(...)` appears throughout `scripts/`) -- this is the direct
wire signal for a scripted creature emote, the counterpart to `monster_say`/
`monster_yell` for the non-text half of `creature_ai_scripts` (command 1,
EMOTE, vs. command 0, TALK, which `author/dialogue.py` already covers).
Also fired for players and aura-driven emotes (a `/dance` command, a food
buff's `EMOTE_ONESHOT_EAT`, the `EMOTE_ONESHOT_WOUNDCRITICAL` flinch on
taking a critical hit -- `Unit.cpp:1837` gates it on `HITINFO_CRITICALHIT`,
not on health) -- same has_entry() gate as every other module here, not
assumed to be creature-only just because most call sites are.

`emote_id` is left as a raw int: it indexes the client's `Emotes.dbc`, which
this toolkit has no access to and does not attempt to name.
"""

from __future__ import annotations

from typing import Iterator

from ..core.base import BaseModule
from ..core.contracts import Column, DecodeContext, Event, Packet, Row, SqlContext, TableSpec
from ..core.reader import ByteReader, guid_entry, guid_type, has_entry
from ..core.registry import module

_TABLE = TableSpec(
    name="capture_emote",
    columns=(
        Column("capture", "VARCHAR(64)", nullable=False),
        Column("t", "DOUBLE"),
        Column("seq", "INT UNSIGNED", nullable=False),
        Column("guid", "BIGINT UNSIGNED", nullable=False),
        Column("entry", "INT UNSIGNED"),
        Column("emote_id", "INT UNSIGNED", nullable=False),
    ),
    key=("capture", "seq"),
)


@module(id="emote", opcodes=("SMSG_EMOTE",), order=21)
class Emote(BaseModule):
    text_section = "SMSG_EMOTE (visual emote)"
    text_templates = {
        "emote": "entry={entry:<7} {guid_type} emote_id={emote_id}",
    }
    sql_tables = (_TABLE,)

    def decode(self, pkt: Packet, ctx: DecodeContext) -> Iterator[Event]:
        r = ByteReader(pkt.body, pkt.name or "SMSG_EMOTE")
        emote_id = r.u32("emoteId")
        guid = r.u64("guid")
        data = {"guid": guid, "guid_type": guid_type(guid), "emote_id": emote_id}
        if has_entry(guid):
            data["entry"] = guid_entry(guid)
        yield self.event(pkt, "emote", **data)

    def text_fields(self, ev: Event):
        data = dict(ev.data)
        data.setdefault("entry", "-")
        return data

    def sql_rows(self, ev: Event, ctx: SqlContext) -> Iterator[Row]:
        d = ev.data
        yield Row(_TABLE.name, {"capture": ctx.capture_id, "t": ev.packet.t,
                                "seq": ev.packet.seq, "guid": d["guid"],
                                "entry": d.get("entry"), "emote_id": d["emote_id"]})
