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
import random
from collections import Counter, defaultdict
from typing import Any, Iterator, Mapping

from ..core.base import BaseAnalyzer
from ..core.contracts import (
    Column, DecodeContext, Event, Packet, Row, SqlContext, TableSpec, spawn_sighting,
)
from ..core.registry import analyzer

# Two hops land on "the same" waypoint within this many yards. Comfortably
# above the sub-centimetre spread seen across laps, well below the tens of
# yards between neighbouring waypoints.
CLUSTER_TOLERANCE = 1.0

MIN_WAYPOINTS = 3            # fewer than this is not a route

# Above this share of hops seen during an aggro-to-death window, a route is
# more likely combat repositioning across many re-engagements than a real
# patrol -- see combat_hop_fraction()'s docstring and docs/feasibility-
# rakameg-pr.md for the real capture that motivated this.
MAX_COMBAT_HOP_FRACTION = 0.5

# A patrol goes from A to B every time; a random mover goes somewhere different
# each time it leaves a point. That is what this measures, and it replaced a
# revisit count that a real capture proved blind: a wanderer confined to a few
# yards revisits its points as surely as a patrol does, and the revisit gate
# accepted 14 of 20 wanderers in Cow_Elwyn_Forest.pcap as patrols. Order,
# measured on that capture against the server's own spawn rows: 9 real patrols
# 0.82-1.00, 19 wanderers 0.30-0.48; Ralthas's route 0.99, Rakameg's 0.60.
MIN_TRANSITION_ORDER = 0.65

# Below this many hops a small random mover can look ordered by chance --
# simulated, up to 30% of 10-20 hop observations inside 2-5 yards, under 1%
# past 30 at every radius. Watching longer is what earns a verdict.
MIN_HOPS_FOR_ORDER = 30


# -- the smallest circle round a wanderer's destinations -------------------
#
# RandomMovementGenerator picks each destination around the creature's respawn
# point, never farther than its wander_distance (Map.cpp GetWalkRandomPosition:
# a radius drawn in [0, maxRadius], and a navmesh point pulled back to it). So
# the true home is always a valid centre for a circle of radius
# wander_distance, and the smallest enclosing circle can only be that small or
# smaller: a lower bound that tightens as destinations reach the edge.

def _circle_on(a, b):
    centre = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    return centre, math.dist(a, centre)


def _circle_through(a, b, c):
    (ax, ay), (bx, by), (cx, cy) = a, b, c
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        # Collinear: no circle passes through all three, and the smallest one
        # holding them is the circle on the two that lie farthest apart.
        return max((_circle_on(p, q) for p, q in ((a, b), (a, c), (b, c))), key=lambda t: t[1])
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay)
          + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx)
          + (cx * cx + cy * cy) * (bx - ax)) / d
    return (ux, uy), math.dist((ux, uy), a)


def _inside(circle, p) -> bool:
    return math.dist(circle[0], p) <= circle[1] + 1e-7


