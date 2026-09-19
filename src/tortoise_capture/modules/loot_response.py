"""SMSG_LOOT_RESPONSE -- what a loot window shows, or why it did not open.

Two distinct shapes share this opcode number:

`Player::SendLootError` (`Player.cpp:9278`) -- the loot attempt failed:

    uint64 guid (raw)
    uint8  0            -- always zero; no loot_type on a failure
    uint8  error        -- LootError (LootMgr.h:85)

`Player::SendLoot` (`Player.cpp:9679`, item shape from `LootMgr.cpp:889`) --
the loot window's contents:

    uint64 guid (raw)   -- the corpse/gameobject/item being looted
    uint8  loot_type    -- LootType (LootMgr.h:49): CORPSE=1, PICKPOCKETING=2,
                            FISHING=3, DISENCHANTING=4, SKINNING=6, ...
    uint32 gold
    uint8  item_count   -- patched to its final value by LootMgr.cpp:1071
                            before the packet is built, so it already reads
                            correctly here
    item_count x:
        uint8  slot
        uint32 item_id
        uint32 count
        uint32 display_info_id
        uint32 unused (always 0)
        int32  random_property_id  -- SIGNED (LootMgr.h:134); written via a
                                       uint32 cast, so it must be read back
                                       as i32 or a negative suffix id (a real
                                       vanilla mechanic) decodes as a huge
                                       positive number instead
        uint8  slot_type

The two shapes are told apart by remaining byte count after the guid, not a
flag: the error form is always exactly 2 bytes there (a placeholder byte
plus the error code); the success form is at least 6 (loot_type + an empty
LootView's gold+count) -- no successful response is ever short enough to be
mistaken for an error, and vice versa. `NONE_PERMISSION` (`LootMgr.cpp:902`)
needs no special-casing either: it writes gold=0, item_count=0, and nothing
more, which this decoder already reads correctly as "zero items".

This is the only wire signal for `creature_loot_template` /
`creature_pickpocketing_loot_template` / `skinning_loot_template` content --
but only ever one roll of the table, once per kill. A drop *chance* (the
authored percentage) needs many kills of the same creature to bound, the
same statistical requirement `delayRepeatMin/Max` already has; there is no
`author/` rule turning a handful of these into a proposed loot-table row for
that reason. This module records what a capture saw; a human (or `trt`)
decides what percentage that supports.
"""

from __future__ import annotations

from typing import Any, Iterator

from ..core.base import BaseModule
from ..core.contracts import Column, DecodeContext, Event, Packet, Row, SqlContext, TableSpec
from ..core.reader import ByteReader, guid_entry, guid_type, has_entry
from ..core.registry import module

_ITEM_TABLE = TableSpec(
    name="capture_loot_item",
    columns=(
        Column("capture", "VARCHAR(64)", nullable=False),
        Column("t", "DOUBLE"),
        Column("seq", "INT UNSIGNED", nullable=False),
        Column("guid", "BIGINT UNSIGNED", nullable=False),
        Column("entry", "INT UNSIGNED"),
        Column("loot_type", "TINYINT UNSIGNED"),
        Column("gold", "INT UNSIGNED"),
        Column("item_id", "INT UNSIGNED"),
        Column("count", "INT UNSIGNED"),
        Column("display_info_id", "INT UNSIGNED"),
        Column("random_property_id", "INT"),
        Column("slot_type", "TINYINT UNSIGNED"),
    ),
    key=("capture", "seq"),
    comment="one roll of a loot table, not a drop chance -- see module docstring",
)

_ERROR_TABLE = TableSpec(
    name="capture_loot_error",
    columns=(
        Column("capture", "VARCHAR(64)", nullable=False),
        Column("t", "DOUBLE"),
        Column("seq", "INT UNSIGNED", nullable=False),
        Column("guid", "BIGINT UNSIGNED", nullable=False),
        Column("entry", "INT UNSIGNED"),
        Column("error", "TINYINT UNSIGNED"),
    ),
    key=("capture", "seq"),
)


@module(id="loot_response", opcodes=("SMSG_LOOT_RESPONSE",), order=48)
class LootResponse(BaseModule):
    text_section = "SMSG_LOOT_RESPONSE (loot window contents)"
    text_templates = {
        "loot_response": "entry={entry:<7} {guid_type} loot_type={loot_type} gold={gold}"
                         "\n{items_text}",
        "loot_error": "entry={entry:<7} {guid_type} loot failed: error={error}",
    }
    sql_tables = (_ITEM_TABLE, _ERROR_TABLE)

    def decode(self, pkt: Packet, ctx: DecodeContext) -> Iterator[Event]:
        r = ByteReader(pkt.body, pkt.name or "SMSG_LOOT_RESPONSE")
        guid = r.u64("guid")
        base: dict[str, Any] = {"guid": guid, "guid_type": guid_type(guid)}
        if has_entry(guid):
            base["entry"] = guid_entry(guid)

        if r.remaining == 2:
            r.u8("unused")
            base["error"] = r.u8("error")
            yield self.event(pkt, "loot_error", **base)
            return

        loot_type = r.u8("lootType")
        gold = r.u32("gold")
        items = []
        for i in range(r.u8("itemCount")):
            items.append({
                "slot": r.u8(f"slot{i}"), "item_id": r.u32(f"itemId{i}"),
                "count": r.u32(f"count{i}"), "display_info_id": r.u32(f"displayInfoId{i}"),
            })
            r.u32(f"unused{i}")
            items[-1]["random_property_id"] = r.i32(f"randomPropertyId{i}")
            items[-1]["slot_type"] = r.u8(f"slotType{i}")
        yield self.event(pkt, "loot_response", loot_type=loot_type, gold=gold,
                         items=items, **base)

    def text_fields(self, ev: Event):
        data = dict(ev.data)
        data.setdefault("entry", "-")
        if ev.kind == "loot_response":
            lines = [f"    slot {it['slot']:>2}  item {it['item_id']:<7} x{it['count']:<3} "
                     f"(display {it['display_info_id']}, random_property {it['random_property_id']})"
                     for it in data["items"]]
            data["items_text"] = "\n".join(lines) if lines else "    (no items)"
        return data

    def sql_rows(self, ev: Event, ctx: SqlContext) -> Iterator[Row]:
        d = ev.data
        head = {"capture": ctx.capture_id, "t": ev.packet.t, "seq": ev.packet.seq,
                "guid": d["guid"], "entry": d.get("entry")}
        if ev.kind == "loot_error":
            yield Row(_ERROR_TABLE.name, {**head, "error": d["error"]})
            return
        rows = d["items"] or [{"item_id": None, "count": None, "display_info_id": None,
                               "random_property_id": None, "slot_type": None}]
        for item in rows:
            yield Row(_ITEM_TABLE.name, {**head, "loot_type": d["loot_type"], "gold": d["gold"],
                                         "item_id": item["item_id"], "count": item["count"],
                                         "display_info_id": item["display_info_id"],
                                         "random_property_id": item["random_property_id"],
                                         "slot_type": item["slot_type"]})
