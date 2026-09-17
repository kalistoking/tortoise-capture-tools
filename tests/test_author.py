"""Authoring rules: the rows proposed for a world database, and their honesty."""

from __future__ import annotations

import struct
import tempfile
from pathlib import Path

from support import StubWorld, author_rows, make_event, make_packet
from tortoise_capture.author.dialogue import EVENT_T_AGGRO, EVENT_T_DEATH, Dialogue
from tortoise_capture.author.equipment import Equipment, unpack_item_info
from tortoise_capture.author.spawn import Spawn
from tortoise_capture.author.spells import Spells
from tortoise_capture.author.stats import Stats
from tortoise_capture.core.contracts import CONVENTION, DERIVED, LOOKUP, WIRE, AuthoredRow
from tortoise_capture.emit.migration import MigrationWriter

ENTRY = 62635
GUID = 0xF130_00F4AB_2787EA          # low 24 bits are the spawn's database id
SPAWN_GUID = 2590698

AGGRO_TEXT = "The Brotherhood Stands for Justice!"
DEATH_TEXT = "The lies of Stormwind, must be told!"


def _fields(**named):
    return [{"index": i, "name": name, "raw": raw}
            for i, (name, raw) in enumerate(named.items())]


def _float_bits(value: float) -> int:
    return struct.unpack("<I", struct.pack("<f", value))[0]


# --------------------------------------------------------------------------
# dialogue: broadcast_text + creature_ai_scripts + creature_ai_events
# --------------------------------------------------------------------------

def _dialogue_events():
    return [
        make_event("monster_say", 59.9, guid=GUID, entry=ENTRY, message=AGGRO_TEXT,
                   chat_type=0x0B, language=0),
        make_event("monster_say", 90.3, guid=GUID, entry=ENTRY, message=DEATH_TEXT,
                   chat_type=0x0B, language=0),
        make_event("creature_query", 12.9, entry=ENTRY, name="Ralthas"),
        make_event("text_trigger", 999.0, entry=ENTRY, subject=AGGRO_TEXT, trigger="aggro"),
        make_event("text_trigger", 999.0, entry=ENTRY, subject=DEATH_TEXT, trigger="death"),
    ]


def test_dialogue_fills_all_three_tables_with_consistent_ids():
    rows, _ = author_rows(Dialogue(), _dialogue_events(), ENTRY)
    by_table = {}
    for row in rows:
        by_table.setdefault(row.table, []).append(row)

    assert set(by_table) == {"broadcast_text", "creature_ai_scripts", "creature_ai_events"}
    text = by_table["broadcast_text"][0]
    script = by_table["creature_ai_scripts"][0]
    event = by_table["creature_ai_events"][0]
    # The trio is only meaningful if the ids line up across it.
    assert text.values["entry"] == script.values["dataint"] == event.values["action1_script"]
    assert text.values["entry"] == ENTRY * 100 + 1          # the authoring convention


def test_trigger_becomes_the_event_type():
    rows, _ = author_rows(Dialogue(), _dialogue_events(), ENTRY)
    events = [r for r in rows if r.table == "creature_ai_events"]
    types = {r.values["comment"].split(" - ")[1].lower(): r.values["event_type"] for r in events}
    assert types == {"aggro text": EVENT_T_AGGRO, "death text": EVENT_T_DEATH}


def test_the_text_is_wire_but_the_ids_are_only_convention():
    rows, _ = author_rows(Dialogue(), _dialogue_events(), ENTRY)
    text = next(r for r in rows if r.table == "broadcast_text")
    assert text.provenance["male_text"] == WIRE
    assert text.provenance["entry"] == CONVENTION
    event = next(r for r in rows if r.table == "creature_ai_events")
    assert event.provenance["event_type"] == DERIVED


def test_unattributed_dialogue_becomes_a_gap_not_a_row():
    events = [
        make_event("monster_say", 5.0, guid=GUID, entry=ENTRY, message="Who knows why",
                   chat_type=0x0B, language=0),
        make_event("text_untriggered", 999.0, entry=ENTRY, subject="Who knows why"),
    ]
    rows, gaps = author_rows(Dialogue(), events, ENTRY)
    assert rows == []
    assert any("Who knows why" in gap for gap in gaps)


