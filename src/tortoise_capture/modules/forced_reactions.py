"""SMSG_SET_FORCED_REACTIONS -- the player's whole table of forced reputation ranks.

`ReputationMgr::SendForceReactions` (`ReputationMgr.cpp:164-176`):

    uint32 count ; count x ( uint32 faction_id ; uint32 rank )

The whole table every time, never a delta: the server sends it whenever a
force-reaction aura is applied *or removed* (`Aura::HandleForceReaction`,
`SpellAuras.cpp:2698-2709`), and not at login. So `count = 0` is a real
message -- every forced reaction has ended -- and survives here as an event.

`faction_id` is a `Faction.dbc` id, not a `FactionTemplate.dbc` id: the
`UNIT_FIELD_FACTIONTEMPLATE` that authoring calls `faction` is the other
table, and the two must not be joined. `rank` is `ReputationRank`
(`SharedDefines.h:102-111`), 0 hated to 7 exalted; a larger value is a
`WireError`, not a guess.

No guid, no entry: like `SMSG_PLAY_SOUND` the event carries neither key, so it
drops out of an `--entry`/`--guid` run by design.
"""

from __future__ import annotations

from typing import Any, Iterator

from ..core.base import BaseModule
from ..core.contracts import Column, DecodeContext, Event, Packet, Row, SqlContext, TableSpec
from ..core.reader import ByteReader, WireError
from ..core.registry import module

_RANK_NAMES = ("hated", "hostile", "unfriendly", "neutral", "friendly",
               "honored", "revered", "exalted")

_TABLE = TableSpec(
    name="capture_forced_reactions",
    columns=(
        Column("capture", "VARCHAR(64)", nullable=False),
        Column("t", "DOUBLE"),
        Column("seq", "INT UNSIGNED", nullable=False),
        Column("slot", "INT UNSIGNED", nullable=False),   # position in the table
        Column("faction_id", "INT UNSIGNED"),             # NULL with rank: an emptied table
        Column("rank", "TINYINT UNSIGNED"),
    ),
    key=("capture", "seq", "slot"),
)


@module(id="forced_reactions", opcodes=("SMSG_SET_FORCED_REACTIONS",), order=25)
class ForcedReactions(BaseModule):
    text_section = "SMSG_SET_FORCED_REACTIONS (the player's forced reactions)"
    text_templates = {"forced_reactions": "count={count} {summary}"}
    sql_tables = (_TABLE,)

    def decode(self, pkt: Packet, ctx: DecodeContext) -> Iterator[Event]:
        r = ByteReader(pkt.body, pkt.name or "SMSG_SET_FORCED_REACTIONS")
        count = r.u32("count")
        reactions = []
        for i in range(count):
            faction_id = r.u32(f"factionId{i}")
            rank = r.u32(f"rank{i}")
            if rank >= len(_RANK_NAMES):
                raise WireError(f"{r.label}: rank{i} is {rank}, ReputationRank ends at "
                                f"{len(_RANK_NAMES) - 1}")
            reactions.append({"faction_id": faction_id, "rank": rank,
                              "rank_name": _RANK_NAMES[rank]})
        yield self.event(pkt, "forced_reactions", count=count, reactions=reactions)

    def text_fields(self, ev: Event) -> dict[str, Any]:
        data = dict(ev.data)
        data["summary"] = ", ".join(
            f"{it['faction_id']}->{it['rank_name']}({it['rank']})" for it in data["reactions"]
        ) or "(cleared)"
        return data

    def sql_rows(self, ev: Event, ctx: SqlContext) -> Iterator[Row]:
        head = {"capture": ctx.capture_id, "t": ev.packet.t, "seq": ev.packet.seq}
        pairs = ev.data["reactions"] or [{"faction_id": None, "rank": None}]
        for slot, it in enumerate(pairs):
            yield Row(_TABLE.name, {**head, "slot": slot,
                                    "faction_id": it["faction_id"], "rank": it["rank"]})
