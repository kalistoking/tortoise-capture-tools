"""Analyzers: patrol reconstruction and cross-opcode behaviour correlation."""

from __future__ import annotations

import math

from support import findings_by_kind, make_event, make_guid, run_analyzer
from tortoise_capture.analyze.behaviour import Behaviour
from tortoise_capture.analyze.patrol import Patrol

ENTRY = 62635
GUID = 0xF130_00F4AB_2787EA

# A square route. The spawn corner is first, as an authored route would be.
SQUARE = [(0.0, 0.0, 10.0), (10.0, 0.0, 10.0), (10.0, 10.0, 10.0), (0.0, 10.0, 10.0)]


def _hops(laps: float, jitter: float = 0.0):
    """Hop events walking the square, optionally starting mid-route."""
    events, t = [], 1.0
    total = int(len(SQUARE) * laps)
    for i in range(total):
        x, y, z = SQUARE[i % len(SQUARE)]
        events.append(make_event("move_linear", t, guid=GUID, entry=ENTRY,
                                 dest=(x + jitter, y + jitter, z)))
        t += 1.0
    return events


def test_patrol_recovers_the_loop_and_its_order():
    found = run_analyzer(Patrol(), _hops(laps=2.5))
    route = findings_by_kind(found)["patrol_route"]
    assert route.data["count"] == len(SQUARE)
    assert route.data["closes_loop"] is True

    points = [ev for ev in found if ev.kind == "patrol_waypoint"]
    assert [p.data["point"] for p in points] == [1, 2, 3, 4]
    for point, (x, y, _) in zip(points, SQUARE):
        assert math.isclose(point.data["position_x"], x, abs_tol=0.01)
        assert math.isclose(point.data["position_y"], y, abs_tol=0.01)


def test_repeated_sightings_of_one_waypoint_collapse_into_one_point():
    """2.5 laps of a 4-point square is 10 hops but still only 4 waypoints."""
    found = run_analyzer(Patrol(), _hops(laps=2.5))
    route = findings_by_kind(found)["patrol_route"]
    assert route.data["hops"] == 10 and route.data["count"] == 4
    seen = {ev.data["point"]: ev.data["observations"] for ev in found
            if ev.kind == "patrol_waypoint"}
    assert sum(seen.values()) == 10


def test_numbering_starts_at_the_spawn_point_not_the_capture_start():
    """A capture that begins mid-route must still number from the spawn."""
    # Start walking at the third corner, so hop order is 3,4,1,2,3,4,...
    events, t = [], 1.0
    for i in range(10):
        x, y, z = SQUARE[(i + 2) % len(SQUARE)]
        events.append(make_event("move_linear", t, guid=GUID, entry=ENTRY, dest=(x, y, z)))
        t += 1.0
    # ... and the respawn sighting pins the origin to the square's first corner.
    events.append(make_event("object_create", 50.0, guid=GUID, entry=ENTRY,
                             movement={"movement_info": {"pos": (0.0, 0.0, 10.0, 0.0)}}))

    points = [ev for ev in run_analyzer(Patrol(), events) if ev.kind == "patrol_waypoint"]
    first = points[0].data
    assert (round(first["position_x"]), round(first["position_y"])) == (0, 0)


def test_combat_detours_do_not_enter_the_route():
    """Positions visited once while fighting are not part of the patrol."""
    events = _hops(laps=2.5)
    events.append(make_event("move_linear", 99.0, guid=GUID, entry=ENTRY,
                             dest=(500.0, 500.0, 10.0)))      # dragged off to fight
    route = findings_by_kind(run_analyzer(Patrol(), events))["patrol_route"]
    assert route.data["count"] == len(SQUARE)


def test_a_wander_never_revisiting_a_point_is_not_a_confident_route():
    """Random movement is out of combat, so the combat-hop check cannot see it.

    A patrol is defined by repetition: the real Ralthas route has 0% of its
    waypoints seen exactly once, the fabricated Rakameg one 73%, and random
    wandering lands near 100% by construction -- every destination is new.
    """
    events, t = [], 1.0
    for i in range(12):                      # twelve destinations, none repeated
        events.append(make_event("move_linear", t, guid=GUID, entry=ENTRY,
                                 dest=(float(i * 20), float(i * 7), 10.0)))
        t += 1.0
    route = findings_by_kind(run_analyzer(Patrol(), events))["patrol_route"]
    assert route.data["confident"] is False
    assert route.data["single_visit_fraction"] > 0.5


def test_a_genuinely_walked_loop_stays_confident():
    """The regression guard: repetition is exactly what a real route has."""
    route = findings_by_kind(run_analyzer(Patrol(), _hops(laps=2.5)))["patrol_route"]
    assert route.data["confident"] is True
    assert route.data["single_visit_fraction"] == 0.0


