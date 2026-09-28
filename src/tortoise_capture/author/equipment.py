"""creature_equip_template: what the creature was holding.

The wire carries `UNIT_VIRTUAL_ITEM_DISPLAY` -- the item's *display* id -- but
the table wants the item *entry*, so this is the one rule that needs the world
database as an input rather than as something to check against afterwards.

The lookup checks itself. `UNIT_VIRTUAL_ITEM_INFO` packs the item's class,
subclass and inventory type into the next slot, and those must match the
`item_template` row the display id resolved to. Two independent paths to the
same item: if they disagree, the lookup is wrong and the row is withheld. The
same word settles a display several items share, when exactly one of them fits
it -- 2 of the 8 shared displays in the test captures, both matching the live
database; the other 6 are items identical in all three, and are left unguessed.

There are three slots, not one: `UNIT_VIRTUAL_ITEM_DISPLAY` is Size:3 and
`UNIT_VIRTUAL_ITEM_INFO` Size:6 (`UpdateFields.h:95-96`), mainhand, offhand
and ranged, with two info words per slot. Only a *base* index carries a name,
because the field table names enum constants rather than the indices a Size:N
field spans -- so the second and third slots can only be read by offset, and
are recognisable precisely by arriving unnamed. Reading slot one alone quietly
dropped a shield or a bow, which neither creature validated so far happens to
carry.

The slots are counted from the field table's index, not from a name the CREATE
happened to carry: a CREATE omits every zero field, so a creature holding only
a bow carries no field named `UNIT_VIRTUAL_ITEM_DISPLAY` at all. Rallic Finn
(1198) in the Elwynn capture is one, and was reported unarmed. The same
omission makes an empty main hand a reading, not a guess: it is a 0.
"""

from __future__ import annotations

from typing import Any, Iterator

from ..core.base import BaseAuthorRule
from ..core.contracts import (
    CONVENTION, LOOKUP, WIRE, AuthorContext, AuthoredRow, Event, is_corpse, is_creature,
    spawn_sighting,
)
from ..core.registry import author_rule

DISPLAY_FIELD = "UNIT_VIRTUAL_ITEM_DISPLAY"
INFO_FIELD = "UNIT_VIRTUAL_ITEM_INFO"
EQUIP_SLOTS = 3                  # mainhand, offhand, ranged
INFO_WORDS_PER_SLOT = 2          # Size:6 over three slots


def unpack_item_info(raw: int) -> dict[str, int]:
    """UNIT_VIRTUAL_ITEM_INFO's first slot: class, subclass, material, inventory type."""
    return {"class": raw & 0xFF, "subclass": (raw >> 8) & 0xFF,
            "material": (raw >> 16) & 0xFF, "inventory_type": (raw >> 24) & 0xFF}


