"""creature_template: the combat stats a CREATE block broadcasts.

Every value here is read straight out of `SMSG_UPDATE_OBJECT`'s field payload
at first sighting -- or, for walk and run speed, its movement block, over the
server's base speeds -- with no correlation and no reconstruction. The stats are broadcast
to every observer (this core has no per-viewer field masking), so a passive
capture sees them exactly as the server does.

That makes this the most direct section in the migration, but not an
unconditional one.

Two things to know before trusting the float columns.

**The wire carries the server's computed value, not the authored one.** Damage
is broadcast after the stat system has run, and that path accumulates float
error: for Ralthas the database holds `dmg_min = 20.2118873596` while the wire
says `20.2119007111`, about three parts in a million apart. Round-tripping a
capture into the same column would therefore nudge the authored value every
time, and enough cycles would visibly drift it.

So when a database is reachable, **a column whose stored value already agrees
within `AGREEMENT_TOLERANCE` is restated as the database's own value, not the
wire's** -- `provenance=CONFIRMED` rather than `WIRE`. Restating it (instead of
omitting it) keeps the `UPDATE`'s shape complete, matching a hand-authored
migration's -- an earlier version of this rule omitted confirmed columns
entirely, which left the statement looking incomplete next to one. Using the
database's value rather than the wire's keeps that restatement a genuine
no-op: applying it cannot introduce the ~3e-6 drift a straight round-trip of
the broadcast value would. Without a database there is nothing to compare
against, and the broadcast value is proposed with that caveat attached.

Integers do not have this problem: attack power, unit class and the rest match
the database exactly.

**Level, health and mana are one spawn's roll, not the template.**
`Creature::SelectLevel` (Creature.cpp:1576-1612) rolls each spawn's level from
`level_min..level_max` and derives its health and mana from where that level
falls in the range (the per-rank rates are 1 on this core, mangosd.conf.dist),
so one spawn can confirm or contradict a stored range but pins at most one end
of it -- and an observed range can only be narrower than the authored one. They
are therefore checked against the database spawn by spawn and reported, never
proposed. Against a fully migrated tw_world the check holds exactly: every
spawn of all 58 creature kinds in the three test captures lands on its
template's level range, health and mana at its own level. `sql/base` alone does
not -- it predates the migration that replaces `creature_template` wholesale
(20260510092659), and compared against it most creatures look x1.1 off.

Floats are written with nine significant digits, the IEEE guarantee for a
float32 round trip, so a value that does get proposed lands in the column
bit-identically to what came off the wire.
"""

from __future__ import annotations

import struct
from typing import Any, Iterator

from ..core.base import BaseAuthorRule
from ..core.contracts import (
    CONFIRMED, CONVENTION, DERIVED, WIRE, AuthorContext, AuthoredRow, Event,
)
from ..core.registry import author_rule
from ..fields import values as fv

# A stored value this close to the broadcast one is the same value seen through
# the server's stat arithmetic. Observed drift is ~3e-6 relative; a real
# authoring change is orders of magnitude larger, so this sits well between.
AGREEMENT_TOLERANCE = 1e-4

DEFAULT_OBJECT_SCALE = 1.0      # what a scale of 0 falls back to without a model (ObjectMgr.cpp:1442)

# UpdateField name -> creature_template column.
_STATS = {
    "OBJECT_FIELD_SCALE_X": "scale",
    "UNIT_FIELD_MINDAMAGE": "dmg_min",
    "UNIT_FIELD_MAXDAMAGE": "dmg_max",
    "UNIT_FIELD_ATTACK_POWER": "attack_power",
    "UNIT_FIELD_MINRANGEDDAMAGE": "ranged_dmg_min",
    "UNIT_FIELD_MAXRANGEDDAMAGE": "ranged_dmg_max",
    "UNIT_FIELD_RANGED_ATTACK_POWER": "ranged_attack_power",
}

# Broadcast per spawn, a template's own business: checked, never proposed.
# The BASE_ fields are SelectLevel's own output, which auras never touch.
_CHECKED = {
    "UNIT_FIELD_LEVEL": ("level_min", "level_max"),
    "UNIT_FIELD_BASE_HEALTH": ("health_min", "health_max"),
    "UNIT_FIELD_BASE_MANA": ("mana_min", "mana_max"),
}
_ROLLED = ("UNIT_FIELD_BASE_HEALTH", "UNIT_FIELD_BASE_MANA")

