"""SMSG_MESSAGECHAT -- monster say / yell / emote (Chat.cpp:2254).

    uint8 msgType ; uint32 language
    MONSTER_SAY (0x0B) / MONSTER_YELL (0x0C):
        uint64 sender (raw) ; uint32 nameLen ; name ; uint64 target (raw)
        ; uint32 msgLen ; message
    MONSTER_EMOTE (0x0D):
        uint32 nameLen ; name ; uint64 target (raw) ; uint32 msgLen ; message

MONSTER_EMOTE carries no sender GUID, only the name string, so it cannot be
attributed to an entry -- it is emitted without the `entry` key and therefore
drops out of `--entry` runs, which is the honest result.

Player and system chat types have their own layouts and are reported as seen
but undecoded rather than guessed at.
"""

from __future__ import annotations

from typing import Iterator

from ..core.base import BaseModule
from ..core.contracts import Column, DecodeContext, Event, Packet, Row, SqlContext, TableSpec
from ..core.reader import ByteReader, guid_entry, guid_type
from ..core.registry import module

CHAT_MSG_MONSTER_SAY = 0x0B
CHAT_MSG_MONSTER_YELL = 0x0C
CHAT_MSG_MONSTER_EMOTE = 0x0D

_KINDS = {CHAT_MSG_MONSTER_SAY: ("monster_say", "SAY"),
          CHAT_MSG_MONSTER_YELL: ("monster_yell", "YELL")}

_TABLE = TableSpec(
    name="capture_monster_chat",
    columns=(
        Column("capture", "VARCHAR(64)", nullable=False),
        Column("t", "DOUBLE"),
        Column("seq", "INT UNSIGNED", nullable=False),
        Column("chat_type", "VARCHAR(16)"),
        Column("guid", "BIGINT UNSIGNED"),
        Column("entry", "INT UNSIGNED"),
        Column("sender", "VARCHAR(64)"),
        Column("message", "TEXT"),
    ),
    key=("capture", "seq"),
)


@module(id="messagechat", opcodes=("SMSG_MESSAGECHAT",), order=20)
class MessageChat(BaseModule):
    text_section = "SMSG_MESSAGECHAT (monster say / yell / emote)"
    text_templates = {
        "monster_say": "[SAY]   {sender!r}: {message!r}",
        "monster_yell": "[YELL]  {sender!r}: {message!r}",
        "monster_emote": "[EMOTE] {sender!r}: {message!r}",
        "chat_other": "(chat type 0x{chat_type:02X} not decoded, {size} bytes)",
    }
    sql_tables = (_TABLE,)

    def decode(self, pkt: Packet, ctx: DecodeContext) -> Iterator[Event]:
        r = ByteReader(pkt.body, pkt.name or "SMSG_MESSAGECHAT")
        msg_type = r.u8("msgType")
        r.u32("language")

        if msg_type in _KINDS:
            kind, label = _KINDS[msg_type]
            sender_guid = r.u64("sender")
            sender = r.sized_string("senderName")
            r.u64("target")
            yield self.event(pkt, kind, guid=sender_guid, entry=guid_entry(sender_guid),
                             guid_type=guid_type(sender_guid), label=label, sender=sender,
                             message=r.sized_string("message"))
        elif msg_type == CHAT_MSG_MONSTER_EMOTE:
            sender = r.sized_string("senderName")
            r.u64("target")
            # No sender guid on the wire: no entry key, so --entry skips it.
            yield self.event(pkt, "monster_emote", sender=sender,
                             message=r.sized_string("message"))
        else:
            yield self.event(pkt, "chat_other", chat_type=msg_type, size=len(pkt.body))

    def sql_rows(self, ev: Event, ctx: SqlContext) -> Iterator[Row]:
        if ev.kind == "chat_other":
            return
        yield Row(_TABLE.name, {"capture": ctx.capture_id, "t": ev.packet.t,
                                "seq": ev.packet.seq, "chat_type": ev.kind,
                                "guid": ev.data.get("guid"), "entry": ev.data.get("entry"),
                                "sender": ev.data["sender"], "message": ev.data["message"]})
