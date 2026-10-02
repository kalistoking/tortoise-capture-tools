"""Capture reading and TCP stream reassembly.

Produces one byte stream per direction plus the capture timestamps needed to
put a wall-clock time on any byte offset. scapy is imported lazily so the
decode and emit layers stay importable (and testable) without it.

Unlike the prototype, the client address is taken from the capture rather
than assumed equal to the server address, so a non-loopback capture works.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from pathlib import Path

from .. import log as _log

_logger = _log.get_logger("wire.pcap")


@dataclass(frozen=True, slots=True)
class Stream:
    """One reassembled direction of a TCP conversation."""

    data: bytes
    breakpoints: tuple[tuple[int, float], ...] = ()   # (buffer offset, capture time)
    segments: tuple[bytes, ...] = ()                  # in order; an overlap's tail joins its chunk
    gaps: tuple[tuple[int, int], ...] = ()            # [start, end) zero-filled, never seen
    _offsets: list[int] = field(default_factory=list, init=False, compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_offsets", [bp[0] for bp in self.breakpoints])

    def time_at(self, pos: int, t0: float) -> float | None:
        """Capture-relative seconds for byte offset `pos`."""
        if not self.breakpoints:
            return None
        idx = bisect.bisect_right(self._offsets, pos) - 1
        idx = max(0, min(idx, len(self.breakpoints) - 1))
        return round(self.breakpoints[idx][1] - t0, 3)

    def unseen(self, start: int, end: int) -> int:
        """How many bytes of [start, end) the capture never saw."""
        return sum(max(0, min(end, b) - max(start, a)) for a, b in self.gaps)

    def __len__(self) -> int:
        return len(self.data)


@dataclass(frozen=True, slots=True)
class CaptureSession:
    s2c: Stream
    c2s: Stream
    t0: float
    server: tuple[str, int]
    client: tuple[str, int]


def _segments(packets, src: tuple[str, int], dst: tuple[str, int]) -> list[tuple[int, bytes, float]]:
    """(seq, payload, capture time) of every payload-carrying segment src -> dst."""
    from scapy.all import IP, Raw, TCP  # noqa: PLC0415 - lazy: scapy is heavy

    return [(p[TCP].seq, bytes(p[Raw].load), float(p.time)) for p in packets
            if TCP in p and IP in p and Raw in p and p[Raw].load
            and (p[IP].src, p[TCP].sport) == src and (p[IP].dst, p[TCP].dport) == dst]


def _reassemble(segments: list[tuple[int, bytes, float]], src: tuple[str, int],
                dst: tuple[str, int]) -> Stream:
    """Orders segments by TCP sequence number and keeps each byte once.

    Every segment is kept, not one per sequence number -- a retransmit can be
    cut at other boundaries than the original, or be shorter -- and the stream
    grows only by the bytes past its end. Sequence numbers count from the
    first segment's, modulo 2^32, so a stream that wraps carries on.

    `Stream.segments` is for key recovery, which reads one message per chunk:
    the fresh tail of an overlapping retransmit starts wherever the overlap
    ended, mid-message more often than not, so it joins the chunk it
    continues instead of being one of its own.

    A gap is zero-filled and reported: the decode after it will desync, and
    saying where is more useful than silently shifting every later offset.
    """
    if not segments:
        return Stream(b"")

    base = segments[0][0]
    # Offsets from the first segment seen, signed: a retransmit of earlier
    # data captured after it sits just before, not 4 GiB after.
    ordered = sorted(((((seq - base + 2**31) % 2**32) - 2**31, data, ts)
                      for seq, data, ts in segments), key=lambda s: (s[0], -len(s[1])))

    buf = bytearray()
    breakpoints: list[tuple[int, float]] = []
    kept: list[bytes] = []
    gaps: list[tuple[int, int]] = []
    start = ordered[0][0]
    end = start                                   # the stream holds [start, end)
    for offset, data, ts in ordered:
        if offset > end:
            _logger.warning("gap in %s:%d -> %s:%d stream: %d bytes missing at offset %d",
                            src[0], src[1], dst[0], dst[1], offset - end, end - start)
            gaps.append((len(buf), len(buf) + offset - end))
            buf.extend(b"\x00" * (offset - end))
            end = offset
        fresh = data[end - offset:]               # only what is past the end so far
        if fresh:
            breakpoints.append((len(buf), ts))
            if offset < end:
                kept[-1] += fresh                 # the tail of an overlap continues its chunk
            else:
                kept.append(fresh)
            buf.extend(fresh)
            end += len(fresh)

    return Stream(bytes(buf), tuple(breakpoints), tuple(kept), tuple(gaps))


def _world_client(senders: list[tuple[str, int]], server_ip: str,
                  port: int) -> tuple[str, int] | None:
    """The first client to send to the world server; the others are named, not
    dropped unseen -- a reconnect, or a session still closing when the
    capture began, is a conversation this run does not decode."""
    clients = list(dict.fromkeys(senders))
    if len(clients) > 1:
        _logger.warning("%d more connection(s) to %s:%d carry payload and are not decoded: "
                        "%s -- only the first, %s:%d, is", len(clients) - 1, server_ip, port,
                        ", ".join(f"{ip}:{sport}" for ip, sport in clients[1:]), *clients[0])
    return clients[0] if clients else None


def read_session(path: Path, server_ip: str, port: int) -> CaptureSession:
    """Reads a capture and reassembles the one world session it contains."""
    from scapy.all import IP, Raw, TCP, rdpcap  # noqa: PLC0415

    _logger.info("reading %s", path)
    packets = rdpcap(str(path))

    client = _world_client([(p[IP].src, p[TCP].sport) for p in packets
                            if TCP in p and IP in p and Raw in p
                            and p[TCP].dport == port and p[IP].dst == server_ip],
                           server_ip, port)
    if client is None:
        raise FileNotFoundError(f"no client->server payload to {server_ip}:{port} in {path}")

    server = (server_ip, port)
    t0 = min(float(p.time) for p in packets)     # one origin, so both directions share a clock
    s2c = _reassemble(_segments(packets, server, client), server, client)
    c2s = _reassemble(_segments(packets, client, server), client, server)

    _logger.info("session %s:%d <-> %s:%d -- S2C %d bytes, C2S %d bytes",
                 client[0], client[1], server[0], server[1], len(s2c), len(c2s))
    return CaptureSession(s2c=s2c, c2s=c2s, t0=t0, server=server, client=client)
