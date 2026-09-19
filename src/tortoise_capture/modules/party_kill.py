"""SMSG_PARTYKILLLOG -- something died (Unit.cpp:1127).

    uint64 killer (raw) ; uint64 victim (raw)

The victim's entry is the conventional `entry` key, so `--entry N` reports
the deaths of that creature rather than its kills. Death time from here
agrees with UNIT_FIELD_HEALTH reaching 0 in SMSG_UPDATE_OBJECT, and the gap
to the next CREATE block for the same entry is the respawn timer.

`killer` is unconditionally `pPlayerTap->GetObjectGuid()` (`Unit.cpp:1128`) --
always a player, in every capture, not just usually -- so it never carries a
creature_template entry and none is computed for it. The victim can also be a
player: this same packet routes a PvP death too (`Unit.cpp:1116`'s "player
kill case"), so its entry is likewise only present when the victim is
actually an entry-bearing type.
"""

from __future__ import annotations

from typing import Iterator

from ..core.base import BaseModule
from ..core.contracts import Column, DecodeContext, Event, Packet, Row, SqlContext, TableSpec
from ..core.reader import ByteReader, guid_entry, guid_type, has_entry
from ..core.registry import module

_TABLE = TableSpec(
    name="capture_party_kill",
    columns=(
        Column("capture", "VARCHAR(64)", nullable=False),
        Column("t", "DOUBLE"),
        Column("seq", "INT UNSIGNED", nullable=False),
        Column("killer_guid", "BIGINT UNSIGNED"),
        Column("victim_guid", "BIGINT UNSIGNED", nullable=False),
        Column("entry", "INT UNSIGNED"),
    ),
    key=("capture", "seq", "victim_guid"),
)


@module(id="party_kill", opcodes=("SMSG_PARTYKILLLOG",), order=30)
class PartyKill(BaseModule):
    text_section = "SMSG_PARTYKILLLOG (deaths)"
    text_templates = {
        "party_kill": "entry={entry:<7} victim=0x{guid:016X} {victim_type} "
                      "killer=0x{killer_guid:016X}",
    }
    sql_tables = (_TABLE,)

    def decode(self, pkt: Packet, ctx: DecodeContext) -> Iterator[Event]:
        r = ByteReader(pkt.body, pkt.name or "SMSG_PARTYKILLLOG")
        killer = r.u64("killer")
        victim = r.u64("victim")
        data = {"guid": victim, "victim_type": guid_type(victim), "killer_guid": killer}
        if has_entry(victim):
            data["entry"] = guid_entry(victim)
        yield self.event(pkt, "party_kill", **data)

    def text_fields(self, ev: Event):
        data = dict(ev.data)
        data.setdefault("entry", "-")
        return data

    def sql_rows(self, ev: Event, ctx: SqlContext) -> Iterator[Row]:
        yield Row(_TABLE.name, {"capture": ctx.capture_id, "t": ev.packet.t,
                                "seq": ev.packet.seq, "killer_guid": ev.data["killer_guid"],
                                "victim_guid": ev.data["guid"], "entry": ev.data.get("entry")})
