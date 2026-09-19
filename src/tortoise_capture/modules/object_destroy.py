"""SMSG_DESTROY_OBJECT -- an object leaves this client's known-object set.

`Object::DestroyForPlayer` (`Object.cpp:406`):

    uint64 guid   -- GetObjectGuid(), a PLAIN 64-bit value, not a packGUID
                     (the 8-byte packet size at Object.cpp:410 confirms it)

Sent to one observing client at a time, for any object type -- not just
creatures. `WorldObject::DestroyForNearbyPlayers` (`Object.cpp:2685`, called
from `Creature::DisappearAndDie`) is the corpse-fade-out path: this is the
wire-level counterpart of a creature's corpse finally vanishing, the other
half of the CREATE/VALUES respawn story in `analyze/behaviour.py` (see its
"a respawn is not always a CREATE" note) -- a creature that leaves visibility
this way needs a fresh CREATE_OBJECT to come back, one that never loses
visibility does not.
"""

from __future__ import annotations

from typing import Iterator

from ..core.base import BaseModule
from ..core.contracts import Column, DecodeContext, Event, Packet, Row, SqlContext, TableSpec
from ..core.reader import ByteReader, guid_entry, guid_type, has_entry
from ..core.registry import module

_TABLE = TableSpec(
    name="capture_object_destroy",
    columns=(
        Column("capture", "VARCHAR(64)", nullable=False),
        Column("t", "DOUBLE"),
        Column("seq", "INT UNSIGNED", nullable=False),
        Column("guid", "BIGINT UNSIGNED", nullable=False),
        Column("entry", "INT UNSIGNED"),
    ),
    key=("capture", "seq"),
)


@module(id="object_destroy", opcodes=("SMSG_DESTROY_OBJECT",), order=61)
class ObjectDestroy(BaseModule):
    text_section = "SMSG_DESTROY_OBJECT (object leaves visibility)"
    text_templates = {
        "object_destroy": "entry={entry:<7} guid=0x{guid:016X} {guid_type}",
    }
    sql_tables = (_TABLE,)

    def decode(self, pkt: Packet, ctx: DecodeContext) -> Iterator[Event]:
        r = ByteReader(pkt.body, pkt.name or "SMSG_DESTROY_OBJECT")
        guid = r.u64("guid")
        data = {"guid": guid, "guid_type": guid_type(guid)}
        if has_entry(guid):
            data["entry"] = guid_entry(guid)
        yield self.event(pkt, "object_destroy", **data)

    def text_fields(self, ev: Event):
        # entry is only present for entry-bearing guids (see has_entry() in
        # decode()); the template still needs a value for the rest.
        data = dict(ev.data)
        data.setdefault("entry", "-")
        return data

    def sql_rows(self, ev: Event, ctx: SqlContext) -> Iterator[Row]:
        d = ev.data
        yield Row(_TABLE.name, {"capture": ctx.capture_id, "t": ev.packet.t,
                                "seq": ev.packet.seq, "guid": d["guid"],
                                "entry": d.get("entry")})
