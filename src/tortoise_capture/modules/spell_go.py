"""SMSG_SPELL_GO -- a spell was cast, and at whom (`Spell::SendSpellGo`, Spell.cpp:4649).

    packGUID cast_item_or_caster
    packGUID caster              -- may be an EMPTY packguid: the spell 10444
                                    hack writes one to suppress the animation
    uint32   spellId
    uint16   castFlags
    uint8    hitCount  ; hitCount  x uint64 target          (RAW guid, not packed)
    uint8    missCount ; missCount x uint64 target + uint8 missCondition
                                    + uint8 reflectResult, IFF the condition is
                                      SPELL_MISS_REFLECT (11)
    SpellCastTargets                -- `SpellCastTargets::write`, Spell.cpp:228
    uint32 ammoDisplayId ; uint32 ammoInventoryType         -- iff CAST_FLAG_AMMO

`SpellCastTargets` is written **unconditionally** (Spell.cpp:4682) and gates
its own contents on a `uint16` mask of its own, not on castFlags:

    uint16 targetMask
    if mask & (UNIT|PVP_CORPSE|OBJECT|CORPSE|UNK2): packGUID, or a lone 0 byte
    if mask & (ITEM|TRADE_ITEM):                    packGUID, or a lone 0 byte
    if mask & SOURCE_LOCATION:                      3 x float
    if mask & DEST_LOCATION:                        3 x float
    if mask & STRING:                               CString

Three traps, and the first one is the expensive kind:

  * **the reflect byte.** A miss entry is nine bytes -- except when its
    condition is `SPELL_MISS_REFLECT`, where Spell.cpp:4798 appends a tenth.
    Reading misses as fixed-width leaves that byte on the floor and every
    field after it, the target block and the ammo, decodes as garbage.
  * the two target lists carry **raw** uint64 guids, while the two guids in the
    head and every guid inside the target block are **packed**. Both encodings
    appear in one packet.
  * `ammoDisplayId` is a `uint32`, not the `uint16` its name in some references
    suggests (Spell.cpp:4754-4755).

The head alone was decoded until 2026-09-23, which was enough for creature
behaviour authoring -- it asks *did this creature cast?* The rest is what a
replay needs, because it asks *at whom*: a hit list is what makes a missile
fly, and a `dest` is where a ground-targeted cast was actually put. Nothing
about authoring changed; `capture_spell_cast` still holds what it held.
"""

from __future__ import annotations

from typing import Iterator

from ..core.base import BaseModule
from ..core.contracts import Column, DecodeContext, Event, Packet, Row, SqlContext, TableSpec
from ..core.reader import ByteReader, guid_entry, guid_type, has_entry
from ..core.registry import module

SPELL_MISS_REFLECT = 11          # SpellDefines.h:106
CAST_FLAG_AMMO = 0x0020          # Spell.h:67

# SpellCastTargets' own mask (DBCEnums.h:131-148). Only the flags that change
# how many bytes follow are named here; the rest never add payload.
TARGET_FLAG_UNIT = 0x00000002
TARGET_FLAG_ITEM = 0x00000010
TARGET_FLAG_SOURCE_LOCATION = 0x00000020
TARGET_FLAG_DEST_LOCATION = 0x00000040
TARGET_FLAG_OBJECT_UNK = 0x00000080
TARGET_FLAG_PVP_CORPSE = 0x00000200
TARGET_FLAG_OBJECT = 0x00000800
TARGET_FLAG_TRADE_ITEM = 0x00001000
TARGET_FLAG_STRING = 0x00002000
TARGET_FLAG_CORPSE = 0x00008000
TARGET_FLAG_UNK2 = 0x00010000

_GUID_FLAGS = (TARGET_FLAG_UNIT | TARGET_FLAG_PVP_CORPSE | TARGET_FLAG_OBJECT
               | TARGET_FLAG_CORPSE | TARGET_FLAG_UNK2)

_TABLE = TableSpec(
    name="capture_spell_cast",
    columns=(
        Column("capture", "VARCHAR(64)", nullable=False),
        Column("t", "DOUBLE"),
        Column("seq", "INT UNSIGNED", nullable=False),
        Column("caster_guid", "BIGINT UNSIGNED", nullable=False),
        Column("entry", "INT UNSIGNED"),
        Column("spell_id", "INT UNSIGNED", nullable=False),
    ),
    key=("capture", "seq"),
)