def enclosing_circle(points) -> tuple[tuple[float, float], float]:
    """Smallest circle holding every (x, y): Welzl's incremental method.

    Correct for any order of the points; the fixed shuffle is only what keeps
    its expected cost linear and its answer repeatable.
    """
    pts = list(points)
    random.Random(0).shuffle(pts)
    circle = (pts[0], 0.0)
    for i, p in enumerate(pts):
        if _inside(circle, p):
            continue
        circle = (p, 0.0)
        for j in range(i):
            if _inside(circle, pts[j]):
                continue
            circle = _circle_on(p, pts[j])
            for k in range(j):
                if not _inside(circle, pts[k]):
                    circle = _circle_through(p, pts[j], pts[k])
    return circle

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
        self.hop_times: list[float] = []            # packet time per hop, parallel to labels
        self.creates: list[tuple[float, tuple[float, float, float]]] = []   # (t, position)
        self.entry: int | None = None
        self.last_packet: Packet | None = None
        self.engagements: list[float] = []          # aggro-trigger timestamps (ai_reaction)
        self.deaths: list[float] = []                # party_kill timestamps

    def add_hop(self, point: tuple[float, float, float], t: float) -> None:
        self.hop_times.append(t)
        for index, members in enumerate(self.clusters):
            if math.dist(members[0][:2], point[:2]) <= CLUSTER_TOLERANCE:
                members.append(point)
                self.labels.append(index)
                return
        self.clusters.append([point])
        self.labels.append(len(self.clusters) - 1)

    def combat_hop_fraction(self) -> float:
        """Share of hops whose timestamp falls inside an aggro-to-death window.

        Mirrors `analyze/behaviour.py`'s own "engagement" (aggro to the death
        that follows it) rather than sharing state with it -- analyzers each
        keep their own view of the same stream. An aggro with no later death
        (still fighting when the capture ends) leaves its window open, which
        only ever overcounts combat time -- the safe direction for a
        confidence check that exists to catch overcounting in the first place.
        """
        if not self.hop_times:
            return 0.0
        deaths = sorted(self.deaths)
        windows = []
        for start in sorted(self.engagements):
            after = [d for d in deaths if d > start]
            windows.append((start, min(after) if after else math.inf))
        if not windows:
            return 0.0
        in_combat = sum(1 for t in self.hop_times
                        if any(start <= t <= end for start, end in windows))
        return in_combat / len(self.hop_times)

    def single_visit_fraction(self, order: list[int]) -> float:
        """Share of the walked waypoints that were only ever seen once.

        Reported, no longer a gate: a real capture showed a wanderer confined
        to a few yards scores 0% here exactly as a patrol does, and scoping it
        to the walked waypoints made that worse, since the walk follows the
        busiest clusters by construction. See transition_order().
        """
        if not order:
            return 0.0
        once = sum(1 for index in order if len(self.clusters[index]) == 1)
        return once / len(order)

    def transition_order(self) -> float | None:
        """How consistently each point is left for the same next point.

        Counted only over points left at least twice: a point left once has one
        successor, which is its commonest by definition, and says nothing about
        order either way. None when no point has been left twice -- then there
        is no evidence of order at all, which is not the same as evidence of
        disorder.
        """
        successors: dict[int, Counter] = defaultdict(Counter)
        for a, b in zip(self.labels, self.labels[1:]):
            if a != b:
                successors[a][b] += 1
        revisited = [c for c in successors.values() if sum(c.values()) >= 2]
        total = sum(sum(c.values()) for c in revisited)
        if not total:
            return None
        return sum(max(c.values()) for c in revisited) / total

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
        """Start at the spawn sighting when one was seen, else the busiest waypoint.

        A creature's route is authored starting from where it spawns, so a
        respawn sighting pins the numbering to the same origin the database
        uses instead of wherever the capture happened to begin. Without a
        death the sighting is the first one -- where the spawn row stands
        too, so the route at least starts where the authored creature does.
        """
        sighting = spawn_sighting(self.creates, self.deaths)
        if sighting is not None:
            spawn = sighting[0]
            return min(range(len(self.clusters)),
                       key=lambda i: math.dist(self.clusters[i][0][:2], spawn[:2]))
        return max(range(len(self.clusters)), key=lambda i: len(self.clusters[i]))


