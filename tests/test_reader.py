"""ByteReader primitives and GUID helpers."""

from __future__ import annotations

import struct

from support import HIGH_GAMEOBJECT, HIGH_UNIT, make_guid, pack_guid
from tortoise_capture.core.reader import (
    ByteReader, WireError, guid_entry, guid_high, guid_type, has_entry, is_unit,
)


def test_primitives_advance_the_cursor():
    r = ByteReader(struct.pack("<BHIQf", 1, 2, 3, 4, 5.5))
    assert r.u8() == 1 and r.u16() == 2 and r.u32() == 3 and r.u64() == 4
    assert abs(r.f32() - 5.5) < 1e-6
    assert r.eof


def test_overrun_names_the_field_and_the_packet():
    r = ByteReader(b"\x01", label="SMSG_TEST")
    r.u8()
    try:
        r.u32("spellId")
    except WireError as exc:
        assert "SMSG_TEST" in str(exc) and "spellId" in str(exc)
    else:
        raise AssertionError("an overrun must raise WireError")


def test_packguid_places_bytes_by_mask_bit_not_read_order():
    # Only byte 3 present: it belongs at bit position 3 (<< 24), not at the front.
    assert ByteReader(bytes([0b00001000, 0x7F])).packguid() == 0x7F000000
    for guid in (0, 1, 0xF13000F4AB2787EA, make_guid(62635, 42)):
        assert ByteReader(pack_guid(guid)).packguid() == guid


def test_cstring_and_sized_string():
    r = ByteReader(b"Ralthas\x00" + struct.pack("<I", 6) + b"hello\x00")
    assert r.cstring() == "Ralthas"
    assert r.sized_string() == "hello"


def test_guid_helpers_gate_on_the_high_word():
    unit = make_guid(62635, 7, HIGH_UNIT)
    gameobject = make_guid(1234, 7, HIGH_GAMEOBJECT)
    assert guid_high(unit) == HIGH_UNIT and guid_entry(unit) == 62635
    assert guid_type(unit) == "UNIT" and guid_type(gameobject) == "GAMEOBJECT"
    assert is_unit(unit) and not is_unit(gameobject)
    assert has_entry(unit) and has_entry(gameobject)
