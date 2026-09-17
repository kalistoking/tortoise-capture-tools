"""SMSG_LOGIN_VERIFY_WORLD / SMSG_NEW_WORLD -- which map the session is on.

Both opcodes write the identical shape (`CharacterHandler.cpp:574`,
`Player.cpp:2826-2841`): `uint32 mapId` followed by a `Vector4` (x, y, z, o).
For `SMSG_NEW_WORLD` that vector is transport-relative or absolute depending
on whether the player is on a transport, but both branches write exactly four
floats either way, so the header past `mapId` never needs to be told apart to
read `map_id` itself.

This is the only source `creature.map` has: the map a creature was captured
on is inferred from the map the *observing player* was on, not from anything
the creature itself broadcasts -- reasonable for a capture recorded near the
creature, and the only kind of capture this toolkit's key-recovery works on
in the first place (see wire-format.md).

`SMSG_NEW_WORLD` fires on every teleport, so a capture with one is normal;
which sighting a rule should trust (last one before the creature was seen) is
an authoring decision, not this module's.
"""

from __future__ import annotations

from typing import Iterator

from ..core.base import BaseModule
from ..core.contracts import Column, DecodeContext, Event, Packet, Row, SqlContext, TableSpec
from ..core.reader import ByteReader
from ..core.registry import module

_SOURCE = {"SMSG_LOGIN_VERIFY_WORLD": "login", "SMSG_NEW_WORLD": "teleport"}

_TABLE = TableSpec(
    name="capture_world_transfer",
    columns=(
        Column("capture", "VARCHAR(64)", nullable=False),
        Column("t", "DOUBLE"),
        Column("seq", "INT UNSIGNED", nullable=False),
        Column("source", "VARCHAR(16)"),
        Column("map_id", "INT UNSIGNED"),
        Column("x", "DOUBLE"), Column("y", "DOUBLE"), Column("z", "DOUBLE"),
        Column("o", "DOUBLE"),
    ),
    key=("capture", "seq"),
)


@module(id="world_transfer", opcodes=("SMSG_LOGIN_VERIFY_WORLD", "SMSG_NEW_WORLD"), order=5)
class WorldTransfer(BaseModule):
    text_section = "SMSG_LOGIN_VERIFY_WORLD / SMSG_NEW_WORLD (map)"
    text_templates = {
        "world_transfer": "map_id={map_id} ({source}) pos=({x:.2f}, {y:.2f}, {z:.2f})",
    }
    sql_tables = (_TABLE,)

    def decode(self, pkt: Packet, ctx: DecodeContext) -> Iterator[Event]:
        r = ByteReader(pkt.body, pkt.name or "world_transfer")
        map_id = r.u32("mapId")
        x, y, z, o = r.vec4("pos")
        # Not about any one creature -- the observing player's map, not the
        # creature's own broadcast data -- so it must reach an --entry-scoped
        # run regardless of which entry that is. See Event.scope.
        yield self.event(pkt, "world_transfer", scope="session",
                         source=_SOURCE.get(pkt.name, pkt.name), map_id=map_id, x=x, y=y, z=z, o=o)

    def sql_rows(self, ev: Event, ctx: SqlContext) -> Iterator[Row]:
        yield Row(_TABLE.name, {"capture": ctx.capture_id, "t": ev.packet.t,
                                "seq": ev.packet.seq, **ev.data})
