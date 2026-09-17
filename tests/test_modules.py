"""Per-module decoding, from synthetic payloads built to the documented layout."""

from __future__ import annotations

import struct
import zlib

from support import (
    HIGH_GAMEOBJECT, HIGH_UNIT, decode_one, make_ctx, make_guid, make_packet, make_tables,
    pack_guid, sized_string, update_mask,
)
from tortoise_capture.modules.ai_reaction import AiReaction
from tortoise_capture.modules.compressed import CompressedMoves, CompressedUpdateObject
from tortoise_capture.modules.creature_query import CreatureQuery
from tortoise_capture.modules.messagechat import MessageChat
from tortoise_capture.modules.monster_move import MonsterMove, unpack_offset
from tortoise_capture.modules.party_kill import PartyKill
from tortoise_capture.modules.spell_go import SpellGo
from tortoise_capture.modules.update_object import UpdateObject

ENTRY = 62635
GUID = make_guid(ENTRY, 42)


def test_ai_reaction():
    body = struct.pack("<QI", GUID, 2)
    ev = decode_one(AiReaction(), make_packet(0x13C, body), make_ctx())
    assert ev.kind == "ai_reaction"
    assert ev.data["entry"] == ENTRY and ev.data["reaction"] == 2


def test_party_kill_reports_the_victim_as_entry():
    killer = make_guid(0, 8, 0x0000)
    body = struct.pack("<QQ", killer, GUID)
    ev = decode_one(PartyKill(), make_packet(0x1F5, body), make_ctx())
    assert ev.data["entry"] == ENTRY and ev.data["killer_guid"] == killer


def test_spell_go_reads_caster_and_spell():
    body = pack_guid(GUID) + pack_guid(GUID) + struct.pack("<IH", 1449, 0)
    ev = decode_one(SpellGo(), make_packet(0x132, body), make_ctx())
    assert ev.data["spell_id"] == 1449 and ev.data["entry"] == ENTRY


def test_monster_say_carries_sender_and_message():
    body = (struct.pack("<BI", 0x0B, 0) + struct.pack("<Q", GUID) + sized_string("Ralthas")
            + struct.pack("<Q", 0) + sized_string("For Justice!"))
    ev = decode_one(MessageChat(), make_packet(0x96, body), make_ctx())
    assert ev.kind == "monster_say"
    assert ev.data["sender"] == "Ralthas" and ev.data["message"] == "For Justice!"
    assert ev.data["entry"] == ENTRY


def test_monster_emote_has_no_entry_so_entry_filters_skip_it():
    body = (struct.pack("<BI", 0x0D, 0) + sized_string("Ralthas") + struct.pack("<Q", 0)
            + sized_string("looks around."))
    ev = decode_one(MessageChat(), make_packet(0x96, body), make_ctx())
    assert ev.kind == "monster_emote" and "entry" not in ev.data


def test_creature_query_response():
    body = (struct.pack("<I", ENTRY) + b"Ralthas\x00" + b"\x00\x00\x00" + b"Paladin\x00"
            + struct.pack("<7I", 0, 7, 0, 0, 0, 0, 13091) + bytes([0, 1]))
    ev = decode_one(CreatureQuery(), make_packet(0x61, body), make_ctx())
    assert ev.data["name"] == "Ralthas" and ev.data["subname"] == "Paladin"
    assert ev.data["display_id"] == 13091 and ev.data["racial_leader"] == 1


def test_creature_query_unknown_entry_has_no_body():
    body = struct.pack("<I", ENTRY | 0x80000000)
    assert list(CreatureQuery().decode(make_packet(0x61, body), make_ctx())) == []


# --------------------------------------------------------------------------
# movement
# --------------------------------------------------------------------------

def _move_head(guid=GUID, start=(1.0, 2.0, 3.0), spline_id=7):
    return pack_guid(guid) + struct.pack("<3fI", *start, spline_id)


def test_monster_move_stop_ends_the_packet():
    body = _move_head() + bytes([1])
    ev = decode_one(MonsterMove(), make_packet(0xDD, body, "SMSG_MONSTER_MOVE"), make_ctx())
    assert ev.kind == "move_stop" and ev.data["points"] == [(1.0, 2.0, 3.0)]


