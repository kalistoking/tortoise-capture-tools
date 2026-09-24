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


# A wanderer confined to a small area, the way a 5-yard random mover is: six
# points it keeps coming back to, but never in the same order twice.
_WANDER_POINTS = [(0.0, 0.0), (3.0, 0.0), (6.0, 0.0), (0.0, 3.0), (3.0, 3.0), (6.0, 3.0)]
_WANDER_LAPS = [[0, 1, 2, 3, 4, 5], [3, 0, 4, 1, 5, 2], [5, 4, 3, 2, 1, 0],
                [1, 3, 5, 0, 2, 4], [2, 5, 1, 4, 0, 3], [4, 2, 0, 5, 3, 1],
                [0, 4, 2, 5, 1, 3], [3, 5, 4, 0, 1, 2]]


def _confined_wander():
    events, t = [], 1.0
    for lap in _WANDER_LAPS:
        for i in lap:
            x, y = _WANDER_POINTS[i]
            events.append(make_event("move_linear", t, guid=GUID, entry=ENTRY, dest=(x, y, 10.0)))
            t += 1.0
    return events


def test_a_confined_wander_revisits_its_points_but_in_no_order():
    """The failure the cow capture proved, kept as the reason for the gate.

    A random mover confined to a few yards DOES revisit its points -- the five
    real cows in Cow_Elwyn_Forest.pcap had 0% single-visit waypoints, and the
    revisit-count gate accepted all five as patrols. What it lacks is order:
    from any one point it goes somewhere different each time. So the assertion
    that carries the test is the second one -- the old measure is fooled here.
    """
    route = findings_by_kind(run_analyzer(Patrol(), _confined_wander()))["patrol_route"]
    assert route.data["confident"] is False
    assert route.data["single_visit_fraction"] == 0.0          # what fooled the old gate
    assert route.data["transition_order"] < 0.65


def test_a_genuinely_walked_loop_stays_confident():
    """The regression guard: a real patrol goes from A to B every time."""
    route = findings_by_kind(run_analyzer(Patrol(), _hops(laps=10)))["patrol_route"]
    assert route.data["confident"] is True
    assert route.data["transition_order"] == 1.0


def test_a_loop_watched_too_briefly_is_not_yet_confident():
    """Order needs watching long enough to show itself.

    Simulated, a random mover within 2-5 yards looks ordered by chance in up to
    30% of 10-20 hop observations and almost never past 30. So a perfect loop
    seen for ten hops is refused: not because it is wrong, but because ten hops
    cannot tell it from luck. Fewer confident routes, never a wrong one.
    """
    route = findings_by_kind(run_analyzer(Patrol(), _hops(laps=2.5)))["patrol_route"]
    assert route.data["confident"] is False


def test_a_confined_wander_is_reported_as_the_area_it_wanders():
    """The smallest circle round a wanderer's destinations is its wander area.

    Its radius cannot overshoot: the true home is always a valid centre, so the
    smallest circle is never larger than wander_distance. Measured on 19 real
    wanderers, it rounded up to the true value 19 of 19 times, and put the
    home within 0.37 yd at the median where the first sighting was 4.53 yd off.
    """
    found = findings_by_kind(run_analyzer(Patrol(), _confined_wander()))
    assert found["patrol_route"].data["refused_because"] == "unordered"
    area = found["wander_area"].data
    # _WANDER_POINTS are the corners and edge midpoints of a 6 x 3 rectangle
    assert math.isclose(area["position_x"], 3.0, abs_tol=0.01)
    assert math.isclose(area["position_y"], 1.5, abs_tol=0.01)
    assert math.isclose(area["radius"], math.hypot(3.0, 1.5), abs_tol=0.01)


def test_a_patrol_is_not_reported_as_a_wander_area():
    found = findings_by_kind(run_analyzer(Patrol(), _hops(laps=10)))
    assert "wander_area" not in found


def test_a_route_watched_too_briefly_is_neither_patrol_nor_wander():
    """Ten hops cannot tell order from luck, so they say nothing either way."""
    found = findings_by_kind(run_analyzer(Patrol(), _hops(laps=2.5)))
    assert found["patrol_route"].data["refused_because"] == "short"
    assert "wander_area" not in found


def test_combat_movement_is_neither_patrol_nor_wander():
    """Rakameg's shape: hops that all happened inside a fight.

    Repositioning around a player looks unordered too, and must not be
    authored as a random mover -- the creature it came from stands still.
    """
    events = [make_event("ai_reaction", 0.5, guid=GUID, entry=ENTRY, reaction=2)]
    found = findings_by_kind(run_analyzer(Patrol(), events + _confined_wander()))
    assert found["patrol_route"].data["refused_because"] == "combat"
    assert "wander_area" not in found


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


def test_a_mid_combat_reaggro_is_not_an_engagement_start():
    """delayInitial counts from a fight that starts from rest, not from every aggro.

    SMSG_AI_REACTION fires again whenever a creature re-aggroes mid-fight --
    a target switch, a player re-engaging after kiting -- and a spell's timer
    does not restart then. Rakameg's capture had 14 aggros over 2 deaths; the
    2 fresh ones gave 4.0s and 4.03s against an authored 4-5s, and the 12
    re-aggros gave anything from 1.68s to 60.6s. min() over all 14 picked 1.68.
    """
    events = [
        make_event("object_create", 10.0, guid=GUID, entry=ENTRY),
        make_event("ai_reaction", 100.0, guid=GUID, entry=ENTRY, reaction=2),   # from rest
        make_event("spell_go", 104.0, guid=GUID, entry=ENTRY, spell_id=28447),
        make_event("ai_reaction", 150.0, guid=GUID, entry=ENTRY, reaction=2),   # re-aggro
        make_event("spell_go", 151.7, guid=GUID, entry=ENTRY, spell_id=28447),
    ]
    delay = findings_by_kind(run_analyzer(Behaviour(), events))["spell_initial_delay"].data
    assert math.isclose(delay["value_min"], 4.0, abs_tol=0.001)
    assert delay["samples"] == 1


