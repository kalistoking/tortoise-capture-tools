"""ByteReader: a typed cursor over a packet body, plus GUID helpers.

Every module decodes through this instead of struct.unpack_from, so an
overrun reports which field of which packet ran out of bytes rather than
raising a bare struct.error, and packGUID is implemented once.

Chosen over a declarative struct DSL because vanilla payloads are heavily
conditional (flag-gated optional blocks, embedded splines, update masks) --
see ARCHITECTURE.md section 3, option F.
"""

from __future__ import annotations

import struct
from typing import Iterator

# GUID high words (src/game/ObjectGuid.h). The high word is bits 48-63.
HIGHGUID_ITEM = 0x4000
HIGHGUID_PLAYER = 0x0000
HIGHGUID_GAMEOBJECT = 0xF110
HIGHGUID_TRANSPORT = 0xF120
HIGHGUID_UNIT = 0xF130
HIGHGUID_PET = 0xF140
HIGHGUID_DYNAMICOBJECT = 0xF100
HIGHGUID_CORPSE = 0xF101
HIGHGUID_MO_TRANSPORT = 0x1FC0

HIGHGUID_NAMES = {
    HIGHGUID_ITEM: "ITEM",
    HIGHGUID_PLAYER: "PLAYER",
    HIGHGUID_GAMEOBJECT: "GAMEOBJECT",
    HIGHGUID_TRANSPORT: "TRANSPORT",
    HIGHGUID_UNIT: "UNIT",
    HIGHGUID_PET: "PET",
    HIGHGUID_DYNAMICOBJECT: "DYNAMICOBJECT",
    HIGHGUID_CORPSE: "CORPSE",
    HIGHGUID_MO_TRANSPORT: "MO_TRANSPORT",
}

# Types whose GUID carries a creature_template/gameobject_template entry.
ENTRY_BEARING = (HIGHGUID_UNIT, HIGHGUID_PET, HIGHGUID_GAMEOBJECT, HIGHGUID_TRANSPORT)


class WireError(Exception):
    """A payload did not match the layout it was read against."""


def guid_high(guid: int) -> int:
    return (guid >> 48) & 0xFFFF


def guid_entry(guid: int) -> int:
    """ObjectGuid::GetEntry -- only meaningful for entry-bearing high types."""
    return (guid >> 24) & 0xFFFFFF


def guid_type(guid: int) -> str:
    return HIGHGUID_NAMES.get(guid_high(guid), f"0x{guid_high(guid):04X}")


def is_unit(guid: int) -> bool:
    """Units and pets -- the only types EUnitFields names are valid for."""
    return guid_high(guid) in (HIGHGUID_UNIT, HIGHGUID_PET)


def has_entry(guid: int) -> bool:
    return guid_high(guid) in ENTRY_BEARING


class ByteReader:
    """Little-endian cursor; `what` names the field being read for errors."""

    __slots__ = ("buf", "pos", "label")

    def __init__(self, buf: bytes, label: str = "") -> None:
        self.buf = buf
        self.pos = 0
        self.label = label

    # -- primitives --------------------------------------------------------

    def _unpack(self, fmt: str, size: int, what: str):
        if self.pos + size > len(self.buf):
            raise WireError(
                f"{self.label or 'payload'}: reading {what} at offset {self.pos} "
                f"needs {size} bytes, only {len(self.buf) - self.pos} left"
            )
        value = struct.unpack_from(fmt, self.buf, self.pos)
        self.pos += size
        return value

    def u8(self, what: str = "u8") -> int:
        return self._unpack("<B", 1, what)[0]

    def i8(self, what: str = "i8") -> int:
        return self._unpack("<b", 1, what)[0]

    def u16(self, what: str = "u16") -> int:
        return self._unpack("<H", 2, what)[0]

    def u16be(self, what: str = "u16be") -> int:
        return self._unpack(">H", 2, what)[0]

    def u32(self, what: str = "u32") -> int:
        return self._unpack("<I", 4, what)[0]

    def i32(self, what: str = "i32") -> int:
        return self._unpack("<i", 4, what)[0]

    def u64(self, what: str = "u64") -> int:
        return self._unpack("<Q", 8, what)[0]

    def f32(self, what: str = "f32") -> float:
        return self._unpack("<f", 4, what)[0]

    def vec3(self, what: str = "vec3") -> tuple[float, float, float]:
        return self._unpack("<3f", 12, what)

    def vec4(self, what: str = "vec4") -> tuple[float, float, float, float]:
        return self._unpack("<4f", 16, what)

    def floats(self, count: int, what: str = "floats") -> tuple[float, ...]:
        return self._unpack(f"<{count}f", count * 4, what)

    def raw(self, n: int, what: str = "bytes") -> bytes:
        if self.pos + n > len(self.buf):
            raise WireError(
                f"{self.label or 'payload'}: reading {what} at offset {self.pos} "
                f"needs {n} bytes, only {len(self.buf) - self.pos} left"
            )
        out = self.buf[self.pos:self.pos + n]
        self.pos += n
        return out

    # -- wow-specific ------------------------------------------------------

    def cstring(self, what: str = "cstring") -> str:
        """NUL-terminated, decoded permissively: capture text is not trusted."""
        end = self.buf.find(b"\x00", self.pos)
        if end < 0:
            raise WireError(f"{self.label or 'payload'}: unterminated {what} at offset {self.pos}")
        out = self.buf[self.pos:end].decode("utf-8", "replace")
        self.pos = end + 1
        return out

    def sized_string(self, what: str = "string") -> str:
        """uint32 length (NUL included) + bytes -- the SMSG_MESSAGECHAT form."""
        length = self.u32(f"{what}.len")
        raw = self.raw(length, what)
        return raw.split(b"\x00", 1)[0].decode("utf-8", "replace")

    def packguid(self, what: str = "packGUID") -> int:
        """ObjectGuid packGUID: a mask byte, then only the non-zero bytes.

        Each present byte belongs at mask_bit_index * 8, NOT at read order --
        getting that wrong yields a plausible but wrong 64-bit value.
        """
        mask = self.u8(f"{what}.mask")
        guid = 0
        for bit in range(8):
            if mask & (1 << bit):
                guid |= self.u8(f"{what}.byte{bit}") << (bit * 8)
        return guid

    # -- cursor ------------------------------------------------------------

    @property
    def remaining(self) -> int:
        return len(self.buf) - self.pos

    @property
    def eof(self) -> bool:
        return self.pos >= len(self.buf)

    def skip(self, n: int, what: str = "padding") -> None:
        self.raw(n, what)

    def __iter__(self) -> Iterator[int]:  # pragma: no cover - convenience only
        while not self.eof:
            yield self.u8()
