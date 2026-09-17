"""SMSG_ATTACKERSTATEUPDATE -- the outcome of one melee swing.

`Unit::SendAttackStateUpdate` (`Unit.cpp:4897`):

    uint32 hitInfo
    packGUID attacker
    packGUID target
    uint32   totalDamage
    uint8    subDamageCount
    subDamageCount x:
        uint32 school           -- GetFirstSchoolInMask(): an index, not a mask
        float  fraction         -- subDamage/totalDamage, or 0 if totalDamage==0
        uint32 damage
        uint32 absorb
        int32  resist
    uint32 targetState          -- VictimState: NORMAL, DODGE, PARRY, BLOCKS, ...
    uint32 unused               -- always 0
    uint32 spellId              -- always 0 through this helper; other send
                                    paths (Frostbrand-style proc attacks) can
                                    set it, so it is still decoded, not assumed
    uint32 blockedAmount

Read as raw combat-log data -- who swung at whom, whether it landed, how much
got through, and why not when it did not (miss / dodge / parry / block /
resist) -- this is unconditionally accurate.

**It is deliberately not used to refine `creature_template.dmg_min/dmg_max`.**
`Object.cpp:4360` (`CalcArmorReducedDamage`) runs *before* this packet is
built:

    tmpvalue = 0.1*armor / (8.5*attackerLevel + 40)     -- clamped to [0, 0.75]
    damage   = round(rawDamage * (1 - tmpvalue))        -- floored at 1

So `total_damage` on the wire is the *post-armor* result against whichever
target it hit, not the pre-mitigation roll `dmg_min/dmg_max` describes.
Reversing it needs the target's armor at the time -- itself a further gap,
since a player target has an entirely different field layout (`EPlayerFields`,
not `EUnitFields`; the same object-type trap `fields/tables.py` already gates
against) that this toolkit does not decode yet -- and the formula's floor at
1 makes small values ambiguous even then. Presenting a "refined" stat from
this data before both of those exist would be exactly the kind of unearned
confidence this project's authoring output otherwise refuses to produce.
"""

from __future__ import annotations

from typing import Iterator

from ..core.base import BaseModule
from ..core.contracts import Column, DecodeContext, Event, Packet, Row, SqlContext, TableSpec
from ..core.reader import ByteReader, guid_entry, guid_type, has_entry
from ..core.registry import module

# HitInfo flags (UnitDefines.h) -- only the ones worth naming for a report.
HITINFO_MISS = 0x00000010
HITINFO_ABSORB = 0x00000020
HITINFO_RESIST = 0x00000040
HITINFO_CRITICALHIT = 0x00000080

# VictimState (UnitDefines.h).
VICTIMSTATE_UNAFFECTED = 0
VICTIMSTATE_NORMAL = 1
VICTIMSTATE_DODGE = 2
VICTIMSTATE_PARRY = 3
VICTIMSTATE_INTERRUPT = 4
VICTIMSTATE_BLOCKS = 5
VICTIMSTATE_EVADES = 6
VICTIMSTATE_IS_IMMUNE = 7
VICTIMSTATE_DEFLECTS = 8

_TARGET_STATE_NAMES = {
    VICTIMSTATE_UNAFFECTED: "unaffected", VICTIMSTATE_NORMAL: "hit",
    VICTIMSTATE_DODGE: "dodge", VICTIMSTATE_PARRY: "parry",
    VICTIMSTATE_INTERRUPT: "interrupt", VICTIMSTATE_BLOCKS: "block",
    VICTIMSTATE_EVADES: "evade", VICTIMSTATE_IS_IMMUNE: "immune",
    VICTIMSTATE_DEFLECTS: "deflect",
}

