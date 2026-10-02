"""Session-key recovery: what the client's headers give, and when it is not trusted."""

from __future__ import annotations

import logging
import struct

from tortoise_capture.wire import crypt, pcap

KEY = bytes((n * 37 + 11) % 256 for n in range(40))
CEILING = 0x500
CLIENT, SERVER = ("10.0.0.2", 50000), ("10.0.0.1", 8090)


def _encrypt(plain: bytes, state: list[int]) -> bytes:
    """EncryptSend (AuthCrypt.cpp:39-51): the cipher advances per header byte, never per segment."""
    out = bytearray()
    for byte in plain:
        cipher = ((byte ^ KEY[state[0] % 40]) + state[1]) & 0xFF
        state[0] += 1
        state[1] = cipher
        out.append(cipher)
    return bytes(out)


def _client_messages(count: int = 30) -> list[bytes]:
    """CMSG_AUTH_SESSION in the clear, then `count` messages with encrypted 6-byte headers
    and bodies of 8 to 30 bytes."""
    state = [0, 0]
    messages = []
    for n in range(count + 1):
        body = bytes((n * 7 + b) % 256 for b in range(8 + (n * 5) % 23))
        header = struct.pack(">H", len(body) + 4) + struct.pack("<I", 0x1ED if n == 0 else 0x95 + n)
        messages.append((header if n == 0 else _encrypt(header, state)) + body)
    return messages


def _server_stream(count: int = 60) -> pcap.Stream:
    """SMSG_AUTH_CHALLENGE in the clear, then `count` messages with encrypted 4-byte headers."""
    state = [0, 0]
    data = bytearray()
    for n in range(count + 1):
        body = bytes((n * 3 + b) % 256 for b in range(2 + (n * 11) % 40))
        header = struct.pack(">H", len(body) + 2) + struct.pack("<H", 0x1EC if n == 0 else 0x80 + n)
        data += (header if n == 0 else _encrypt(header, state)) + body
    return pcap.Stream(bytes(data))


def _recover(chunks, server=None, ceiling=CEILING):
    return crypt.recover_session_key(chunks, server, ceiling)


def test_the_key_comes_out_of_one_chunk_per_message():
    stream = _server_stream()
    assert _recover(_client_messages(), stream) == KEY
    assert crypt.recover_session_key(_client_messages()) == KEY      # unchecked, as before


def test_a_fresh_tail_of_a_retransmit_kept_as_its_own_chunk_is_not_a_key():
    """[M1 + 10 B of M2, the rest of M2, M3, ...]: the second chunk starts in the
    middle of M2, its bytes 4 and 5 are body, and the two slots they give are
    garbage. The server's headers do not decode with the key, so it is not one."""
    messages = _client_messages()
    chunks = [messages[0], messages[1] + messages[2][:10], messages[2][10:], *messages[3:]]
    assert _recover(chunks, _server_stream()) in (None, KEY)
    assert _recover(chunks, _server_stream()) is None          # nothing here lets it be right
    assert crypt.recover_session_key(chunks) != KEY            # and unchecked, it was wrong


def test_two_messages_in_one_segment_do_not_give_a_wrong_key_either():
    """The cipher advances per header, so a segment holding two messages moves it
    twelve slots and recovery counted six."""
    messages = _client_messages()
    chunks = [messages[0], messages[1] + messages[2], *messages[3:]]
    assert _recover(chunks, _server_stream()) in (None, KEY)
    assert crypt.recover_session_key(chunks) != KEY


def test_a_key_that_fails_the_server_headers_says_which_slots_to_look_at():
    messages = _client_messages()
    chunks = [messages[0], messages[1] + messages[2][:10], messages[2][10:], *messages[3:]]
    seen = []

    class Catch(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    handler = Catch(logging.ERROR)
    crypt._logger.addHandler(handler)
    try:
        assert _recover(chunks, _server_stream()) is None
    finally:
        crypt._logger.removeHandler(handler)
    assert any("not recovered" in line and "slot" in line for line in seen)


def test_a_key_is_checked_against_the_opcode_ceiling_too():
    """A bad slot pair in the opcode bytes of every tenth server header leaves the
    size right and the opcode wrong: only the ceiling shows it."""
    wrong = bytearray(KEY)
    wrong[6] ^= 0x5A
    wrong[7] ^= 0xA5
    assert crypt.key_fails_server(bytes(wrong), _server_stream(), CEILING) is not None
    assert crypt.key_fails_server(KEY, _server_stream(), CEILING) is None


def test_a_retransmit_overlapping_a_message_still_gives_the_key_through_reassembly():
    """What pcap.py hands recovery when M2 is captured as its first 10 bytes and a
    retransmit then carries it from byte 4: the fresh tail is the rest of M2, and
    belongs to the chunk it continues rather than being a chunk starting mid-message."""
    messages = _client_messages()
    pieces, off = [], 0
    for n, message in enumerate(messages):
        pieces += [(off, 10), (off + 4, len(message) - 4)] if n == 2 else [(off, len(message))]
        off += len(message)
    sent = b"".join(messages)
    segments = [(1000 + at, sent[at:at + size], float(i)) for i, (at, size) in enumerate(pieces)]
    stream = pcap._reassemble(segments, CLIENT, SERVER)
    assert stream.data == sent
    assert stream.segments == tuple(messages)
    assert crypt.recover_session_key(stream.segments, _server_stream(), CEILING) == KEY
