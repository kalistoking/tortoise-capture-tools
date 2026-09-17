"""creature_equip_template: what the creature was holding.

The wire carries `UNIT_VIRTUAL_ITEM_DISPLAY` -- the item's *display* id -- but
the table wants the item *entry*, so this is the one rule that needs the world
database as an input rather than as something to check against afterwards.

The lookup checks itself. `UNIT_VIRTUAL_ITEM_INFO` packs the item's class,
subclass and inventory type into the next slot, and those must match the
`item_template` row the display id resolved to. Two independent paths to the
same item: if they disagree, the lookup is wrong and the row is withheld.
"""

from __future__ import annotations

from typing import Any, Iterator

from ..core.base import BaseAuthorRule
from ..core.contracts import LOOKUP, WIRE, AuthorContext, AuthoredRow, Event
from ..core.registry import author_rule

DISPLAY_FIELD = "UNIT_VIRTUAL_ITEM_DISPLAY"
INFO_FIELD = "UNIT_VIRTUAL_ITEM_INFO"


def unpack_item_info(raw: int) -> dict[str, int]:
    """UNIT_VIRTUAL_ITEM_INFO's first slot: class, subclass, material, inventory type."""
    return {"class": raw & 0xFF, "subclass": (raw >> 8) & 0xFF,
            "material": (raw >> 16) & 0xFF, "inventory_type": (raw >> 24) & 0xFF}


@author_rule(id="equipment", table="creature_equip_template", order=40)
class Equipment(BaseAuthorRule):
    def __init__(self) -> None:
        self._display: int | None = None
        self._info: int | None = None
        self._unresolved: str | None = None

    def handle(self, ev: Event, mod: Any = None) -> None:
        if ev.kind != "object_create":
            return
        for field in ev.data.get("fields", ()):
            if field["name"] == DISPLAY_FIELD and self._display is None:
                self._display = field["raw"]
            elif field["name"] == INFO_FIELD and self._info is None:
                self._info = field["raw"]

    def rows(self, ctx: AuthorContext) -> Iterator[AuthoredRow]:
        if not self._display:
            return
        if ctx.world is None:
            self._unresolved = ("no database configured, so display id "
                                f"{self._display} could not be resolved to an item")
            return

        entry = ctx.world.item_entry_for_display(self._display)
        if entry is None:
            self._unresolved = (f"display id {self._display} did not resolve to exactly one "
                                "item_template row")
            return

        notes = [f"item {entry} resolved from {DISPLAY_FIELD} = {self._display}"]
        if self._info is not None:
            packed = unpack_item_info(self._info)
            checks = {"class": packed["class"], "subclass": packed["subclass"],
                      "inventory_type": packed["inventory_type"]}
            mismatched = []
            for column, expected in checks.items():
                actual = ctx.world.column("item_template", column, f"entry = {entry}")
                if actual is not None and int(actual) != expected:
                    mismatched.append(f"{column} {actual} != {expected} on the wire")
            if mismatched:
                self._unresolved = (f"display id {self._display} resolved to item {entry}, but "
                                    f"{INFO_FIELD} disagrees ({'; '.join(mismatched)}) -- "
                                    "withheld rather than guess")
                return
            notes.append(f"cross-checked against {INFO_FIELD}: class, subclass and "
                         "inventory type all match")

        yield self.row({"entry": ctx.entry, "equipentry1": entry},
                       {"entry": WIRE, "equipentry1": LOOKUP}, notes=tuple(notes))

    def gaps(self, ctx: AuthorContext) -> Iterator[str]:
        if self._unresolved:
            yield f"creature_equip_template.equipentry1 -- {self._unresolved}"
        elif not self._display:
            yield ("creature_equip_template -- no virtual item was broadcast; the creature "
                   "was unarmed, or no CREATE block was captured")
