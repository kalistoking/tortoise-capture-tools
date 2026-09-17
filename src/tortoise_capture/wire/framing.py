"""Framing: reassembled stream -> decrypted Packet sequence.

Header layouts (src/framework/Network/MangosSocket.h):

    ServerPktHeader  uint16 size (BIG endian) + uint16 cmd (little) = 4 bytes
    ClientPktHeader  uint16 size (BIG endian) + uint32 cmd (little) = 6 bytes

body_len is size minus the opcode width. The mixed endianness is a real trap:
iSendPacket byte-swaps `size` but leaves `cmd` native, so reading both as
">HH" yields 0xEC01 where the opcode is 0x1EC.

This is the only place outside `modules/` that names an opcode, and only two
of them: the plaintext handshake messages that delimit where encryption
starts. They are a transport concern -- they exist before any decoding does --
which is why they are not modules. See ARCHITECTURE.md section 7.1.
"""

from __future__ import annotations

import struct
from typing import Callable, Iterator

from .. import log as _log
from ..core.contracts import Direction, Packet
from .crypt import HeaderCrypt
from .opcodes import OpcodeTable
from .pcap import Stream

_logger = _log.get_logger("wire.framing")

SMSG_AUTH_CHALLENGE = 0x1EC   # first S2C message, plaintext
CMSG_AUTH_SESSION = 0x1ED     # first C2S message, plaintext

HeaderReader = Callable[[bytes], tuple[int, int, int, int]]


def read_smsg_header(header: bytes) -> tuple[int, int, int, int]:
    """-> (size, opcode, header_len, body_len)"""
    size = struct.unpack_from(">H", header, 0)[0]
    opcode = struct.unpack_from("<H", header, 2)[0]
    return size, opcode, 4, size - 2


def read_cmsg_header(header: bytes) -> tuple[int, int, int, int]:
    size = struct.unpack_from(">H", header, 0)[0]
    opcode = struct.unpack_from("<I", header, 2)[0]
    return size, opcode, 6, size - 4


_DIRECTION_SETUP = {
    Direction.S2C: (read_smsg_header, 4, SMSG_AUTH_CHALLENGE, "SMSG_AUTH_CHALLENGE"),
    Direction.C2S: (read_cmsg_header, 6, CMSG_AUTH_SESSION, "CMSG_AUTH_SESSION"),
}


def walk(stream: Stream, direction: Direction, key: bytes, table: OpcodeTable,
         t0: float) -> Iterator[Packet]:
    """Yields every message in one direction, in stream order.

    Stops at the first desync (a body length that runs past the end of the
    stream, or an opcode above this fork's ceiling) after reporting where --
    the offset is unrecoverable past that point, and the other direction is
    unaffected because each has its own cipher state.
    """
    read_header, header_len, bootstrap_opcode, bootstrap_name = _DIRECTION_SETUP[direction]
    if not len(stream):
        _logger.error("%s stream is empty", direction)
        return

    # The handshake message is plaintext -- crypto is only initialised once
    # auth succeeds. Its own `size` field says exactly where it ends, so its
    # body never needs parsing to find the start of the encrypted traffic.
    size, opcode, hdr_len, body_len = read_header(stream.data[:header_len])
    if opcode != bootstrap_opcode:
        _logger.error("%s starts with opcode 0x%X, expected %s (0x%X) -- capture may not "
                      "begin at session start; decode will desync",
                      direction, opcode, bootstrap_name, bootstrap_opcode)
    yield Packet(seq=0, t=stream.time_at(0, t0), direction=direction, opcode=opcode,
                 name=table.name(opcode) or bootstrap_name,
                 body=stream.data[hdr_len:hdr_len + body_len])

    pos = hdr_len + body_len
    seq = 1
    crypt = HeaderCrypt(key)
    max_opcode = table.max_opcode
    data = stream.data

    while pos + header_len <= len(data):
        header = bytearray(data[pos:pos + header_len])
        crypt.decrypt(header)
        size, opcode, hdr_len, body_len = read_header(bytes(header))

        if max_opcode is not None and opcode > max_opcode:
            _logger.error("%s desync at offset %d: opcode 0x%X exceeds this fork's highest "
                          "known opcode 0x%X -- stopping this direction after %d packet(s)",
                          direction, pos, opcode, max_opcode, seq)
            return
        if body_len < 0 or pos + hdr_len + body_len > len(data):
            _logger.error("%s desync at offset %d: size=%d opcode=0x%X runs past the stream end "
                          "-- stopping this direction after %d packet(s)",
                          direction, pos, size, opcode, seq)
            return

        yield Packet(seq=seq, t=stream.time_at(pos, t0), direction=direction, opcode=opcode,
                     name=table.name(opcode), body=data[pos + hdr_len:pos + hdr_len + body_len])
        seq += 1
        pos += hdr_len + body_len

    _logger.info("%s: %d packets framed", direction, seq)