@author_rule(id="equipment", table="creature_equip_template", order=40)
class Equipment(BaseAuthorRule):
    def __init__(self) -> None:
        self._raw: dict[int, int] | None = None     # the chosen CREATE's fields, by index
        self._named: dict[str, int] = {}             # name -> index, as that CREATE named them
        self._after_death = False                   # whether that CREATE followed a death
        self._from_corpse = False                   # whether it was a corpse's, for want of any
        self._unresolved: str | None = None
        self._unresolved_extra: list[str] = []
        # guid -> [(t, fields by index, names)], its deaths, and every VALUES
        # block's fields: all a script's weapon swap can show up in.
        self._creates: dict[int, list[tuple[float, dict[int, int], dict[str, int]]]] = {}
        self._deaths: dict[int, list[float]] = {}
        self._corpses: list[tuple[float, dict[int, int], dict[str, int]]] = []
        self._updates: list[tuple[dict[int, int], set[int]]] = []

    def handle(self, ev: Event, mod: Any = None) -> None:
        guid = ev.data.get("guid")
        if not is_creature(guid) or ev.packet.t is None:
            return
        if ev.kind == "party_kill":
            self._deaths.setdefault(guid, []).append(ev.packet.t)
        elif ev.kind == "object_values" and ev.data.get("fields"):
            self._updates.append(({f["index"]: f["raw"] for f in ev.data["fields"]},
                                  {f["index"] for f in ev.data["fields"] if f["name"]}))
        elif (ev.kind == "object_create" and ev.data.get("named_ok", True)
                and ev.data.get("fields")):
            seen = (ev.packet.t, {f["index"]: f["raw"] for f in ev.data["fields"]},
                    {f["name"]: f["index"] for f in ev.data["fields"] if f["name"]})
            if is_corpse(ev):
                self._corpses.append(seen)
            else:
                self._creates.setdefault(guid, []).append(seen)

    def _choose(self) -> None:
        """What a creature holds is its template's only as it spawns: a script
        can swap it mid-fight (Mr. Smite's does, boss_mr_smite.cpp:165-180) and
        its Reset() loads the template's back. So the CREATE after a death is
        taken when there is one -- any spawn's -- else the first."""
        if self._raw is not None:
            return
        if not self._creates:
            if self._corpses:
                self._raw, self._named = min(self._corpses, key=lambda c: c[0])[1:]
                self._from_corpse = True
            return
        first = min((c for creates in self._creates.values() for c in creates), key=lambda c: c[0])
        self._raw, self._named = first[1], first[2]
        for guid, creates in sorted(self._creates.items()):
            sighting = spawn_sighting([(t, (raw, named)) for t, raw, named in creates],
                                      self._deaths.get(guid, ()))
            if sighting is not None and sighting[1]:
                (self._raw, self._named), self._after_death = sighting[0], True
                return

    def _other_displays(self, ctx: AuthorContext, held: set[int]) -> list[int]:
        """Display ids the capture showed it holding besides `held`."""
        base = self._base(ctx, DISPLAY_FIELD)
        if base is None:
            return []
        creates = [c for creates in self._creates.values() for c in creates] + self._corpses
        blocks = [(fields, set(named.values())) for _, fields, named in creates] + self._updates
        # As in _slots: a sub-slot arrives unnamed, a named one is another field.
        seen = {fields.get(base + i) for fields, named in blocks for i in range(EQUIP_SLOTS)
                if not (i and base + i in named)}
        return sorted(d for d in seen - held if d)

    def _base(self, ctx: AuthorContext, name: str) -> int | None:
        table = getattr(ctx, "fields", None)
        index = table.index_of(name) if table is not None else None
        return index if index is not None else self._named.get(name)

    def _slots(self, ctx: AuthorContext) -> list[tuple[int, int, int | None]]:
        """(slot, display, info) for every slot that broadcast an item."""
        self._choose()
        if not self._raw:
            return []
        named = set(self._named.values())

        def read(base: int | None, offset: int) -> int | None:
            # A sub-slot always arrives unnamed; a named index at the offset is
            # a different field that merely sits next to this one.
            if base is None or (offset and base + offset in named):
                return None
            return self._raw.get(base + offset)

        display_base, info_base = self._base(ctx, DISPLAY_FIELD), self._base(ctx, INFO_FIELD)
        return [(index + 1, display, read(info_base, index * INFO_WORDS_PER_SLOT))
                for index in range(EQUIP_SLOTS)
                if (display := read(display_base, index))]

    def rows(self, ctx: AuthorContext) -> Iterator[AuthoredRow]:
        slots = self._slots(ctx)
        if not slots:
            return
        if ctx.world is None:
            self._unresolved = ("no database configured, so display id "
                                f"{slots[0][1]} could not be resolved to an item")
            return

        others = self._other_displays(ctx, {display for _, display, _ in slots})
        if others and not self._after_death:
            self._unresolved = (f"it held other weapons in this capture too (display "
                                f"{', '.join(map(str, others))}) -- a script swaps them "
                                "mid-fight -- and none was seen after a death, which alone "
                                "shows what it spawns holding; not proposed, decide by hand")
            return

        # Keyed by the creature's entry, where most templates point their
        # equipment_id; gaps() says when this one does not.
        values: dict[str, Any] = {"entry": ctx.entry}
        provenance = {"entry": CONVENTION}
        notes: list[str] = []
        if self._from_corpse:
            notes.append("read off its corpse, the only sighting of it: a corpse holds what "
                         "the creature died with, the template's unless a script swapped it")
        if others:
            notes.append(f"it held other weapons in this capture too (display "
                         f"{', '.join(map(str, others))}), as a script swaps them mid-fight; "
                         "these are what the CREATE after its death showed, which is what "
                         "it spawns holding")
        skip = []
        if slots[0][0] != 1:
            values["equipentry1"], provenance["equipentry1"] = 0, WIRE
            notes.append("no main-hand item was broadcast; a CREATE carries every non-zero "
                         "field, so its absence is a 0, not an unknown")
        for slot, display, info in slots:
            found, found_notes, failure = self._resolve(ctx, display, info)
            if found is None:
                if slot == 1:
                    self._unresolved = failure
                    return
                # A slot that broadcast an item but could not be resolved must
                # not fall back to the schema's 0, which reads as "no item".
                skip.append(f"equipentry{slot}")
                self._unresolved_extra.append(f"equipentry{slot} -- {failure}")
                continue
            values[f"equipentry{slot}"] = found
            provenance[f"equipentry{slot}"] = LOOKUP
            notes.extend(found_notes)
        self.fill_schema_defaults(ctx, "creature_equip_template", values, provenance, notes,
                                  skip=skip)
        yield self.row(values, provenance, notes=tuple(notes))

    def _resolve(self, ctx: AuthorContext, display: int,
                 info: int | None) -> tuple[int | None, list[str], str | None]:
        """One slot's display id to an item entry, settled by its info word.

        The info word packs the class, subclass and inventory type of the item
        itself, so it is a second, independent path to it: it cross-checks a
        display only one item wears, and it settles a display several items
        share -- when exactly one of them fits. When none or several fit, the
        slot is left to a human with the candidates named.
        """
        items = ctx.world.items_for_display(display)
        if not items:
            return None, [], f"display id {display} matches no item_template row"
        if info is None:
            if len(items) == 1:
                return items[0][0], [f"item {items[0][0]} resolved from {DISPLAY_FIELD} = "
                                     f"{display}"], None
            return None, [], (f"display id {display} matches {len(items)} items "
                              f"({', '.join(str(i[0]) for i in items)}) and no "
                              f"{INFO_FIELD} was broadcast to tell them apart")

        packed = unpack_item_info(info)
        wire = (packed["class"], packed["subclass"], packed["inventory_type"])
        fitting = [item[0] for item in items if tuple(item[1:]) == wire]
        if len(items) == 1:
            if not fitting:
                return None, [], (f"display id {display} resolved to item {items[0][0]}, but "
                                  f"{INFO_FIELD} disagrees (class/subclass/inventory type "
                                  f"{'/'.join(map(str, items[0][1:]))} != "
                                  f"{'/'.join(map(str, wire))} on the wire) -- withheld "
                                  "rather than guess")
            return fitting[0], [f"item {fitting[0]} resolved from {DISPLAY_FIELD} = {display}",
                                f"cross-checked against {INFO_FIELD}: class, subclass and "
                                "inventory type all match"], None
        if len(fitting) == 1:
            return fitting[0], [f"item {fitting[0]}: {DISPLAY_FIELD} = {display} matches "
                                f"{len(items)} items, and only this one has the class, subclass "
                                f"and inventory type {INFO_FIELD} packs"], None
        named = ", ".join(str(e) for e in (fitting or [i[0] for i in items]))
        return None, [], (f"display id {display} matches {len(items)} items, and "
                          f"{len(fitting)} of them fit {INFO_FIELD}'s class, subclass and "
                          f"inventory type ({named}) -- not guessing which")

    def gaps(self, ctx: AuthorContext) -> Iterator[str]:
        if ctx.world is not None and self._slots(ctx) and not self._unresolved:
            # The server equips what the template names, unless the spawn's
            # creature_addon or a game event names its own (Creature.cpp:410-419).
            stored = ctx.world.column("creature_template", "equipment_id", f"entry = {ctx.entry}")
            if stored is not None and stored != str(ctx.entry):
                yield (f"creature_template.equipment_id -- the template names {stored}, not "
                       f"{ctx.entry}, so the creature_equip_template {ctx.entry} proposed here "
                       "is used only once it is pointed at; what was held may also come from "
                       "the spawn's creature_addon or a game event -- decide by hand")
        for unresolved in self._unresolved_extra:
            yield f"creature_equip_template.{unresolved}"
        if self._unresolved:
            yield f"creature_equip_template.equipentry1 -- {self._unresolved}"
        elif not self._slots(ctx):
            yield ("creature_equip_template -- no virtual item was broadcast; the creature "
                   "was unarmed, or no CREATE block was captured")
