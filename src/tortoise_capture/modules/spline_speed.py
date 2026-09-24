"""SMSG_SPLINE_SET_*_SPEED / SMSG_SPLINE_MOVE_SET_*_MODE -- a server-moved unit's pace.

    speed:  packGUID unit ; float speed      (MovementPacketSender.cpp:118-131)
    mode:   packGUID unit                    (Unit.cpp:11487)

The spline forms are the ones for units the server moves (MovementPacketSender
.cpp:25); a player-controlled unit gets the FORCE_/MSG_MOVE_ forms instead. The
float is the speed itself, `rate x baseMoveSpeed` -- Ralthas broadcasts 8.0 run,
7.0 x a `speed_run` of 1.14286.

What these are not is the template's speed. Most of them record a change from
it: in the Ralthas and Rakameg captures the creature alternates between 8.0 and
5.6, a 30% slow landing and falling off. And MoveSplineInit.cpp:173-187 sends a
run speed of its own around a charge -- the charge's velocity, then the real
speed back. The speed a creature was created with is in its CREATE block's
movement data; these are the changes after it.
"""

from __future__ import annotations

from typing import Iterator

from ..core.base import BaseModule
from ..core.contracts import Column, DecodeContext, Event, Packet, Row, SqlContext, TableSpec
from ..core.reader import ByteReader, guid_entry, guid_type, has_entry
from ..core.registry import module

# The order of moveTypeToOpcode (MovementPacketSender.cpp:26-34).
_SPEEDS = {
    "SMSG_SPLINE_SET_WALK_SPEED": "walk",
    "SMSG_SPLINE_SET_RUN_SPEED": "run",
    "SMSG_SPLINE_SET_RUN_BACK_SPEED": "run_back",
    "SMSG_SPLINE_SET_SWIM_SPEED": "swim",
    "SMSG_SPLINE_SET_SWIM_BACK_SPEED": "swim_back",
    "SMSG_SPLINE_SET_TURN_RATE": "turn_rate",
}
_MODES = {
    "SMSG_SPLINE_MOVE_SET_WALK_MODE": "walk",
    "SMSG_SPLINE_MOVE_SET_RUN_MODE": "run",
}

_TABLE = TableSpec(
    name="capture_spline_speed",
    columns=(
        Column("capture", "VARCHAR(64)", nullable=False),
        Column("t", "DOUBLE"),
        Column("seq", "INT UNSIGNED", nullable=False),
        Column("guid", "BIGINT UNSIGNED", nullable=False),
        Column("entry", "INT UNSIGNED"),
        Column("move_type", "VARCHAR(16)", nullable=False),   # a speed's movement, or a mode
        Column("speed", "FLOAT"),                              # NULL for a mode switch
    ),
    key=("capture", "seq", "guid", "move_type"),
)


@module(id="spline_speed", opcodes=(*_SPEEDS, *_MODES), order=45)
class SplineSpeed(BaseModule):
    text_section = "SMSG_SPLINE_SET_*_SPEED / SMSG_SPLINE_MOVE_SET_*_MODE (pace)"
    text_templates = {
        "speed_change": "entry={entry:<7} guid=0x{guid:016X} {move_type} speed {speed:.4f}",
        "move_mode": "entry={entry:<7} guid=0x{guid:016X} {mode} mode",
    }
    sql_tables = (_TABLE,)

    def decode(self, pkt: Packet, ctx: DecodeContext) -> Iterator[Event]:
        r = ByteReader(pkt.body, pkt.name or "SMSG_SPLINE_SET_SPEED")
        guid = r.packguid("unit")
        data = {"guid": guid, "guid_type": guid_type(guid)}
        # A player's pace has no creature_template entry to carry.
        if has_entry(guid):
            data["entry"] = guid_entry(guid)
        if pkt.name in _MODES:
            yield self.event(pkt, "move_mode", mode=_MODES[pkt.name], **data)
        else:
            yield self.event(pkt, "speed_change", move_type=_SPEEDS[pkt.name],
                             speed=r.f32("speed"), **data)

    def text_fields(self, ev: Event):
        data = dict(ev.data)
        data.setdefault("entry", "-")
        return data

    def sql_rows(self, ev: Event, ctx: SqlContext) -> Iterator[Row]:
        move_type = ev.data.get("move_type") or f"{ev.data['mode']}_mode"
        yield Row(_TABLE.name, {"capture": ctx.capture_id, "t": ev.packet.t,
                                "seq": ev.packet.seq, "guid": ev.data["guid"],
                                "entry": ev.data.get("entry"), "move_type": move_type,
                                "speed": ev.data.get("speed")})
