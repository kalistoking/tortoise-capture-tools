"""The server's client data files, read-only, for what authoring needs of them.

One table so far: `CreatureDisplayInfo.dbc`, for a model's own scale. A
`creature_template.scale` of 0 is not a scale -- it tells the server to take
the model's, from this file (ObjectMgr.cpp:1436-1443) -- so a capture's
broadcast scale can only be checked against a stored 0 through it. The world
database carries a `creaturedisplayinfo` table, but on this server it is
empty; the server itself reads the file.

The layout is checked, not assumed: DBCfmt.h:33 reads twelve 4-byte fields
with the scale a float in field 4 (DBCStructure.h:168-180), and a file of any
other width is refused rather than read at an offset that would return a
plausible, wrong number.
"""

from __future__ import annotations

import struct
from pathlib import Path

from . import log as _log

_logger = _log.get_logger("dbc")

DISPLAY_INFO = "CreatureDisplayInfo.dbc"
_DISPLAY_FIELDS = 12
_SCALE_FIELD = 4
_HEADER = struct.Struct("<4s4I")         # magic, records, fields, record size, string block


class DisplayScales:
    """display id -> the model's own scale, as the server loads it."""

    def __init__(self, scales: dict[int, float], source: Path) -> None:
        self._scales = scales
        self.source = source

    def scale(self, display_id: int) -> float | None:
        return self._scales.get(display_id)


def display_scales(dbc_dir: Path | None) -> DisplayScales | None:
    """Reads `CreatureDisplayInfo.dbc` from the server's dbc directory, if one
    is configured and the file is laid out as the server reads it."""
    if dbc_dir is None:
        return None
    path = Path(dbc_dir) / DISPLAY_INFO
    try:
        data = path.read_bytes()
        magic, records, fields, size, _ = _HEADER.unpack_from(data)
    except (OSError, struct.error) as exc:
        _logger.warning("%s not readable: %s", path, exc)
        return None
    if magic != b"WDBC" or fields != _DISPLAY_FIELDS or size != fields * 4:
        _logger.warning("%s is not laid out as the server reads it (%s, %d fields of "
                        "%d bytes); model scales not used", path, magic, fields, size)
        return None
    if len(data) < _HEADER.size + records * size:
        _logger.warning("%s is truncated: %d record(s) declared, %d byte(s) short; model "
                        "scales not used", path, records, _HEADER.size + records * size - len(data))
        return None
    scales = {}
    for record in range(_HEADER.size, _HEADER.size + records * size, size):
        display, = struct.unpack_from("<I", data, record)
        scales[display], = struct.unpack_from("<f", data, record + _SCALE_FIELD * 4)
    return DisplayScales(scales, path)
