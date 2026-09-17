"""Reconstruct a creature's patrol route from the movement hops it broadcast.

A patrol arrives as a stream of independent point-to-point hops, repeated for
as long as the capture runs and interrupted whenever the creature is pulled
into combat, dies and respawns. Recovering the authored route means answering
two questions: which distinct waypoints exist, and in what order.

Neither is a trimming problem. Cutting the stream at the first return to its
start (what the prototype did) needs one uninterrupted loop to be present, and
in a real session there often is none -- the Ralthas capture holds 113 hops
over roughly 2.7 laps, with a fight and a respawn in the middle, and not a
single clean lap.

So instead:

  1. **Cluster** hop destinations by proximity. Every lap re-broadcasts the
     same authored position, so one cluster is one waypoint. Positions the
     creature only visited while fighting form their own clusters.
  2. **Count the transitions** between clusters. Each consecutive hop pair is
     one observed edge; the patrol's real edges are seen once per lap, combat
     detours once.
  3. **Walk** from the waypoint nearest the spawn point, always taking the
     most-travelled outgoing edge, until the walk returns to where it began.
     Combat detours drop out on their own: they are never the busiest edge.

Validated against the live `tw_world`: recovers all 41 distinct waypoints of
Ralthas's route, in the authored order, mean XY error 0.006 yards. The
authored table repeats the first point as a 42nd to close the loop, which is
why the recovered count is one lower -- `closes_loop` reports it.

Z is broadcast after the server's runtime ground-snap, so it tracks the
terrain rather than the authored value (~0.35 yd apart at worst). Positions
here are what the creature walked, which is not the same as what an author
typed; see docs/feasibility-ralthas-pr.md.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Any, Iterator, Mapping

from ..core.base import BaseAnalyzer
from ..core.contracts import (
    Column, DecodeContext, Event, Packet, Row, SqlContext, TableSpec,
)
from ..core.registry import analyzer

# Two hops land on "the same" waypoint within this many yards. Comfortably
# above the sub-centimetre spread seen across laps, well below the tens of
# yards between neighbouring waypoints.
CLUSTER_TOLERANCE = 1.0

MIN_WAYPOINTS = 3            # fewer than this is not a route

_TABLE = TableSpec(
    name="capture_patrol_waypoint",
    columns=(
        Column("capture", "VARCHAR(64)", nullable=False),
        Column("guid", "BIGINT UNSIGNED", nullable=False),
        Column("entry", "INT UNSIGNED"),
        Column("point", "INT UNSIGNED", nullable=False),
        Column("position_x", "DOUBLE"),
        Column("position_y", "DOUBLE"),
        Column("position_z", "DOUBLE"),
        Column("observations", "INT UNSIGNED"),
    ),
    key=("capture", "guid", "point"),
    comment="patrol route reconstructed from observed hops; Z is ground-snapped",
)


class _Route:
    """Hops and spawn sightings for one creature, until the stream ends."""

    def __init__(self) -> None:
        self.clusters: list[list[tuple[float, float, float]]] = []
        self.labels: list[int] = []
        self.spawn: tuple[float, float, float] | None = None
        self.entry: int | None = None
        self.last_packet: Packet | None = None

    def add_hop(self, point: tuple[float, float, float]) -> None:
        for index, members in enumerate(self.clusters):
            if math.dist(members[0][:2], point[:2]) <= CLUSTER_TOLERANCE:
                members.append(point)
                self.labels.append(index)
                return
        self.clusters.append([point])
        self.labels.append(len(self.clusters) - 1)

    def centre(self, index: int) -> tuple[float, float, float]:
        """Mean of every observation of a waypoint -- one lap's noise averaged out."""
        members = self.clusters[index]
        n = len(members)
        return (sum(m[0] for m in members) / n,
                sum(m[1] for m in members) / n,
                sum(m[2] for m in members) / n)

    def walk(self) -> list[int]:
        """Cycle of cluster indices, starting nearest the spawn point."""
        if len(self.clusters) < MIN_WAYPOINTS:
            return []

        edges: dict[int, Counter] = defaultdict(Counter)
        for a, b in zip(self.labels, self.labels[1:]):
            if a != b:
                edges[a][b] += 1

        anchor = self._anchor()
        order: list[int] = []
        seen: set[int] = set()
        node: int | None = anchor
        while node is not None and node not in seen:
            seen.add(node)
            order.append(node)
            node = edges[node].most_common(1)[0][0] if edges[node] else None
        self.returns_to_start = bool(order and edges[order[-1]]
                                     and edges[order[-1]].most_common(1)[0][0] == anchor)
        return order

    def _anchor(self) -> int:
        """Start at the spawn point when one was seen, else the busiest waypoint.

        A creature's route is authored starting from where it spawns, so a
        respawn sighting pins the numbering to the same origin the database
        uses instead of wherever the capture happened to begin.
        """
        if self.spawn is not None:
            return min(range(len(self.clusters)),
                       key=lambda i: math.dist(self.clusters[i][0][:2], self.spawn[:2]))
        return max(range(len(self.clusters)), key=lambda i: len(self.clusters[i]))


