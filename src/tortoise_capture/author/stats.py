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
within `AGREEMENT_TOLERANCE` is left alone** -- the capture confirms it rather
than revising it, and the migration says so instead of proposing churn. Without
a database there is nothing to compare against, and the broadcast value is
emitted with that caveat attached.

Integers do not have this problem: level, attack power, unit class and the rest
match the database exactly.

Floats are written with nine significant digits, the IEEE guarantee for a
float32 round trip, so a value that does get proposed lands in the column
bit-identically to what came off the wire.
"""

from __future__ import annotations

from typing import Any, Iterator

from ..core.base import BaseAuthorRule
from ..core.contracts import CONVENTION, WIRE, AuthorContext, AuthoredRow, Event
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
        self._saw_spells = False

    def handle(self, ev: Event, mod: Any = None) -> None:
        if ev.kind == "object_create":
            # First CREATE wins: a creature's static stats never change after
            # it is created, and later blocks only carry what moved.
            for field in ev.data.get("fields", ()):
                if field["name"] and field["name"] not in self._fields:
                    self._fields[field["name"]] = field["raw"]
        elif ev.kind == "spell_go":
            self._saw_spells = True

    def _typed(self, name: str) -> Any:
        value = fv.decode(name, self._fields[name])
        return self.wire_float(value) if isinstance(value, float) else value

    def _agrees(self, ctx: AuthorContext, column: str, observed: Any) -> bool:
        """True when the database already holds this value, allowing for drift."""
        if ctx.world is None:
            return False
        stored = ctx.world.column("creature_template", column, f"entry = {ctx.entry}")
        if stored is None:
            return False
        try:
            stored_value = float(stored)
        except ValueError:
            return False
        if isinstance(observed, float):
            scale = max(abs(stored_value), abs(observed), 1.0)
            return abs(stored_value - observed) / scale <= AGREEMENT_TOLERANCE
        return stored_value == float(observed)

    def rows(self, ctx: AuthorContext) -> Iterator[AuthoredRow]:
        values: dict[str, Any] = {}
        provenance: dict[str, str] = {}
        confirmed: list[str] = []

        for name, column in _STATS.items():
            if name not in self._fields:
                continue
            observed = self._typed(name)
            if self._agrees(ctx, column, observed):
                confirmed.append(column)
                continue
            values[column] = observed
            provenance[column] = WIRE

        if "UNIT_FIELD_BYTES_0" in self._fields:
            unit_class = fv.bytes_0(self._fields["UNIT_FIELD_BYTES_0"])["class"]
            if self._agrees(ctx, "unit_class", unit_class):
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
            notes.append("already correct in the database, so not re-stated: "
                         + ", ".join(sorted(confirmed)))
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
        elif confirmed:
            ctx.log.info("creature_template for entry %d already matches the capture "
                         "in every column it could confirm", ctx.entry)

    def gaps(self, ctx: AuthorContext) -> Iterator[str]:
        if not self._fields:
            yield ("creature_template -- no CREATE block for this entry in the capture, "
                   "so no stats were broadcast to read")
        elif ctx.world is None:
            yield ("creature_template level/health/mana not checked -- no database "
                   "configured to compare against")
