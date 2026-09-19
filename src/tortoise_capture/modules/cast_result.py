"""SMSG_CAST_RESULT -- why the caster's own spell cast did, or did not, start.

`Spell::SendCastResult` (`Spell.cpp:4567,4581`):

    if (!m_caster->IsPlayer()) return;   -- NPCs never send this; no client to tell

    uint32 spellId
    uint8  status        -- 0 = SPELL_CAST_OK, 2 = failed (no third value is ever sent)
    if status != 0:
        uint8 reason      -- SpellCastResult (SpellDefines.h:284); passive spells
                             report SPELL_FAILED_DONT_REPORT instead of the real one
        # Three reasons carry extra fields the client uses to fill in a %s:
        #   SPELL_FAILED_REQUIRES_SPELL_FOCUS -> uint32 gameobject requirement
        #   SPELL_FAILED_REQUIRES_AREA        -> uint32 required area id
        #   SPELL_FAILED_EQUIPPED_ITEM_CLASS  -> uint32 x3 (class, subclass mask,
        #                                        inventory type mask)
        # Not decoded: none of the three is common enough in a solo melee/spell
        # capture to be worth the extra surface, and leaving bytes unread past
        # this point is the same "decode less than the whole payload" call
        # spell_go.py already makes for its target block.

This packet is sent straight to `caster->GetSession()`, never broadcast --
there is no target and no caster guid anywhere on the wire. Every instance in
a capture is therefore about the recording player's own attempted cast, not
about the creature being observed, and it correctly carries no entry/guid key
(same as `play_sound.py`) so it drops out of an --entry/--guid filtered run.
Read as a diagnostic: an unexpectedly sparse `SMSG_SPELL_GO` sample (feeding
`behaviour.py`'s repeat-delay finding, say) may simply be explained by a run
of SPELL_FAILED_NOT_READY or SPELL_FAILED_OUT_OF_RANGE sitting right here.
"""

from __future__ import annotations

from typing import Iterator

from ..core.base import BaseModule
from ..core.contracts import Column, DecodeContext, Event, Packet, Row, SqlContext, TableSpec
from ..core.reader import ByteReader
from ..core.registry import module

# SpellCastResult (SpellDefines.h:284) -- alphabetical, not numbered by hand;
# only the reasons plausible in a solo melee/spell capture are named here,
# the same "worth naming for a report" call attacker_state.py makes for its
# own flags. Anything else still decodes -- it just prints as a bare number.
_REASON_NAMES = {
    10: "bad_targets", 19: "caster_dead", 23: "dont_report", 25: "equipped_item_class",
    29: "fizzle", 35: "interrupted", 42: "line_of_sight", 46: "moving", 50: "nopath",
    60: "not_ready", 89: "out_of_range", 93: "requires_area", 94: "requires_spell_focus",
    96: "silenced", 97: "spell_in_progress", 100: "stunned", 105: "target_enemy",
    107: "target_friendly",
}

_TABLE = TableSpec(
    name="capture_cast_result",
    columns=(
        Column("capture", "VARCHAR(64)", nullable=False),
        Column("t", "DOUBLE"),
        Column("seq", "INT UNSIGNED", nullable=False),
        Column("spell_id", "INT UNSIGNED", nullable=False),
        Column("is_success", "TINYINT UNSIGNED", nullable=False),
        Column("reason", "VARCHAR(24)"),
    ),
    key=("capture", "seq"),
)


@module(id="cast_result", opcodes=("SMSG_CAST_RESULT",), order=38)
class CastResult(BaseModule):
    text_section = "SMSG_CAST_RESULT (own cast succeeded / failed)"
    text_templates = {
        "cast_result": "spell={spell_id:<6} {outcome}",
    }
    sql_tables = (_TABLE,)

    def decode(self, pkt: Packet, ctx: DecodeContext) -> Iterator[Event]:
        r = ByteReader(pkt.body, pkt.name or "SMSG_CAST_RESULT")
        spell_id = r.u32("spellId")
        status = r.u8("status")
        data: dict = {"spell_id": spell_id, "is_success": status == 0}
        if status != 0:
            data["reason"] = r.u8("reason")   # raw code; named at render time
        yield self.event(pkt, "cast_result", **data)

    def text_fields(self, ev: Event):
        data = dict(ev.data)
        if data["is_success"]:
            data["outcome"] = "ok"
        else:
            reason = data["reason"]
            data["outcome"] = f"failed ({_REASON_NAMES.get(reason, reason)})"
        return data

    def sql_rows(self, ev: Event, ctx: SqlContext) -> Iterator[Row]:
        d = ev.data
        reason = None if d["is_success"] else _REASON_NAMES.get(d["reason"], str(d["reason"]))
        yield Row(_TABLE.name, {"capture": ctx.capture_id, "t": ev.packet.t, "seq": ev.packet.seq,
                                "spell_id": d["spell_id"], "is_success": int(d["is_success"]),
                                "reason": reason})