_TABLE = TableSpec(
    name="capture_attacker_state",
    columns=(
        Column("capture", "VARCHAR(64)", nullable=False),
        Column("t", "DOUBLE"),
        Column("seq", "INT UNSIGNED", nullable=False),
        Column("guid", "BIGINT UNSIGNED", nullable=False),
        Column("entry", "INT UNSIGNED"),
        Column("target_guid", "BIGINT UNSIGNED"),
        Column("total_damage", "INT UNSIGNED"),
        Column("target_state", "VARCHAR(16)"),
        Column("is_miss", "TINYINT UNSIGNED"),
        Column("is_critical", "TINYINT UNSIGNED"),
        Column("spell_id", "INT UNSIGNED"),
        Column("blocked_amount", "INT UNSIGNED"),
    ),
    key=("capture", "seq"),
    comment="post-armor-mitigation damage; NOT the raw creature_template roll -- see module docstring",
)


@module(id="attacker_state", opcodes=("SMSG_ATTACKERSTATEUPDATE",), order=45)
class AttackerState(BaseModule):
    text_section = "SMSG_ATTACKERSTATEUPDATE (melee swings, post-mitigation)"
    text_templates = {
        "attacker_state": "entry={entry:<7} -> {target_guid_type} total={total_damage:<5} "
                          "state={target_state}{crit}",
    }
    sql_tables = (_TABLE,)

    def decode(self, pkt: Packet, ctx: DecodeContext) -> Iterator[Event]:
        r = ByteReader(pkt.body, pkt.name or "SMSG_ATTACKERSTATEUPDATE")
        hit_info = r.u32("hitInfo")
        attacker = r.packguid("attacker")
        target = r.packguid("target")
        total_damage = r.u32("totalDamage")

        sub_damage = []
        for i in range(r.u8("subDamageCount")):
            school = r.u32(f"school{i}")
            r.f32(f"fraction{i}")            # derivable from damage/total; not carried forward
            damage = r.u32(f"damage{i}")
            absorb = r.u32(f"absorb{i}")
            resist = r.i32(f"resist{i}")
            sub_damage.append({"school": school, "damage": damage, "absorb": absorb,
                               "resist": resist})

        target_state = r.u32("targetState")
        r.u32("unused")
        spell_id = r.u32("spellId")
        blocked = r.u32("blockedAmount")

        data = {
            "guid": attacker, "entry": guid_entry(attacker), "guid_type": guid_type(attacker),
            "target_guid": target, "target_guid_type": guid_type(target),
            "total_damage": total_damage, "sub_damage": sub_damage,
            "target_state": target_state,      # raw VictimState int; name it at render time
            "is_miss": bool(hit_info & HITINFO_MISS),
            "is_critical": bool(hit_info & HITINFO_CRITICALHIT),
            "is_normal_hit": target_state == VICTIMSTATE_NORMAL and not hit_info & HITINFO_MISS,
            "spell_id": spell_id, "blocked_amount": blocked,
        }
        # Only named when the target's GUID type actually carries a
        # creature_template/gameobject_template entry -- a player's does not,
        # and guid_entry() on one would return a number that names nothing.
        if has_entry(target):
            data["target_entry"] = guid_entry(target)

        yield self.event(pkt, "attacker_state", **data)

    def text_fields(self, ev: Event):
        data = dict(ev.data)
        data["crit"] = " (crit)" if data.get("is_critical") else ""
        data["target_state"] = _TARGET_STATE_NAMES.get(data["target_state"], data["target_state"])
        return data

    def sql_rows(self, ev: Event, ctx: SqlContext) -> Iterator[Row]:
        d = ev.data
        yield Row(_TABLE.name, {
            "capture": ctx.capture_id, "t": ev.packet.t, "seq": ev.packet.seq,
            "guid": d["guid"], "entry": d["entry"], "target_guid": d["target_guid"],
            "total_damage": d["total_damage"],
            "target_state": _TARGET_STATE_NAMES.get(d["target_state"], str(d["target_state"])),
            "is_miss": int(d["is_miss"]), "is_critical": int(d["is_critical"]),
            "spell_id": d["spell_id"], "blocked_amount": d["blocked_amount"],
        })
