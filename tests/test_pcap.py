"""TCP reassembly: one direction of a connection back into the bytes that were sent."""

from __future__ import annotations

from tortoise_capture.wire import pcap

CLIENT, SERVER = ("10.0.0.2", 50000), ("10.0.0.1", 8090)
SENT = bytes(range(256)) * 2          # 512 bytes the client wrote


def _stream(pieces, isn=1000):
    """`pieces` are (offset, length) into SENT, in capture order."""
    segments = [((isn + off) % 2**32, SENT[off:off + n], float(i))
                for i, (off, n) in enumerate(pieces)]
    return pcap._reassemble(segments, CLIENT, SERVER).data


def test_a_retransmit_cut_at_other_boundaries_is_trimmed_to_what_is_new():
    """The overlap was trimmed, but the next expected byte was set from the
    trimmed piece's own start: short by the overlap, so the segment after it
    looked like a gap and zeros went in."""
    assert _stream([(0, 200), (100, 200), (300, 212)]) == SENT


def test_a_segment_inside_what_is_already_there_adds_nothing():
    """A retransmit wholly inside earlier data moved the stream position back."""
    assert _stream([(0, 300), (50, 100), (300, 212)]) == SENT


def test_a_shorter_retransmit_at_the_same_seq_does_not_replace_the_original():
    """Segments were kept by seq, the last one seen winning: a 100-byte
    retransmit at the seq of a 300-byte original lost 200 bytes to zeros."""
    assert _stream([(0, 300), (0, 100), (300, 212)]) == SENT


def test_a_sequence_number_that_wraps_past_2_32_carries_on():
    """Sorted by raw seq, the bytes after the wrap came first and a 4 GiB gap
    was zero-filled between them -- Ralthas's client began 47.8 MB short of it."""
    assert _stream([(0, 200), (200, 312)], isn=2**32 - 100) == SENT


def test_a_real_gap_is_still_filled_and_reported():
    data = _stream([(0, 200), (300, 212)])
    assert len(data) == len(SENT) and data[200:300] == b"\x00" * 100


def test_every_other_connection_to_the_world_port_is_named_not_dropped_unseen():
    """The first client to send to the world port was decoded and every other
    connection to it ignored without a word -- a reconnect after a disconnect,
    or a session that was still closing when the capture began."""
    import logging

    seen = []

    class Catch(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    handler = Catch(logging.WARNING)
    pcap._logger.addHandler(handler)
    try:
        client = pcap._world_client([("10.0.0.2", 50000), ("10.0.0.2", 50000),
                                     ("10.0.0.2", 50007)], "10.0.0.1", 8090)
    finally:
        pcap._logger.removeHandler(handler)
    assert client == ("10.0.0.2", 50000)
    assert any("50007" in message for message in seen)



def test_a_message_whose_body_the_capture_never_saw_is_not_decoded():
    """A gap wholly inside one message's body is zero-filled, and the framing
    stays aligned -- so the message went on to its decoder with zeros for
    bytes, and came out as confidently wrong values. It is dropped, and said."""
    import logging
    import struct

    from tortoise_capture.core.contracts import Direction
    from tortoise_capture.wire import framing

    key = bytes(range(40))
    i = j = 0

    def encrypt(header: bytes) -> bytes:
        nonlocal i, j
        out = bytearray()
        for plain in header:
            cipher = ((plain ^ key[i % len(key)]) + j) & 0xFF
            i += 1
            j = cipher
            out.append(cipher)
        return bytes(out)

    def message(opcode, body):
        return encrypt(struct.pack(">H", len(body) + 2) + struct.pack("<H", opcode)) + body

    data = (struct.pack(">H", 6) + struct.pack("<H", framing.SMSG_AUTH_CHALLENGE) + b"seed"
            + message(0x96, b"A" * 40) + message(0x97, b"B" * 4))
    body_start = 8 + 4
    stream = pcap.Stream(data, ((0, 0.0),), gaps=((body_start + 10, body_start + 30),))

    class Table:
        max_opcode = 0x400

        def name(self, opcode):
            return None

    seen = []

    class Catch(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    handler = Catch(logging.ERROR)
    framing._logger.addHandler(handler)
    try:
        packets = list(framing.walk(stream, Direction.S2C, key, Table(), 0.0))
    finally:
        framing._logger.removeHandler(handler)
    assert [p.opcode for p in packets] == [framing.SMSG_AUTH_CHALLENGE, 0x97]
    assert any("20 byte" in message for message in seen)


def test_the_reassembly_says_where_it_filled_a_gap():
    segments = [(1000, SENT[:200], 0.0), (1300, SENT[300:], 1.0)]
    assert pcap._reassemble(segments, CLIENT, SERVER).gaps == ((200, 300),)
