"""SMSG_MONSTER_MOVE (0x0DD) and SMSG_MONSTER_MOVE_TRANSPORT (0x2AE).

PacketBuilder::WriteCommonMonsterMovePart + WriteMonsterMove:

    packGUID unit [+ packGUID transport for the _TRANSPORT variant]
    Vector3 start_pos
    uint32  splineId
    uint8   moveType   0 Normal | 1 Stop | 2 FacingSpot(+Vector3)
                       | 3 FacingTarget(+uint64) | 4 FacingAngle(+float)
    -- Stop ends the packet here --
    uint32  flags
    uint32  duration
    flags & Mask_CatmullRom: uint32 count ; Vector3[count]
    else:                    uint32 lastIdx ; Vector3 dest ; packedOffset[lastIdx-1]

A patrol is sent as a series of point-to-point linear hops, so the ordered
hop destinations are the waypoint list. Turning those hops into
`creature_movement` rows (loop detection, deduplication) is analysis, not
decoding, and belongs in analyze/ -- this module records the hops as captured.
"""

from __future__ import annotations

from typing import Any, Iterator, Mapping

from ..core.base import BaseModule
from ..core.contracts import Column, DecodeContext, Event, Packet, Row, SqlContext, TableSpec
from ..core.reader import ByteReader, guid_entry, guid_type
from ..core.registry import module

SMSG_MONSTER_MOVE_TRANSPORT = 0x2AE

SPLINEFLAG_CYCLIC = 0x00100000
SPLINEFLAG_CATMULLROM = 0x00000200   # MoveSplineFlag::Flying == Mask_CatmullRom

MOVE_NORMAL, MOVE_STOP, MOVE_FACING_SPOT, MOVE_FACING_TARGET, MOVE_FACING_ANGLE = range(5)

_TABLE = TableSpec(
    name="capture_monster_move",
    columns=(
        Column("capture", "VARCHAR(64)", nullable=False),
        Column("t", "DOUBLE"),
        Column("seq", "INT UNSIGNED"),
        Column("guid", "BIGINT UNSIGNED", nullable=False),
        Column("entry", "INT UNSIGNED"),
        Column("move_type", "VARCHAR(16)"),
        Column("spline_id", "INT UNSIGNED"),
        Column("duration_ms", "INT UNSIGNED"),
        Column("point", "INT UNSIGNED", nullable=False),
        Column("x", "DOUBLE"), Column("y", "DOUBLE"), Column("z", "DOUBLE"),
    ),
    key=("capture", "seq", "point"),
    comment="movement hops as captured; waypoint authoring happens downstream",
)


def unpack_offset(packed: int) -> tuple[float, float, float]:
    """ByteBuffer::appendPackXYZ inverted: 11/11/10-bit signed, x0.25 scale."""
    x, y, z = packed & 0x7FF, (packed >> 11) & 0x7FF, (packed >> 22) & 0x3FF
    if x >= 0x400:
        x -= 0x800
    if y >= 0x400:
        y -= 0x800
    if z >= 0x200:
        z -= 0x400
    return x * 0.25, y * 0.25, z * 0.25


@module(id="monster_move", opcodes=("SMSG_MONSTER_MOVE", "SMSG_MONSTER_MOVE_TRANSPORT"), order=70)
class MonsterMove(BaseModule):
    text_section = "SMSG_MONSTER_MOVE / _TRANSPORT (movement)"
    text_templates = {
        "move_stop": "entry={entry:<7} STOP at ({x:.2f}, {y:.2f}, {z:.2f})",
        "move_linear": "entry={entry:<7} -> ({x:.2f}, {y:.2f}, {z:.2f})  {duration_ms}ms  {n_points} pt(s)",
        "move_spline": "entry={entry:<7} catmullrom cyclic={cyclic} {n_points} pts  "
                       "start=({sx:.2f}, {sy:.2f}, {sz:.2f})",
    }
    sql_tables = (_TABLE,)

    def decode(self, pkt: Packet, ctx: DecodeContext) -> Iterator[Event]:
        r = ByteReader(pkt.body, pkt.name or "monster_move")
        guid = r.packguid("unit")
        transport = None
        if pkt.opcode == ctx.tables.opcodes.by_name.get("SMSG_MONSTER_MOVE_TRANSPORT",
                                                        SMSG_MONSTER_MOVE_TRANSPORT):
            transport = r.packguid("transport")

        start = r.vec3("start_pos")
        spline_id = r.u32("splineId")
        move_type = r.u8("moveType")

        common = {"guid": guid, "entry": guid_entry(guid), "guid_type": guid_type(guid),
                  "transport": transport, "spline_id": spline_id, "start": start}
        ctx.log.debug("%s: entry=%d moveType=%d spline=%d", pkt.describe(),
                      common["entry"], move_type, spline_id)

        if move_type == MOVE_STOP:
            yield self.event(pkt, "move_stop", **common, points=[start])
            return
        if move_type == MOVE_FACING_SPOT:
            r.vec3("facing_spot")
        elif move_type == MOVE_FACING_TARGET:
            r.u64("facing_target")
        elif move_type == MOVE_FACING_ANGLE:
            r.f32("facing_angle")

        flags = r.u32("flags")
        duration = r.u32("duration")

        if flags & SPLINEFLAG_CATMULLROM:
            count = r.u32("count")
            points = [r.vec3(f"point{i}") for i in range(count)]
            yield self.event(pkt, "move_spline", **common, flags=flags, duration_ms=duration,
                             cyclic=bool(flags & SPLINEFLAG_CYCLIC), points=points)
            return

        last_index = r.u32("lastIdx")
        dest = r.vec3("dest")
        # Intermediate points are stored as offsets from the destination.
        points = []
        for i in range(max(last_index - 1, 0)):
            ox, oy, oz = unpack_offset(r.u32(f"offset{i}"))
            points.append((dest[0] - ox, dest[1] - oy, dest[2] - oz))
        points.append(dest)
        yield self.event(pkt, "move_linear", **common, flags=flags, duration_ms=duration,
                         dest=dest, points=points)

    # -- text ---------------------------------------------------------------

    def text_fields(self, ev: Event) -> Mapping[str, Any]:
        data = dict(ev.data)
        target = data.get("dest") or data["start"]
        data.update(x=target[0], y=target[1], z=target[2],
                    sx=data["start"][0], sy=data["start"][1], sz=data["start"][2],
                    n_points=len(data.get("points", [])))
        return data

    # -- sql ----------------------------------------------------------------

    def sql_rows(self, ev: Event, ctx: SqlContext) -> Iterator[Row]:
        kind = {"move_stop": "stop", "move_linear": "linear", "move_spline": "catmullrom"}[ev.kind]
        for index, (x, y, z) in enumerate(ev.data.get("points", []), start=1):
            yield Row(_TABLE.name, {
                "capture": ctx.capture_id, "t": ev.packet.t, "seq": ev.packet.seq,
                "guid": ev.data["guid"], "entry": ev.data["entry"], "move_type": kind,
                "spline_id": ev.data["spline_id"], "duration_ms": ev.data.get("duration_ms"),
                "point": index, "x": x, "y": y, "z": z,
            })
