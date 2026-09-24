"""Helpers for building synthetic packets and stub tables.

Every module test builds its payload byte by byte from the layout documented
in docs/wire-format.md, so the suite needs no capture, raises no privacy
question, and runs anywhere.
"""

from __future__ import annotations

import struct
from typing import Any

from tortoise_capture.core.contracts import DecodeContext, Direction, Packet, Tables
from tortoise_capture.fields.tables import FieldTable
from tortoise_capture.log import get_logger
from tortoise_capture.wire.opcodes import OpcodeTable

HIGH_UNIT = 0xF130
HIGH_GAMEOBJECT = 0xF110


def make_guid(entry: int, counter: int = 1, high: int = HIGH_UNIT) -> int:
    return (high << 48) | (entry << 24) | counter


def pack_guid(guid: int) -> bytes:
    """The packGUID encoding: mask byte, then only the non-zero bytes."""
    mask, body = 0, bytearray()
    for bit in range(8):
        byte = (guid >> (bit * 8)) & 0xFF
        if byte:
            mask |= 1 << bit
            body.append(byte)
    return bytes([mask]) + bytes(body)


def sized_string(text: str) -> bytes:
    raw = text.encode("utf-8") + b"\x00"
    return struct.pack("<I", len(raw)) + raw


def update_mask(fields: dict[int, int]) -> bytes:
    """uint8 blockCount, blockCount*4 mask bytes, then one uint32 per set bit."""
    highest = max(fields) if fields else 0
    blocks = highest // 32 + 1
    mask = bytearray(blocks * 4)
    for index in fields:
        mask[index >> 3] |= 1 << (index & 7)
    body = b"".join(struct.pack("<I", fields[i]) for i in sorted(fields))
    return bytes([blocks]) + bytes(mask) + body


def make_packet(opcode: int, body: bytes, name: str = "", t: float = 1.0,
                direction: Direction = Direction.S2C, seq: int = 0) -> Packet:
    return Packet(seq=seq, t=t, direction=direction, opcode=opcode, name=name, body=body)


def make_tables(opcode_names: dict[int, str] | None = None,
                field_names: dict[int, str] | None = None) -> Tables:
    by_value = opcode_names or {}
    by_index = field_names or {}
    return Tables(
        opcodes=OpcodeTable(by_value=dict(by_value),
                            by_name={v: k for k, v in by_value.items()}),
        fields=FieldTable(by_index=dict(by_index),
                          by_name={v: k for k, v in by_index.items()},
                          object_end=6, unit_end=188),
    )


def make_ctx(tables: Tables | None = None, **options: Any) -> DecodeContext:
    return DecodeContext(tables=tables or make_tables(), log=get_logger("test"),
                         options=options)


def decode_one(mod, pkt: Packet, ctx: DecodeContext):
    events = list(mod.decode(pkt, ctx))
    assert len(events) == 1, f"expected 1 event, got {len(events)}"
    return events[0]


def make_event(kind: str, t: float, module_id: str = "test", **data: Any) -> "Event":
    """A decoded event as an analyzer sees it: a timestamp and a data mapping."""
    from tortoise_capture.core.contracts import Event
    return Event(packet=make_packet(0, b"", t=t), module_id=module_id, kind=kind, data=data)


def run_analyzer(an, events, ctx: DecodeContext | None = None) -> list["Event"]:
    """Feeds an analyzer a whole stream and returns its findings."""
    for ev in events:
        an.feed(ev)
    return list(an.finish(ctx or make_ctx()))


def findings_by_kind(found) -> dict:
    return {ev.kind: ev for ev in found}


class StubWorld:
    """A world database stand-in: answers from a dict, records nothing."""

    def __init__(self, columns: dict | None = None, displays: dict | None = None,
                schema: dict | None = None):
        self._columns = columns or {}      # (table, column) -> stored value as string
        self._displays = displays or {}    # display id -> item entry
        self._schema = schema or {}        # table -> {column: default}; empty = no schema-fill

    def column(self, table: str, column: str, where: str):
        return self._columns.get((table, column))

    def numeric_column(self, table: str, column: str, where: str):
        value = self._columns.get((table, column))
        return float(value) if value is not None else None

    def item_entry_for_display(self, display_id: int):
        return self._displays.get(display_id)

    def row_exists(self, table: str, where: str) -> bool:
        return False

    def describe(self, table: str) -> dict:
        return self._schema.get(table, {})


def author_rows(rule, events, entry: int, world=None, displays=None, fields=None):
    """Feeds an authoring rule a stream and returns (rows, gaps)."""
    from tortoise_capture.core.contracts import AuthorContext
    from tortoise_capture.log import get_logger
    for ev in events:
        rule.handle(ev, None)
    ctx = AuthorContext(capture_id="test", entry=entry, log=get_logger("test"), world=world,
                        displays=displays, fields=fields)
    return list(rule.rows(ctx)), list(rule.gaps(ctx))
