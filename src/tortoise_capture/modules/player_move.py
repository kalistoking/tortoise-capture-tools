"""MSG_MOVE_* -- the recording player's own path.

The twenty-two opcodes `Opcodes.cpp` routes to `WorldSession::HandleMovementOpcodes`
(twenty-one `MSG_MOVE_*` plus `CMSG_MOVE_FALL_RESET`) all carry the same
payload, and the handler reads it as a bare `MovementInfo` (`MovementHandler.cpp:320`,
layout at `Object.cpp:64`):

    uint32 moveFlags
    uint32 ctime               -- the CLIENT's clock, not the server's
    float  x, y, z, o
    if ONTRANSPORT (0x02000000):  uint64 t_guid + 4 x float t_pos
    if SWIMMING    (0x00200000):  float s_pitch
    uint32 fallTime            -- unconditional, the classic trap
    if JUMPING     (0x00002000):  4 x float
    if SPLINE_ELEVATION:          float

**There is no guid in it, and that is the point.** The server takes the mover
from the session (`MovementHandler.cpp:304`), so a client-sent movement *is*
the recording player by construction — a stronger attribution than any
heuristic could offer, and the reason these survive `--entry` as session-scoped
facts. `modules/update_object.py` finds the same player from the other
direction, by `UPDATEFLAG_SELF`; this needs no marker at all.

`MSG_` means the number is used in BOTH directions with **different layouts**.
The server relays another player's movement to us by prefixing the mover's
packGUID (`MovementHandler.cpp:456`) and then the same `MovementInfo`. That is
a different fact about a different body, so it is not decoded here: reading it
as the observer's own path would put a stranger's position in the recording's
viewpoint. Measured on the reference capture, the split is 2424 C2S against 15
S2C, so what is skipped is a rounding error next to what is kept.

No `author/` rule consumes this. Where the observer stood is context for a
human reading a capture back — it is not a column in any world table, and a
creature's authored content must never be derived from where the person
recording it happened to be standing.
"""

from __future__ import annotations

from typing import Any, Iterator

from ..core.base import BaseModule
from ..core.contracts import (
    Column, DecodeContext, Direction, Event, Packet, Row, SqlContext, TableSpec,
)
from ..core.reader import ByteReader
from ..core.registry import module
from .update_object import read_movement_info

# Every opcode Opcodes.cpp hands to HandleMovementOpcodes. Declared by symbol,
# so a checkout that renumbers them keeps working and one that drops a symbol
# reports it rather than decoding the wrong payload.
MOVEMENT_OPCODES = (
    "MSG_MOVE_START_FORWARD", "MSG_MOVE_START_BACKWARD", "MSG_MOVE_STOP",
    "MSG_MOVE_START_STRAFE_LEFT", "MSG_MOVE_START_STRAFE_RIGHT", "MSG_MOVE_STOP_STRAFE",
    "MSG_MOVE_JUMP", "MSG_MOVE_START_TURN_LEFT", "MSG_MOVE_START_TURN_RIGHT",
    "MSG_MOVE_STOP_TURN", "MSG_MOVE_START_PITCH_UP", "MSG_MOVE_START_PITCH_DOWN",
    "MSG_MOVE_STOP_PITCH", "MSG_MOVE_SET_RUN_MODE", "MSG_MOVE_SET_WALK_MODE",
    "MSG_MOVE_FALL_LAND", "MSG_MOVE_START_SWIM", "MSG_MOVE_STOP_SWIM",
    "MSG_MOVE_SET_FACING", "MSG_MOVE_SET_PITCH", "MSG_MOVE_HEARTBEAT",
    "CMSG_MOVE_FALL_RESET",
)

_TABLE = TableSpec(
    name="capture_player_move",
    columns=(
        Column("capture", "VARCHAR(64)", nullable=False),
        Column("t", "DOUBLE"),
        Column("seq", "INT UNSIGNED", nullable=False),
        Column("opcode_name", "VARCHAR(48)"),
        Column("move_flags", "INT UNSIGNED"),
        Column("ctime", "INT UNSIGNED"),
        Column("position_x", "DOUBLE"),
        Column("position_y", "DOUBLE"),
        Column("position_z", "DOUBLE"),
        Column("orientation", "DOUBLE"),
        Column("fall_time", "INT UNSIGNED"),
    ),
    key=("capture", "seq"),
    comment="where the person recording stood; never a source for authored content",
)


@module(id="player_move", opcodes=MOVEMENT_OPCODES, order=12)
class PlayerMove(BaseModule):
    text_section = "MSG_MOVE_* (the recording player's path)"
    text_templates = {
        "session_player_move": "{opcode_name:<28} ({position_x:11.4f}, {position_y:11.4f}, "
                               "{position_z:9.4f})  facing {orientation:6.3f}",
    }
    sql_tables = (_TABLE,)

    def decode(self, pkt: Packet, ctx: DecodeContext) -> Iterator[Event]:
        if pkt.direction is not Direction.C2S:
            # A relayed move: packGUID + MovementInfo, and a different body.
            ctx.log.debug("%s S2C: another mover's movement, not the observer's", pkt.name or "?")
            return
        r = ByteReader(pkt.body, pkt.name or "MSG_MOVE")
        info = read_movement_info(r)
        yield self.event(pkt, "session_player_move", scope="session",
                         opcode_name=pkt.name or "", **info)

    def text_fields(self, ev: Event) -> dict[str, Any]:
        data = dict(ev.data)
        x, y, z, o = data["pos"]
        data.update(position_x=x, position_y=y, position_z=z, orientation=o)
        return data

    def sql_rows(self, ev: Event, ctx: SqlContext) -> Iterator[Row]:
        d = ev.data
        x, y, z, o = d["pos"]
        yield Row(_TABLE.name, {
            "capture": ctx.capture_id, "t": ev.packet.t, "seq": ev.packet.seq,
            "opcode_name": d.get("opcode_name"), "move_flags": d["move_flags"],
            "ctime": d["stime"], "position_x": x, "position_y": y, "position_z": z,
            "orientation": o, "fall_time": d["fall_time"],
        })
