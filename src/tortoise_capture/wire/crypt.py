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
encrypted client messages recover all 40 bytes.
"""

from __future__ import annotations

from .. import log as _log

_logger = _log.get_logger("wire.crypt")

SESSION_KEY_LEN = 40      # two concatenated SHA-1 hashes
CLIENT_HEADER_LEN = 6     # uint16 size (BE) + uint32 cmd (LE)


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


def recover_session_key(client_segments: tuple[bytes, ...] | list[bytes]) -> bytes | None:
    """Known-plaintext recovery from the client->server segments.

    Assumes each TCP segment starts on a message boundary, which is how the
    client writes: one world message per send. The first segment is the
    plaintext CMSG_AUTH_SESSION and does not advance the cipher state.
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
            _logger.info("session key recovered: %s", bytes(key).hex())
            return bytes(key)

    _logger.error("recovered only %d/%d session key bytes (need ~20 encrypted client messages)",
                  sum(known), SESSION_KEY_LEN)
    return None
