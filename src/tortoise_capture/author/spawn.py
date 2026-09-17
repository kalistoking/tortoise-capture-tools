"""creature and creature_movement: where it stands, and where it walks.

One file for both, because `creature_movement.id` is `creature.guid` -- the two
tables are one fact split across a foreign key, and a rule that emitted only
one of them would be proposing an orphan.

Three things are worth knowing about how the spawn row is recovered:

**The guid is on the wire.** A unit's 64-bit object guid carries the spawn's
database id in its low 24 bits, the same way it carries the template entry in
bits 24-47. No lookup, no guessing.

**The spawn position comes from a respawn, not from first sighting.** The first
CREATE block catches a patrolling creature wherever it happens to be standing;
only the create that follows a death puts it back at its spawn point. When the
capture has no death, the position is still emitted but flagged, because it is
then wherever the creature was when the capture started.

**Z is what the creature walked, not what an author typed.** The server
ground-snaps at runtime, so broadcast Z tracks the terrain to within a few
tenths of a yard of the authored value. X and Y match to sub-centimetre; Z is
close, and says so.
"""

from __future__ import annotations

from typing import Any, Iterator

from ..core.base import BaseAuthorRule
from ..core.contracts import CONVENTION, DERIVED, WIRE, AuthorContext, AuthoredRow, Event
from ..core.registry import author_rule

MOVEMENT_TYPE_WAYPOINT = 2      # MovementGeneratorType: follows creature_movement
GUID_COUNTER_MASK = 0xFFFFFF    # ObjectGuid low bits: the spawn's database id


@author_rule(id="spawn", table="creature", order=60)
class Spawn(BaseAuthorRule):
    def __init__(self) -> None:
        self._guid: int | None = None
        self._creates: list[tuple[float, tuple[float, ...]]] = []
        self._deaths: list[float] = []
        self._respawn: float | None = None
        self._waypoints: list[dict[str, Any]] = []
        self._closes_loop = False

    # -- collect -----------------------------------------------------------

    def handle(self, ev: Event, mod: Any = None) -> None:
        if ev.kind == "object_create":
            position = (ev.data.get("movement") or {}).get("movement_info", {}).get("pos")
            if position and ev.packet.t is not None:
                self._guid = ev.data.get("guid", self._guid)
                self._creates.append((ev.packet.t, tuple(position)))
        elif ev.kind == "party_kill":
            if ev.packet.t is not None:
                self._deaths.append(ev.packet.t)
        elif ev.kind == "respawn_timer":
            self._respawn = ev.data.get("value_min")
        elif ev.kind == "patrol_waypoint":
            self._waypoints.append(dict(ev.data))
        elif ev.kind == "patrol_route":
            self._closes_loop = bool(ev.data.get("closes_loop"))

    def _spawn_sighting(self) -> tuple[tuple[float, ...], bool] | None:
        """The create that followed a death, else the earliest one seen."""
        for death in sorted(self._deaths):
            after = [(t, pos) for t, pos in self._creates if t > death]
            if after:
                return min(after)[1], True
        return (min(self._creates)[1], False) if self._creates else None

    # -- emit --------------------------------------------------------------

    def rows(self, ctx: AuthorContext) -> Iterator[AuthoredRow]:
        sighting = self._spawn_sighting()
        db_guid = (self._guid & GUID_COUNTER_MASK) if self._guid is not None else None

        if sighting and db_guid is not None:
            (x, y, z, o), after_death = sighting
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

            if self._respawn is not None:
                seconds = int(round(self._respawn))
                values["spawntimesecsmin"] = values["spawntimesecsmax"] = seconds
                provenance["spawntimesecsmin"] = provenance["spawntimesecsmax"] = DERIVED
                notes.append(f"respawn {seconds}s from the death-to-create gap "
                             f"({self._respawn:.3f}s observed once; min and max are "
                             "indistinguishable from a single observation)")
            if self._waypoints:
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
            if not after_death:
                notes.append("position is the first sighting, NOT a respawn -- the capture "
                             "holds no death for this creature, so this may be mid-route")
            notes.append("position_z is ground-snapped by the server at runtime and tracks "
                         "the terrain, not the authored value")
            # map is never schema-filled: its default (0) is a real place
            # (Eastern Kingdoms), not neutral boilerplate, and this capture
            # carries no opcode that says which map the creature is on.
            self.fill_schema_defaults(ctx, "creature", values, provenance, notes, skip={"map"})

            yield self.row(values, provenance, notes=tuple(notes))

        if self._waypoints and db_guid is not None:
            points = sorted(self._waypoints, key=lambda w: w["point"])
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
            if self._closes_loop:
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
        if not self._creates:
            yield "creature -- no CREATE block for this entry, so no spawn position was seen"
            return
        yield ("creature.map -- the map id is not in any opcode decoded so far; it arrives "
               "at login (SMSG_LOGIN_VERIFY_WORLD / SMSG_NEW_WORLD)")
        if not self._deaths:
            yield ("creature.spawntimesecsmin/max -- the creature never died in this capture, "
                   "so the respawn timer could not be measured")
        if not self._waypoints:
            yield ("creature_movement -- no closed route was reconstructed; the creature may "
                   "be stationary, or the capture may be too short to see a full lap")
