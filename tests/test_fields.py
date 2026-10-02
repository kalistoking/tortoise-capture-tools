"""The UpdateFields table and how each slot's 32 bits are read."""

from __future__ import annotations

import logging
import struct

from tortoise_capture.fields import tables, values

# The shape of the server's own UpdateFields.h, cut down: OBJECT_END is also
# the first unit field's index (UpdateFields.h:34, :72).
HEADER = """
enum EObjectFields
{
    OBJECT_FIELD_GUID                          = 0x00, // Size:2
    OBJECT_FIELD_TYPE                          = 0x02, // Size:1
    OBJECT_FIELD_ENTRY                         = 0x03, // Size:1
    OBJECT_FIELD_SCALE_X                       = 0x04, // Size:1
    OBJECT_FIELD_PADDING                       = 0x05, // Size:1
    OBJECT_END                                 = 0x06,
};

enum EUnitFields
{
    UNIT_FIELD_CHARM                           = 0x00 + OBJECT_END, // Size:2
    UNIT_FIELD_SUMMON                          = 0x02 + OBJECT_END, // Size:2
    UNIT_END                                   = 0x04 + OBJECT_END,
};
"""


def test_a_field_is_named_rather_than_the_end_marker_it_shares_an_index_with():
    """OBJECT_END and UNIT_FIELD_CHARM are both 6; the marker was declared
    first and kept the index, so a charmed unit's field came out named
    OBJECT_END and index_of("UNIT_FIELD_CHARM") found nothing."""
    env, by_index = tables._evaluate(HEADER, ("EObjectFields", "EUnitFields"))
    assert by_index[6] == "UNIT_FIELD_CHARM" and env["OBJECT_END"] == 6


def test_the_last_member_of_an_enum_needs_no_trailing_comma():
    """The member pattern required a comma, and C++ allows none after the last one:
    EUnitFields ends `PLAYER_END` bare, and it was dropped without a word."""
    header = HEADER.replace("0x04 + OBJECT_END,\n}", "0x04 + OBJECT_END\n}")
    assert header != HEADER
    env, by_index = tables._evaluate(header, ("EObjectFields", "EUnitFields"))
    assert env["UNIT_END"] == 10 and by_index[10] == "UNIT_END"


def test_a_symbol_the_header_does_not_define_is_reported_not_read_as_zero():
    """An unknown term summed as 0 would shift every index after it in silence."""
    header = HEADER.replace("0x00 + OBJECT_END", "0x00 + OBJECT_ENDS")
    seen = []

    class Catch(logging.Handler):
        def emit(self, record):
            seen.append(record)

    handler = Catch(logging.ERROR)
    tables._logger.addHandler(handler)
    try:
        tables._evaluate(header, ("EObjectFields", "EUnitFields"))
    finally:
        tables._logger.removeHandler(handler)
    assert any("OBJECT_ENDS" in record.getMessage() for record in seen)


def _raw(value: float) -> int:
    return struct.unpack("<I", struct.pack("<f", value))[0]


def test_the_cast_speed_and_power_cost_multipliers_are_floats():
    """The server sends UNIT_MOD_CAST_SPEED as a float (Object.cpp:782-787), 1.0
    for every creature (Creature.cpp:425) -- read as an int it was 1065353216.
    UNIT_FIELD_POWER_COST_MULTIPLIER is one too (SpellAuras.cpp:5491)."""
    assert values.decode("UNIT_MOD_CAST_SPEED", _raw(1.0)) == 1.0
    assert values.decode("UNIT_FIELD_POWER_COST_MULTIPLIER_03", _raw(0.5)) == 0.5


def test_resistances_and_power_cost_modifiers_are_signed():
    """SetResistance writes an int32 (Unit.h:416): a negative resistance came
    out as 4294967221; the power cost modifier too (SpellAuras.cpp:5502)."""
    assert values.decode("UNIT_FIELD_RESISTANCES_02", 0xFFFFFFB5) == -75
    assert values.decode("UNIT_FIELD_POWER_COST_MODIFIER", 0xFFFFFFFE) == -2