def test_monster_move_linear_yields_the_destination():
    dest = (10.0, 20.0, 30.0)
    body = _move_head() + bytes([0]) + struct.pack("<III", 0, 3200, 1) + struct.pack("<3f", *dest)
    ev = decode_one(MonsterMove(), make_packet(0xDD, body, "SMSG_MONSTER_MOVE"), make_ctx())
    assert ev.kind == "move_linear" and ev.data["duration_ms"] == 3200
    assert ev.data["points"] == [dest]


def test_packed_offsets_are_signed_and_quarter_scaled():
    assert unpack_offset(0) == (0.0, 0.0, 0.0)
    x, y, z = unpack_offset(4 | (0x7FF << 11))      # y = -1 in 11-bit two's complement
    assert x == 1.0 and y == -0.25 and z == 0.0


# --------------------------------------------------------------------------
# containers -- the two framings differ, which is the trap worth a test
# --------------------------------------------------------------------------

def _deflate(raw: bytes) -> bytes:
    return struct.pack("<I", len(raw)) + zlib.compress(raw)


def test_compressed_moves_unpacks_a_batch_of_micro_packets():
    inner = b""
    for opcode, payload in ((0xDD, b"\x01\x02"), (0x2AE, b"\x03")):
        inner += bytes([2 + len(payload)]) + struct.pack("<H", opcode) + payload
    pkt = make_packet(0x2FB, _deflate(inner), "SMSG_COMPRESSED_MOVES")
    children = list(CompressedMoves().expand(pkt, make_ctx()))
    assert [(c.opcode, c.body) for c in children] == [(0xDD, b"\x01\x02"), (0x2AE, b"\x03")]
    assert all(c.via == "compressed_moves" and c.t == pkt.t for c in children)


def test_compressed_update_object_is_one_message_not_a_batch():
    raw = b"\x01\x00\x00\x00\x00rest-of-the-update-body"
    tables = make_tables(opcode_names={0x0A9: "SMSG_UPDATE_OBJECT"})
    pkt = make_packet(0x1F6, _deflate(raw), "SMSG_COMPRESSED_UPDATE_OBJECT")
    children = list(CompressedUpdateObject().expand(pkt, make_ctx(tables)))
    assert len(children) == 1
    assert children[0].opcode == 0x0A9 and children[0].body == raw


# --------------------------------------------------------------------------
# update object
# --------------------------------------------------------------------------

FIELD_NAMES = {6: "UNIT_FIELD_HEALTH", 8: "UNIT_FIELD_MINDAMAGE"}
MINDAMAGE_BITS = struct.unpack("<I", struct.pack("<f", 20.2119))[0]


def _create_block(guid: int) -> bytes:
    return (struct.pack("<I", 1) + bytes([0])          # blockCount, hasTransport
            + bytes([2])                                # CREATE_OBJECT
            + pack_guid(guid) + bytes([3])              # guid, objectTypeId = Unit
            + bytes([0])                                # updateFlags: no movement payload
            + update_mask({6: 342, 8: MINDAMAGE_BITS}))


def test_create_block_names_unit_fields():
    ctx = make_ctx(make_tables(field_names=FIELD_NAMES))
    ev = decode_one(UpdateObject(), make_packet(0xA9, _create_block(GUID)), ctx)
    assert ev.kind == "object_create" and ev.data["block"] == "CREATE"
    by_name = {f["name"]: f["raw"] for f in ev.data["fields"]}
    assert by_name["UNIT_FIELD_HEALTH"] == 342
    text = UpdateObject().text_fields(ev)["fields_text"]
    assert "UNIT_FIELD_HEALTH = 342" in text and "UNIT_FIELD_MINDAMAGE = 20.2119" in text


def test_gameobject_fields_are_never_given_unit_names():
    """Regression: unit field names applied to a gameobject are silent nonsense."""
    ctx = make_ctx(make_tables(field_names=FIELD_NAMES))
    guid = make_guid(1234, 5, HIGH_GAMEOBJECT)
    ev = decode_one(UpdateObject(), make_packet(0xA9, _create_block(guid)), ctx)
    assert ev.data["named_ok"] is False
    assert all(f["name"] is None for f in ev.data["fields"])
    assert list(UpdateObject().sql_rows(ev, _sql_ctx())) == []