def test_too_few_points_is_not_a_route():
    events = [make_event("move_linear", 1.0, guid=GUID, entry=ENTRY, dest=(0.0, 0.0, 0.0))]
    assert run_analyzer(Patrol(), events) == []


def test_a_route_with_no_entry_bearing_guid_is_not_reported():
    """Regression, found decoding a real second capture: SMSG_MONSTER_MOVE
    also carries forced player movement (knockback etc.), and since the
    entry-attribution fix this correctly omits `entry` for a non-entry-
    bearing guid -- route.entry stays None. Rendering `patrol_route`'s
    "entry={entry:<7}" template on None raised TypeError; the real fix is
    that a route with no creature to attribute it to is not a patrol at
    all, so it should never be reported, not just render without crashing."""
    player_guid = make_guid(0, 9, 0x0000)   # high word 0 == HIGHGUID_PLAYER
    events = [make_event("move_linear", float(i), guid=player_guid, dest=xyz)
             for i, xyz in enumerate(SQUARE * 3, start=1)]
    assert run_analyzer(Patrol(), events) == []


# --------------------------------------------------------------------------
# behaviour
# --------------------------------------------------------------------------

AGGRO_TEXT = "The Brotherhood Stands for Justice!"
DEATH_TEXT = "The lies of Stormwind, must be told!"


def _session():
    """The Ralthas shape: aggro, casts, death, respawn, aggro again."""
    return [
        make_event("object_create", 12.904, guid=GUID, entry=ENTRY),
        make_event("ai_reaction", 59.979, guid=GUID, entry=ENTRY, reaction=2),
        make_event("monster_say", 59.979, guid=GUID, entry=ENTRY, message=AGGRO_TEXT),
        make_event("spell_go", 60.026, guid=GUID, entry=ENTRY, spell_id=1449),
        make_event("spell_go", 78.317, guid=GUID, entry=ENTRY, spell_id=1449),
        make_event("monster_say", 90.307, guid=GUID, entry=ENTRY, message=DEATH_TEXT),
        make_event("party_kill", 90.307, guid=GUID, entry=ENTRY),
        make_event("object_create", 389.841, guid=GUID, entry=ENTRY),
        make_event("ai_reaction", 395.152, guid=GUID, entry=ENTRY, reaction=2),
        make_event("monster_say", 395.152, guid=GUID, entry=ENTRY, message=AGGRO_TEXT),
        make_event("spell_go", 395.199, guid=GUID, entry=ENTRY, spell_id=1449),
    ]


def test_respawn_timer_is_the_death_to_create_gap():
    found = findings_by_kind(run_analyzer(Behaviour(), _session()))
    respawn = found["respawn_timer"].data
    assert math.isclose(respawn["value_min"], 299.534, abs_tol=0.01)
    assert respawn["samples"] == 1 and respawn["confident"] is False


def _fields(**named):
    return [{"index": i, "name": name, "raw": raw} for i, (name, raw) in enumerate(named.items())]


def test_respawn_is_also_detected_from_a_health_reset_with_no_fresh_create():
    """A player who never loses sight of the creature never gets a fresh
    CREATE on respawn -- the server just resets HEALTH via a VALUES block,
    since the object never left the client's known-objects set. Real capture
    data: two such gaps landed at 300.022s and 299.217s against an authored
    300s timer."""
    events = [
        make_event("object_create", 10.0, guid=GUID, entry=ENTRY,
                  fields=_fields(UNIT_FIELD_HEALTH=342)),
        make_event("party_kill", 755.841, guid=GUID, entry=ENTRY),
        make_event("object_values", 756.0, guid=GUID, entry=ENTRY,
                  fields=_fields(UNIT_FIELD_HEALTH=0)),
        make_event("object_values", 1055.863, guid=GUID, entry=ENTRY,
                  fields=_fields(UNIT_FIELD_HEALTH=342)),
        make_event("party_kill", 1499.668, guid=GUID, entry=ENTRY),
        make_event("object_values", 1798.885, guid=GUID, entry=ENTRY,
                  fields=_fields(UNIT_FIELD_HEALTH=342)),
    ]
    found = findings_by_kind(run_analyzer(Behaviour(), events))
    respawn = found["respawn_timer"].data
    assert respawn["samples"] == 2
    assert math.isclose(respawn["value_min"], 299.217, abs_tol=0.01)
    assert math.isclose(respawn["value_max"], 300.022, abs_tol=0.01)
    assert respawn["confident"] is True