# creature_template column -> (index in a CREATE's six speeds, base speed). The
# server multiplies the template's rate into baseMoveSpeed (Unit.cpp:7671-7674,
# :76-84): every one of 58 creature kinds in the test captures broadcasts exactly
# that -- 299 spawns checked against a live tw_world, five custom kinds against
# the migrations that author them.
_SPEEDS = {"speed_walk": (0, 2.5), "speed_run": (1, 7.0)}


def _shown(values: list) -> str:
    return ", ".join(f"{v:g}" if isinstance(v, float) else str(v) for v in values)


def _agrees(stored: float, observed: float) -> bool:
    scale = max(abs(stored), abs(observed), 1.0)
    return abs(stored - observed) / scale <= AGREEMENT_TOLERANCE


def _f32(value: float) -> float:
    return struct.unpack("<f", struct.pack("<f", value))[0]


def _at_level(level: int, levels: tuple[int, int], stored: tuple[int, int]) -> int:
    """SelectLevel's health or mana for a spawn at `level`, in its float32
    arithmetic -- at rate 1, the two share one formula."""
    (lo, hi), (least, most) = levels, stored
    if lo == hi:
        return least
    rellevel = _f32((level - lo) / (hi - lo))
    return least + int(_f32(rellevel * (most - least)))


