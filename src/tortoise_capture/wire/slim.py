"""Keep only the WoW conversation of a capture, record for record.

A capture of a WoW session is mostly everything else the machine was doing:
`Cow_Elwyn_Forest.pcap` is 285 MB, its WoW session under 2 MB on disk. This
writes the WoW part beside the original, so the original can go.

**The conversation is two connections.** The client talks to the logon server
first (3724 by default), and learns the world server's address from the realm
list it sends back (AuthSocket.cpp:1221-1280). Keeping only the world port
would drop the logon exchange; keeping it means the world address is read from
the capture rather than assumed. Either endpoint can be named instead -- any
address, any port -- and a named one always wins over a detected one. A
capture with no realm list (it began after the logon) and no named world port
is refused: guessing would keep the wrong conversation.

**The unit is a connection, not an address.** On a loopback capture every
connection -- WoW, a web server, the database -- is 127.0.0.1.

**Records are copied verbatim**, header and bytes, never re-serialised, so the
slim copy is the original with records removed. One record outside the
conversation is kept on purpose: the capture's earliest, because every
capture-relative time is measured from it (`pcap.read_session`'s `t0`), and
dropping it would shift every timestamp the decoder reports.

Only libpcap files are read (the format the captures here are in); a pcapng
file is refused with a message rather than misread.
"""

from __future__ import annotations

import struct
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

from .. import log as _log

_logger = _log.get_logger("wire.slim")

LOGON_PORT = 3724
CMD_REALM_LIST = 0x10

# magic -> (byte order, fraction per second)
_MAGICS = {0xA1B2C3D4: ("<", 1e6), 0xD4C3B2A1: (">", 1e6),
           0xA1B23C4D: ("<", 1e9), 0x4D3CB2A1: (">", 1e9)}
LINKTYPE_NULL, LINKTYPE_ETHERNET, LINKTYPE_RAW = 0, 1, 101


class SlimError(Exception):
    """The capture cannot be slimmed without guessing; the message says why."""


@dataclass(frozen=True, slots=True)
class Endpoint:
    """A server endpoint; `ip` None matches any address on the port."""

    ip: str | None
    port: int

    def matches(self, ip: str, port: int) -> bool:
        return port == self.port and self.ip in (None, ip)

    def __str__(self) -> str:
        return f"{self.ip or '*'}:{self.port}"


@dataclass(frozen=True, slots=True)
class _Record:
    """Offsets into the file rather than copies: a capture can be hundreds of MB."""

    index: int
    time: float
    start: int                   # the 16-byte record header, then its data, up to `end`
    end: int
    tcp: tuple | None            # (src, sport, dst, dport, seq, payload start, payload end)

    @property
    def size(self) -> int:
        return self.end - self.start


@dataclass
class SlimPlan:
    source: Path
    data: bytes                              # the whole capture, its header first
    records: list[_Record]
    logon: Endpoint
    world: Endpoint
    world_server: tuple[str, int] | None    # the first world server a client talked to
    realms: list[tuple[str, str]]           # (name, address) from the realm list
    world_source: str = "as named"           # or "from the realm list"
    keep: set[int] = field(default_factory=set)
    time_origin: int | None = None           # index of a record kept only as t0
    kept_connections: dict[str, int] = field(default_factory=dict)   # role -> count
    dropped_connections: int = 0
    dropped_records: int = 0
    dropped_bytes: int = 0
    dropped_other: int = 0                   # records that are not TCP over IPv4 at all

    @property
    def kept_bytes(self) -> int:
        return len(self.header) + sum(r.size for r in self.records if r.index in self.keep)

    @property
    def header(self) -> bytes:
        return self.data[:24]

    @property
    def has_foreign(self) -> bool:
        return self.dropped_records > 0


