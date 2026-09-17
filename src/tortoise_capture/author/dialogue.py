"""The dialogue trio: broadcast_text, creature_ai_scripts, creature_ai_events.

These three tables only make sense together -- an event points at a script,
which points at a text -- so one file builds all three and keeps their ids
consistent. Splitting them per table would mean three rules agreeing on the
same `entry*100+n` numbering by convention rather than by construction.

What comes from where:

  the text itself, its chat type and language   read off the wire
  which trigger fires it                        inferred by correlation
  the ids, the 100% chance, the script linkage  authoring convention

`sound_id` and the emote columns are left out rather than written as zero:
this capture holds no `SMSG_PLAY_SOUND`, and a zero that means "not observed"
is indistinguishable from a zero that means "silent" once it is in a table.
Omitting them lets the column defaults apply and keeps the claim honest.
"""

from __future__ import annotations

from typing import Any, Iterator

from ..core.base import BaseAuthorRule
from ..core.contracts import CONVENTION, DERIVED, WIRE, AuthorContext, AuthoredRow, Event
from ..core.registry import author_rule

EVENT_T_AGGRO = 4          # CreatureEventAI.h
EVENT_T_DEATH = 6
SCRIPT_COMMAND_TALK = 0    # ScriptCommands.cpp

_EVENT_TYPES = {"aggro": EVENT_T_AGGRO, "death": EVENT_T_DEATH}

# broadcast_text.chat_type is the say/yell distinction, which the wire carries
# as the SMSG_MESSAGECHAT message type.
CHAT_MSG_MONSTER_SAY = 0x0B
CHAT_MSG_MONSTER_YELL = 0x0C
_CHAT_TYPES = {CHAT_MSG_MONSTER_SAY: 0, CHAT_MSG_MONSTER_YELL: 1}


@author_rule(id="dialogue", table="broadcast_text", order=10)
class Dialogue(BaseAuthorRule):
    tables = ("broadcast_text", "creature_ai_scripts", "creature_ai_events")

    def __init__(self) -> None:
        self._said: dict[str, dict[str, Any]] = {}     # message -> what the wire said
        self._triggers: dict[str, str] = {}            # message -> aggro | death
        self._untriggered: list[str] = []
        self._name: str | None = None

    # -- collect -----------------------------------------------------------

    def handle(self, ev: Event, mod: Any = None) -> None:
        if ev.kind in ("monster_say", "monster_yell"):
            self._said.setdefault(ev.data["message"], {
                "chat_type": ev.data.get("chat_type"),
                "language": ev.data.get("language"),
            })
        elif ev.kind == "text_trigger":
            self._triggers[ev.data["subject"]] = ev.data["trigger"]
        elif ev.kind == "text_untriggered":
            self._untriggered.append(ev.data["subject"])
        elif ev.kind == "creature_query":
            self._name = ev.data.get("name")

    # -- emit --------------------------------------------------------------

    def _numbered(self) -> list[tuple[int, str, str]]:
        """(n, message, trigger) for every line whose trigger is known.

        Ordered aggro first, then death, so the ids read the way a hand-written
        migration would.
        """
        ordered = [m for m in self._said if self._triggers.get(m) == "aggro"]
        ordered += [m for m in self._said if self._triggers.get(m) == "death"]
        return [(n, m, self._triggers[m]) for n, m in enumerate(ordered, start=1)]

    def rows(self, ctx: AuthorContext) -> Iterator[AuthoredRow]:
        for n, message, trigger in self._numbered():
            said = self._said[message]
            text_id = self.authored_id(ctx.entry, n)
            chat_type = _CHAT_TYPES.get(said.get("chat_type"), 0)

            bt_values = {"entry": text_id, "male_text": message, "female_text": message,
                        "chat_type": chat_type, "language_id": said.get("language", 0)}
            bt_provenance = {"entry": CONVENTION, "male_text": WIRE, "female_text": WIRE,
                             "chat_type": WIRE, "language_id": WIRE}
            bt_notes = []
            self.fill_schema_defaults(ctx, "broadcast_text", bt_values, bt_provenance, bt_notes)
            yield AuthoredRow(table="broadcast_text", values=bt_values,
                              provenance=bt_provenance, notes=tuple(bt_notes))

            script_values = {"id": text_id, "command": SCRIPT_COMMAND_TALK, "dataint": text_id,
                             "comments": f"{self._name or ctx.entry} - {trigger.capitalize()} text"}
            script_provenance = {"id": CONVENTION, "command": CONVENTION,
                                 "dataint": CONVENTION, "comments": CONVENTION}
            script_notes = []
            self.fill_schema_defaults(ctx, "creature_ai_scripts", script_values,
                                      script_provenance, script_notes)
            yield AuthoredRow(table="creature_ai_scripts", values=script_values,
                              provenance=script_provenance, notes=tuple(script_notes))

            event_values = {"id": text_id, "creature_id": ctx.entry,
                            "event_type": _EVENT_TYPES[trigger], "event_chance": 100,
                            "action1_script": text_id,
                            "comment": f"{self._name or ctx.entry} - {trigger.capitalize()} text"}
            event_provenance = {"id": CONVENTION, "creature_id": WIRE, "event_type": DERIVED,
                                "event_chance": CONVENTION, "action1_script": CONVENTION,
                                "comment": CONVENTION}
            event_notes = [f"event_type {_EVENT_TYPES[trigger]} ({trigger}) inferred from the "
                          "text firing on that trigger's timestamp every time it was seen"]
            self.fill_schema_defaults(ctx, "creature_ai_events", event_values,
                                      event_provenance, event_notes)
            yield AuthoredRow(table="creature_ai_events", values=event_values,
                              provenance=event_provenance, notes=tuple(event_notes))

    def gaps(self, ctx: AuthorContext) -> Iterator[str]:
        for message in self._untriggered:
            yield (f"broadcast_text/creature_ai_events for {message!r} -- seen, but it did not "
                   "coincide with the same trigger every time, so what fires it is unknown")
        if self._said and not self._triggers:
            yield ("creature_ai_events -- dialogue was captured but no aggro or death "
                   "coincided with it")