def test_a_health_update_while_already_alive_is_not_mistaken_for_a_respawn():
    """Ordinary combat damage (health going up and down while alive, e.g. a
    heal) must not be counted -- only a HEALTH>0 sighting that follows a
    confirmed death is a revival."""
    events = [
        make_event("object_create", 10.0, guid=GUID, entry=ENTRY,
                  fields=_fields(UNIT_FIELD_HEALTH=342)),
        make_event("object_values", 20.0, guid=GUID, entry=ENTRY,
                  fields=_fields(UNIT_FIELD_HEALTH=200)),      # took damage
        make_event("object_values", 25.0, guid=GUID, entry=ENTRY,
                  fields=_fields(UNIT_FIELD_HEALTH=342)),      # healed back up
    ]
    found = run_analyzer(Behaviour(), events)
    assert "respawn_timer" not in {ev.kind for ev in found}


def test_texts_are_attributed_to_the_trigger_they_coincide_with():
    found = run_analyzer(Behaviour(), _session())
    triggers = {ev.data["subject"]: ev.data["trigger"]
                for ev in found if ev.kind == "text_trigger"}
    assert triggers == {AGGRO_TEXT: "aggro", DEATH_TEXT: "death"}


def test_a_text_that_only_sometimes_coincides_is_not_attributed():
    """One coincidence out of two is not evidence, and must not be reported as one."""
    events = _session()
    events.append(make_event("monster_say", 200.0, guid=GUID, entry=ENTRY, message=AGGRO_TEXT))
    found = run_analyzer(Behaviour(), events)
    kinds = {ev.kind for ev in found if ev.data.get("subject") == AGGRO_TEXT}
    assert "text_untriggered" in kinds and "text_trigger" not in kinds


def test_initial_cast_delay_is_measured_from_engagement():
    found = findings_by_kind(run_analyzer(Behaviour(), _session()))
    delay = found["spell_initial_delay"].data
    assert math.isclose(delay["value_min"], 0.047, abs_tol=0.001) and delay["samples"] == 2


def test_a_single_repeat_interval_is_reported_but_not_trusted():
    """The honest failure: one interval cannot bound an authored min/max."""
    found = findings_by_kind(run_analyzer(Behaviour(), _session()))
    repeat = found["spell_repeat_delay"].data
    assert repeat["samples"] == 1 and repeat["confident"] is False
    assert math.isclose(repeat["value_min"], 18.291, abs_tol=0.01)


def test_an_interval_spanning_a_death_is_not_a_repeat_delay():
    """Casts either side of a respawn are two engagements, not one cooldown."""
    found = findings_by_kind(run_analyzer(Behaviour(), _session()))
    # 60.026 -> 78.317 counts; 78.317 -> 395.199 spans the death and must not.
    assert found["spell_repeat_delay"].data["samples"] == 1


# --------------------------------------------------------------------------
# sound attribution: SMSG_PLAY_SOUND has no sender, only timestamp coincidence
# --------------------------------------------------------------------------

def test_a_sound_next_to_one_creatures_text_is_attributed():
    events = _session() + [make_event("play_sound", 59.98, sound_id=5150)]  # next to aggro text
    found = findings_by_kind(run_analyzer(Behaviour(), events))
    aggro = next(ev for ev in run_analyzer(Behaviour(), events)
                if ev.kind == "text_trigger" and ev.data["subject"] == AGGRO_TEXT)
    assert aggro.data.get("sound_id") == 5150


def test_a_sound_matching_nothing_is_simply_not_attributed():
    events = _session() + [make_event("play_sound", 200.0, sound_id=999)]  # nowhere near any text
    found = run_analyzer(Behaviour(), events)
    assert all("sound_id" not in ev.data for ev in found if ev.kind == "text_trigger")


def test_a_sound_equidistant_between_two_creatures_texts_is_attributed_to_neither():
    """No sender guid on SMSG_PLAY_SOUND: if two creatures' lines land in the
    same instant, guessing which one played the sound would be a coin flip
    dressed up as data."""
    other_entry = 9999
    events = _session() + [
        make_event("monster_say", 59.979, guid=make_guid_for(other_entry), entry=other_entry,
                   message="Also right here"),
        make_event("text_trigger", 999.0, entry=other_entry, subject="Also right here",
                   trigger="aggro"),
        make_event("play_sound", 59.979, sound_id=42),
    ]
    found = run_analyzer(Behaviour(), events)
    triggers = [ev for ev in found if ev.kind == "text_trigger"]
    assert all("sound_id" not in ev.data for ev in triggers)


def make_guid_for(entry):
    return (0xF130 << 48) | (entry << 24) | 1
