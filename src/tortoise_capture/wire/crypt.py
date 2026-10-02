"""Header crypto and session-key recovery (src/shared/Auth/AuthCrypt.cpp).

The world session encrypts only packet *headers*, with a running stream
cipher holding independent (i, j) state per direction:

    EncryptSend (SMSG, 4 header bytes):  cipher = (plain ^ key[i]) + j ; j = cipher
    DecryptRecv (CMSG, 6 header bytes):  plain  = (cipher - j) ^ key[i] ; j = cipher

Decoding a passive capture uses the DecryptRecv form in *both* directions --
j is always the previous ciphertext byte -- with separate state per stream.

Key recovery needs no server cooperation: bytes 4 and 5 of every client
header are the high bytes of a 32-bit opcode and are therefore always
plaintext zero, so key[i] = (cipher[i] - cipher[i-1]) & 0xFF. Twenty
encrypted client messages recover all 40 bytes. The server's headers then
check it: see recover_session_key.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .. import log as _log

if TYPE_CHECKING:
    from .pcap import Stream

_logger = _log.get_logger("wire.crypt")

SESSION_KEY_LEN = 40      # two concatenated SHA-1 hashes
CLIENT_HEADER_LEN = 6     # uint16 size (BE) + uint32 cmd (LE)
SERVER_HEADER_LEN = 4     # uint16 size (BE) + uint16 cmd (LE)
SERVER_HEADERS_CHECKED = 100
SERVER_HEADERS_TO_COVER = SESSION_KEY_LEN // SERVER_HEADER_LEN


class HeaderCrypt:
    """Running per-direction header cipher state."""

    __slots__ = ("key", "i", "j")

    def __init__(self, key: bytes) -> None:
        if len(key) != SESSION_KEY_LEN:
            raise ValueError(f"session key must be {SESSION_KEY_LEN} bytes, got {len(key)}")
        self.key = key
        self.i = 0
        self.j = 0

    def decrypt(self, header: bytearray) -> None:
        """In place, advancing the stream state by len(header) bytes."""
        n = len(self.key)
        for pos in range(len(header)):
            cipher = header[pos]
            header[pos] = ((cipher - self.j) & 0xFF) ^ self.key[self.i % n]
            self.i += 1
            self.j = cipher


def _walk_server(key: bytes, server: Stream, ceiling: int | None) -> tuple[int, str | None]:
    """(headers checked, why the first one failed -- or None) decrypting the
    server's headers with `key`.

    The first message is the plaintext SMSG_AUTH_CHALLENGE and ends where its
    own size says; every later one starts with a 4-byte header, and the size in
    it says where the next begins, so one wrong slot of the key turns the size
    (a wrong header next) or the opcode (a wrong module, silently) into
    nonsense. The walk stops at the first zero-filled gap: past it the bytes
    were never seen and a failure would say nothing about the key. The header
    layout is framing.read_smsg_header's, repeated here because framing
    imports this module.
    """
    data = server.data
    limit = server.gaps[0][0] if server.gaps else len(data)
    if len(data) < 2:
        return 0, None
    pos = 2 + int.from_bytes(data[:2], "big")
    cipher = HeaderCrypt(key)
    checked = 0
    while checked < SERVER_HEADERS_CHECKED and pos + SERVER_HEADER_LEN <= limit:
        slot = cipher.i % SESSION_KEY_LEN
        header = bytearray(data[pos:pos + SERVER_HEADER_LEN])
        cipher.decrypt(header)
        size = (header[0] << 8) | header[1]
        opcode = header[2] | (header[3] << 8)
        checked += 1
        above = ceiling is not None and opcode > ceiling
        if size < 2 or above:
            slots = ", ".join(str((slot + n) % SESSION_KEY_LEN) for n in range(SERVER_HEADER_LEN))
            return checked, (f"server header {checked} (stream offset {pos}) reads size {size}, "
                             f"opcode 0x{opcode:X}"
                             + (f" above this fork's highest, 0x{ceiling:X}" if above else "")
                             + f"; key slot(s) {slots} are in doubt")
        pos += 2 + size
    return checked, None


def key_fails_server(key: bytes, server: Stream, ceiling: int | None = None) -> str | None:
    """None if the server's first headers decode with `key`, else what went wrong:
    a size that cannot be one, or an opcode above `ceiling` (the highest this
    fork defines). A key that recovery got wrong in a slot or two decodes most
    headers and fails at the one that uses them."""
    return _walk_server(key, server, ceiling)[1]


def recover_session_key(client_segments: tuple[bytes, ...] | list[bytes],
                        server: Stream | None = None,
                        ceiling: int | None = None) -> bytes | None:
    """Known-plaintext recovery from the client->server segments.

    Assumes each chunk starts on a message boundary and holds exactly one
    message, which is how the client writes: one world message per send. The
    cipher advances per header, never per segment (AuthCrypt.cpp:39-51), so a
    chunk that is not one message -- two coalesced into a segment, or the middle
    of one -- leaves wrong slots and every later slot shifted. The first chunk
    is the plaintext CMSG_AUTH_SESSION and does not advance the cipher state.

    Nothing in the client's own bytes shows that assumption broke, so with the
    `server` stream the key is checked before it is trusted: its first headers
    must decrypt to sizes that chain and opcodes below `ceiling`. A key that
    fails is not returned.
    """
    if len(client_segments) < 2:
        _logger.error("only %d client segment(s): need at least 21 to recover a key",
                      len(client_segments))
        return None

    key = bytearray(SESSION_KEY_LEN)
    known = [False] * SESSION_KEY_LEN
    idx = 0

    for segment in client_segments[1:]:
        if len(segment) >= CLIENT_HEADER_LEN:
            header = segment[:CLIENT_HEADER_LEN]
            # header[4] and header[5] encrypt a known plaintext 0, and j for
            # each is simply the preceding ciphertext byte of the same header.
            for offset in (4, 5):
                slot = (idx + offset) % SESSION_KEY_LEN
                if not known[slot]:
                    key[slot] = (header[offset] - header[offset - 1]) & 0xFF
                    known[slot] = True
        idx = (idx + CLIENT_HEADER_LEN) % SESSION_KEY_LEN
        if all(known):
            return _checked(bytes(key), server, ceiling)

    _logger.error("recovered only %d/%d session key bytes (need ~20 encrypted client messages)",
                  sum(known), SESSION_KEY_LEN)
    return None


def _checked(key: bytes, server: Stream | None, ceiling: int | None) -> bytes | None:
    if server is not None:
        checked, failure = _walk_server(key, server, ceiling)
        if failure:
            _logger.error("session key not recovered: the key the client's headers give does not "
                          "decode the server's -- %s (a client chunk that is not one message "
                          "shifts the slots after it)", failure)
            return None
        if checked < SERVER_HEADERS_TO_COVER:
            _logger.warning("the session key was checked against only %d server header(s); "
                            "%d cover every slot of it", checked, SERVER_HEADERS_TO_COVER)
    _logger.info("session key recovered: %s", key.hex())
    return key