def test_a_cast_after_the_creature_died_does_not_count_for_the_life_before():
    """First cast after a fresh aggro, but only within the fight that aggro began.

    Without an upper bound, a creature that died before ever casting a spell
    lends that engagement the first cast of its NEXT life, ninety seconds on.
    """
    events = [
        make_event("object_create", 10.0, guid=GUID, entry=ENTRY),
        make_event("ai_reaction", 100.0, guid=GUID, entry=ENTRY, reaction=2),
        make_event("party_kill", 110.0, guid=GUID, entry=ENTRY),                # never cast
        make_event("object_create", 180.0, guid=GUID, entry=ENTRY),
        make_event("ai_reaction", 200.0, guid=GUID, entry=ENTRY, reaction=2),
        make_event("spell_go", 203.0, guid=GUID, entry=ENTRY, spell_id=28447),
    ]
    delay = findings_by_kind(run_analyzer(Behaviour(), events))["spell_initial_delay"].data
    assert math.isclose(delay["value_min"], 3.0, abs_tol=0.001)
    assert math.isclose(delay["value_max"], 3.0, abs_tol=0.001)
    assert delay["samples"] == 1


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


# One entry, several spawns: every measurement belongs to the spawn it was taken on.
OTHER = GUID + 1


def test_a_respawn_is_only_the_same_spawn_seen_alive_again():
    """Keyed by entry, one Prowler dying and another ticking its health 20 s
    later read as a 20 s respawn -- the dead one's `alive` flag was shared."""
    events = [
        make_event("object_create", 10.0, guid=GUID, entry=ENTRY,
                   fields=_fields(UNIT_FIELD_HEALTH=206)),
        make_event("object_create", 11.0, guid=OTHER, entry=ENTRY,
                   fields=_fields(UNIT_FIELD_HEALTH=231)),
        make_event("party_kill", 100.0, guid=GUID, entry=ENTRY),
        make_event("object_values", 120.0, guid=OTHER, entry=ENTRY,
                   fields=_fields(UNIT_FIELD_HEALTH=231)),
        make_event("object_create", 130.0, guid=OTHER, entry=ENTRY),
        make_event("object_create", 400.0, guid=GUID, entry=ENTRY),
    ]
    respawns = [ev.data for ev in run_analyzer(Behaviour(), events) if ev.kind == "respawn_timer"]
    assert [(r["guid"], r["value_min"]) for r in respawns] == [(GUID, 300.0)]


def test_two_spawns_respawn_rows_do_not_share_a_key():
    """capture_behaviour's key has no guid; a respawn's subject is its spawn."""
    from tortoise_capture.core.contracts import SqlContext
    events = [ev for guid in (GUID, OTHER) for ev in (
        make_event("object_create", 10.0, guid=guid, entry=ENTRY),
        make_event("party_kill", 50.0, guid=guid, entry=ENTRY),
        make_event("object_create", 350.0, guid=guid, entry=ENTRY),
    )]
    behaviour = Behaviour()
    rows = [row for ev in run_analyzer(behaviour, events) if ev.kind == "respawn_timer"
            for row in behaviour.sql_rows(ev, SqlContext(capture_id="test"))]
    keys = {tuple(row.values[k] for k in ("capture", "entry", "finding", "subject"))
            for row in rows}
    assert len(rows) == 2 and len(keys) == 2


def test_each_spawn_starts_its_own_fight_and_casts_its_own_repeats():
    """Two spawns pulled one after the other are two fresh engagements, and one
    spawn's cast followed by the other's is no repeat delay at all."""
    events = [
        make_event("ai_reaction", 10.0, guid=GUID, entry=ENTRY, reaction=2),
        make_event("spell_go", 12.0, guid=GUID, entry=ENTRY, spell_id=1449),
        make_event("ai_reaction", 50.0, guid=OTHER, entry=ENTRY, reaction=2),
        make_event("spell_go", 53.0, guid=OTHER, entry=ENTRY, spell_id=1449),
    ]
    found = findings_by_kind(run_analyzer(Behaviour(), events))
    delay = found["spell_initial_delay"].data
    assert (delay["value_min"], delay["value_max"], delay["samples"]) == (2.0, 3.0, 2)
    assert "spell_repeat_delay" not in found


def test_a_line_is_matched_against_its_own_speakers_triggers():
    """A spawn speaking the moment a neighbour dies has not said a death line."""
    events = [
        make_event("party_kill", 90.0, guid=GUID, entry=ENTRY),
        make_event("monster_say", 90.0, guid=OTHER, entry=ENTRY, message=AGGRO_TEXT),
    ]
    kinds = {ev.kind for ev in run_analyzer(Behaviour(), events)}
    assert "text_untriggered" in kinds and "text_trigger" not in kinds


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
