"""The server's own client data: CreatureDisplayInfo.dbc's model scales."""

from __future__ import annotations

import struct
import tempfile
from pathlib import Path

from tortoise_capture import dbc


def _display_info(rows, fields=12):
    """A WDBC file laid out as DBCfmt.h:33 reads it: id, model, sound, extra, scale, ..."""
    size = fields * 4
    body = b"".join(struct.pack("<4If", display, 0, 0, 0, scale) + b"\0" * (size - 20)
                    for display, scale in rows)
    return struct.pack("<4s4I", b"WDBC", len(rows), fields, size, 1) + body + b"\0"


def _scales_from(content):
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / dbc.DISPLAY_INFO).write_bytes(content)
        return dbc.display_scales(Path(tmp))


def test_a_display_scale_is_field_four_of_its_record():
    scales = _scales_from(_display_info([(11415, 0.85), (11382, 2.0)]))
    assert abs(scales.scale(11415) - 0.85) < 1e-6
    assert scales.scale(11382) == 2.0
    assert scales.scale(1) is None


def test_a_file_laid_out_otherwise_is_refused_not_misread():
    """A different client build could move the field; reading offset 16 of a
    record of another width would return a plausible, wrong number."""
    assert _scales_from(_display_info([(11415, 0.85)], fields=13)) is None


def test_no_directory_configured_means_no_scales():
    assert dbc.display_scales(None) is None


def test_a_truncated_file_is_refused_not_read_past_its_end():
    content = _display_info([(11415, 0.85), (11382, 2.0)])
    assert _scales_from(content[:-30]) is None
