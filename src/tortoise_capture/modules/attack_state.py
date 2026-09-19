"""SMSG_ATTACKSTART / SMSG_ATTACKSTOP -- melee engage / disengage.

`Unit::SendMeleeAttackStart` (`Unit.cpp:2704`):

    uint64 attacker    -- GetObjectGuid(), PLAIN (the 8+8 packet size confirms it)
    uint64 victim      -- pVictim->GetObjectGuid(), also PLAIN

`Unit::SendMeleeAttackStop` (`Unit.cpp:2714`):

    packGUID attacker  -- GetPackGUID() -- NOT plain, unlike the START variant
    packGUID victim
    uint32   unused    -- comment says "can be 0x1"; every call site in this
                          checkout sends 0, so it is read but not attached to
                          anything (same convention as attacker_state.py's
                          own "unused" field)

The asymmetry between the two opcodes' guid encoding is the trap worth a
test: assuming STOP mirrors START's plain guids (or vice versa) decodes a
plausible but wrong 64-bit value instead of failing loudly. The two are told
apart by opcode NUMBER, never by `pkt.name`: a dump made without a checkout
replays with an empty name (`wire/opcodes.py` resolves unknown to "",
`emit/jsonl.py` carries it verbatim), and a name-based branch would then read
every STOP as a START -- silently, for exactly the reason above.

Both opcodes fire from `Unit::Attack()`/`Unit::AttackStop()` -- any unit type
can be attacker or victim, players included, so both guids need the same
has_entry() gate every other module in this project applies.
"""

from __future__ import annotations

from typing import Iterator

from ..core.base import BaseModule
from ..core.contracts import Column, DecodeContext, Event, Packet, Row, SqlContext, TableSpec
from ..core.reader import ByteReader, guid_entry, guid_type, has_entry
from ..core.registry import module

# Fallback for a checkout that does not define the symbol (same convention as
# monster_move.py's SMSG_MONSTER_MOVE_TRANSPORT); the table's number wins.
SMSG_ATTACKSTOP = 0x144

_TABLE = TableSpec(
    name="capture_attack_state",
    columns=(
        Column("capture", "VARCHAR(64)", nullable=False),
        Column("t", "DOUBLE"),
        Column("seq", "INT UNSIGNED", nullable=False),
        Column("kind", "VARCHAR(12)", nullable=False),
        Column("guid", "BIGINT UNSIGNED", nullable=False),
        Column("entry", "INT UNSIGNED"),
        Column("victim_guid", "BIGINT UNSIGNED", nullable=False),
        Column("victim_entry", "INT UNSIGNED"),
    ),
    key=("capture", "seq"),
)


def _guid_pair(attacker: int, victim: int) -> dict:
    data = {"guid": attacker, "guid_type": guid_type(attacker),
            "victim_guid": victim, "victim_guid_type": guid_type(victim)}
    if has_entry(attacker):
        data["entry"] = guid_entry(attacker)
    if has_entry(victim):
        data["victim_entry"] = guid_entry(victim)
    return data


@module(id="attack_state", opcodes=("SMSG_ATTACKSTART", "SMSG_ATTACKSTOP"), order=44)
class AttackState(BaseModule):
    text_section = "SMSG_ATTACKSTART / SMSG_ATTACKSTOP (melee engage/disengage)"
    text_templates = {
        "attack_start": "entry={entry:<7} {guid_type} -> ENGAGES victim_entry={victim_entry:<7} "
                        "{victim_guid_type}",
        "attack_stop": "entry={entry:<7} {guid_type} -> DISENGAGES victim_entry={victim_entry:<7} "
                       "{victim_guid_type}",
    }
    sql_tables = (_TABLE,)

    def decode(self, pkt: Packet, ctx: DecodeContext) -> Iterator[Event]:
        r = ByteReader(pkt.body, pkt.name or "attack_state")
        # By number, not name -- see the module docstring for why the name
        # can be "" and what a name-based branch would then misdecode.
        stop = pkt.opcode == ctx.tables.opcodes.by_name.get("SMSG_ATTACKSTOP", SMSG_ATTACKSTOP)
        if stop:
            attacker, victim = r.packguid("attacker"), r.packguid("victim")
            r.u32("unused")
        else:
            attacker, victim = r.u64("attacker"), r.u64("victim")
        yield self.event(pkt, "attack_stop" if stop else "attack_start",
                         **_guid_pair(attacker, victim))

    def text_fields(self, ev: Event):
        data = dict(ev.data)
        data.setdefault("entry", "-")
        data.setdefault("victim_entry", "-")
        return data

    def sql_rows(self, ev: Event, ctx: SqlContext) -> Iterator[Row]:
        d = ev.data
        yield Row(_TABLE.name, {
            "capture": ctx.capture_id, "t": ev.packet.t, "seq": ev.packet.seq,
            "kind": "start" if ev.kind == "attack_start" else "stop",
            "guid": d["guid"], "entry": d.get("entry"),
            "victim_guid": d["victim_guid"], "victim_entry": d.get("victim_entry"),
        })
