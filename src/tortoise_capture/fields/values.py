"""Wire typing of UpdateField slots.

Every field arrives as a uint32. Most are plain unsigned integers, but some
carry an IEEE-754 float in the same slot, some a signed int16 pair, and
UNIT_FIELD_BYTES_0 packs four bytes of identity. Which is which was verified
against Object::SetByteValue / StatSystem.cpp; anything not listed is an
unsigned int, which is correct for flags, ids and counts.

`decode` returns a value fit for storing (int or float); `render` returns the
human form for text output. Keeping them apart means SQL never has to parse a
display string back into a number.
"""

from __future__ import annotations

import struct

FLOAT_FIELDS = frozenset({
    "OBJECT_FIELD_SCALE_X",
    "UNIT_FIELD_BOUNDINGRADIUS", "UNIT_FIELD_COMBATREACH",
    "UNIT_FIELD_MINDAMAGE", "UNIT_FIELD_MAXDAMAGE",
    "UNIT_FIELD_MINOFFHANDDAMAGE", "UNIT_FIELD_MAXOFFHANDDAMAGE",
    "UNIT_FIELD_MINRANGEDDAMAGE", "UNIT_FIELD_MAXRANGEDDAMAGE",
    "UNIT_FIELD_ATTACK_POWER_MULTIPLIER", "UNIT_FIELD_RANGED_ATTACK_POWER_MULTIPLIER",
})

SIGNED_FIELDS = frozenset({
    "UNIT_FIELD_ATTACK_POWER", "UNIT_FIELD_RANGED_ATTACK_POWER",
})

# A signed int16 pair (positive mod, negative mod) packed into one slot.
TWO_SHORT_FIELDS = frozenset({
    "UNIT_FIELD_ATTACK_POWER_MODS", "UNIT_FIELD_RANGED_ATTACK_POWER_MODS",
})

BYTES_0_FIELD = "UNIT_FIELD_BYTES_0"

# Race 0 is left unnamed on purpose: creatures hardcode it, and printing
# "race=None" for that reads like missing data rather than the value 0.
RACE_NAMES = {1: "Human", 2: "Orc", 3: "Dwarf", 4: "NightElf",
              5: "Undead", 6: "Tauren", 7: "Gnome", 8: "Troll"}
CLASS_NAMES = {1: "Warrior", 2: "Paladin", 3: "Hunter", 4: "Rogue", 5: "Priest",
               7: "Shaman", 8: "Mage", 9: "Warlock", 11: "Druid"}
GENDER_NAMES = {0: "Male", 1: "Female", 2: "None"}
POWER_NAMES = {0: "Mana", 1: "Rage", 2: "Focus", 3: "Energy", 4: "Happiness"}


def as_float(raw: int) -> float:
    return struct.unpack("<f", struct.pack("<I", raw))[0]


def as_int32(raw: int) -> int:
    return struct.unpack("<i", struct.pack("<I", raw))[0]


def two_shorts(raw: int) -> tuple[int, int]:
    pos = struct.unpack("<h", struct.pack("<H", raw & 0xFFFF))[0]
    neg = struct.unpack("<h", struct.pack("<H", (raw >> 16) & 0xFFFF))[0]
    return pos, neg


def bytes_0(raw: int) -> dict[str, int]:
    """race | class<<8 | gender<<16 | powertype<<24.

    Race is hardcoded 0 for creatures; class comes from
    creature_template.unit_class.
    """
    return {"race": raw & 0xFF, "class": (raw >> 8) & 0xFF,
            "gender": (raw >> 16) & 0xFF, "power": (raw >> 24) & 0xFF}


def decode(name: str | None, raw: int) -> int | float:
    """Storable value for a raw slot."""
    if name in FLOAT_FIELDS:
        return as_float(raw)
    if name in SIGNED_FIELDS:
        return as_int32(raw)
    return raw


def render(name: str | None, raw: int) -> str:
    """Human form for text output."""
    if name in FLOAT_FIELDS:
        return f"{as_float(raw):.4f}"
    if name in TWO_SHORT_FIELDS:
        pos, neg = two_shorts(raw)
        return f"pos={pos} neg={neg}"
    if name == BYTES_0_FIELD:
        parts = bytes_0(raw)
        return (f"race={RACE_NAMES.get(parts['race'], parts['race'])} "
                f"class={CLASS_NAMES.get(parts['class'], parts['class'])} "
                f"gender={GENDER_NAMES.get(parts['gender'], parts['gender'])} "
                f"power={POWER_NAMES.get(parts['power'], parts['power'])}")
    if name in SIGNED_FIELDS:
        return str(as_int32(raw))
    return str(raw)
