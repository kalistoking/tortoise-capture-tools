"""Which SMSG_SPELL_GOs a creature chose to cast, and which the server cast for it.

Every spell a creature casts goes out as `SMSG_SPELL_GO`, but not every one is a
spell *from its creature_spells row*. A `TRIGGER_SPELL` effect's child is cast
by the server (`SpellEffects.cpp:1318`, `CastSpell(..., triggered=true)`) and
goes out as its own SMSG_SPELL_GO when it has a visual (`Spell.cpp:3994`,
`:4649`); authoring it into a slot makes the creature cast it on its own.

The wire tells the two apart: a triggered cast skips SMSG_SPELL_START
(`Spell.cpp:3647-3650`, `if (!m_IsTriggeredSpell) SendSpellStart()`), so a
SMSG_SPELL_GO with no start before it, by the same caster for the same spell,
is not a cast the creature chose. One caveat the wire cannot settle: a
creature_spells row (or an EventAI action) can cast with CF_TRIGGERED, and that
looks the same. The rules that use this say so beside what they leave out.

Shared by `spells.py` (the slots) and `stats.py` (`spell_list_id` points at the
row `spells.py` writes), so the two cannot disagree about whether there is one.
"""

from __future__ import annotations

from ..core.contracts import Event, is_creature


class CastLedger:
    """Pairs each creature SMSG_SPELL_GO with the SMSG_SPELL_START before it."""

    def __init__(self) -> None:
        # (caster guid, spell id) of a start whose go has not come yet. A cast
        # whose start was dropped, or began before the capture did, shows as
        # triggered; a start that was interrupted leaves its mark until the next
        # go of that spell by that caster, which it can then mislabel -- both
        # rare, and only for a spell the creature casts anyway.
        self._started: set[tuple[int, int]] = set()

    def classify(self, ev: Event) -> bool | None:
        """For a creature's SMSG_SPELL_GO: True if the creature chose the cast,
        False if no SMSG_SPELL_START came before it. None for any other event
        (a start is remembered, and is not itself a cast)."""
        if ev.kind not in ("spell_start", "spell_go"):
            return None
        guid = ev.data.get("guid")
        if not is_creature(guid):
            return None
        key = (guid, ev.data["spell_id"])
        if ev.kind == "spell_start":
            self._started.add(key)
            return None
        if key in self._started:
            self._started.discard(key)
            return True
        return False
