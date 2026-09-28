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