# --------------------------------------------------------------------------
# stats: creature_template
# --------------------------------------------------------------------------

def _stats_events():
    return [make_event("object_create", 12.9, guid=GUID, entry=ENTRY, fields=_fields(
        UNIT_FIELD_MINDAMAGE=_float_bits(20.2119007111),
        UNIT_FIELD_ATTACK_POWER=44,
        UNIT_FIELD_BYTES_0=512,                     # class 2 in byte 1
        OBJECT_FIELD_SCALE_X=_float_bits(1.0),
    ))]


def test_stats_reads_the_create_block():
    rows, _ = author_rows(Stats(), _stats_events(), ENTRY)
    row = rows[0]
    assert row.statement == "update" and row.where == {"entry": ENTRY}
    assert row.values["attack_power"] == 44
    assert row.values["unit_class"] == 2
    assert abs(row.values["dmg_min"] - 20.2119007) < 1e-6


def test_a_float_that_round_trips_lands_on_the_same_float32():
    """Nine significant digits, not fixed decimals: the column must not shift."""
    rows, _ = author_rows(Stats(), _stats_events(), ENTRY)
    emitted = rows[0].values["dmg_min"]
    assert struct.pack("<f", emitted) == struct.pack("<f", 20.2119007111)


def test_a_column_the_database_already_holds_is_confirmed_not_restated():
    """The wire carries the server's computed value, which drifts from the
    authored one. Re-proposing it every capture would walk the stored value."""
    world = StubWorld(columns={
        ("creature_template", "dmg_min"): "20.2118873596",   # authored, ~3e-6 away
        ("creature_template", "attack_power"): "44",
        ("creature_template", "unit_class"): "2",
        ("creature_template", "scale"): "1",
    })
    rows, _ = author_rows(Stats(), _stats_events(), ENTRY, world=world)
    assert rows == []          # nothing to change: every column confirmed


def test_a_genuinely_different_value_is_still_proposed():
    world = StubWorld(columns={("creature_template", "attack_power"): "10"})
    rows, _ = author_rows(Stats(), _stats_events(), ENTRY, world=world)
    assert rows[0].values["attack_power"] == 44


# --------------------------------------------------------------------------
# spawn: creature + creature_movement
# --------------------------------------------------------------------------

def _create(t, x, y, z=70.0, o=1.0):
    return make_event("object_create", t, guid=GUID, entry=ENTRY,
                      movement={"movement_info": {"pos": (x, y, z, o)}})


def _spawn_events():
    return [
        _create(12.9, -9177.9, -1026.8, o=0.59),          # mid-patrol first sighting
        make_event("party_kill", 90.3, guid=GUID, entry=ENTRY),
        _create(389.8, -9129.66, -1098.79, 73.66, -2.3216900825500488),   # the respawn
        make_event("respawn_timer", 999.0, entry=ENTRY, value_min=299.534, samples=1),
        make_event("patrol_route", 999.0, guid=GUID, entry=ENTRY, count=2, closes_loop=True),
        make_event("patrol_waypoint", 999.0, guid=GUID, entry=ENTRY, point=1,
                   position_x=-9129.66, position_y=-1098.79, position_z=73.66),
        make_event("patrol_waypoint", 999.0, guid=GUID, entry=ENTRY, point=2,
                   position_x=-9135.66, position_y=-1099.79, position_z=73.41),
    ]


def test_the_spawn_guid_comes_out_of_the_wire_guid():
    rows, _ = author_rows(Spawn(), _spawn_events(), ENTRY)
    creature = next(r for r in rows if r.table == "creature")
    assert creature.values["guid"] == SPAWN_GUID
    assert creature.provenance["guid"] == WIRE


def test_the_spawn_position_is_taken_from_the_respawn_not_first_sighting():
    """First sighting catches a patrolling creature wherever it happens to be."""
    rows, _ = author_rows(Spawn(), _spawn_events(), ENTRY)
    creature = next(r for r in rows if r.table == "creature")
    assert abs(creature.values["position_x"] - (-9129.66)) < 0.01
    assert abs(creature.values["orientation"] - (-2.32169008)) < 1e-6
    assert creature.values["spawntimesecsmin"] == 300      # 299.534 rounded
    assert creature.values["movement_type"] == 2