def _read_cast_targets(r: ByteReader) -> dict:
    """`SpellCastTargets::write` (Spell.cpp:228), branch for branch.

    The guid branches mirror the C++ if/else-if chain rather than testing each
    flag independently: a mask carrying both UNIT and CORPSE writes ONE guid,
    the unit's, and reading two would desync the rest.
    """
    mask = r.u16("targetMask")
    out: dict = {"target_mask": mask}
    if mask & _GUID_FLAGS:
        if mask & TARGET_FLAG_UNIT:
            out["target_guid"] = r.packguid("unitTarget")
        elif mask & (TARGET_FLAG_OBJECT | TARGET_FLAG_OBJECT_UNK):
            out["target_guid"] = r.packguid("goTarget")
        elif mask & (TARGET_FLAG_CORPSE | TARGET_FLAG_PVP_CORPSE):
            out["target_guid"] = r.packguid("corpseTarget")
        else:
            r.u8("noTarget")
    if mask & (TARGET_FLAG_ITEM | TARGET_FLAG_TRADE_ITEM):
        out["item_guid"] = r.packguid("itemTarget")
    if mask & TARGET_FLAG_SOURCE_LOCATION:
        out["source"] = [r.f32("srcX"), r.f32("srcY"), r.f32("srcZ")]
    if mask & TARGET_FLAG_DEST_LOCATION:
        out["dest"] = [r.f32("destX"), r.f32("destY"), r.f32("destZ")]
    if mask & TARGET_FLAG_STRING:
        out["target_string"] = r.cstring("strTarget")
    return out


@module(id="spell_go", opcodes=("SMSG_SPELL_GO",), order=40)
class SpellGo(BaseModule):
    text_section = "SMSG_SPELL_GO (spell casts)"
    text_templates = {
        "spell_go": "entry={entry:<7} caster=0x{guid:016X} {guid_type} spell={spell_id}"
                    "{targets_text}",
    }
    sql_tables = (_TABLE,)

    def decode(self, pkt: Packet, ctx: DecodeContext) -> Iterator[Event]:
        r = ByteReader(pkt.body, pkt.name or "SMSG_SPELL_GO")
        r.packguid("cast_item_or_caster")
        caster = r.packguid("caster")
        cast_flags = 0
        data = {"guid": caster, "guid_type": guid_type(caster), "spell_id": r.u32("spellId")}
        data["cast_flags"] = cast_flags = r.u16("castFlags")
        # Players cast spells too -- a non-entry-bearing caster guid carries
        # no creature_template entry.
        if has_entry(caster):
            data["entry"] = guid_entry(caster)

        data["hits"] = [r.u64(f"hit{i}") for i in range(r.u8("hitCount"))]
        misses = []
        for i in range(r.u8("missCount")):
            miss = {"guid": r.u64(f"miss{i}"), "condition": r.u8(f"missCondition{i}")}
            if miss["condition"] == SPELL_MISS_REFLECT:
                miss["reflect_result"] = r.u8(f"reflectResult{i}")
            misses.append(miss)
        data["misses"] = misses

        data.update(_read_cast_targets(r))
        if cast_flags & CAST_FLAG_AMMO:
            data["ammo_display_id"] = r.u32("ammoDisplayId")
            data["ammo_inventory_type"] = r.u32("ammoInventoryType")
        yield self.event(pkt, "spell_go", **data)

    def text_fields(self, ev: Event):
        data = dict(ev.data)
        data.setdefault("entry", "-")
        parts = []
        if data.get("hits"):
            parts.append(f"hits={len(data['hits'])}")
        if data.get("misses"):
            conditions = ",".join(str(m["condition"]) for m in data["misses"])
            parts.append(f"misses={len(data['misses'])}({conditions})")
        if "dest" in data:
            x, y, z = data["dest"]
            parts.append(f"dest=({x:.2f}, {y:.2f}, {z:.2f})")
        if "ammo_display_id" in data:
            parts.append(f"ammo={data['ammo_display_id']}")
        data["targets_text"] = ("  " + " ".join(parts)) if parts else ""
        return data

    def sql_rows(self, ev: Event, ctx: SqlContext) -> Iterator[Row]:
        yield Row(_TABLE.name, {"capture": ctx.capture_id, "t": ev.packet.t,
                                "seq": ev.packet.seq, "caster_guid": ev.data["guid"],
                                "entry": ev.data.get("entry"), "spell_id": ev.data["spell_id"]})
