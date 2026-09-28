"""JSON Lines: the interchange format between `tct dump` and `tct decode`.

This module owns the record schema in both directions, so a dumped session
can be re-decoded without touching the capture again -- useful because
framing a large pcap is the slow part, and decoding is what changes while a
new opcode module is being written.

Packet record: {"seq","t","dir","opcode","name","size","hex","via"}
Event record:  {"seq","t","dir","module","kind","data"}
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterator, TextIO

from .. import log as _log
from ..core.contracts import Direction, Event, Packet

_logger = _log.get_logger("emit.jsonl")


def _jsonable(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    return str(value)


def packet_record(pkt: Packet) -> dict[str, Any]:
    return {"seq": pkt.seq, "t": pkt.t, "dir": str(pkt.direction), "opcode": f"0x{pkt.opcode:X}",
            "name": pkt.name, "size": len(pkt.body), "hex": pkt.body.hex(), "via": pkt.via}


def finite(value: Any) -> Any:
    """`value` with NaN and infinity as None: a wire float can carry them, and
    json.dumps would write a bare NaN token no strict JSON reader accepts."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [finite(v) for v in value]
    return value


def event_record(ev: Event) -> dict[str, Any]:
    return {"seq": ev.packet.seq, "t": ev.packet.t, "dir": str(ev.packet.direction),
            "module": ev.module_id, "kind": ev.kind, "data": ev.data}


def read_packets(path: Path) -> Iterator[Packet]:
    """Replays a dump back into Packets."""
    with open(path, encoding="utf-8") as fp:
        for line_no, line in enumerate(fp, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                yield Packet(seq=rec["seq"], t=rec["t"], direction=Direction(rec["dir"]),
                             opcode=int(rec["opcode"], 16), name=rec.get("name", ""),
                             body=bytes.fromhex(rec["hex"]), via=rec.get("via", "direct"))
            except (ValueError, KeyError) as exc:
                _logger.error("%s:%d is not a usable packet record: %s", path, line_no, exc)


class PacketSink:
    """Writes the framed session; used by `tct dump`."""

    def __init__(self, out: TextIO) -> None:
        self._out = out
        self.count = 0

    def handle(self, pkt: Packet) -> None:
        self._out.write(json.dumps(packet_record(pkt), default=_jsonable) + "\n")
        self.count += 1

    def close(self) -> None:
        self._out.flush()


class EventSink:
    """Writes decoded events; used by `tct decode --format jsonl`."""

    def __init__(self, out: TextIO) -> None:
        self._out = out
        self.count = 0

    def handle(self, ev: Event, mod: Any = None) -> None:
        self._out.write(json.dumps(finite(event_record(ev)), default=_jsonable) + "\n")
        self.count += 1

    def close(self) -> None:
        self._out.flush()