def test_without_a_death_the_position_is_emitted_but_flagged():
    events = [_create(12.9, -9177.9, -1026.8)]
    rows, gaps = author_rows(Spawn(), events, ENTRY)
    creature = next(r for r in rows if r.table == "creature")
    assert any("NOT a respawn" in note for note in creature.notes)
    assert any("respawn timer" in gap for gap in gaps)


def test_a_closed_route_repeats_its_first_point_to_close_the_loop():
    rows, _ = author_rows(Spawn(), _spawn_events(), ENTRY)
    movement = [r for r in rows if r.table == "creature_movement"]
    assert [r.values["point"] for r in movement] == [1, 2, 3]
    assert movement[-1].values["position_x"] == movement[0].values["position_x"]
    assert movement[-1].provenance["point"] == CONVENTION


def test_the_map_id_is_reported_as_missing_when_no_transfer_was_seen():
    _, gaps = author_rows(Spawn(), _spawn_events(), ENTRY)
    assert any("creature.map" in gap for gap in gaps)


def test_the_map_id_comes_from_a_world_transfer_event_when_one_was_seen():
    events = _spawn_events() + [make_event("world_transfer", 1.0, map_id=0, source="login")]
    rows, gaps = author_rows(Spawn(), events, ENTRY)
    creature = next(r for r in rows if r.table == "creature")
    assert creature.values["map"] == 0
    assert creature.provenance["map"] == WIRE
    assert not any("creature.map" in gap for gap in gaps)


def test_the_last_world_transfer_wins_over_an_earlier_one():
    """A capture with a teleport mid-session should trust the map the
    creature was actually observed on, not wherever the player started."""
    events = _spawn_events() + [
        make_event("world_transfer", 1.0, map_id=0, source="login"),
        make_event("world_transfer", 5.0, map_id=1, source="teleport"),
    ]
    rows, _ = author_rows(Spawn(), events, ENTRY)
    creature = next(r for r in rows if r.table == "creature")
    assert creature.values["map"] == 1


def test_map_is_never_schema_filled_even_when_a_default_is_offered():
    """0 there is Eastern Kingdoms -- a real place, not a neutral default."""
    world = StubWorld(schema={"creature": {"map": 0, "id2": 0, "spawn_flags": 0}})
    rows, gaps = author_rows(Spawn(), _spawn_events(), ENTRY, world=world)
    creature = next(r for r in rows if r.table == "creature")
    assert "map" not in creature.values
    assert any("creature.map" in gap for gap in gaps)


def test_boilerplate_columns_are_widened_from_the_schema():
    world = StubWorld(schema={"creature": {"id2": 0, "id3": 0, "id4": 0,
                                           "spawn_flags": 0}})
    rows, _ = author_rows(Spawn(), _spawn_events(), ENTRY, world=world)
    creature = next(r for r in rows if r.table == "creature")
    assert creature.values["id2"] == 0
    assert creature.provenance["id2"] == CONVENTION


def test_wander_distance_is_zeroed_for_a_waypoint_mover_not_left_at_the_schema_default():
    """wander_distance only affects random movement (Creature.cpp has no reader
    for it on a waypoint-follower); the schema's default (5, sized for random
    wandering) would be inert but misleading next to movement_type=2."""
    world = StubWorld(schema={"creature": {"wander_distance": 5}})
    rows, _ = author_rows(Spawn(), _spawn_events(), ENTRY, world=world)
    creature = next(r for r in rows if r.table == "creature")
    assert creature.values["wander_distance"] == 0
    assert creature.provenance["wander_distance"] == CONVENTION


def test_without_a_database_the_row_stays_narrow():
    """No schema to read from, so no widening -- but nothing else breaks."""
    rows, _ = author_rows(Spawn(), _spawn_events(), ENTRY, world=None)
    creature = next(r for r in rows if r.table == "creature")
    assert "id2" not in creature.values
    assert creature.values["wander_distance"] == 0    # set explicitly, not schema-filled


# --------------------------------------------------------------------------
# equipment: creature_equip_template
# --------------------------------------------------------------------------

# class 2, subclass 10, inventory_type 17 -- a two-handed staff.
ITEM_INFO = 0x11000A02


