"""SMSG_PERIODICAURALOG -- one DoT/HoT/mana tick from an active aura.

`Unit::SendPeriodicAuraLog` (`Unit.cpp:4715`):

    packGUID target      -- aura->GetTarget()->GetPackGUID()
    packGUID caster      -- aura->GetRealCasterGuid().WriteAsPacked() -- same
                             packGUID encoding as GetPackGUID() (ObjectGuid.h:273)
    uint32   spellId
    uint32   count        -- always 1 through this send path
    uint32   auraType     -- AuraType (SpellAuraDefines.h); the payload's tail
                             depends on this value:

    PERIODIC_DAMAGE(3) / PERIODIC_DAMAGE_PERCENT(89):
        uint32 damage ; uint32 school ; uint32 absorb ; int32 resist
    PERIODIC_HEAL(8) / OBS_MOD_HEALTH(20):
        uint32 amount
    OBS_MOD_MANA(21) / PERIODIC_ENERGIZE(24):
        uint32 powerType ; uint32 amount
    PERIODIC_MANA_LEECH(64):
        uint32 powerType ; uint32 amount ; float multiplier

Any other `auraType` is never sent: `Unit.cpp:4750` logs a server-side error
and returns *before* writing the packet, so there is no byte layout to guess
at for one -- decoding stops with `WireError` rather than assuming a shape.

Read as a combat log, this is unconditionally accurate (unlike
`SMSG_ATTACKERSTATEUPDATE`'s post-mitigation caveat, damage/heal/mana amounts
here are already the final per-tick numbers with no further processing).
"""

from __future__ import annotations

from typing import Iterator

from ..core.base import BaseModule
from ..core.contracts import Column, DecodeContext, Event, Packet, Row, SqlContext, TableSpec
from ..core.reader import ByteReader, WireError, guid_entry, guid_type, has_entry
from ..core.registry import module

# AuraType (SpellAuraDefines.h) -- only the values this opcode's switch
# handles; every other value is unreachable on the wire (see module docstring).
AURA_PERIODIC_DAMAGE = 3
AURA_PERIODIC_HEAL = 8
AURA_OBS_MOD_HEALTH = 20
AURA_OBS_MOD_MANA = 21
AURA_PERIODIC_ENERGIZE = 24
AURA_PERIODIC_MANA_LEECH = 64
AURA_PERIODIC_DAMAGE_PERCENT = 89

_AURA_TYPE_NAMES = {
    AURA_PERIODIC_DAMAGE: "periodic_damage", AURA_PERIODIC_DAMAGE_PERCENT: "periodic_damage_percent",
    AURA_PERIODIC_HEAL: "periodic_heal", AURA_OBS_MOD_HEALTH: "obs_mod_health",
    AURA_OBS_MOD_MANA: "obs_mod_mana", AURA_PERIODIC_ENERGIZE: "periodic_energize",
    AURA_PERIODIC_MANA_LEECH: "periodic_mana_leech",
}

_TABLE = TableSpec(
    name="capture_periodic_aura",
    columns=(
        Column("capture", "VARCHAR(64)", nullable=False),
        Column("t", "DOUBLE"),
        Column("seq", "INT UNSIGNED", nullable=False),
        Column("guid", "BIGINT UNSIGNED", nullable=False),
        Column("entry", "INT UNSIGNED"),
        Column("target_guid", "BIGINT UNSIGNED", nullable=False),
        Column("target_entry", "INT UNSIGNED"),
        Column("spell_id", "INT UNSIGNED", nullable=False),
        Column("aura_type", "VARCHAR(24)"),
        Column("amount", "INT UNSIGNED"),
        Column("school", "TINYINT UNSIGNED"),
        Column("absorb", "INT UNSIGNED"),
        Column("resist", "INT"),
        Column("power_type", "TINYINT UNSIGNED"),
        Column("multiplier", "FLOAT"),
    ),
    key=("capture", "seq"),
)


@module(id="periodic_aura", opcodes=("SMSG_PERIODICAURALOG",), order=47)
class PeriodicAura(BaseModule):
    text_section = "SMSG_PERIODICAURALOG (DoT/HoT/mana ticks)"
    text_templates = {
        "periodic_aura": "entry={entry:<7} spell={spell_id:<6} {aura_type_name} "
                         "-> target_entry={target_entry:<7} amount={amount}",
    }
    sql_tables = (_TABLE,)

    def decode(self, pkt: Packet, ctx: DecodeContext) -> Iterator[Event]:
        r = ByteReader(pkt.body, pkt.name or "SMSG_PERIODICAURALOG")
        target = r.packguid("target")
        caster = r.packguid("caster")
        spell_id = r.u32("spellId")
        r.u32("count")
        aura_type = r.u32("auraType")

        data = {
            "guid": caster, "guid_type": guid_type(caster),
            "target_guid": target, "target_guid_type": guid_type(target),
            "spell_id": spell_id, "aura_type": aura_type,
        }
        if aura_type in (AURA_PERIODIC_DAMAGE, AURA_PERIODIC_DAMAGE_PERCENT):
            data["amount"] = r.u32("damage")
            data["school"] = r.u32("school")
            data["absorb"] = r.u32("absorb")
            data["resist"] = r.i32("resist")
        elif aura_type in (AURA_PERIODIC_HEAL, AURA_OBS_MOD_HEALTH):
            data["amount"] = r.u32("amount")
        elif aura_type in (AURA_OBS_MOD_MANA, AURA_PERIODIC_ENERGIZE):
            data["power_type"] = r.u32("powerType")
            data["amount"] = r.u32("amount")
        elif aura_type == AURA_PERIODIC_MANA_LEECH:
            data["power_type"] = r.u32("powerType")
            data["amount"] = r.u32("amount")
            data["multiplier"] = r.f32("multiplier")
        else:
            raise WireError(f"aura type {aura_type} is never sent by this server build "
                            "(Unit.cpp:4750 logs an error and returns instead) -- "
                            "no byte layout to decode")

        if has_entry(caster):
            data["entry"] = guid_entry(caster)
        if has_entry(target):
            data["target_entry"] = guid_entry(target)
        yield self.event(pkt, "periodic_aura", **data)

    def text_fields(self, ev: Event):
        data = dict(ev.data)
        data["aura_type_name"] = _AURA_TYPE_NAMES[data["aura_type"]]   # decode() rejected any other
        data.setdefault("entry", "-")
        data.setdefault("target_entry", "-")
        return data

    def sql_rows(self, ev: Event, ctx: SqlContext) -> Iterator[Row]:
        d = ev.data
        yield Row(_TABLE.name, {
            "capture": ctx.capture_id, "t": ev.packet.t, "seq": ev.packet.seq,
            "guid": d["guid"], "entry": d.get("entry"),
            "target_guid": d["target_guid"], "target_entry": d.get("target_entry"),
            "spell_id": d["spell_id"],
            "aura_type": _AURA_TYPE_NAMES[d["aura_type"]],
            "amount": d.get("amount"), "school": d.get("school"),
            "absorb": d.get("absorb"), "resist": d.get("resist"),
            "power_type": d.get("power_type"), "multiplier": d.get("multiplier"),
        })
