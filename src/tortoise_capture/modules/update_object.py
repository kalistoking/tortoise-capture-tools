"""SMSG_UPDATE_OBJECT (0x0A9) -- object creation, field updates, movement.

Outer framing (UpdateData::BuildPacket):

    uint32 blockCount ; uint8 hasTransport ; blockCount x block

Block layouts (ObjectUpdateType), all from Object.cpp:

    VALUES=0             packGUID + UpdateMask + fields   (no object-type byte:
                         the type must come from the GUID high word)
    MOVEMENT=1           RAW 8-byte guid (not packed!) + BuildMovementUpdate
    CREATE_OBJECT=2/3    packGUID + uint8 typeId + BuildMovementUpdate
                         + UpdateMask + fields
    OUT_OF_RANGE=4       uint32 count + count x packGUID
    NEAR_OBJECTS=5       not observed; stops the message if it ever appears

CREATE blocks carry the full field snapshot -- that is where mindamage,
maxdamage, attack_power, scale and bytes_0 appear, once, at first sighting.
VALUES blocks carry only what changed, in practice health for a unit in
combat. There is no per-viewer field masking in this core, so a creature's
combat stats are broadcast to every observer.
"""

from __future__ import annotations

from typing import Any, Iterator, Mapping

from ..core.base import BaseModule
from ..core.contracts import Column, DecodeContext, Event, Packet, Row, SqlContext, TableSpec
from ..core.reader import ByteReader, WireError, guid_entry, guid_type, is_unit
from ..core.registry import module
from ..fields import values as fv

UPDATETYPE_VALUES = 0
UPDATETYPE_MOVEMENT = 1
UPDATETYPE_CREATE_OBJECT = 2
UPDATETYPE_CREATE_OBJECT2 = 3
UPDATETYPE_OUT_OF_RANGE = 4
UPDATETYPE_NEAR_OBJECTS = 5

UPDATEFLAG_TRANSPORT = 0x02
UPDATEFLAG_MELEE_ATTACKING = 0x04
UPDATEFLAG_HIGHGUID = 0x08
UPDATEFLAG_ALL = 0x10
UPDATEFLAG_LIVING = 0x20
UPDATEFLAG_HAS_POSITION = 0x40

MOVEFLAG_JUMPING = 0x00002000
MOVEFLAG_SWIMMING = 0x00200000
MOVEFLAG_SPLINE_ENABLED = 0x00400000
MOVEFLAG_ONTRANSPORT = 0x02000000
MOVEFLAG_SPLINE_ELEVATION = 0x04000000

SPLINEFLAG_FINAL_POINT = 0x00010000
SPLINEFLAG_FINAL_TARGET = 0x00020000
SPLINEFLAG_FINAL_ANGLE = 0x00040000
SPLINEFLAG_CYCLIC = 0x00100000

# Which fields the text report highlights. Purely a presentation choice, so it
# lives with the module that prints, not with the shared field tables.
INTERESTING = (
    "UNIT_FIELD_DISPLAYID", "UNIT_FIELD_NATIVEDISPLAYID", "UNIT_FIELD_MOUNTDISPLAYID",
    "UNIT_FIELD_LEVEL", "UNIT_FIELD_FACTIONTEMPLATE", "UNIT_FIELD_BYTES_0",
    "UNIT_FIELD_HEALTH", "UNIT_FIELD_MAXHEALTH",
    "UNIT_FIELD_BASE_MANA", "UNIT_FIELD_BASE_HEALTH",
    "UNIT_FIELD_STAT0", "UNIT_FIELD_STAT1", "UNIT_FIELD_STAT2", "UNIT_FIELD_STAT3",
    "UNIT_FIELD_STAT4", "UNIT_FIELD_RESISTANCES",
    "UNIT_FIELD_BOUNDINGRADIUS", "UNIT_FIELD_COMBATREACH",
    "UNIT_FIELD_MINDAMAGE", "UNIT_FIELD_MAXDAMAGE",
    "UNIT_FIELD_MINOFFHANDDAMAGE", "UNIT_FIELD_MAXOFFHANDDAMAGE",
    "UNIT_FIELD_MINRANGEDDAMAGE", "UNIT_FIELD_MAXRANGEDDAMAGE",
    "UNIT_FIELD_ATTACK_POWER", "UNIT_FIELD_ATTACK_POWER_MODS",
    "UNIT_FIELD_ATTACK_POWER_MULTIPLIER",
    "UNIT_FIELD_RANGED_ATTACK_POWER", "UNIT_FIELD_RANGED_ATTACK_POWER_MODS",
    "UNIT_FIELD_RANGED_ATTACK_POWER_MULTIPLIER",
    "OBJECT_FIELD_SCALE_X",
)

