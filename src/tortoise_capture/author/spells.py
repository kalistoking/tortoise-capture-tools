"""creature_spells: which spells, and the timings a capture can and cannot give.

The spell ids are read straight off `SMSG_SPELL_GO`, and the delay from
engagement to first cast is a clean measurement. Everything else in an unused
slot (`spellId_2`..`_8` and their siblings, for a creature with only one spell)
is boilerplate the table already knows the value of, so it comes from
`fill_schema_defaults` rather than being retyped here -- see
`BaseAuthorRule.fill_schema_defaults` and ARCHITECTURE.md §17.2.

Two columns are the deliberate exception, and stay real gaps rather than
schema-filled zeros:

`delayRepeatMin/Max` is an authored *range* the server draws from, and its
schema default (0) means "does not repeat" -- false for a spell this capture
watched fire twice. What a capture observes is that draw plus cast time,
global cooldown and whatever the target was doing, and one interval bounds
nothing: the Ralthas session yields a single 18.291 s gap where the
hand-authored values are 12 and 17. A capture long enough to pass
`behaviour.py`'s confidence threshold (`MIN_INTERVALS_FOR_CONFIDENCE`
repeats) *does* get its observed range proposed as `DERIVED` -- the refusal
is about small samples, not about the column in general.

`castTarget` looked at first like a decoding gap -- `SMSG_SPELL_GO` has a
target block, so surely it says who a spell was aimed at. It does, but that
is not what this column means. `castTarget` is the *rule* a creature's AI
used to pick a target (`GetTargetByType()`, `CreatureAI.cpp:230` --
"nearest enemy", "self", "whoever provoked it") -- a server-side authoring
choice consulted *before* casting, never serialized in any form. The wire
only ever carries the *result* of that choice (the resolved guid the spell
hit), not the rule that produced it, so no decoder could recover this column
from a capture no matter how complete. This is why the hand-authored PR this
was checked against sets `castTarget` to the same value (`1`) in all eight
slots, including the seven it never uses: whoever wrote that migration could
not read it off a capture either, and picked the common default. This
toolkit schema-fills it the same way, with a note that it is a default, not a
measurement.
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
        # By spell id, not by how often each was seen: creature_spells is
        # positional, so cast counts would make a slot assignment depend on
        # how long the capture happened to run.
        spells = sorted(self._casts)[:MAX_SLOTS]

        values: dict[str, Any] = {"entry": ctx.entry, "name": self._name or str(ctx.entry)}
        provenance: dict[str, str] = {"entry": WIRE, "name": WIRE}
        notes: list[str] = []
        skip: set[str] = set()          # columns fill_schema_defaults must never touch

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
                             f"a fight began from rest, so delayInitial rounds to {seconds}")
            else:
                # 0 would read as "casts the moment it aggroes" -- a claim, where
                # the truth is that no fresh engagement ever reached this spell.
                skip.add(f"delayInitialMin_{slot}")
                skip.add(f"delayInitialMax_{slot}")

            repeat = self._repeat.get(spell)
            if repeat and repeat.get("confident"):
                values[f"delayRepeatMin_{slot}"] = int(round(repeat["value_min"]))
                values[f"delayRepeatMax_{slot}"] = int(round(repeat["value_max"]))
                provenance[f"delayRepeatMin_{slot}"] = DERIVED
                provenance[f"delayRepeatMax_{slot}"] = DERIVED
                notes.append(f"spell {spell}: delayRepeat from {repeat['samples']} observed "
                             "intervals, enough to bound it")
            else:
                # Not enough samples (or none) to trust a bound -- see module
                # docstring. Left out of `values` entirely, so the generic
                # schema-fill below must not paper over it with a false zero.
                skip.add(f"delayRepeatMin_{slot}")
                skip.add(f"delayRepeatMax_{slot}")

        notes.append(f"{len(spells)} spell(s) seen cast; a spell never used during the "
                     "capture cannot appear here at all")
        notes.append("castTarget is left at the table's own default (1) for every slot: "
                     "it is the AI's target-selection rule (CreatureAI.cpp GetTargetByType), "
                     "consulted before a spell is cast and never put on the wire in any "
                     "form -- no capture, however complete, can recover it, which is also "
                     "why the reference migration this was checked against uses the same "
                     "value uniformly, used slots and empty ones alike")

        self.fill_schema_defaults(ctx, "creature_spells", values, provenance, notes, skip=skip)
        yield self.row(values, provenance, notes=tuple(notes))

    def gaps(self, ctx: AuthorContext) -> Iterator[str]:
        for spell in sorted(self._casts):
            if spell not in self._initial:
                yield (f"creature_spells.delayInitialMin/Max for spell {spell} -- seen cast, "
                       "but never inside a fight that began from rest (the first aggro of "
                       "a life); a re-aggro mid-fight does not restart the timer, so it "
                       "cannot be measured from one")
            observed = self._repeat.get(spell)
            if observed and not observed.get("confident"):
                yield (f"creature_spells.delayRepeatMin/Max for spell {spell} -- "
                       f"{observed['samples']} interval(s) observed "
                       f"({observed['value_min']:.3f}s); an observed gap is the authored "
                       "range plus cast time and GCD, so one sample bounds nothing")
            elif not observed:
                yield (f"creature_spells.delayRepeatMin/Max for spell {spell} -- cast only "
                       "once, so no interval exists to measure")