@analyzer(id="patrol", order=10)
class Patrol(BaseAnalyzer):
    text_section = "Patrol routes (reconstructed)"
    text_templates = {
        "patrol_route": "entry={entry:<7} {count} waypoint(s), closes loop: {closes_loop}, "
                        "from {hops} hop(s)",
        "patrol_waypoint": "  point {point:>3}  ({position_x:11.4f}, {position_y:11.4f}, "
                           "{position_z:9.4f})  seen {observations}x",
        "wander_area": "entry={entry:<7} wanders within {radius:.2f} yd of ({position_x:.2f}, "
                       "{position_y:.2f}), from {hops} hop(s)",
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
            if ev.packet.t is None:
                return
            route = self._routes.setdefault(guid, _Route())
            route.entry = ev.data.get("entry")
            route.last_packet = ev.packet
            route.add_hop(tuple(ev.data["dest"]), ev.packet.t)
        elif ev.kind == "object_create":
            # A create block is the creature standing where it spawned -- but
            # only the respawn one is; the first sighting catches it mid-route.
            # spawn_sighting() tells them apart once the deaths are known too.
            position = (ev.data.get("movement") or {}).get("movement_info", {}).get("pos")
            if position and ev.packet.t is not None:
                route = self._routes.setdefault(guid, _Route())
                route.entry = ev.data.get("entry")
                route.creates.append((ev.packet.t, tuple(position[:3])))
        elif ev.kind == "ai_reaction" and ev.data.get("reaction") == 2 and ev.packet.t is not None:
            self._routes.setdefault(guid, _Route()).engagements.append(ev.packet.t)
        elif ev.kind == "party_kill" and ev.packet.t is not None:
            self._routes.setdefault(guid, _Route()).deaths.append(ev.packet.t)

    # -- report ------------------------------------------------------------

    def finish(self, ctx: DecodeContext) -> Iterator[Event]:
        for guid, route in sorted(self._routes.items()):
            if route.last_packet is None:
                continue
            if route.entry is None:
                # SMSG_MONSTER_MOVE also carries forced player movement
                # (knockback etc.); a non-entry-bearing guid has no
                # creature_movement row to reconstruct, so it is not a
                # patrol at all -- not a route missing a label.
                continue
            order = route.walk()
            if not order:
                ctx.log.debug("entry %s: %d hop cluster(s), too few for a route",
                              route.entry, len(route.clusters))
                continue

            combat_fraction = route.combat_hop_fraction()
            single_visit = route.single_visit_fraction(order)
            ordered = route.transition_order()
            # Why a route is not trusted, in the order the reasons dominate. Only
            # "unordered" is positive evidence of anything: watched long enough,
            # outside combat, and still going somewhere different from each point.
            if combat_fraction > MAX_COMBAT_HOP_FRACTION:
                refused = "combat"
            elif len(route.labels) < MIN_HOPS_FOR_ORDER:
                refused = "short"
            elif ordered is None:
                refused = "unrevisited"     # e.g. a long route seen for under a lap
            elif ordered < MIN_TRANSITION_ORDER:
                refused = "unordered"
            else:
                refused = None
            confident = refused is None
            ctx.log.info("entry %s: %d waypoints from %d hops (%d cluster(s) seen, "
                         "%.0f%% during combat, order %s)",
                         route.entry, len(order), len(route.labels), len(route.clusters),
                         combat_fraction * 100, "-" if ordered is None else f"{ordered:.2f}")
            yield self.event(route.last_packet, "patrol_route", guid=guid, entry=route.entry,
                             count=len(order), hops=len(route.labels),
                             closes_loop=getattr(route, "returns_to_start", False),
                             combat_hop_fraction=combat_fraction,
                             single_visit_fraction=single_visit,
                             transition_order=ordered, confident=confident,
                             refused_because=refused)
            if refused == "unordered":
                points = [p for members in route.clusters for p in members]
                (cx, cy), radius = enclosing_circle((p[0], p[1]) for p in points)
                # The centre is a 2-D answer; its Z is the destination nearest to
                # it, a real ground sample rather than an average that could hang
                # in the air above a slope.
                cz = min(points, key=lambda p: math.dist((p[0], p[1]), (cx, cy)))[2]
                ctx.log.info("entry %s: wanders within %.2f yd of (%.2f, %.2f)",
                             route.entry, radius, cx, cy)
                yield self.event(route.last_packet, "wander_area", guid=guid, entry=route.entry,
                                 position_x=cx, position_y=cy, position_z=cz,
                                 radius=radius, hops=len(route.labels))
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