@analyzer(id="patrol", order=10)
class Patrol(BaseAnalyzer):
    text_section = "Patrol routes (reconstructed)"
    text_templates = {
        "patrol_route": "entry={entry:<7} {count} waypoint(s), closes loop: {closes_loop}, "
                        "from {hops} hop(s)",
        "patrol_waypoint": "  point {point:>3}  ({position_x:11.4f}, {position_y:11.4f}, "
                           "{position_z:9.4f})  seen {observations}x",
    }
    sql_tables = (_TABLE,)

    def __init__(self) -> None:
        self._routes: dict[int, _Route] = {}

    # -- collect -----------------------------------------------------------

    def feed(self, ev: Event) -> None:
        guid = ev.data.get("guid")
        if guid is None:
            return

        if ev.kind == "move_linear":
            route = self._routes.setdefault(guid, _Route())
            route.entry = ev.data.get("entry")
            route.last_packet = ev.packet
            route.add_hop(tuple(ev.data["dest"]))
        elif ev.kind == "object_create":
            # A create block is the creature standing where it spawned -- but
            # only the respawn one is; the first sighting catches it mid-route.
            # Both land here, and the later one wins, which is the respawn.
            position = (ev.data.get("movement") or {}).get("movement_info", {}).get("pos")
            if position:
                route = self._routes.setdefault(guid, _Route())
                route.entry = ev.data.get("entry")
                route.spawn = tuple(position[:3])

    # -- report ------------------------------------------------------------

    def finish(self, ctx: DecodeContext) -> Iterator[Event]:
        for guid, route in sorted(self._routes.items()):
            if route.last_packet is None:
                continue
            order = route.walk()
            if not order:
                ctx.log.debug("entry %s: %d hop cluster(s), too few for a route",
                              route.entry, len(route.clusters))
                continue

            ctx.log.info("entry %s: %d waypoints from %d hops (%d cluster(s) seen)",
                         route.entry, len(order), len(route.labels), len(route.clusters))
            yield self.event(route.last_packet, "patrol_route", guid=guid, entry=route.entry,
                             count=len(order), hops=len(route.labels),
                             closes_loop=getattr(route, "returns_to_start", False))
            for point, index in enumerate(order, start=1):
                x, y, z = route.centre(index)
                yield self.event(route.last_packet, "patrol_waypoint", guid=guid,
                                 entry=route.entry, point=point,
                                 position_x=x, position_y=y, position_z=z,
                                 observations=len(route.clusters[index]))

    # -- sql ---------------------------------------------------------------

    def sql_rows(self, ev: Event, ctx: SqlContext) -> Iterator[Row]:
        if ev.kind != "patrol_waypoint":
            return
        yield Row(_TABLE.name, {"capture": ctx.capture_id, **ev.data})
