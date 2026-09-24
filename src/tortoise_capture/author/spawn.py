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
capture started.

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
from ..core.contracts import CONVENTION, DERIVED, WIRE, AuthorContext, AuthoredRow, Event
from ..core.registry import author_rule

MOVEMENT_TYPE_RANDOM = 1        # MovementGeneratorType: wanders within wander_distance of its spawn
MOVEMENT_TYPE_WAYPOINT = 2      # MovementGeneratorType: follows creature_movement
GUID_COUNTER_MASK = 0xFFFFFF    # ObjectGuid low bits: the spawn's database id


@dataclass
class _Spawn:
    """Everything the capture showed about one spawn -- the makings of one row."""

    creates: list[tuple[float, tuple[float, ...]]] = field(default_factory=list)
    deaths: list[float] = field(default_factory=list)
    waypoints: list[dict[str, Any]] = field(default_factory=list)
    route: dict[str, Any] | None = None       # its patrol_route finding
    wander: dict[str, Any] | None = None      # its wander_area finding
    respawn: dict[str, Any] | None = None     # its respawn_timer finding

    def sighting(self) -> tuple[tuple[float, ...], bool] | None:
        """The create that followed a death, else the earliest one seen."""
        for death in sorted(self.deaths):
            after = [(t, pos) for t, pos in self.creates if t > death]
            if after:
                return min(after)[1], True
        return (min(self.creates)[1], False) if self.creates else None

    def patrols(self) -> bool:
        return bool(self.waypoints) and bool((self.route or {}).get("confident", True))


