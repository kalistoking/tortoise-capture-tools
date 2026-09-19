"""SMSG_SPELLNONMELEEDAMAGELOG -- the outcome of one spell hit's damage.

`WorldObject::SendSpellNonMeleeDamageLog` (`Object.cpp:4319`):

    packGUID target
    packGUID attacker
    uint32   spellID
    uint32   damage         -- final, after block/absorb/resist (see below)
    uint8    school         -- GetFirstSchoolInMask(): an index, not a mask
    uint32   absorb
    int32    resist
    uint8    periodicLog    -- 1 for a DoT/HoT tick, not a direct cast hit
    uint8    unused         -- always 0
    uint32   blocked
    uint32   hitInfo        -- SPELL_HIT_TYPE_*: only CRIT and SPLIT are ever set
    uint8    unused2        -- "flag to use extend data", always 0 through this path

Read as raw combat-log data -- who cast what on whom, for how much, and how
much of that was absorbed/resisted/blocked -- this is unconditionally accurate.

**It is deliberately not used to refine a spell's base damage or coefficients.**
`Unit::CalculateAbsorbResistBlock` (`Unit.cpp:2333`) mutates `damage` in place
through a block step, then a resist/absorb step:

    damage -= blockDamageReduction; damage -= blocked        -- if partially blocked
    CalculateDamageAbsorbAndResist(..., &damage, &absorb, &resist, ...)
    bonus = resist<0 ? -resist : 0;  malus = absorb + max(resist,0)
    damage = (damage <= malus) ? 0 : (damage + bonus - malus)

So the wire's `damage` is what actually landed, not the spell's rolled base
damage -- and unlike melee's clamp-at-1, this one clamps at *0*, which means a
resisted-to-nothing hit and a small real hit are wire-indistinguishable at the
low end. Reconstructing the pre-mitigation roll would need the target's
resistance and any block value at cast time, neither of which this toolkit
resolves, so it is not attempted.
"""

from __future__ import annotations

from typing import Iterator

from ..core.base import BaseModule
from ..core.contracts import Column, DecodeContext, Event, Packet, Row, SqlContext, TableSpec
from ..core.reader import ByteReader, guid_entry, guid_type, has_entry
from ..core.registry import module

# SpellHitType (SpellDefines.h) -- only the ones this server build ever sets;
# the *_DEBUG and VICTIM_IS_ATTACKER bits are declared but never assigned.
SPELL_HIT_TYPE_CRIT = 0x02
SPELL_HIT_TYPE_SPLIT = 0x08

_TABLE = TableSpec(
    name="capture_spell_damage_log",
    columns=(
        Column("capture", "VARCHAR(64)", nullable=False),
        Column("t", "DOUBLE"),
        Column("seq", "INT UNSIGNED", nullable=False),
        Column("guid", "BIGINT UNSIGNED", nullable=False),
        Column("entry", "INT UNSIGNED"),
        Column("target_guid", "BIGINT UNSIGNED", nullable=False),
        Column("spell_id", "INT UNSIGNED", nullable=False),
        Column("damage", "INT UNSIGNED"),
        Column("school", "TINYINT UNSIGNED"),
        Column("absorb", "INT UNSIGNED"),
        Column("resist", "INT"),
        Column("blocked_amount", "INT UNSIGNED"),
        Column("is_periodic", "TINYINT UNSIGNED"),
        Column("is_critical", "TINYINT UNSIGNED"),
    ),
    key=("capture", "seq"),
    comment="post-block/absorb/resist damage; NOT the spell's pre-mitigation roll -- see module docstring",
)


@module(id="spell_damage_log", opcodes=("SMSG_SPELLNONMELEEDAMAGELOG",), order=46)
class SpellDamageLog(BaseModule):
    text_section = "SMSG_SPELLNONMELEEDAMAGELOG (spell damage, post-mitigation)"
    text_templates = {
        "spell_damage_log": "entry={entry:<7} spell={spell_id:<6} -> {target_guid_type} "
                            "damage={damage:<5}{crit}{periodic}",
    }
    sql_tables = (_TABLE,)

    def decode(self, pkt: Packet, ctx: DecodeContext) -> Iterator[Event]:
        r = ByteReader(pkt.body, pkt.name or "SMSG_SPELLNONMELEEDAMAGELOG")
        target = r.packguid("target")
        attacker = r.packguid("attacker")
        spell_id = r.u32("spellID")
        damage = r.u32("damage")
        school = r.u8("school")
        absorb = r.u32("absorb")
        resist = r.i32("resist")
        periodic = r.u8("periodicLog")
        r.u8("unused")
        blocked = r.u32("blockedAmount")
        hit_info = r.u32("hitInfo")
        r.u8("unused2")

        data = {
            "guid": attacker, "guid_type": guid_type(attacker),
            "target_guid": target, "target_guid_type": guid_type(target),
            "spell_id": spell_id, "damage": damage, "school": school,
            "absorb": absorb, "resist": resist, "blocked_amount": blocked,
            "is_periodic": bool(periodic),
            "is_critical": bool(hit_info & SPELL_HIT_TYPE_CRIT),
            "is_split": bool(hit_info & SPELL_HIT_TYPE_SPLIT),
        }
        # Players cast spells too -- same object-type trap as attacker_state.py:
        # a non-entry-bearing GUID carries no creature_template/gameobject_
        # template entry, and guid_entry() on one is meaningless (or, for a
        # type whose low bits are a large server-wide counter, misleading).
        if has_entry(attacker):
            data["entry"] = guid_entry(attacker)
        if has_entry(target):
            data["target_entry"] = guid_entry(target)

        yield self.event(pkt, "spell_damage_log", **data)

    def text_fields(self, ev: Event):
        data = dict(ev.data)
        data["crit"] = " (crit)" if data.get("is_critical") else ""
        data["periodic"] = " (periodic)" if data.get("is_periodic") else ""
        data.setdefault("entry", "-")
        return data

    def sql_rows(self, ev: Event, ctx: SqlContext) -> Iterator[Row]:
        d = ev.data
        yield Row(_TABLE.name, {
            "capture": ctx.capture_id, "t": ev.packet.t, "seq": ev.packet.seq,
            "guid": d["guid"], "entry": d.get("entry"), "target_guid": d["target_guid"],
            "spell_id": d["spell_id"], "damage": d["damage"], "school": d["school"],
            "absorb": d["absorb"], "resist": d["resist"], "blocked_amount": d["blocked_amount"],
            "is_periodic": int(d["is_periodic"]), "is_critical": int(d["is_critical"]),
        })