_TABLE = TableSpec(
    name="capture_unit_field",
    columns=(
        Column("capture", "VARCHAR(64)", nullable=False),
        Column("t", "DOUBLE"),
        Column("seq", "INT UNSIGNED"),
        Column("guid", "BIGINT UNSIGNED", nullable=False),
        Column("entry", "INT UNSIGNED"),
        Column("block", "VARCHAR(8)"),
        Column("field_index", "INT UNSIGNED", nullable=False),
        Column("field_name", "VARCHAR(64)"),
        Column("raw", "INT UNSIGNED"),
        Column("value", "DOUBLE"),
    ),
    key=("capture", "seq", "guid", "field_index"),
    comment="one row per update field; unit/pet blocks only, where names are valid",
)


# --------------------------------------------------------------------------
# readers (mirror Object.cpp byte for byte)
# --------------------------------------------------------------------------

def read_movement_info(r: ByteReader) -> dict[str, Any]:
    """MovementInfo::Write. fallTime is unconditional -- a classic trap."""
    flags = r.u32("moveFlags")
    info: dict[str, Any] = {"move_flags": flags, "stime": r.u32("stime"), "pos": r.vec4("pos")}
    if flags & MOVEFLAG_ONTRANSPORT:
        info["transport"] = {"guid": r.u64("t_guid"), "pos": r.vec4("t_pos")}
    if flags & MOVEFLAG_SWIMMING:
        info["s_pitch"] = r.f32("s_pitch")
    info["fall_time"] = r.u32("fallTime")
    if flags & MOVEFLAG_JUMPING:
        info["jump"] = r.vec4("jump")          # zspeed, cosAngle, sinAngle, xyspeed
    if flags & MOVEFLAG_SPLINE_ELEVATION:
        info["spline_elevation"] = r.f32("spline_elevation")
    return info


def read_spline_create(r: ByteReader) -> dict[str, Any]:
    """PacketBuilder::WriteCreate -- the spline snapshot of a unit already moving.

    For a patrolling creature this is the normal case at first sighting, not
    an edge case: without it the field payload cannot be reached at all.
    """
    flags = r.u32("splineFlags")
    facing: Any = None
    if flags & SPLINEFLAG_FINAL_ANGLE:
        facing = ("angle", r.f32("final_angle"))
    elif flags & SPLINEFLAG_FINAL_TARGET:
        facing = ("target", r.u64("final_target"))
    elif flags & SPLINEFLAG_FINAL_POINT:
        facing = ("point", r.vec3("final_point"))
    time_passed, duration, spline_id = r.u32("timePassed"), r.u32("duration"), r.u32("splineId")
    nodes = r.u32("nodes")
    points = [r.vec3(f"node{i}") for i in range(nodes)]
    return {"flags": flags, "facing": facing, "time_passed": time_passed, "duration": duration,
            "spline_id": spline_id, "cyclic": bool(flags & SPLINEFLAG_CYCLIC),
            "points": points, "final_dest": r.vec3("finalDest")}


def read_movement_update(r: ByteReader) -> dict[str, Any]:
    """Object::BuildMovementUpdate."""
    flags = r.u8("updateFlags")
    out: dict[str, Any] = {"update_flags": flags}
    if flags & UPDATEFLAG_LIVING:
        info = read_movement_info(r)
        out["movement_info"] = info
        out["speeds"] = r.floats(6, "speeds")   # walk, run, run_back, swim, swim_back, turn
        if info["move_flags"] & MOVEFLAG_SPLINE_ENABLED:
            out["spline"] = read_spline_create(r)
    elif flags & UPDATEFLAG_HAS_POSITION:
        out["position"] = r.vec4("position")
    if flags & UPDATEFLAG_HIGHGUID:
        r.u32("highguid")
    if flags & UPDATEFLAG_ALL:
        r.u32("all")
    if flags & UPDATEFLAG_MELEE_ATTACKING:
        out["victim"] = r.packguid("victim")
    if flags & UPDATEFLAG_TRANSPORT:
        r.u32("transport_time")
    return out


def read_update_fields(r: ByteReader) -> dict[int, int]:
    """UpdateMask then one uint32 per set bit, ascending index.

    uint8 blockCount, blockCount*4 mask bytes; bit i lives at byte i>>3, bit i&7.
    """
    block_count = r.u8("maskBlocks")
    mask = r.raw(block_count * 4, "mask")
    indices = [i for i in range(block_count * 32) if mask[i >> 3] & (1 << (i & 7))]
    return {index: r.u32(f"field{index}") for index in indices}


