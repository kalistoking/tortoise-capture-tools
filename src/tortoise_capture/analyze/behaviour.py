"""Correlate what a creature did across opcodes: respawn, triggers, cast timing.

Three questions that no single packet answers:

  **Respawn timer** -- the gap between a death and the next create block for
  the same creature. Straightforward, and it lands within half a second of the
  authored `spawntimesecsmin/max`.

  **What triggers a text** -- a creature's lines arrive as plain chat packets
  with no hint of why. But an aggro line lands on the same timestamp as
  `SMSG_AI_REACTION` and a death line on the same timestamp as
  `SMSG_PARTYKILLLOG`, every time, so the trigger can be read off the clock.
  A line is only attributed when *every* occurrence agrees; one coincidence is
  not evidence, and this says so rather than guessing.

  **Cast timing** -- delay from engagement to first cast, and the intervals
  between repeats. The first is usually clean. The second is not: an observed
  interval is the authored random delay plus cast time, global cooldown and
  whatever the target was doing, so recovering authored bounds needs many
  samples. The finding carries its sample count for exactly that reason, and
  `confident` stays false until there are enough of them.

A fourth question is answered across creatures rather than within one:
`SMSG_PLAY_SOUND` carries a sound id and nothing else -- no sender, no
target -- so attributing a captured sound to a particular line of dialogue is
pure timestamp coincidence. `_sound_for` looks across *every* creature's
dialogue at once and attributes a sound only when exactly one line, from any
of them, falls inside the coincidence window: two creatures talking in the
same instant makes that sound's owner a coin flip, not a fact, and it is left
unattributed rather than guessed.

Findings are observations with their evidence attached, never rounded into a
conclusion they cannot support.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterator

from ..core.base import BaseAnalyzer
from ..core.contracts import (
    Column, DecodeContext, Event, Packet, Row, SqlContext, TableSpec,
)
from ..core.registry import analyzer

# Two events are "simultaneous" within this many seconds. Observed offsets in
# practice are 0.000 s -- the packets share a batch -- so this is slack, not a
# working margin.
COINCIDENCE_WINDOW = 1.0

# Below this many observed repeat intervals, the spread is not a measurement.
MIN_INTERVALS_FOR_CONFIDENCE = 5

_TRIGGERS = {"ai_reaction": "aggro", "party_kill": "death"}

_TABLE = TableSpec(
    name="capture_behaviour",
    columns=(
        Column("capture", "VARCHAR(64)", nullable=False),
        Column("entry", "INT UNSIGNED", nullable=False),
        Column("finding", "VARCHAR(32)", nullable=False),
        Column("subject", "VARCHAR(255)"),        # the text, or the spell id
        Column("detail", "VARCHAR(32)"),          # the trigger, where there is one
        Column("value_min", "DOUBLE"),
        Column("value_max", "DOUBLE"),
        Column("samples", "INT UNSIGNED"),
        Column("confident", "TINYINT UNSIGNED"),
    ),
    key=("capture", "entry", "finding", "subject"),
    comment="cross-opcode observations with their sample counts; not authored values",
)


@dataclass
class _Creature:
    entry: int
    triggers: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    texts: dict[str, list[float]] = field(default_factory=lambda: defaultdict(list))
    spells: dict[int, list[float]] = field(default_factory=lambda: defaultdict(list))
    deaths: list[float] = field(default_factory=list)
    creates: list[float] = field(default_factory=list)
    last_packet: Packet | None = None


@analyzer(id="behaviour", order=20)
class Behaviour(BaseAnalyzer):
    text_section = "Behaviour (correlated across opcodes)"
    text_templates = {
        "respawn_timer": "entry={entry:<7} respawn {value_min:.1f}s "
                         "(death -> create, {samples} observation(s))",
        "text_trigger": "entry={entry:<7} {trigger} text: {subject!r} "
                        "({samples}x, offset {offset:+.3f}s)",
        "text_untriggered": "entry={entry:<7} text with no matching trigger: {subject!r} "
                            "({samples}x)",
        "spell_initial_delay": "entry={entry:<7} spell {subject} first cast {value_min:.3f}s "
                               "after engage ({samples} engagement(s))",
        "spell_repeat_delay": "entry={entry:<7} spell {subject} repeat {value_min:.1f}-"
                              "{value_max:.1f}s from {samples} interval(s){caveat}",
    }
    sql_tables = (_TABLE,)

    def __init__(self) -> None:
        self._seen: dict[int, _Creature] = {}
        self._sounds: list[tuple[float, int]] = []     # session-wide: no entry on the wire

    # -- collect -----------------------------------------------------------

    def feed(self, ev: Event) -> None:
        t = ev.packet.t
        if t is None:
            return
        if ev.kind == "play_sound":
            # Not entry-scoped: SMSG_PLAY_SOUND carries no sender, so it is
            # matched against every creature's dialogue at once in finish().
            self._sounds.append((t, ev.data["sound_id"]))
            return

        entry = ev.data.get("entry")
        if entry is None:
            return
        creature = self._seen.setdefault(entry, _Creature(entry=entry))
        creature.last_packet = ev.packet

        if ev.kind in _TRIGGERS:
            creature.triggers[_TRIGGERS[ev.kind]].append(t)
            if ev.kind == "party_kill":
                creature.deaths.append(t)
        elif ev.kind in ("monster_say", "monster_yell"):
            creature.texts[ev.data["message"]].append(t)
        elif ev.kind == "spell_go":
            creature.spells[ev.data["spell_id"]].append(t)
        elif ev.kind == "object_create":
            creature.creates.append(t)

    # -- report ------------------------------------------------------------

    def finish(self, ctx: DecodeContext) -> Iterator[Event]:
        sound_for = self._sound_attribution()
        for entry, creature in sorted(self._seen.items()):
            if creature.last_packet is None:
                continue
            yield from self._respawn(creature)
            yield from self._text_triggers(creature, sound_for)
            yield from self._spell_timing(creature)

    def _sound_attribution(self) -> dict[tuple[int, str], int]:
        """(entry, message) -> sound_id, only where exactly one line -- from
        any creature -- falls inside the window of a captured sound."""
        occurrences = [(entry, message, t) for entry, creature in self._seen.items()
                       for message, times in creature.texts.items() for t in times]

        attributed: dict[tuple[int, str], int] = {}
        for t_sound, sound_id in self._sounds:
            candidates = {(entry, message) for entry, message, t in occurrences
                          if abs(t - t_sound) <= COINCIDENCE_WINDOW}
            if len(candidates) == 1:
                attributed[next(iter(candidates))] = sound_id
            # 0 candidates: nothing nearby. >1: two lines equally close --
            # which one actually made the sound is a coin flip, not derivable.
        return attributed

    def _respawn(self, c: _Creature) -> Iterator[Event]:
        gaps = []
        for death in c.deaths:
            after = [t for t in c.creates if t > death]
            if after:
                gaps.append(min(after) - death)
        if gaps:
            yield self.event(c.last_packet, "respawn_timer", entry=c.entry,
                             value_min=min(gaps), value_max=max(gaps), samples=len(gaps))

    def _text_triggers(self, c: _Creature,
                       sound_for: dict[tuple[int, str], int]) -> Iterator[Event]:
        for message, times in c.texts.items():
            matched: dict[str, list[float]] = defaultdict(list)
            for t in times:
                for name, trigger_times in c.triggers.items():
                    near = [abs(t - tt) for tt in trigger_times
                            if abs(t - tt) <= COINCIDENCE_WINDOW]
                    if near:
                        matched[name].append(min(near))

            sound_id = sound_for.get((c.entry, message))
            extra = {"sound_id": sound_id} if sound_id is not None else {}

            # Only attribute when one trigger explains every occurrence: a text
            # that lines up once out of three has told us nothing.
            winner = next((name for name, offsets in matched.items()
                           if len(offsets) == len(times)), None)
            if winner:
                offsets = matched[winner]
                yield self.event(c.last_packet, "text_trigger", entry=c.entry, subject=message,
                                 trigger=winner, samples=len(times),
                                 offset=sum(offsets) / len(offsets), **extra)
            else:
                yield self.event(c.last_packet, "text_untriggered", entry=c.entry,
                                 subject=message, samples=len(times), **extra)

    def _spell_timing(self, c: _Creature) -> Iterator[Event]:
        engagements = sorted(c.triggers.get("aggro", []))
        for spell_id, casts in sorted(c.spells.items()):
            casts = sorted(casts)

            # Delay from each engagement to the first cast that followed it.
            initial = []
            for start in engagements:
                after = [t for t in casts if t >= start]
                if after:
                    initial.append(min(after) - start)
            if initial:
                yield self.event(c.last_packet, "spell_initial_delay", entry=c.entry,
                                 subject=spell_id, value_min=min(initial),
                                 value_max=max(initial), samples=len(initial))

            # Intervals between consecutive casts inside one engagement: a gap
            # spanning a death and respawn is not a repeat delay.
            intervals = [b - a for a, b in zip(casts, casts[1:])
                         if not any(a < d < b for d in c.deaths)]
            if intervals:
                confident = len(intervals) >= MIN_INTERVALS_FOR_CONFIDENCE
                yield self.event(c.last_packet, "spell_repeat_delay", entry=c.entry,
                                 subject=spell_id, value_min=min(intervals),
                                 value_max=max(intervals), samples=len(intervals),
                                 confident=confident,
                                 caveat="" if confident else
                                        f" -- too few to bound (want {MIN_INTERVALS_FOR_CONFIDENCE}+)")

    # -- sql ---------------------------------------------------------------

    def sql_rows(self, ev: Event, ctx: SqlContext) -> Iterator[Row]:
        data = ev.data
        # A range needs two observations before min/max mean anything; one
        # sample is a measurement of that one occurrence, not a bound.
        samples = data.get("samples", 0)
        confident = data.get("confident", samples >= 2)
        yield Row(_TABLE.name, {
            "capture": ctx.capture_id, "entry": data["entry"], "finding": ev.kind,
            "subject": str(data.get("subject", "")),
            "detail": data.get("trigger"),
            "value_min": data.get("value_min"), "value_max": data.get("value_max"),
            "samples": samples, "confident": int(bool(confident)),
        })