def test_values_block_and_out_of_range_block():
    ctx = make_ctx(make_tables(field_names=FIELD_NAMES))
    body = (struct.pack("<I", 2) + bytes([0])
            + bytes([4]) + struct.pack("<I", 1) + pack_guid(GUID)      # OUT_OF_RANGE
            + bytes([0]) + pack_guid(GUID) + update_mask({6: 300}))    # VALUES
    events = list(UpdateObject().decode(make_packet(0xA9, body), ctx))
    assert [e.kind for e in events] == ["objects_out_of_range", "object_values"]
    assert events[1].data["fields"][0]["raw"] == 300


def _sql_ctx():
    from tortoise_capture.core.contracts import SqlContext
    return SqlContext(capture_id="test")


def test_unit_fields_become_sql_rows():
    ctx = make_ctx(make_tables(field_names=FIELD_NAMES))
    ev = decode_one(UpdateObject(), make_packet(0xA9, _create_block(GUID)), ctx)
    rows = list(UpdateObject().sql_rows(ev, _sql_ctx()))
    assert {r.values["field_name"] for r in rows} == set(FIELD_NAMES.values())
    mindamage = next(r for r in rows if r.values["field_name"] == "UNIT_FIELD_MINDAMAGE")
    assert abs(mindamage.values["value"] - 20.2119) < 1e-4       # float bits, not the raw int


# --------------------------------------------------------------------------
# world_transfer: SMSG_LOGIN_VERIFY_WORLD / SMSG_NEW_WORLD -- the map id
# --------------------------------------------------------------------------

from tortoise_capture.modules.world_transfer import WorldTransfer  # noqa: E402


def _transfer_body(map_id=0, x=-9100.0, y=-1000.0, z=70.0, o=1.5):
    return struct.pack("<I4f", map_id, x, y, z, o)


def test_login_verify_world_reads_the_map_id():
    pkt = make_packet(0x236, _transfer_body(map_id=0), "SMSG_LOGIN_VERIFY_WORLD")
    ev = decode_one(WorldTransfer(), pkt, make_ctx())
    assert ev.kind == "world_transfer"
    assert ev.data["map_id"] == 0
    assert ev.data["source"] == "login"


def test_new_world_reads_the_map_id_too_and_tags_it_a_teleport():
    """Same 20-byte body (uint32 mapId + 4 floats) as LOGIN_VERIFY_WORLD --
    Player.cpp:2826 writes the same shape whether or not m_transport is set."""
    pkt = make_packet(0x3E, _transfer_body(map_id=1), "SMSG_NEW_WORLD")
    ev = decode_one(WorldTransfer(), pkt, make_ctx())
    assert ev.data["map_id"] == 1
    assert ev.data["source"] == "teleport"


def test_a_later_transfer_is_what_authoring_should_use():
    """Both opcodes decode independently; picking "the last one seen" for
    authoring is the author rule's job, not this module's -- it just reports
    what each packet said."""
    ev1 = decode_one(WorldTransfer(), make_packet(0x236, _transfer_body(0),
                                                  "SMSG_LOGIN_VERIFY_WORLD"), make_ctx())
    ev2 = decode_one(WorldTransfer(), make_packet(0x3E, _transfer_body(1),
                                                  "SMSG_NEW_WORLD"), make_ctx())
    assert ev1.data["map_id"] == 0 and ev2.data["map_id"] == 1


# --------------------------------------------------------------------------
# play_sound: SMSG_PLAY_SOUND -- sound id only, no sender identification
# --------------------------------------------------------------------------

from tortoise_capture.modules.play_sound import PlaySound  # noqa: E402


def test_play_sound_reads_the_sound_id():
    body = struct.pack("<I", 5150)
    ev = decode_one(PlaySound(), make_packet(0x2D2, body, "SMSG_PLAY_SOUND"), make_ctx())
    assert ev.kind == "play_sound" and ev.data["sound_id"] == 5150


