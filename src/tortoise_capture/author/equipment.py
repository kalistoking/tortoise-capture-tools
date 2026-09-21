"""creature_equip_template: what the creature was holding.

The wire carries `UNIT_VIRTUAL_ITEM_DISPLAY` -- the item's *display* id -- but
the table wants the item *entry*, so this is the one rule that needs the world
database as an input rather than as something to check against afterwards.

The lookup checks itself. `UNIT_VIRTUAL_ITEM_INFO` packs the item's class,
subclass and inventory type into the next slot, and those must match the
`item_template` row the display id resolved to. Two independent paths to the
same item: if they disagree, the lookup is wrong and the row is withheld.

There are three slots, not one: `UNIT_VIRTUAL_ITEM_DISPLAY` is Size:3 and
`UNIT_VIRTUAL_ITEM_INFO` Size:6 (`UpdateFields.h:95-96`), mainhand, offhand
and ranged, with two info words per slot. Only a *base* index carries a name,
because the field table names enum constants rather than the indices a Size:N
field spans -- so the second and third slots can only be read by offset, and
are recognisable precisely by arriving unnamed. Reading slot one alone quietly
dropped a shield or a bow, which neither creature validated so far happens to
carry.
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
        self._display: int | None = None
        self._info: int | None = None
        self._extra: list[tuple[int, int, int | None]] = []   # (slot, display, info)
        self._unresolved: str | None = None
        self._unresolved_extra: list[str] = []

    def handle(self, ev: Event, mod: Any = None) -> None:
        if ev.kind != "object_create" or self._display is not None:
            return
        fields = list(ev.data.get("fields", ()))
        raw = {f["index"]: f["raw"] for f in fields}
        named = {f["index"] for f in fields if f["name"]}

        def slot(base: int | None, offset: int) -> int | None:
            # A sub-slot always arrives unnamed; a named index at the offset is
            # a different field that merely sits next to this one.
            if base is None or (offset and base + offset in named):
                return None
            return raw.get(base + offset)

        display_base = next((f["index"] for f in fields if f["name"] == DISPLAY_FIELD), None)
        info_base = next((f["index"] for f in fields if f["name"] == INFO_FIELD), None)
        self._display = slot(display_base, 0)
        self._info = slot(info_base, 0)
        for index in range(1, EQUIP_SLOTS):
            found = slot(display_base, index)
            if found:
                self._extra.append((index + 1, found,
                                    slot(info_base, index * INFO_WORDS_PER_SLOT)))

    def rows(self, ctx: AuthorContext) -> Iterator[AuthoredRow]:
        if not self._display:
            return
        if ctx.world is None:
            self._unresolved = ("no database configured, so display id "
                                f"{self._display} could not be resolved to an item")
            return

        entry, notes, failure = self._resolve(ctx, self._display, self._info)
        if entry is None:
            self._unresolved = failure
            return

        values = {"entry": ctx.entry, "equipentry1": entry}
        provenance = {"entry": WIRE, "equipentry1": LOOKUP}
        skip = []
        for slot, display, info in self._extra:
            found, extra_notes, extra_failure = self._resolve(ctx, display, info)
            if found is None:
                # A slot that broadcast an item but could not be resolved must
                # not fall back to the schema's 0, which reads as "no item".
                skip.append(f"equipentry{slot}")
                self._unresolved_extra.append(f"equipentry{slot} -- {extra_failure}")
                continue
            values[f"equipentry{slot}"] = found
            provenance[f"equipentry{slot}"] = LOOKUP
            notes.extend(extra_notes)
        self.fill_schema_defaults(ctx, "creature_equip_template", values, provenance, notes,
                                  skip=skip)
        yield self.row(values, provenance, notes=tuple(notes))

    def _resolve(self, ctx: AuthorContext, display: int,
                 info: int | None) -> tuple[int | None, list[str], str | None]:
        """One slot's display id to an item entry, cross-checked against its info word."""
        entry = ctx.world.item_entry_for_display(display)
        if entry is None:
            return None, [], (f"display id {display} did not resolve to exactly one "
                              "item_template row")

        notes = [f"item {entry} resolved from {DISPLAY_FIELD} = {display}"]
        if info is None:
            return entry, notes, None

        packed = unpack_item_info(info)
        checks = {"class": packed["class"], "subclass": packed["subclass"],
                  "inventory_type": packed["inventory_type"]}
        mismatched = []
        for column, expected in checks.items():
            actual = ctx.world.column("item_template", column, f"entry = {entry}")
            if actual is not None and int(actual) != expected:
                mismatched.append(f"{column} {actual} != {expected} on the wire")
        if mismatched:
            return None, [], (f"display id {display} resolved to item {entry}, but "
                              f"{INFO_FIELD} disagrees ({'; '.join(mismatched)}) -- "
                              "withheld rather than guess")
        notes.append(f"cross-checked against {INFO_FIELD}: class, subclass and "
                     "inventory type all match")
        return entry, notes, None

    def gaps(self, ctx: AuthorContext) -> Iterator[str]:
        for unresolved in self._unresolved_extra:
            yield f"creature_equip_template.{unresolved}"
        if self._unresolved:
            yield f"creature_equip_template.equipentry1 -- {self._unresolved}"
        elif not self._display:
            yield ("creature_equip_template -- no virtual item was broadcast; the creature "
                   "was unarmed, or no CREATE block was captured")