@author_rule(id="stats", table="creature_template", order=50)
class Stats(BaseAuthorRule):
    def __init__(self) -> None:
        self._fields: dict[str, int] = {}
        self._speeds: dict[str, float] = {}                 # column -> first CREATE's speed
        self._spawns: dict[int, dict[str, int]] = {}       # guid -> its own first CREATE
        self._disagreed: dict[str, set[tuple[int, int]]] = {}  # name -> (first, later)
        self._saw_spells = False

    def handle(self, ev: Event, mod: Any = None) -> None:
        if ev.kind == "object_create":
            # First CREATE wins: a creature's static stats never change after
            # it is created, and later blocks only carry what moved. That holds
            # only if the first sighting caught the creature unmodified, which
            # nothing here can guarantee -- so a later CREATE that disagrees is
            # kept and reported rather than dropped on the floor.
            #
            # "Later" means the same spawn's: each spawn rolls its own level
            # from the template's range (Creature::SelectLevel), so two spawns
            # of one entry disagreeing is the template, not a modified creature.
            first = self._spawns.setdefault(ev.data.get("guid"), {})
            for field in ev.data.get("fields", ()):
                name = field["name"]
                if not name:
                    continue
                self._fields.setdefault(name, field["raw"])
                if name not in first:
                    first[name] = field["raw"]
                elif field["raw"] != first[name] and (name in _STATS or name in _CHECKED):
                    # Only fields that feed an authored column: UNIT_FIELD_FLAGS
                    # and friends move between sightings by design (in combat,
                    # and so on) and say nothing about the stats being authored.
                    self._disagreed.setdefault(name, set()).add((first[name], field["raw"]))
            speeds = (ev.data.get("movement") or {}).get("speeds")
            for column, (index, _) in _SPEEDS.items() if speeds else ():
                self._speeds.setdefault(column, speeds[index])
                if column not in first:
                    first[column] = speeds[index]
                elif speeds[index] != first[column]:
                    self._disagreed.setdefault(column, set()).add((first[column], speeds[index]))
        elif ev.kind == "spell_go":
            self._saw_spells = True

    def _typed(self, name: str) -> Any:
        value = fv.decode(name, self._fields[name])
        return self.wire_float(value) if isinstance(value, float) else value

    def _confirmed_value(self, ctx: AuthorContext, column: str, observed: Any) -> Any | None:
        """The database's own (full-precision) value, if it agrees with the
        wire within drift tolerance -- None when it disagrees or is unknown.

        Deliberately reads via `numeric_column` (a wide-DECIMAL CAST), not the
        plain string `column()` would give: a plain SELECT of a FLOAT column
        truncates to ~6 significant digits, which is *less* precise than the
        wire value it would be compared against -- restating that truncated
        text would be a regression, not a no-op.
        """
        if ctx.world is None:
            return None
        stored_value = ctx.world.numeric_column("creature_template", column,
                                                 f"entry = {ctx.entry}")
        if stored_value is None:
            return None
        if isinstance(observed, float):
            return self.wire_float(stored_value) if _agrees(stored_value, observed) else None
        if stored_value != float(observed):
            return None
        return int(round(stored_value))

    def _zero_scale(self, ctx: AuthorContext,
                    observed: float) -> tuple[float | None, str | None, str] | None:
        """A stored scale of 0 is not a scale but "the model's own": at load
        the server gives it the scale of the first of the template's display
        ids that CreatureDisplayInfo.dbc knows, else 1 (ObjectMgr.cpp:1295-1306,
        1436-1443). The wire carries that resolution, so it is checked against
        the model, and a 0 it agrees with is restated, never pinned over.

        None when the stored scale is not 0 and the ordinary comparison
        applies; otherwise (value, provenance, note), value None to leave the
        column out.
        """
        if ctx.world is None:
            return None
        where = f"entry = {ctx.entry}"
        stored = ctx.world.numeric_column("creature_template", "scale", where)
        if stored is None or stored > 0:
            return None
        if ctx.displays is None:
            return None, None, (f"scale left out: the database stores {stored:g}, the model's "
                                "own scale, and no CreatureDisplayInfo.dbc was configured to "
                                f"check the wire's {observed:g} against")

        model = DEFAULT_OBJECT_SCALE
        source = "no display id of the template is in CreatureDisplayInfo.dbc, so the default"
        for slot in range(1, 5):
            display = ctx.world.numeric_column("creature_template", f"display_id{slot}", where)
            if display and (found := ctx.displays.scale(int(display))) is not None:
                model = found
                source = f"display {int(display)} is {found:g} in CreatureDisplayInfo.dbc"
                break
        if _agrees(model, observed):
            return self.wire_float(stored), CONFIRMED, (
                f"scale {stored:g} is the model's own, as the wire says: {source}")
        return observed, WIRE, (f"scale: the database's {stored:g} resolves to {model:g} "
                                f"({source}), the wire says {observed:g}")

    def rows(self, ctx: AuthorContext) -> Iterator[AuthoredRow]:
        values: dict[str, Any] = {}
        provenance: dict[str, str] = {}
        confirmed: list[str] = []
        scale_note = None

        for name, column in _STATS.items():
            if name not in self._fields:
                continue
            observed = self._typed(name)
            if column == "scale" and (zero := self._zero_scale(ctx, observed)) is not None:
                value, source, scale_note = zero
                if value is not None:
                    values[column], provenance[column] = value, source
                    if source == CONFIRMED:
                        confirmed.append(column)
                continue
            stored = self._confirmed_value(ctx, column, observed)
            if stored is not None:
                values[column] = stored
                provenance[column] = CONFIRMED
                confirmed.append(column)
            else:
                values[column] = observed
                provenance[column] = WIRE

        for column, (_, base) in _SPEEDS.items():
            if column not in self._speeds:
                continue
            observed = self.wire_float(self._speeds[column] / base)
            stored = self._confirmed_value(ctx, column, observed)
            if stored is not None:
                values[column], provenance[column] = stored, CONFIRMED
                confirmed.append(column)
            else:
                values[column], provenance[column] = observed, DERIVED

        if "UNIT_FIELD_BYTES_0" in self._fields:
            unit_class = fv.bytes_0(self._fields["UNIT_FIELD_BYTES_0"])["class"]
            stored = self._confirmed_value(ctx, "unit_class", unit_class)
            if stored is not None:
                values["unit_class"] = stored
                provenance["unit_class"] = CONFIRMED
                confirmed.append("unit_class")
            else:
                values["unit_class"] = unit_class
                provenance["unit_class"] = WIRE

        # spell_list_id points a creature at its creature_spells row, so it
        # only belongs here when there is one to point at -- and never over a
        # list the template already has (see gaps()).
        own = self._own_spell_list(ctx) if self._saw_spells else None
        if self._saw_spells and own in (None, ctx.entry):
            values["spell_list_id"] = ctx.entry
            provenance["spell_list_id"] = CONFIRMED if own else CONVENTION
            if own:
                confirmed.append("spell_list_id")

        notes = []
        if confirmed:
            notes.append("restated as the database's own value, a no-op -- already agreed "
                         "with the wire within drift tolerance: " + ", ".join(sorted(confirmed)))
        if scale_note:
            notes.append(scale_note)
        levels = sorted({spawn["UNIT_FIELD_LEVEL"] for spawn in self._spawns.values()
                         if "UNIT_FIELD_LEVEL" in spawn})
        if len(levels) > 1:
            spawned = sum("UNIT_FIELD_LEVEL" in spawn for spawn in self._spawns.values())
            notes.append(f"UNIT_FIELD_LEVEL seen as {levels[0]}-{levels[-1]} across {spawned} "
                         "spawns, each rolling its own; level_min/level_max span at least "
                         "this, and are not proposed from it -- an observed range can only "
                         "be narrower than the authored one")
        if ctx.world is None:
            notes.append("float stats are the server's computed broadcast values, which "
                         "drift ~3e-6 from the authored ones; no database was available to "
                         "compare against, so applying these would nudge the stored values")

        if values:
            yield self.row(values, provenance, statement="update",
                           where={"entry": ctx.entry}, notes=tuple(notes))

    def gaps(self, ctx: AuthorContext) -> Iterator[str]:
        if not self._fields:
            yield ("creature_template -- no CREATE block for this entry in the capture, "
                   "so no stats were broadcast to read")
        elif ctx.world is None:
            yield ("creature_template level/health/mana not checked -- no database "
                   "configured to compare against")
        else:
            yield from self._checked(ctx)
        if self._saw_spells and (own := self._own_spell_list(ctx)) not in (None, ctx.entry):
            yield (f"creature_template.spell_list_id -- the template already points at spell "
                   f"list {own}; the spells this capture saw are proposed as creature_spells "
                   f"{ctx.entry}, so compare the two and merge by hand -- repointing it would "
                   "drop every spell the capture did not happen to see")

        for name, pairs in sorted(self._disagreed.items()):
            column = _STATS.get(name) or _CHECKED.get(name, (name,))[0]
            firsts = sorted({first for first, _ in pairs})
            laters = sorted({later for _, later in pairs})
            yield (f"creature_template.{column} -- a later CREATE of the same spawn broadcast "
                   f"{name} as {_shown(laters)} where its first said {_shown(firsts)}; "
                   "the first sighting was used, but a creature whose stats move between "
                   "sightings was not in its authored state in at least one of them")

    def _own_spell_list(self, ctx: AuthorContext) -> int | None:
        """The spell list the template already points at, if any."""
        if ctx.world is None:
            return None
        stored = ctx.world.numeric_column("creature_template", "spell_list_id",
                                          f"entry = {ctx.entry}")
        return int(stored) if stored else None

    def _stored_range(self, ctx: AuthorContext, columns: tuple[str, str]) -> tuple[int, int] | None:
        stored = [ctx.world.numeric_column("creature_template", column, f"entry = {ctx.entry}")
                  for column in columns]
        if None in stored:
            return None
        return tuple(sorted(int(value) for value in stored))

    def _checked(self, ctx: AuthorContext) -> Iterator[str]:
        """Each spawn's level, health and mana against the database's ranges."""
        spawns = [spawn for spawn in self._spawns.values() if "UNIT_FIELD_LEVEL" in spawn]
        levels = self._stored_range(ctx, _CHECKED["UNIT_FIELD_LEVEL"])
        if not spawns or levels is None:
            return
        outside = sorted({spawn["UNIT_FIELD_LEVEL"] for spawn in spawns
                          if not levels[0] <= spawn["UNIT_FIELD_LEVEL"] <= levels[1]})
        if outside:
            yield (f"creature_template.level_min/level_max -- spawns broadcast level "
                   f"{outside}, outside the database's {levels[0]}-{levels[1]}; not "
                   "proposed, an observed range can only be narrower than the authored one")
            return      # health and mana follow the level: a wrong range checks neither

        for name in _ROLLED:
            columns = _CHECKED[name]
            stored = self._stored_range(ctx, columns)
            if stored is None:
                continue
            off = sorted({(spawn["UNIT_FIELD_LEVEL"], spawn[name]) for spawn in spawns
                          if name in spawn
                          and spawn[name] != _at_level(spawn["UNIT_FIELD_LEVEL"], levels, stored)})
            if off:
                seen = ", ".join(f"{value} at level {level}" for level, value in off)
                yield (f"creature_template.{columns[0]}/{columns[1]} -- spawns broadcast "
                       f"{name} {seen}, off the database's {stored[0]}-{stored[1]} over "
                       f"levels {levels[0]}-{levels[1]}; not proposed, one spawn pins at "
                       "most one end of the range")