def test_play_sound_carries_no_sender_so_no_entry_key():
    """Object.cpp's PlayDirectSound writes only uint32 sound_id -- no guid.
    No entry key means the event correctly drops out of a --entry run,
    same as MONSTER_EMOTE."""
    ev = decode_one(PlaySound(), make_packet(0x2D2, struct.pack("<I", 1)), make_ctx())
    assert "entry" not in ev.data and "guid" not in ev.data


# --------------------------------------------------------------------------
# attacker_state: SMSG_ATTACKERSTATEUPDATE -- one melee swing's outcome
# --------------------------------------------------------------------------

from tortoise_capture.modules.attacker_state import (  # noqa: E402
    HITINFO_CRITICALHIT, HITINFO_MISS, VICTIMSTATE_NORMAL, AttackerState,
)

ATTACKER = make_guid(62635, 42)
TARGET = make_guid(0, 7, 0x0000)   # a player: high word 0x0000


def _swing_body(hit_info=0, total_damage=18, sub_damage=(0, 18, 0, 0),
               target_state=VICTIMSTATE_NORMAL, spell_id=0, blocked=0):
    school, dmg, absorb, resist = sub_damage
    return (struct.pack("<I", hit_info) + pack_guid(ATTACKER) + pack_guid(TARGET)
            + struct.pack("<I", total_damage) + bytes([1])
            + struct.pack("<IfIiI", school, (dmg / total_damage) if total_damage else 0.0,
                          dmg, absorb, resist)
            + struct.pack("<IIII", target_state, 0, spell_id, blocked))


def test_a_normal_hit_reads_attacker_target_and_damage():
    ev = decode_one(AttackerState(), make_packet(0x14A, _swing_body(total_damage=18)), make_ctx())
    assert ev.kind == "attacker_state"
    assert ev.data["guid"] == ATTACKER and ev.data["entry"] == 62635
    assert ev.data["target_guid"] == TARGET
    assert ev.data["total_damage"] == 18
    assert ev.data["target_state"] == VICTIMSTATE_NORMAL
    assert ev.data["sub_damage"] == [{"school": 0, "damage": 18, "absorb": 0, "resist": 0}]


def test_hit_info_and_target_state_flags_are_exposed_not_just_the_raw_int():
    ev = decode_one(AttackerState(), make_packet(0x14A, _swing_body(
        hit_info=HITINFO_CRITICALHIT, total_damage=36)), make_ctx())
    assert ev.data["is_miss"] is False
    assert ev.data["is_critical"] is True
    assert ev.data["is_normal_hit"] is True


def test_a_miss_is_flagged_and_carries_no_usable_damage():
    ev = decode_one(AttackerState(), make_packet(0x14A, _swing_body(
        hit_info=HITINFO_MISS, total_damage=0, sub_damage=(0, 0, 0, 0),
        target_state=0)), make_ctx())
    assert ev.data["is_miss"] is True
    assert ev.data["is_normal_hit"] is False


def test_multiple_sub_damage_entries_are_all_decoded():
    """A weapon with more than one damage school (rare, but the count is on
    the wire) must not silently lose entries past the first."""
    total = 20
    body = (struct.pack("<I", 0) + pack_guid(ATTACKER) + pack_guid(TARGET)
            + struct.pack("<I", total) + bytes([2])
            + struct.pack("<IfIiI", 0, 15/total, 15, 0, 0)
            + struct.pack("<IfIiI", 3, 5/total, 5, 0, 0)
            + struct.pack("<IIII", VICTIMSTATE_NORMAL, 0, 0, 0))
    ev = decode_one(AttackerState(), make_packet(0x14A, body), make_ctx())
    assert len(ev.data["sub_damage"]) == 2
    assert ev.data["sub_damage"][1] == {"school": 3, "damage": 5, "absorb": 0, "resist": 0}


def test_target_with_no_entry_bearing_guid_has_no_target_entry_key():
    """The target here is a player (high word 0x0000) -- guid_entry() would
    return a meaningless number for one, so it must not be exposed as if it
    named a creature_template row."""
    ev = decode_one(AttackerState(), make_packet(0x14A, _swing_body()), make_ctx())
    assert "target_entry" not in ev.data