@author_rule(id="spawn", table="creature", order=60)
class Spawn(BaseAuthorRule):
    def __init__(self) -> None:
        self._spawns: dict[int, _Spawn] = {}
        self._map_id: int | None = None

    # -- collect -----------------------------------------------------------

    def _of(self, ev: Event) -> _Spawn | None:
        guid = ev.data.get("guid")
        return None if guid is None else self._spawns.setdefault(guid, _Spawn())

    def handle(self, ev: Event, mod: Any = None) -> None:
        if ev.kind == "object_create":
            position = (ev.data.get("movement") or {}).get("movement_info", {}).get("pos")
            spawn = self._of(ev)
            if position and ev.packet.t is not None and spawn is not None:
                spawn.creates.append((ev.packet.t, tuple(position)))
        elif ev.kind == "party_kill":
            spawn = self._of(ev)
            if ev.packet.t is not None and spawn is not None:
                spawn.deaths.append(ev.packet.t)
        elif ev.kind == "respawn_timer":
            if (spawn := self._of(ev)) is not None:
                spawn.respawn = dict(ev.data)
        elif ev.kind == "patrol_waypoint":
            if (spawn := self._of(ev)) is not None:
                spawn.waypoints.append(dict(ev.data))
        elif ev.kind == "patrol_route":
            if (spawn := self._of(ev)) is not None:
                spawn.route = dict(ev.data)
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
        return [(guid, s) for guid, s in sorted(self._spawns.items()) if s.creates]

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
            min_s, max_s = int(round(r["value_min"])), int(round(r["value_max"]))
            values["spawntimesecsmin"], values["spawntimesecsmax"] = min_s, max_s
            provenance["spawntimesecsmin"] = provenance["spawntimesecsmax"] = DERIVED
            notes.append(f"respawn {min_s}-{max_s}s from {r['samples']} observation(s) of the "
                         f"death-to-next-sighting gap ({r['value_min']:.3f}"
                         f"-{r['value_max']:.3f}s) -- a spread this tight against an "
                         "authored single value is plausibly measurement noise around a "
                         "fixed timer, not genuine randomisation; worth a human's judgement")
        else:
            # None seen at all, or only one sample -- a single gap cannot
            # even split a min from a max, the same refusal creature_spells
            # already applies to delayRepeatMin/Max from one interval.
            # Left out of `values` entirely; see gaps().
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
        if wander and not after_death:
            notes.append("position is the centre of the area it wandered, NOT a respawn -- "
                         "the capture holds no death for it; orientation is whichever way "
                         "it faced when first seen")
        elif not after_death:
            notes.append("position is the first sighting, NOT a respawn -- the capture "
                         "holds no death for this creature, so this may be mid-route")
        notes.append("position_z is ground-snapped by the server at runtime and tracks "
                     "the terrain, not the authored value")

        if self._map_id is not None:
            values["map"] = self._map_id
            provenance["map"] = WIRE
            notes.append(f"map {self._map_id} from the observing player's own "
                         "SMSG_LOGIN_VERIFY_WORLD/SMSG_NEW_WORLD, not from anything "
                         "the creature itself broadcasts")
        # map is never schema-filled when it is still unknown: its
        # default (0) is a real place (Eastern Kingdoms), not neutral
        # boilerplate, and guessing it would be worse than a named gap.
        self.fill_schema_defaults(ctx, "creature", values, provenance, notes, skip=skip)
        yield self.row(values, provenance, notes=tuple(notes))

        if not spawn.patrols():
            return
        points = sorted(spawn.waypoints, key=lambda w: w["point"])
        for waypoint in points:
            mv_values = {"id": db_guid, "point": waypoint["point"],
                         "position_x": self.wire_float(waypoint["position_x"]),
                         "position_y": self.wire_float(waypoint["position_y"]),
                         "position_z": self.wire_float(waypoint["position_z"])}
            mv_provenance = {"id": WIRE, "point": DERIVED, "position_x": DERIVED,
                             "position_y": DERIVED, "position_z": DERIVED}
            mv_notes = []
            self.fill_schema_defaults(ctx, "creature_movement", mv_values,
                                      mv_provenance, mv_notes)
            yield AuthoredRow(table="creature_movement", values=mv_values,
                              provenance=mv_provenance, notes=tuple(mv_notes))
        if (spawn.route or {}).get("closes_loop"):
            first = points[0]
            close_values = {"id": db_guid, "point": len(points) + 1,
                            "position_x": self.wire_float(first["position_x"]),
                            "position_y": self.wire_float(first["position_y"]),
                            "position_z": self.wire_float(first["position_z"])}
            close_provenance = {"id": WIRE, "point": CONVENTION, "position_x": DERIVED,
                                "position_y": DERIVED, "position_z": DERIVED}
            close_notes = ["the route closes, so the first point is repeated as the "
                          "last, the way authored routes close a loop"]
            self.fill_schema_defaults(ctx, "creature_movement", close_values,
                                      close_provenance, close_notes)
            yield AuthoredRow(table="creature_movement", values=close_values,
                              provenance=close_provenance, notes=tuple(close_notes))

    def gaps(self, ctx: AuthorContext) -> Iterator[str]:
        seen = self._seen()
        if not seen:
            yield "creature -- no CREATE block for this entry, so no spawn position was seen"
            return
        if self._map_id is None:
            yield ("creature.map -- no SMSG_LOGIN_VERIFY_WORLD or SMSG_NEW_WORLD in this "
                   "capture; a capture that starts after login never carries one")
        if not any(s.deaths for _, s in seen):
            yield ("creature.spawntimesecsmin/max -- the creature never died in this capture, "
                   "so the respawn timer could not be measured")
        else:
            for guid, spawn in seen:
                table = ("creature" if len(seen) == 1
                         else f"creature (spawn {guid & GUID_COUNTER_MASK})")
                r = spawn.respawn
                if not spawn.deaths:
                    yield (f"{table}.spawntimesecsmin/max -- this spawn never died in the "
                           "capture, so its respawn timer could not be measured")
                elif r is None:
                    yield (f"{table}.spawntimesecsmin/max -- it died but was not seen alive "
                           "again before the capture ended")
                elif not r.get("confident"):
                    yield (f"{table}.spawntimesecsmin/max -- only {r.get('samples', 1)} "
                           f"respawn observation(s) ({r['value_min']:.3f}s); one sample cannot "
                           "even split a min from a max, let alone bound a spread")
        for guid, spawn in seen:
            # Named per spawn only when there is more than one to tell apart.
            table = ("creature_movement" if len(seen) == 1
                     else f"creature_movement (spawn {guid & GUID_COUNTER_MASK})")
            if spawn.patrols() or spawn.wander:
                continue
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
    None: "a route was reconstructed but not trusted enough to propose",
}
_REFUSED_GAP["unordered"] = _REFUSED_GAP[None]
