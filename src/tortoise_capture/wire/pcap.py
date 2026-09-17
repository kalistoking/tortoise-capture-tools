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
    segments: tuple[bytes, ...] = ()                  # in order, for key recovery
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

    def __len__(self) -> int:
        return len(self.data)


@dataclass(frozen=True, slots=True)
class CaptureSession:
    s2c: Stream
    c2s: Stream
    t0: float
    server: tuple[str, int]
    client: tuple[str, int]


def _reassemble(packets, src: tuple[str, int], dst: tuple[str, int]) -> Stream:
    """Orders segments by TCP sequence number, trimming retransmit overlap.

    A gap is zero-filled and reported: the decode after it will desync, and
    saying where is more useful than silently shifting every later offset.
    """
    from scapy.all import IP, Raw, TCP  # noqa: PLC0415 - lazy: scapy is heavy

    chunks: dict[int, tuple[bytes, float]] = {}
    for p in packets:
        if not (TCP in p and IP in p and Raw in p):
            continue
        if (p[IP].src, p[TCP].sport) != src or (p[IP].dst, p[TCP].dport) != dst:
            continue
        payload = bytes(p[Raw].load)
        if payload:
            chunks[p[TCP].seq] = (payload, float(p.time))

    if not chunks:
        return Stream(b"")

    buf = bytearray()
    breakpoints: list[tuple[int, float]] = []
    segments: list[bytes] = []
    ordered = sorted(chunks.items())
    expected = ordered[0][0]
    for seq, (data, ts) in ordered:
        if seq < expected:                       # retransmission overlap
            overlap = expected - seq
            data = data[overlap:] if overlap < len(data) else b""
        elif seq > expected:
            _logger.error("gap in %s:%d -> %s:%d stream: expected seq %d, got %d (%d bytes missing)",
                          src[0], src[1], dst[0], dst[1], expected, seq, seq - expected)
            buf.extend(b"\x00" * (seq - expected))
        if data:
            breakpoints.append((len(buf), ts))
            segments.append(data)
        buf.extend(data)
        expected = seq + len(data)

    return Stream(bytes(buf), tuple(breakpoints), tuple(segments))


def read_session(path: Path, server_ip: str, port: int) -> CaptureSession:
    """Reads a capture and reassembles the one world session it contains."""
    from scapy.all import IP, Raw, TCP, rdpcap  # noqa: PLC0415

    _logger.info("reading %s", path)
    packets = rdpcap(str(path))

    client = None
    for p in packets:
        if TCP in p and IP in p and Raw in p and p[TCP].dport == port and p[IP].dst == server_ip:
            client = (p[IP].src, p[TCP].sport)
            break
    if client is None:
        raise FileNotFoundError(f"no client->server payload to {server_ip}:{port} in {path}")

    server = (server_ip, port)
    t0 = min(float(p.time) for p in packets)     # one origin, so both directions share a clock
    s2c = _reassemble(packets, server, client)
    c2s = _reassemble(packets, client, server)

    _logger.info("session %s:%d <-> %s:%d -- S2C %d bytes, C2S %d bytes",
                 client[0], client[1], server[0], server[1], len(s2c), len(c2s))
    return CaptureSession(s2c=s2c, c2s=c2s, t0=t0, server=server, client=client)
