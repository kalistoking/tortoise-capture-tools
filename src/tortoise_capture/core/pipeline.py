"""Packet production: framing both directions, merged by time, containers expanded.

Container opcodes (the compressed ones) are not special-cased here. Any
module that offers `expand()` is asked to unpack the packets it holds, and
the result re-enters the same stream -- so a nested container would work
without a line changing, and the runner keeps its promise of never branching
on an opcode.
"""

from __future__ import annotations

import heapq
from dataclasses import replace
from typing import Iterator

from .. import log as _log
from ..wire import framing
from ..wire.pcap import CaptureSession
from .contracts import DecodeContext, Direction, Packet, offers_expand
from .registry import Registry

_logger = _log.get_logger("pipeline")

MAX_EXPANSION_DEPTH = 4   # a container inside a container inside... is a bug, not data


def _time_key(pkt: Packet) -> tuple[float, int]:
    return (pkt.t if pkt.t is not None else 0.0, pkt.seq)


def _expand(pkt: Packet, registry: Registry, ctx: DecodeContext, depth: int) -> Iterator[Packet]:
    """Yields the packet, then whatever a container module unpacks from it."""
    yield pkt
    if depth >= MAX_EXPANSION_DEPTH:
        _logger.error("expansion depth limit reached at %s -- not unpacking further",
                      pkt.describe())
        return
    for mod in registry.for_opcode(pkt.opcode):
        if not offers_expand(mod):
            continue
        try:
            children = list(mod.expand(pkt, ctx))
        except Exception as exc:                      # one bad container, not the run
            _logger.error("module %s failed to expand %s: %s", mod.id, pkt.describe(), exc)
            continue
        for child in children:
            yield from _expand(child, registry, ctx, depth + 1)


def packets(session: CaptureSession, key: bytes, registry: Registry,
            ctx: DecodeContext) -> Iterator[Packet]:
    """The full Packet stream: both directions in capture order, expanded.

    Each direction is framed in stream order (which is time order within that
    direction), so merging the two by timestamp stays streaming -- no buffering
    of the whole session.
    """
    table = ctx.tables.opcodes
    streams = (
        framing.walk(session.s2c, Direction.S2C, key, table, session.t0),
        framing.walk(session.c2s, Direction.C2S, key, table, session.t0),
    )
    index = 0
    for pkt in heapq.merge(*streams, key=_time_key):
        for out in _expand(pkt, registry, ctx, depth=0):
            yield replace(out, seq=index)     # one running index over the whole session
            index += 1
