"""creature_template: the combat stats a CREATE block broadcasts.

Every value here is read straight out of `SMSG_UPDATE_OBJECT`'s field payload
at first sighting -- no correlation, no reconstruction. The stats are broadcast
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

Integers do not have this problem: level, attack power, unit class and the rest
match the database exactly.

Floats are written with nine significant digits, the IEEE guarantee for a
float32 round trip, so a value that does get proposed lands in the column
bit-identically to what came off the wire.
"""

from __future__ import annotations

from typing import Any, Iterator

from ..core.base import BaseAuthorRule
from ..core.contracts import CONFIRMED, CONVENTION, WIRE, AuthorContext, AuthoredRow, Event
from ..core.registry import author_rule
from ..fields import values as fv

# A stored value this close to the broadcast one is the same value seen through
# the server's stat arithmetic. Observed drift is ~3e-6 relative; a real
# authoring change is orders of magnitude larger, so this sits well between.
AGREEMENT_TOLERANCE = 1e-4

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

# Broadcast, but a template's own business: proposed only on disagreement.
_CHECKED = {
    "UNIT_FIELD_LEVEL": ("level_min", "level_max"),
    "UNIT_FIELD_MAXHEALTH": ("health_min", "health_max"),
    "UNIT_FIELD_BASE_MANA": ("mana_min", "mana_max"),
}


@author_rule(id="stats", table="creature_template", order=50)
class Stats(BaseAuthorRule):
    def __init__(self) -> None:
        self._fields: dict[str, int] = {}
        self._disagreed: dict[str, set[int]] = {}
        self._saw_spells = False

    def handle(self, ev: Event, mod: Any = None) -> None:
        if ev.kind == "object_create":
            # First CREATE wins: a creature's static stats never change after
            # it is created, and later blocks only carry what moved. That holds
            # only if the first sighting caught the creature unmodified, which
            # nothing here can guarantee -- so a later CREATE that disagrees is
            # kept and reported rather than dropped on the floor.
            for field in ev.data.get("fields", ()):
                name = field["name"]
                if not name:
                    continue
                if name not in self._fields:
                    self._fields[name] = field["raw"]
                elif field["raw"] != self._fields[name] and (name in _STATS or name in _CHECKED):
                    # Only fields that feed an authored column: UNIT_FIELD_FLAGS
                    # and friends move between sightings by design (in combat,
                    # and so on) and say nothing about the stats being authored.
                    self._disagreed.setdefault(name, set()).add(field["raw"])
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
            scale = max(abs(stored_value), abs(observed), 1.0)
            if abs(stored_value - observed) / scale > AGREEMENT_TOLERANCE:
                return None
            return self.wire_float(stored_value)
        if stored_value != float(observed):
            return None
        return int(round(stored_value))

    def rows(self, ctx: AuthorContext) -> Iterator[AuthoredRow]:
        values: dict[str, Any] = {}
        provenance: dict[str, str] = {}
        confirmed: list[str] = []

        for name, column in _STATS.items():
            if name not in self._fields:
                continue
            observed = self._typed(name)
            stored = self._confirmed_value(ctx, column, observed)
            if stored is not None:
                values[column] = stored
                provenance[column] = CONFIRMED
                confirmed.append(column)
            else:
                values[column] = observed
                provenance[column] = WIRE

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
        # only belongs here when there is one to point at.
        if self._saw_spells:
            values["spell_list_id"] = ctx.entry
            provenance["spell_list_id"] = CONVENTION

        notes = []
        if confirmed:
            notes.append("restated as the database's own value, a no-op -- already agreed "
                         "with the wire within drift tolerance: " + ", ".join(sorted(confirmed)))
        if ctx.world is None:
            notes.append("float stats are the server's computed broadcast values, which "
                         "drift ~3e-6 from the authored ones; no database was available to "
                         "compare against, so applying these would nudge the stored values")
        for name, columns in _CHECKED.items():
            if name not in self._fields or ctx.world is None:
                continue
            observed = self._fields[name]
            current = ctx.world.column("creature_template", columns[0], f"entry = {ctx.entry}")
            if current is not None and str(observed) != str(int(float(current))):
                for column in columns:
                    values[column] = observed
                    provenance[column] = WIRE
                notes.append(f"{columns[0]}/{columns[1]}: capture says {observed}, "
                             f"database has {current}")

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

        for name, others in sorted(self._disagreed.items()):
            column = _STATS.get(name) or _CHECKED[name][0]
            yield (f"creature_template.{column} -- a later CREATE for this entry broadcast "
                   f"{name} as {sorted(others)} where the first said {self._fields[name]}; "
                   "the first sighting was used, but a creature whose stats move between "
                   "sightings was not in its authored state in at least one of them")
