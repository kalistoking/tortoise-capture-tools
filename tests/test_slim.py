"""Keeping only the WoW conversation of a capture, record for record."""

from __future__ import annotations

import struct
import tempfile
from pathlib import Path

from tortoise_capture.wire import slim
from tortoise_capture.wire.slim import Endpoint, SlimError

LOCAL = "127.0.0.1"


def _segment(src, sport, dst, dport, seq, payload=b"", link=0):
    tcp = struct.pack(">HHIIBBHHH", sport, dport, seq, 0, 5 << 4, 0x18, 65535, 0, 0) + payload
    ip = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + len(tcp), 0, 0, 64, 6, 0,
                     bytes(map(int, src.split("."))), bytes(map(int, dst.split(".")))) + tcp
    return (struct.pack("<I", 2) if link == 0 else b"\0" * 12 + b"\x08\x00") + ip


def _pcap(frames, link=0):
    out = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 262144, link)
    for t, frame in frames:
        out += struct.pack("<IIII", int(t), round((t % 1) * 1e6), len(frame), len(frame)) + frame
    return out


def _realm_list(*realms):
    """AuthSocket.cpp:1221-1280, as the logon server sends it."""
    body = struct.pack("<IB", 0, len(realms))
    for name, address in realms:
        body += (struct.pack("<IB", 6, 0) + name.encode() + b"\0" + address.encode() + b"\0"
                 + struct.pack("<fBBB", 0.0, 1, 1, 0))
    body += b"\x02\x00"
    return bytes([slim.CMD_REALM_LIST]) + struct.pack("<H", len(body)) + body


# The challenge carries random bytes; a 0x10 among them must not read as a realm list.
_CHALLENGE = bytes([0x00, 0x00, 0x00, 0x10, 0x32, 0x00]) + bytes(range(40))


def _session(world_port=8090, listed="127.0.0.1:8090", with_logon=True, link=0):
    frames = [(100.0, _segment(LOCAL, 50000, LOCAL, 8011, 1, b"x" * 500, link))]    # the board
    if with_logon:
        frames += [
            (101.00, _segment(LOCAL, 5000, LOCAL, 3724, 1, b"\x00challenge", link)),
            (101.01, _segment(LOCAL, 3724, LOCAL, 5000, 1, _CHALLENGE, link)),
            (101.02, _segment(LOCAL, 3724, LOCAL, 5000, 1 + len(_CHALLENGE),
                              _realm_list(("TurtleWoW Local", listed)), link)),
        ]
    frames += [
        (101.13, _segment(LOCAL, 5001, LOCAL, world_port, 1, b"\x00\x04world", link)),
        (101.14, _segment(LOCAL, world_port, LOCAL, 5001, 1, b"\x00\x06reply!", link)),
        (101.50, _segment(LOCAL, 6000, LOCAL, 3306, 1, b"SELECT 1", link)),        # the database
        (102.00, _segment(LOCAL, 50000, LOCAL, 8011, 501, b"y" * 500, link)),
    ]
    return _pcap(frames, link)


def _with_capture(content, body):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "capture.pcap"
        path.write_bytes(content)
        return body(path)


def _plan(content, world=None, logon=Endpoint(None, slim.LOGON_PORT)):
    return _with_capture(content, lambda path: slim.plan(path, logon, world))


def test_the_realm_list_is_read_from_the_logon_reply_and_nothing_else():
    stream = _CHALLENGE + _realm_list(("TurtleWoW Local", "127.0.0.1:8090"), ("PTR", "10.0.0.2:8085"))
    assert slim.realm_lists(stream) == [[("TurtleWoW Local", "127.0.0.1:8090"),
                                         ("PTR", "10.0.0.2:8085")]]
    assert slim.realm_lists(_CHALLENGE) == []


def test_logon_and_world_are_kept_and_every_other_connection_dropped():
    """Both halves of the conversation: the world address comes from the realm list."""
    p = _plan(_session())
    assert p.world == Endpoint(LOCAL, 8090) and p.world_server == (LOCAL, 8090)
    assert p.kept_connections == {"logon": 1, "world": 1}
    assert p.dropped_connections == 2                        # the board and the database
    kept = {r.tcp[3] if r.tcp[3] in (3724, 8090) else r.tcp[1]
            for r in p.records if r.index in p.keep and r.index != p.time_origin}
    assert kept == {3724, 8090}


def test_the_earliest_record_stays_as_the_time_origin():
    """Every capture-relative time is measured from it; without it they all shift."""
    p = _plan(_session())
    assert p.time_origin == 0 and 0 in p.keep
    assert p.dropped_records == 2                            # the database, the board's second


def test_a_named_world_port_wins_over_the_realm_list():
    p = _plan(_session(world_port=8085, listed="127.0.0.1:8090"), world=Endpoint(None, 8085))
    assert p.world_server == (LOCAL, 8085)


def test_a_capture_begun_after_the_logon_with_no_world_port_named_stops():
    try:
        _plan(_session(with_logon=False))
    except SlimError as exc:
        assert "no realm list" in str(exc) and "--port" in str(exc)
    else:
        raise AssertionError("slimmed without knowing which conversation to keep")


def test_the_same_capture_slims_once_a_world_port_is_named():
    p = _plan(_session(with_logon=False), world=Endpoint(None, 8090))
    assert p.kept_connections == {"world": 1}


def test_a_realm_nobody_talked_to_is_not_kept_on_its_word():
    try:
        _plan(_session(listed="10.0.0.9:8090"))
    except SlimError as exc:
        assert "10.0.0.9:8090" in str(exc)
    else:
        raise AssertionError("kept a conversation the realm list named but nobody had")


def test_the_slim_copy_is_the_original_with_records_removed_verbatim():
    def body(path):
        p = slim.plan(path, Endpoint(None, slim.LOGON_PORT), None)
        out = path.with_name("slim.pcap")
        slim.write(p, out)
        data, records = slim.read_records(out)
        kept = [p.data[r.start:r.end] for r in p.records if r.index in p.keep]
        return p, data, [data[r.start:r.end] for r in records], kept, out.stat().st_size
    p, data, raws, kept, size = _with_capture(_session(), body)
    assert data[:24] == p.header
    assert raws == kept
    assert size == p.kept_bytes


def test_an_ethernet_capture_reads_the_same():
    p = _plan(_session(link=1))
    assert p.kept_connections == {"logon": 1, "world": 1}


def test_pcapng_is_refused_rather_than_misread():
    try:
        _plan(struct.pack("<I", 0x0A0D0D0A) + b"\0" * 28)
    except SlimError as exc:
        assert "pcapng" in str(exc)
    else:
        raise AssertionError("read a pcapng file as libpcap")


def test_the_report_says_what_went_and_where_the_world_address_came_from():
    lines = list(slim.describe(_plan(_session())))
    assert any("dropped 2 other connection(s) and 0 non-TCP" in line for line in lines)
    assert any("earliest" in line for line in lines)
    assert any("from the realm list" in line for line in lines)
    named = list(slim.describe(_plan(_session(), world=Endpoint(None, 8090))))
    assert not any("realm list" in line for line in named)


def test_the_slim_copy_sits_beside_the_original():
    assert slim.slim_name(Path("x/Death Prophet Rakameg.pcap")) == Path("x/Death Prophet Rakameg.wow.pcap")