def _equip_events(display=5010, info=ITEM_INFO):
    return [make_event("object_create", 12.9, guid=GUID, entry=ENTRY, fields=_fields(
        UNIT_VIRTUAL_ITEM_DISPLAY=display, UNIT_VIRTUAL_ITEM_INFO=info))]


def test_item_info_unpacks_to_the_item_template_columns():
    assert unpack_item_info(ITEM_INFO) == {"class": 2, "subclass": 10,
                                           "material": 0, "inventory_type": 17}


def test_the_display_id_resolves_to_an_item_entry():
    world = StubWorld(displays={5010: 5276}, columns={
        ("item_template", "class"): "2", ("item_template", "subclass"): "10",
        ("item_template", "inventory_type"): "17"})
    rows, gaps = author_rows(Equipment(), _equip_events(), ENTRY, world=world)
    assert rows[0].values["equipentry1"] == 5276
    assert rows[0].provenance["equipentry1"] == LOOKUP
    assert gaps == []


def test_a_lookup_the_packed_info_contradicts_is_withheld():
    """Two independent paths to the item; disagreement means the lookup is wrong."""
    world = StubWorld(displays={5010: 5276}, columns={
        ("item_template", "class"): "4",        # armour, not a weapon
        ("item_template", "subclass"): "10", ("item_template", "inventory_type"): "17"})
    rows, gaps = author_rows(Equipment(), _equip_events(), ENTRY, world=world)
    assert rows == []
    assert any("disagrees" in gap for gap in gaps)


def test_without_a_database_equipment_is_a_gap_not_a_guess():
    rows, gaps = author_rows(Equipment(), _equip_events(), ENTRY, world=None)
    assert rows == []
    assert any("no database configured" in gap for gap in gaps)


# --------------------------------------------------------------------------
# spells: creature_spells
# --------------------------------------------------------------------------

def _spell_events(confident=False, samples=1):
    return [
        make_event("creature_query", 12.9, entry=ENTRY, name="Ralthas"),
        make_event("spell_go", 60.0, guid=GUID, entry=ENTRY, spell_id=1449),
        make_event("spell_go", 78.3, guid=GUID, entry=ENTRY, spell_id=1449),
        make_event("spell_initial_delay", 999.0, entry=ENTRY, subject=1449,
                   value_min=0.047, value_max=0.047, samples=2),
        make_event("spell_repeat_delay", 999.0, entry=ENTRY, subject=1449,
                   value_min=18.291, value_max=18.291, samples=samples,
                   confident=confident),
    ]


def test_spells_emits_what_it_saw_cast():
    rows, _ = author_rows(Spells(), _spell_events(), ENTRY)
    row = rows[0]
    assert row.values["spellId_1"] == 1449 and row.values["name"] == "Ralthas"
    assert row.values["delayInitialMin_1"] == 0           # 0.047 s rounds to 0
    assert row.provenance["spellId_1"] == WIRE
    assert row.provenance["probability_1"] == CONVENTION


def test_an_unbounded_repeat_delay_is_a_gap_never_a_number():
    """One interval cannot recover an authored min/max, so it must not appear."""
    rows, gaps = author_rows(Spells(), _spell_events(confident=False), ENTRY)
    assert "delayRepeatMin_1" not in rows[0].values
    assert any("delayRepeatMin/Max" in gap and "bounds nothing" in gap for gap in gaps)


def test_cast_target_is_schema_filled_with_a_caveat_not_withheld():
    """Unlike delayRepeat, castTarget's default reads as the table's own
    convention (the reference PR uses it uniformly, used slots and empty
    ones alike) -- so it is filled, with a note, rather than left a gap."""
    world = StubWorld(schema={"creature_spells": {"castTarget_1": 1}})
    rows, gaps = author_rows(Spells(), _spell_events(), ENTRY, world=world)
    assert rows[0].values["castTarget_1"] == 1
    assert rows[0].provenance["castTarget_1"] == CONVENTION
    assert not any("castTarget" in gap for gap in gaps)
    assert any("castTarget" in note for note in rows[0].notes)