def read_records(path: Path) -> tuple[bytes, list[_Record]]:
    """The whole file and every record in it, with its TCP fields when it has any."""
    data = Path(path).read_bytes()
    if len(data) < 24:
        raise SlimError(f"{path}: too short to be a capture")
    magic, = struct.unpack_from("<I", data)
    if magic == 0x0A0D0D0A:
        raise SlimError(f"{path}: pcapng is not read here -- save the capture as .pcap")
    if magic not in _MAGICS:
        raise SlimError(f"{path}: not a libpcap capture (magic {magic:#010x})")
    order, fraction = _MAGICS[magic]
    link, = struct.unpack_from(order + "I", data, 20)
    if link not in (LINKTYPE_NULL, LINKTYPE_ETHERNET, LINKTYPE_RAW):
        raise SlimError(f"{path}: link type {link} is not read here")

    records, pos, index = [], 24, 0
    while pos + 16 <= len(data):
        sec, frac, incl, _ = struct.unpack_from(order + "IIII", data, pos)
        end = pos + 16 + incl
        if end > len(data):
            _logger.warning("%s: last record truncated, %d byte(s) short", path, end - len(data))
            break
        records.append(_Record(index, sec + frac / fraction, pos, end,
                               _tcp(data, pos + 16, end, link)))
        pos, index = end, index + 1
    return data, records


def _tcp(data: bytes, frame: int, end: int, link: int) -> tuple | None:
    """TCP fields of the frame at data[frame:end], by offset -- nothing copied."""
    if link == LINKTYPE_NULL:
        # A 4-byte address family in the capturing host's byte order; 2 is AF_INET.
        if end - frame < 4 or 2 not in (data[frame], data[frame + 3]):
            return None
        ip = frame + 4
    elif link == LINKTYPE_ETHERNET:
        if end - frame < 14 or data[frame + 12:frame + 14] != b"\x08\x00":
            return None
        ip = frame + 14
    else:
        ip = frame
    if end - ip < 20 or data[ip] >> 4 != 4 or data[ip + 9] != 6:
        return None
    total, = struct.unpack_from(">H", data, ip + 2)
    # Large-send offload leaves 0 here on some loopback captures; the frame's
    # own captured length bounds it then.
    tcp, stop = ip + (data[ip] & 0x0F) * 4, min(ip + total, end) if total else end
    if stop - tcp < 20:
        return None
    sport, dport, seq = struct.unpack_from(">HHI", data, tcp)
    return (".".join(map(str, data[ip + 12:ip + 16])), sport,
            ".".join(map(str, data[ip + 16:ip + 20])), dport,
            seq, tcp + (data[tcp + 12] >> 4) * 4, stop)


def realm_lists(stream: bytes) -> list[list[tuple[str, str]]]:
    """Every realm list in a logon server's reply stream, as (name, address).

    Found by parsing, not by position: a candidate is accepted only when its
    declared size holds exactly the realms it counts and the closing 0x0002
    (AuthSocket.cpp:1230-1280), so a stray 0x10 elsewhere in the stream --
    the challenge carries random bytes -- is not mistaken for one.
    """
    found = []
    at = stream.find(bytes([CMD_REALM_LIST]))
    while at != -1:
        realms = _realm_list_at(stream, at)
        if realms is not None:
            found.append(realms)
        at = stream.find(bytes([CMD_REALM_LIST]), at + 1)
    return found


def _realm_list_at(stream: bytes, at: int) -> list[tuple[str, str]] | None:
    try:
        size, = struct.unpack_from("<H", stream, at + 1)
        body = stream[at + 3:at + 3 + size]
        if len(body) != size:
            return None
        unused, count = struct.unpack_from("<IB", body)
        if unused != 0:
            return None
        pos, realms = 5, []
        for _ in range(count):
            pos += 5                                          # icon, flags
            name_end = body.index(b"\0", pos)
            address_end = body.index(b"\0", name_end + 1)
            realms.append((body[pos:name_end].decode("utf-8", "replace"),
                           body[name_end + 1:address_end].decode("ascii")))
            pos = address_end + 1 + 7                         # population, chars, tz, 0
        if pos + 2 != size or body[pos:pos + 2] != b"\x02\x00":
            return None
        return realms
    except (struct.error, ValueError, UnicodeDecodeError):
        return None


def _address(text: str) -> Endpoint | None:
    host, _, port = text.rpartition(":")
    return Endpoint(host, int(port)) if host and port.isdigit() else None


