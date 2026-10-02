"""creature and creature_movement: where it stands, and where it walks.

One file for both, because `creature_movement.id` is `creature.guid` -- the two
tables are one fact split across a foreign key, and a rule that emitted only
one of them would be proposing an orphan.

**One row per spawn, not per entry.** `creature` has a row for every spawn, and
a capture routinely sees many spawns of one entry -- the Elwynn capture saw
five cows and twenty-seven Prowlers. Everything below is therefore kept per
guid. It used to be kept per entry, which was invisible while the only captures
were of creatures that exist once in the world, and then took the guid from the
last spawn seen and the position from the first.

Four things are worth knowing about how a spawn row is recovered:

**The guid is on the wire.** A unit's 64-bit object guid carries the spawn's
database id in its low 24 bits, the same way it carries the template entry in
bits 24-47. No lookup, no guessing.

**The spawn position comes from a respawn, not from first sighting.** The first
CREATE block catches a moving creature wherever it happens to be standing; only
the create that follows a death puts it back at its spawn point. Without a
death, a wanderer's position is the centre of the area it was seen to wander
(`analyze/patrol.py`'s `wander_area`), and anything else keeps the first
sighting, flagged, because it is then wherever the creature was when the
capture started. A patrol's route offers nothing better: against the twelve
authored patrols in the Elwynn capture, the first sighting lands a median 31
yd from the authored spawn and the route's centre 35 yd, and the authored spawn
sits 2.5-17 yd off its own waypoints, at no point the route itself marks. Both
this rule and `patrol.py` read the spawn through `spawn_sighting()`, so the
route is numbered from where the row stands.

`spawntimesecsmin/max` is looser about this than position has to be: it reads
the spawn's own `respawn_timer` finding from `behaviour.py`, which counts a
respawn from *either* a fresh CREATE or a VALUES block resetting HEALTH off 0
-- position genuinely needs the CREATE (a VALUES block carries no
coordinates), but the timer only needs to know the creature is alive again,
and a player who never lost sight of it never gets a fresh CREATE to say so.

**Movement is one of three answers or a gap.** A patrol that `patrol.py`
trusts is `movement_type` 2 with its waypoints; a wanderer it recognised is
`movement_type` 1 with a `wander_distance`; anything it could not classify is
left out, with the reason it could not.

**Z is what the creature walked, not what an author typed.** The server
ground-snaps at runtime, so broadcast Z tracks the terrain to within a few
tenths of a yard of the authored value. X and Y match to sub-centimetre; Z is
close, and says so.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterator

from ..core.base import BaseAuthorRule
from ..core.contracts import (
    CONVENTION, DERIVED, WIRE, AuthorContext, AuthoredRow, Event, Lifeline, is_corpse,
    is_creature, spawn_sighting,
)
from ..core.registry import author_rule

MOVEMENT_TYPE_RANDOM = 1        # MovementGeneratorType: wanders within wander_distance of its spawn
MOVEMENT_TYPE_WAYPOINT = 2      # MovementGeneratorType: follows creature_movement
GUID_COUNTER_MASK = 0xFFFFFF    # ObjectGuid low bits: the spawn's database id

# Set only on what a spell or an owner makes -- spell summons, pets, totems
# (SpellEffects.cpp:2199-2206, :2668-2673, Pet.cpp:285, Totem.cpp:162-163) --
# never on a spawn. A script's summon (WorldObject::SummonCreature,
# Object.cpp:2190) sets neither, and still passes for a spawn.
_SUMMONER = {"UNIT_FIELD_CREATEDBY", "UNIT_FIELD_SUMMONEDBY"}


# Said where a position is not a respawn's although the capture shows a death.
_DIED_NOT_SEEN_AGAIN = "it died in the capture but no CREATE showed it standing again afterwards"


@dataclass
class _Spawn:
    """Everything the capture showed about one spawn -- the makings of one row."""

    creates: list[tuple[float, tuple[float, ...]]] = field(default_factory=list)
    life: Lifeline = field(default_factory=Lifeline)    # its deaths, by every witness
    waypoints: list[dict[str, Any]] = field(default_factory=list)
    route: dict[str, Any] | None = None       # its patrol_route finding
    wander: dict[str, Any] | None = None      # its wander_area finding
    respawn: dict[str, Any] | None = None     # its respawn_timer finding
    hops: int = 0                             # linear moves broadcast, any kind
    corpses: list[tuple[float, tuple[float, ...]]] = field(default_factory=list)  # lying dead
    summoned: bool = False                    # a CREATE named whoever summoned it
    map: int | None = None                    # the session's map when it was first seen

    def sighting(self) -> tuple[tuple[float, ...], bool] | None:
        """The create that followed a death, else the earliest one seen --
        or, for a spawn only ever seen lying dead, where it lay."""
        if not self.creates and self.lies_dead():
            return min(self.corpses)[1], False
        return spawn_sighting(self.creates, self.life.deaths)

    def lies_dead(self) -> bool:
        """Seen only dead, and never seen dying: a spawn that stands dead by
        default (SPAWN_FLAG_DEAD, Creature.cpp:1749) at its spawn point, or one
        killed before the capture began, where it died."""
        return bool(self.corpses) and not self.creates and not self.life.deaths

    def died_out_of_sight(self) -> bool:
        """Seen dying and then only as the corpse, which lies where it died."""
        return bool(self.corpses) and not self.creates and bool(self.life.deaths)

    def patrols(self) -> bool:
        return bool(self.waypoints) and bool((self.route or {}).get("confident", True))

    def moved(self) -> int:
        """Hops seen, counted here or by the route patrol.py built from them."""
        return self.hops or (self.route or {}).get("hops") or len(self.waypoints)


@author_rule(id="spawn", table="creature", order=60)
class Spawn(BaseAuthorRule):
    def __init__(self) -> None:
        self._spawns: dict[int, _Spawn] = {}
        self._map_id: int | None = None

    # -- collect -----------------------------------------------------------

    def _of(self, ev: Event) -> _Spawn | None:
        guid = ev.data.get("guid")
        return self._spawns.setdefault(guid, _Spawn()) if is_creature(guid) else None

    def handle(self, ev: Event, mod: Any = None) -> None:
        if ev.kind == "object_create":
            position = (ev.data.get("movement") or {}).get("movement_info", {}).get("pos")
            spawn = self._of(ev)
            if spawn is not None and spawn.map is None and not (spawn.creates or spawn.corpses):
                # The map it stood on is the session's at the time, not the one
                # the capture ends on -- a dungeon entered later is not its map.
                spawn.map = self._map_id
            if spawn is not None and _SUMMONER & {f.get("name") for f in ev.data.get("fields", ())}:
                spawn.summoned = True
            if spawn is not None:
                spawn.life.see(ev)
            if spawn is not None and is_corpse(ev):
                if position and ev.packet.t is not None:
                    spawn.corpses.append((ev.packet.t, tuple(position)))
            elif position and ev.packet.t is not None and spawn is not None:
                spawn.creates.append((ev.packet.t, tuple(position)))
        elif ev.kind in ("party_kill", "object_values"):
            # A death has three witnesses (Lifeline); a creature somebody else
            # killed leaves only the HEALTH block and, if the player was out of
            # range, a corpse CREATE above.
            if (spawn := self._of(ev)) is not None:
                spawn.life.see(ev)
        elif ev.kind == "respawn_timer":
            if (spawn := self._of(ev)) is not None:
                spawn.respawn = dict(ev.data)
        elif ev.kind == "patrol_waypoint":
            if (spawn := self._of(ev)) is not None:
                spawn.waypoints.append(dict(ev.data))
        elif ev.kind == "patrol_route":
            if (spawn := self._of(ev)) is not None:
                spawn.route = dict(ev.data)
        elif ev.kind in ("move_linear", "move_spline"):
            # A spline counts too: a flying random mover circles its spawn in
            # one (RandomMovementGenerator.cpp:33-47), and is no stander.
            if (spawn := self._of(ev)) is not None:
                spawn.hops += 1
        elif ev.kind == "wander_area":
            if (spawn := self._of(ev)) is not None:
                spawn.wander = dict(ev.data)
        elif ev.kind == "world_transfer":
            # Session-scoped (see Event.scope): not about this creature
            # specifically, but the map the whole session was observed on.
            # Last one wins -- a teleport mid-session supersedes login.
            self._map_id = ev.data["map_id"]

    def _seen(self) -> list[tuple[int, _Spawn]]:
        """Spawns the capture saw a position for, in guid order."""
        return [(guid, s) for guid, s in sorted(self._spawns.items())
                if (s.creates or s.lies_dead()) and not s.summoned]

    # -- emit --------------------------------------------------------------

    def rows(self, ctx: AuthorContext) -> Iterator[AuthoredRow]:
        for guid, spawn in self._seen():
            yield from self._spawn_rows(ctx, guid & GUID_COUNTER_MASK, spawn)

    def _spawn_rows(self, ctx: AuthorContext, db_guid: int,
                    spawn: _Spawn) -> Iterator[AuthoredRow]:
        (x, y, z, o), after_death = spawn.sighting()
        wander = spawn.wander if not spawn.patrols() else None
        values: dict[str, Any] = {
            "guid": db_guid, "id": ctx.entry,
            "position_x": self.wire_float(x), "position_y": self.wire_float(y),
            "position_z": self.wire_float(z), "orientation": self.wire_float(o),
            "health_percent": 100, "mana_percent": 100,
        }
        provenance = {"guid": WIRE, "id": WIRE, "position_x": WIRE, "position_y": WIRE,
                      "position_z": WIRE, "orientation": WIRE,
                      "health_percent": CONVENTION, "mana_percent": CONVENTION}
        notes = []
        if wander and not after_death:
            for axis in ("position_x", "position_y", "position_z"):
                values[axis] = self.wire_float(wander[axis])
                provenance[axis] = DERIVED

        skip: set[str] = {"map"}
        if spawn.respawn is not None and spawn.respawn.get("confident"):
            r = spawn.respawn
            values["spawntimesecsmin"] = values["spawntimesecsmax"] = r["seconds"]
            provenance["spawntimesecsmin"] = provenance["spawntimesecsmax"] = DERIVED
            notes.append(f"respawn {r['seconds']}s: {r['samples']} observation(s) of the "
                         f"death-to-next-sighting gap ({r['value_min']:.3f}"
                         f"-{r['value_max']:.3f}s), counted in whole seconds from the death, "
                         f"all fit {r['seconds']} and no other -- one draw of this spawn's "
                         "timer, which it draws once when it loads (Creature.cpp:1748), so a "
                         "range the row holds cannot be seen from it; min = max is right for "
                         "a fixed timer, as 95% of creature rows are. Dynamic respawn cuts it "
                         "by up to a quarter while 4+ players are within 120 yd "
                         "(Creature.cpp:2427-2500, mangosd.conf.dist:2057-2064), the same "
                         "cut for the same crowd: many players about, and this may be the "
                         "cut timer, not the authored one")
        else:
            # None seen, one sample, or samples no one timer explains --
            # left out of `values` entirely; see gaps().
            skip.add("spawntimesecsmin")
            skip.add("spawntimesecsmax")
        if spawn.patrols():
            values["movement_type"] = MOVEMENT_TYPE_WAYPOINT
            provenance["movement_type"] = DERIVED
            notes.append("movement_type 2 (waypoint) because the creature walked a "
                         "closed route")
            # wander_distance only has an effect for random movers
            # (Creature.cpp: m_wanderDistance has no reader for a
            # waypoint-follower); the schema's own default (5, sized for
            # random wandering) would be inert here but misleading to
            # read next to movement_type=2, so it is set explicitly
            # instead of left to the generic schema-fill below.
            values["wander_distance"] = 0
            provenance["wander_distance"] = CONVENTION
        elif wander:
            values["movement_type"] = MOVEMENT_TYPE_RANDOM
            values["wander_distance"] = math.ceil(wander["radius"])
            provenance["movement_type"] = provenance["wander_distance"] = DERIVED
            notes.append(f"movement_type 1 (random): {wander['hops']} hops went somewhere "
                         "different from each point, where a patrol goes to the same next "
                         "point every time")
            notes.append(f"wander_distance {values['wander_distance']}: the smallest circle "
                         f"round every destination has radius {wander['radius']:.2f} yd, which "
                         "can only undershoot the true value, so it is rounded up -- exact on "
                         "all 19 real wanderers it was measured against (5 and 12 yd); a wider "
                         "radius needs more hops before destinations reach its edge")
        elif spawn.waypoints:
            notes.append(_WITHHELD_NOTE.get((spawn.route or {}).get("refused_because"),
                                            _WITHHELD_NOTE[None]))
        elif not spawn.moved():
            # All 32,150 stationary spawns in the live database carry 0: the
            # schema's 5 is sized for a random mover, as for a waypoint mover.
            values["wander_distance"] = 0
            provenance["wander_distance"] = CONVENTION
        if wander and not after_death:
            why = _DIED_NOT_SEEN_AGAIN if spawn.life.deaths else "the capture holds no death for it"
            notes.append("position is the centre of the area it wandered, NOT a respawn -- "
                         f"{why}; orientation is whichever way it faced when first seen")
        elif spawn.lies_dead():
            notes.append("position is where it lay dead, the only way it was seen: its spawn "
                         "point if it stands dead by default, where it died if it was killed "
                         "before the capture began -- see gaps")
        elif not after_death:
            why = (_DIED_NOT_SEEN_AGAIN if spawn.life.deaths
                   else "the capture holds no death for this creature")
            notes.append(f"position is the first sighting, NOT a respawn -- {why}, so this "
                         "may be mid-route")
        notes.append("position_z is ground-snapped by the server at runtime and tracks "
                     "the terrain, not the authored value")

        if spawn.map is not None:
            values["map"] = spawn.map
            provenance["map"] = WIRE
            notes.append(f"map {spawn.map} from the observing player's own "
                         "SMSG_LOGIN_VERIFY_WORLD/SMSG_NEW_WORLD in force when it was seen, "
                         "not from anything the creature itself broadcasts")
        # map is never schema-filled when it is still unknown: its
        # default (0) is a real place (Eastern Kingdoms), not neutral
        # boilerplate, and guessing it would be worse than a named gap.
        self.fill_schema_defaults(ctx, "creature", values, provenance, notes, skip=skip)
        yield self.row(values, provenance, notes=tuple(notes))

        if not spawn.patrols():
            return
        points = sorted(spawn.waypoints, key=lambda w: w["point"])
        # A point the path holds twice in a row is written twice, where the
        # capture showed it; the first point's second copy goes last, closing
        # the path the way Tortoise's own capture-authored paths are written.
        rows = []
        for index, waypoint in enumerate(points):
            rows.append((waypoint, False))
            if index and waypoint.get("repeats"):
                rows.append((waypoint, True))
        if points and points[0].get("repeats"):
            rows.append((points[0], True))
        first_row: dict[int, int] = {}              # waypoint point -> the row it is written on
        for number, (waypoint, again) in enumerate(rows, start=1):
            first_row.setdefault(waypoint["point"], number)
            mv_values = {"id": db_guid, "point": number,
                         "position_x": self.wire_float(waypoint["position_x"]),
                         "position_y": self.wire_float(waypoint["position_y"]),
                         "position_z": self.wire_float(waypoint["position_z"])}
            mv_provenance = {"id": WIRE, "point": DERIVED, "position_x": DERIVED,
                             "position_y": DERIVED, "position_z": DERIVED}
            mv_notes = []
            if again:
                mv_notes.append(f"row {first_row[waypoint['point']]} again: the capture shows the "
                                "server moving the creature to where it already stood there "
                                f"on {waypoint['repeats']} of {waypoint['arrivals']} arrivals, "
                                "which only a path holding the point twice in a row does "
                                "(WaypointMovementGenerator.cpp:192-211)")
            elif (number == len(rows) and (spawn.route or {}).get("closes_loop")
                    and not points[0].get("still_hops")):
                mv_notes.append("the path ends here: the capture shows no hop repeating "
                                "point 1, and after its last point the server walks on to "
                                "point 1 anyway (WaypointMovementGenerator.cpp:198-207) -- "
                                "the way 2,540 of 3,995 paths in the live database end")
            self.fill_schema_defaults(ctx, "creature_movement", mv_values,
                                      mv_provenance, mv_notes)
            yield AuthoredRow(table="creature_movement", values=mv_values,
                              provenance=mv_provenance, notes=tuple(mv_notes))

    def gaps(self, ctx: AuthorContext) -> Iterator[str]:
        summoned = [guid for guid, s in sorted(self._spawns.items()) if s.summoned]
        if summoned:
            yield (f"creature -- {len(summoned)} summon(s), not proposed: a CREATE named "
                   "who summoned them, so they despawn rather than respawn (guid "
                   f"{', '.join(str(g & GUID_COUNTER_MASK) for g in summoned)})")
        for guid, spawn in sorted(self._spawns.items()):
            if spawn.lies_dead() and not spawn.summoned:
                yield (f"creature (spawn {guid & GUID_COUNTER_MASK}).spawn_flags -- only ever "
                       "seen lying dead, never dying: if it stands dead by default, spawn_flags "
                       "needs SPAWN_FLAG_DEAD (0x80) and the position is its spawn point; if it "
                       "was killed before the capture began, the position is where it died "
                       "-- decide by hand")
        only_dead = [guid for guid, s in sorted(self._spawns.items()) if s.died_out_of_sight()]
        if only_dead:
            yield (f"creature -- {len(only_dead)} spawn(s) seen only as a corpse, which lies "
                   "where it died: no spawn point to author (guid "
                   f"{', '.join(str(g & GUID_COUNTER_MASK) for g in only_dead)})")
        seen = self._seen()
        if not seen:
            # A summon or a corpse is a CREATE too, and says so above.
            if not any(s.creates or s.corpses or s.summoned for s in self._spawns.values()):
                yield "creature -- no CREATE block for this entry, so no spawn position was seen"
            return
        if self._map_id is None:
            yield ("creature.map -- no SMSG_LOGIN_VERIFY_WORLD or SMSG_NEW_WORLD in this "
                   "capture; a capture that starts after login never carries one")
        elif any(s.map is None for _, s in seen):
            yield ("creature.map -- seen before the capture's first SMSG_LOGIN_VERIFY_WORLD "
                   "or SMSG_NEW_WORLD, so the map it stood on is not known")
        if not any(s.life.deaths for _, s in seen):
            yield ("creature.spawntimesecsmin/max -- the creature never died in this capture, "
                   "so the respawn timer could not be measured")
        else:
            for guid, spawn in seen:
                table = ("creature" if len(seen) == 1
                         else f"creature (spawn {guid & GUID_COUNTER_MASK})")
                r = spawn.respawn
                if not spawn.life.deaths:
                    yield (f"{table}.spawntimesecsmin/max -- this spawn never died in the "
                           "capture, so its respawn timer could not be measured")
                elif not spawn.life.timed:
                    yield (f"{table}.spawntimesecsmin/max -- it died out of sight: the capture "
                           "holds only the corpse that was lying there when the player came "
                           "back, so when it died is not known and the gap to its respawn is "
                           "no measure of the timer")
                elif r is None:
                    yield (f"{table}.spawntimesecsmin/max -- it died but was not seen alive "
                           "again before the capture ended")
                elif r.get("samples", 1) < 2:
                    yield (f"{table}.spawntimesecsmin/max -- only one sample of the respawn "
                           f"({r['value_min']:.3f}s), which cannot tell a fixed timer from one "
                           "drawn again at each death")
                elif not r.get("confident") and len(r.get("fits") or ()) > 1:
                    yield (f"{table}.spawntimesecsmin/max -- {r['samples']} respawns took "
                           f"{r['value_min']:.3f}-{r['value_max']:.3f}s, which fits a timer "
                           f"of {' or '.join(map(str, r['fits']))} s alike: the server counts "
                           "whole seconds from the death, so one more respawn would settle "
                           "it -- not authored")
                elif not r.get("confident"):
                    yield (f"{table}.spawntimesecsmin/max -- {r['samples']} respawns took "
                           f"{r['value_min']:.3f}-{r['value_max']:.3f}s, more apart than timing "
                           "in whole seconds explains: its timer is drawn again at each death "
                           "(spawn_flags' random respawn), cut differently by dynamic respawn "
                           "as players came and went, or one sighting came late -- not authored")
        for guid, spawn in seen:
            # Named per spawn only when there is more than one to tell apart.
            table = ("creature_movement" if len(seen) == 1
                     else f"creature_movement (spawn {guid & GUID_COUNTER_MASK})")
            if spawn.patrols():
                for w in spawn.waypoints:
                    if w.get("still_hops") and not w.get("repeats"):
                        yield (f"{table} -- point {w['point']} was left standing still, back "
                               f"to itself, on {w['still_hops']} of {w.get('arrivals')} "
                               "arrivals: too few to call it held twice in a row, too many "
                               "to call it none -- check whether the path holds it twice")
            if spawn.patrols() or spawn.wander:
                continue
            if spawn.moved():
                # An INSERT needs a value, and the schema's 0 is the one known to
                # be wrong: 185 of 186 spawns in the test captures that moved
                # without a settled route are 1 or 2 in the live database.
                row = table.replace("creature_movement", "creature", 1)
                yield (f"{row}.movement_type -- it moved ({spawn.moved()} hop(s)) but how was "
                       "not settled, so any 0 in the row is only the table's placeholder, "
                       "right only if it stands still out of combat -- as 1 in 186 such "
                       "spawns in the test captures did; set it by hand")
            if not spawn.waypoints:
                yield (f"{table} -- no closed route was reconstructed; the creature may "
                       "be stationary, or the capture may be too short to see a full lap")
                continue
            route = spawn.route or {}
            yield f"{table} -- " + _REFUSED_GAP.get(route.get("refused_because"),
                                                    _REFUSED_GAP[None]).format(
                hops=route.get("hops", "too few"))


# Why a reconstructed route was not proposed, by patrol.py's own reason.
_WITHHELD_NOTE = {
    "combat": ("a route was reconstructed but withheld: most of its hops happened during "
               "combat, so it may be re-engagement repositioning rather than a real "
               "patrol -- see gaps"),
    "branching": ("a route was reconstructed but withheld: some point is left for two "
                  "different next points, as a route that turns back (A-B-C-B) or forks is, "
                  "and following one of them would drop the rest -- see gaps"),
    None: "a route was reconstructed but withheld as untrustworthy -- see gaps",
}
_WITHHELD_NOTE.update({k: _WITHHELD_NOTE[None] for k in ("short", "unrevisited", "unordered")})

_REFUSED_GAP = {
    "combat": ("a route was reconstructed, but most of its hops happened during combat; "
               "re-engaging a stationary creature many times can produce a closed loop "
               "out of nothing but repositioning, so it is not proposed without enough "
               "movement seen outside of combat to trust it"),
    "short": ("a route was reconstructed from {hops} hop(s), and fewer than 30 hops cannot "
              "tell a patrol's order from a small random mover's luck, so it is not "
              "proposed until the creature has been watched for longer"),
    "unrevisited": ("a route of {hops} hop(s) never left any point twice -- a long route "
                    "watched for less than a lap cannot yet show whether it keeps an order"),
    "branching": ("a route was reconstructed, but some point is left for two different "
                  "next points: it turns back (A-B-C-B) or forks, and one successor per "
                  "point cannot number it -- author the points in walking order by hand"),
    None: "a route was reconstructed but not trusted enough to propose",
}
_REFUSED_GAP["unordered"] = _REFUSED_GAP[None]