@module(id="update_object", opcodes=("SMSG_UPDATE_OBJECT",), order=60)
class UpdateObject(BaseModule):
    text_section = "SMSG_UPDATE_OBJECT (combat stats / fields)"
    text_templates = {
        "object_create": "entry={entry:<7} {block} {guid_type}\n{fields_text}",
        "object_values": "entry={entry:<7} {block} {guid_type}\n{fields_text}",
        "object_movement": "entry={entry:<7} MOVEMENT {guid_type} flags=0x{update_flags:02X}",
        "objects_out_of_range": "{count} object(s) left range",
    }
    sql_tables = (_TABLE,)

    def decode(self, pkt: Packet, ctx: DecodeContext) -> Iterator[Event]:
        r = ByteReader(pkt.body, pkt.name or "SMSG_UPDATE_OBJECT")
        block_count = r.u32("blockCount")
        r.u8("hasTransport")
        ctx.log.debug("%s: %d block(s)", pkt.describe(), block_count)

        for index in range(block_count):
            update_type = r.u8("updateType")
            if update_type == UPDATETYPE_OUT_OF_RANGE:
                count = r.u32("count")
                guids = [r.packguid(f"guid{i}") for i in range(count)]
                yield self.event(pkt, "objects_out_of_range", count=count, guids=guids)
            elif update_type == UPDATETYPE_MOVEMENT:
                guid = r.u64("guid")             # raw, not packed, in this block only
                movement = read_movement_update(r)
                yield self.event(pkt, "object_movement", guid=guid, entry=guid_entry(guid),
                                 guid_type=guid_type(guid), update_flags=movement["update_flags"],
                                 movement=movement)
            elif update_type in (UPDATETYPE_CREATE_OBJECT, UPDATETYPE_CREATE_OBJECT2):
                guid = r.packguid("guid")
                object_type = r.u8("objectTypeId")
                movement = read_movement_update(r)
                fields = read_update_fields(r)
                yield self._field_event(pkt, ctx, "object_create", guid, fields,
                                        block="CREATE2" if update_type == UPDATETYPE_CREATE_OBJECT2
                                        else "CREATE", object_type=object_type, movement=movement)
            elif update_type == UPDATETYPE_VALUES:
                guid = r.packguid("guid")
                fields = read_update_fields(r)
                yield self._field_event(pkt, ctx, "object_values", guid, fields, block="VALUES")
            else:
                # NEAR_OBJECTS or garbage: the offset is unrecoverable past an
                # unknown block, so stop this message rather than guess.
                raise WireError(f"unsupported update block type {update_type} "
                                f"(block {index + 1}/{block_count})")

    def _field_event(self, pkt: Packet, ctx: DecodeContext, kind: str, guid: int,
                     fields: Mapping[int, int], **extra: Any) -> Event:
        """Fields as [{index, name, raw}]; names only for units and pets.

        One list rather than parallel index/name maps, so text and SQL read the
        same structure and neither has to re-derive the other's key.
        """
        decoded = [{"index": index, "name": ctx.tables.fields.name_for(index, guid), "raw": raw}
                   for index, raw in sorted(fields.items())]
        return self.event(pkt, kind, guid=guid, entry=guid_entry(guid), guid_type=guid_type(guid),
                          fields=decoded, named_ok=is_unit(guid), **extra)

    # -- text ---------------------------------------------------------------

    def text_fields(self, ev: Event) -> Mapping[str, Any]:
        data = dict(ev.data)
        by_name = {f["name"]: f["raw"] for f in data.get("fields", ()) if f["name"]}
        lines = [f"    {name} = {fv.render(name, by_name[name])}"
                 for name in INTERESTING if name in by_name]
        extra = len(data.get("fields", ())) - len(lines)
        if extra > 0:
            lines.append(f"    (+ {extra} other field(s) not shown)")
        if not data.get("named_ok", True):
            lines.insert(0, "    (field names unavailable: not a unit or pet)")
        data["fields_text"] = "\n".join(lines)
        return data

    # -- sql ----------------------------------------------------------------

    def sql_rows(self, ev: Event, ctx: SqlContext) -> Iterator[Row]:
        if ev.kind not in ("object_create", "object_values") or not ev.data.get("named_ok"):
            return
        for field in ev.data.get("fields", ()):
            yield Row(_TABLE.name, {
                "capture": ctx.capture_id, "t": ev.packet.t, "seq": ev.packet.seq,
                "guid": ev.data["guid"], "entry": ev.data["entry"], "block": ev.data.get("block"),
                "field_index": field["index"], "field_name": field["name"],
                "raw": field["raw"], "value": float(fv.decode(field["name"], field["raw"])),
            })