def plan(path: Path, logon: Endpoint, world: Endpoint | None) -> SlimPlan:
    """Decides what to keep. `world` None means "read it from the realm list"."""
    data, records = read_records(path)

    # The logon server's replies, one stream per client connection.
    replies: dict[tuple, list[tuple[int, bytes]]] = defaultdict(list)
    for r in records:
        if r.tcp and logon.matches(r.tcp[0], r.tcp[1]) and r.tcp[6] > r.tcp[5]:
            replies[(r.tcp[2], r.tcp[3])].append((r.tcp[4], data[r.tcp[5]:r.tcp[6]]))
    realms = []
    for segments in replies.values():
        stream = b"".join(dict(sorted(segments)).values())    # one copy per sequence number
        for listing in realm_lists(stream):
            realms.extend(listing)

    source = "as named"
    if world is None:
        source = "from the realm list"
        listed = [e for e in (_address(a) for _, a in realms) if e is not None]
        if not listed:
            raise SlimError(f"{path}: no realm list from a logon server at {logon} (the capture "
                            "may begin after the logon) and no world port was named -- name "
                            "it with --port, TCT_PORT or [capture] port")
        talked = [e for e in dict.fromkeys(listed)
                  if any(r.tcp and e.matches(r.tcp[2], r.tcp[3]) for r in records)]
        if not talked:
            raise SlimError(f"{path}: the realm list names {', '.join(map(str, listed))}, but "
                            "no client in the capture talks to it -- name the world port")
        if len(talked) > 1:
            raise SlimError(f"{path}: clients talk to more than one listed realm "
                            f"({', '.join(map(str, talked))}) -- name the one to keep")
        world = talked[0]

    out = SlimPlan(source=Path(path), data=data, records=records, logon=logon, world=world,
                   world_server=None, realms=realms, world_source=source)
    connections: dict[frozenset, str] = {}
    for r in records:
        if r.tcp is None:
            continue
        src, sport, dst, dport = r.tcp[:4]
        pair = frozenset(((src, sport), (dst, dport)))
        role = ("world" if world.matches(dst, dport) or world.matches(src, sport)
                else "logon" if logon.matches(dst, dport) or logon.matches(src, sport)
                else None)
        connections.setdefault(pair, role)
        if role:
            out.keep.add(r.index)
            if (role == "world" and out.world_server is None and r.tcp[6] > r.tcp[5]
                    and world.matches(dst, dport)):
                out.world_server = (dst, dport)
    if out.world_server is None:
        raise SlimError(f"{path}: no client talks to a world server at {world}")

    for role in connections.values():
        if role:
            out.kept_connections[role] = out.kept_connections.get(role, 0) + 1
        else:
            out.dropped_connections += 1

    earliest = min(records, key=lambda r: r.time)
    if earliest.index not in out.keep:
        out.keep.add(earliest.index)
        out.time_origin = earliest.index
    for r in records:
        if r.index not in out.keep:
            out.dropped_records += 1
            out.dropped_bytes += r.size
            out.dropped_other += r.tcp is None
    return out


def write(p: SlimPlan, out: Path) -> None:
    with open(out, "wb") as fp:
        fp.write(p.header)
        for r in p.records:
            if r.index in p.keep:
                fp.write(p.data[r.start:r.end])


def slim_name(capture: Path) -> Path:
    return capture.with_name(f"{capture.stem}.wow{capture.suffix}")


def describe(p: SlimPlan) -> Iterator[str]:
    kept = ", ".join(f"{n} {role}" for role, n in sorted(p.kept_connections.items()))
    yield (f"kept {kept} connection(s) -- world {p.world_server[0]}:{p.world_server[1]} "
           f"{p.world_source}, logon {p.logon}")
    if p.time_origin is not None:
        yield "kept 1 record outside them: the capture's earliest, which every time is measured from"
    yield (f"dropped {p.dropped_connections} other connection(s) and {p.dropped_other} "
           f"non-TCP record(s): {p.dropped_records} record(s), {p.dropped_bytes:,} bytes -- "
           f"{p.kept_bytes:,} bytes remain")
