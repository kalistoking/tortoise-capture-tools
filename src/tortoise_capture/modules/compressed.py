"""Compressed containers: SMSG_COMPRESSED_MOVES and SMSG_COMPRESSED_UPDATE_OBJECT.

Both are `uint32 uncompressed_size` + zlib blob, and the inner framing is
where they differ -- treating the second like the first misparses it silently
(it yields a plausible frame or two, then overruns):

  SMSG_COMPRESSED_MOVES (MovementData::AddPacket)
      a batch of independently-opcoded micro-packets, repeated
      [uint8 len_incl_opcode][uint16 opcode][payload]

  SMSG_COMPRESSED_UPDATE_OBJECT (UpdateData::BuildPacket)
      the raw SMSG_UPDATE_OBJECT body verbatim, no per-block opcode --
      one logical message, not a batch

These are modules rather than runner code on purpose: `expand()` puts the
packets they hold back into the same stream, so the runner never learns that
containers exist.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import replace
from typing import Iterator

from ..core.base import BaseModule
from ..core.contracts import DecodeContext, Packet
from ..core.registry import module

SMSG_UPDATE_OBJECT_FALLBACK = 0x0A9   # used only if the checkout did not resolve the symbol


def _inflate(pkt: Packet, ctx: DecodeContext) -> bytes:
    declared = struct.unpack_from("<I", pkt.body, 0)[0]
    inner = zlib.decompress(pkt.body[4:])
    if len(inner) != declared:
        ctx.log.error("%s: container declares %d bytes, inflated %d",
                      pkt.describe(), declared, len(inner))
    return inner


@module(id="compressed_moves", opcodes=("SMSG_COMPRESSED_MOVES",), order=90)
class CompressedMoves(BaseModule):
    """Unpacks a batch of micro-packets, each with its own opcode."""

    def expand(self, pkt: Packet, ctx: DecodeContext) -> Iterator[Packet]:
        inner = _inflate(pkt, ctx)
        pos = 0
        while pos < len(inner):
            length = inner[pos]                       # includes the 2 opcode bytes
            opcode = struct.unpack_from("<H", inner, pos + 1)[0]
            payload = inner[pos + 3:pos + 1 + length]
            pos += 1 + length
            yield replace(pkt, opcode=opcode, name=ctx.tables.opcodes.name(opcode),
                          body=payload, via=self.id)


@module(id="compressed_update_object", opcodes=("SMSG_COMPRESSED_UPDATE_OBJECT",), order=90)
class CompressedUpdateObject(BaseModule):
    """Unpacks one SMSG_UPDATE_OBJECT body -- not a batch, despite the name."""

    def expand(self, pkt: Packet, ctx: DecodeContext) -> Iterator[Packet]:
        inner = _inflate(pkt, ctx)
        opcode = ctx.tables.opcodes.by_name.get("SMSG_UPDATE_OBJECT", SMSG_UPDATE_OBJECT_FALLBACK)
        yield replace(pkt, opcode=opcode, name=ctx.tables.opcodes.name(opcode) or "SMSG_UPDATE_OBJECT",
                      body=inner, via=self.id)