def test_delay_repeat_is_never_schema_filled_even_with_a_default_available():
    """0 there means "does not repeat", which is known false -- it must stay
    a real gap, not become a silent (wrong) schema default."""
    world = StubWorld(schema={"creature_spells": {"delayRepeatMin_1": 0,
                                                  "delayRepeatMax_1": 0}})
    rows, gaps = author_rows(Spells(), _spell_events(confident=False), ENTRY, world=world)
    assert "delayRepeatMin_1" not in rows[0].values
    assert "delayRepeatMax_1" not in rows[0].values
    assert any("delayRepeatMin/Max" in gap for gap in gaps)


def test_a_confident_repeat_delay_is_proposed_as_derived():
    """The refusal is about small samples, not the column in general."""
    events = _spell_events(confident=True, samples=6)
    rows, _ = author_rows(Spells(), events, ENTRY)
    assert rows[0].values["delayRepeatMin_1"] == 18
    assert rows[0].provenance["delayRepeatMin_1"] == DERIVED


def test_unused_slots_are_widened_to_match_the_table_shape():
    """Slot 2 was never cast, but the reference migration still fills it."""
    world = StubWorld(schema={"creature_spells": {"spellId_2": 0, "probability_2": 100,
                                                  "castTarget_2": 1}})
    rows, _ = author_rows(Spells(), _spell_events(), ENTRY, world=world)
    assert rows[0].values["spellId_2"] == 0
    assert rows[0].provenance["spellId_2"] == CONVENTION


# --------------------------------------------------------------------------
# the migration file itself
# --------------------------------------------------------------------------

def test_the_migration_states_provenance_and_lists_gaps():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "out.sql"
        writer = MigrationWriter(path, capture_id="test", entry=ENTRY)
        writer.add([AuthoredRow(table="creature_equip_template",
                                values={"entry": ENTRY, "equipentry1": 5276},
                                provenance={"entry": WIRE, "equipentry1": LOOKUP},
                                notes=("cross-checked",))])
        writer.add_gaps(["creature.map -- not in any decoded opcode"])
        writer.write()
        sql = path.read_text(encoding="utf-8")

    assert "read off the wire): entry" in sql
    assert "resolved against the world database): equipentry1" in sql
    assert "-- NOTE: cross-checked" in sql
    assert "NOT DERIVED" in sql and "creature.map" in sql
    assert "INSERT INTO `creature_equip_template`" in sql


def test_rows_group_by_their_own_table_in_first_appearance_order():
    """One rule fills several tables; the sections must still apply in order."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "out.sql"
        writer = MigrationWriter(path, capture_id="test", entry=ENTRY)
        writer.add([
            AuthoredRow(table="broadcast_text", values={"entry": 1}),
            AuthoredRow(table="creature_ai_events", values={"id": 1}),
            AuthoredRow(table="broadcast_text", values={"entry": 2}),
        ])
        writer.write()
        sql = path.read_text(encoding="utf-8")

    assert sql.index("broadcast_text  (2 row(s))") < sql.index("creature_ai_events")


# --------------------------------------------------------------------------
# World.describe() -- parsing and caching, without a real database
# --------------------------------------------------------------------------

def _fake_world(responses: dict[str, list[list[str]]]):
    """A World whose query() answers from a table->rows map instead of a shell."""
    from tortoise_capture.world import World

    calls: list[str] = []

    class FakeWorld(World):
        def query(self, sql: str):
            calls.append(sql)
            for table, rows in responses.items():
                if f"`{table}`" in sql:
                    return rows
            return []

    return FakeWorld(client="unused"), calls


def test_describe_coerces_types_and_treats_null_as_no_default():
    world, _ = _fake_world({"creature_template": [
        ["entry", "int", "NO", "PRI", "0", ""],
        ["scale", "float", "NO", "", "1.5", ""],
        ["name", "char(100)", "NO", "", "NULL", ""],
        ["guid", "bigint", "NO", "PRI", "NULL", ""],
    ]})
    schema = world.describe("creature_template")
    assert schema["entry"] == 0 and schema["scale"] == 1.5
    assert "name" not in schema and "guid" not in schema     # NULL -> no usable default


def test_describe_is_cached_per_table():
    world, calls = _fake_world({
        "creature": [["x", "int", "NO", "", "0", ""]],
        "creature_movement": [["y", "int", "NO", "", "0", ""]],
    })
    world.describe("creature")
    world.describe("creature")
    world.describe("creature_movement")
    assert len(calls) == 2          # one per distinct table, not per call
