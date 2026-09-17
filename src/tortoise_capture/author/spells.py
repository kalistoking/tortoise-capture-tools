"""creature_spells: which spells, and the timings a capture can and cannot give.

The spell ids are read straight off `SMSG_SPELL_GO`, and the delay from
engagement to first cast is a clean measurement. The repeat delay is not, and
this rule refuses to invent it.

`delayRepeatMin/Max` is an authored *range* the server draws from. What a
capture observes is that draw plus cast time, global cooldown and whatever the
target was doing, and a fight that produced one interval has bounded nothing:
the Ralthas session yields a single 18.291 s gap where the hand-authored values
are 12 and 17. Emitting `12` and `17`-shaped numbers from that would be
fabrication, so the columns are omitted and the observation is reported as a
gap with its sample count. Enough samples and the range becomes defensible --
that is a longer capture's job, not a cleverer decoder's.

`castTarget` is a different kind of missing: the `SMSG_SPELL_GO` target block
has a known layout, it simply is not decoded yet.
"""

from __future__ import annotations

from typing import Any, Iterator

from ..core.base import BaseAuthorRule
from ..core.contracts import CONVENTION, DERIVED, WIRE, AuthorContext, AuthoredRow, Event
from ..core.registry import author_rule

MAX_SLOTS = 8            # creature_spells has spellId_1 .. spellId_8
DEFAULT_PROBABILITY = 100


@author_rule(id="spells", table="creature_spells", order=80)
class Spells(BaseAuthorRule):
    def __init__(self) -> None:
        self._casts: dict[int, int] = {}          # spell id -> times seen
        self._initial: dict[int, float] = {}      # spell id -> engage-to-first-cast
        self._repeat: dict[int, dict[str, Any]] = {}
        self._name: str | None = None

    def handle(self, ev: Event, mod: Any = None) -> None:
        if ev.kind == "spell_go":
            spell = ev.data["spell_id"]
            self._casts[spell] = self._casts.get(spell, 0) + 1
        elif ev.kind == "spell_initial_delay":
            self._initial[int(ev.data["subject"])] = ev.data["value_min"]
        elif ev.kind == "spell_repeat_delay":
            self._repeat[int(ev.data["subject"])] = dict(ev.data)
        elif ev.kind == "creature_query":
            self._name = ev.data.get("name")

    def rows(self, ctx: AuthorContext) -> Iterator[AuthoredRow]:
        if not self._casts:
            return
        spells = sorted(self._casts, key=lambda s: -self._casts[s])[:MAX_SLOTS]

        values: dict[str, Any] = {"entry": ctx.entry, "name": self._name or str(ctx.entry)}
        provenance: dict[str, str] = {"entry": WIRE, "name": WIRE}
        notes: list[str] = []

        for slot, spell in enumerate(spells, start=1):
            values[f"spellId_{slot}"] = spell
            provenance[f"spellId_{slot}"] = WIRE
            values[f"probability_{slot}"] = DEFAULT_PROBABILITY
            provenance[f"probability_{slot}"] = CONVENTION

            if spell in self._initial:
                seconds = int(round(self._initial[spell]))
                values[f"delayInitialMin_{slot}"] = seconds
                values[f"delayInitialMax_{slot}"] = seconds
                provenance[f"delayInitialMin_{slot}"] = DERIVED
                provenance[f"delayInitialMax_{slot}"] = DERIVED
                notes.append(f"spell {spell}: first cast {self._initial[spell]:.3f}s after "
                             f"engagement, so delayInitial rounds to {seconds}")

        notes.append(f"{len(spells)} spell(s) seen cast; a spell never used during the "
                     "capture cannot appear here at all")
        yield self.row(values, provenance, notes=tuple(notes))

    def gaps(self, ctx: AuthorContext) -> Iterator[str]:
        for spell in sorted(self._casts):
            observed = self._repeat.get(spell)
            if observed and not observed.get("confident"):
                yield (f"creature_spells.delayRepeatMin/Max for spell {spell} -- "
                       f"{observed['samples']} interval(s) observed "
                       f"({observed['value_min']:.3f}s); an observed gap is the authored "
                       "range plus cast time and GCD, so one sample bounds nothing")
            elif not observed:
                yield (f"creature_spells.delayRepeatMin/Max for spell {spell} -- cast only "
                       "once, so no interval exists to measure")
            yield (f"creature_spells.castTarget for spell {spell} -- the SMSG_SPELL_GO "
                   "target block is not decoded yet (layout known, Spell.cpp:4662)")
