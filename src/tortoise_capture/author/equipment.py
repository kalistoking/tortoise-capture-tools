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
from ..core.contracts import LOOKUP, WIRE, AuthorContext, AuthoredRow, Event
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
        self._raw: dict[int, int] | None = None     # the first unit CREATE's fields, by index
        self._named: dict[str, int] = {}             # name -> index, as that CREATE named them
        self._unresolved: str | None = None
        self._unresolved_extra: list[str] = []

    def handle(self, ev: Event, mod: Any = None) -> None:
        # First CREATE wins, as for every template column; later ones carry the same.
        if (ev.kind != "object_create" or self._raw is not None
                or not ev.data.get("named_ok", True) or not ev.data.get("fields")):
            return
        self._raw = {f["index"]: f["raw"] for f in ev.data["fields"]}
        self._named = {f["name"]: f["index"] for f in ev.data["fields"] if f["name"]}

    def _base(self, ctx: AuthorContext, name: str) -> int | None:
        table = getattr(ctx, "fields", None)
        index = table.index_of(name) if table is not None else None
        return index if index is not None else self._named.get(name)

    def _slots(self, ctx: AuthorContext) -> list[tuple[int, int, int | None]]:
        """(slot, display, info) for every slot that broadcast an item."""
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

        values: dict[str, Any] = {"entry": ctx.entry}
        provenance = {"entry": WIRE}
        notes: list[str] = []
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
        for unresolved in self._unresolved_extra:
            yield f"creature_equip_template.{unresolved}"
        if self._unresolved:
            yield f"creature_equip_template.equipentry1 -- {self._unresolved}"
        elif not self._slots(ctx):
            yield ("creature_equip_template -- no virtual item was broadcast; the creature "
                   "was unarmed, or no CREATE block was captured")
