"""Text output: the module declares the template, this renders it.

A module never prints. It declares `text_templates[kind]` and optionally
overrides `text_fields()`; everything around the template -- timestamp
column, section header, entry highlight, ordering, empty-section note -- is
owned here, so output stays uniform no matter who wrote the module.

Layouts:
  grouped  one section per module, in registration order (the report form)
  stream   strictly chronological across modules (the timeline form)
"""

from __future__ import annotations

from typing import Any, Iterable, TextIO

from .. import log as _log
from ..core.contracts import Event, offers_text

_logger = _log.get_logger("emit.text")

_TIME_WIDTH = 12          # "[  90.307s] "
_CONT_INDENT = " " * (_TIME_WIDTH + 2)


def _stamp(ev: Event) -> str:
    t = ev.packet.t
    return f"[{t:8.3f}s] " if t is not None else "[    ?    ] "


class TextSink:
    def __init__(self, out: TextIO, modules: Iterable[Any], layout: str = "grouped",
                 highlight_entry: int | None = None) -> None:
        self._out = out
        self._layout = layout
        self._highlight = highlight_entry
        self._modules = [m for m in modules if offers_text(m)]
        self._sections: dict[str, list[str]] = {m.id: [] for m in self._modules}
        self._titles = {m.id: (m.text_section or m.id) for m in self._modules}
        self._missing: set[tuple[str, str]] = set()   # reported once per module/kind

    # -- rendering ---------------------------------------------------------

    def _render(self, ev: Event, mod: Any) -> str | None:
        template = getattr(mod, "text_templates", {}).get(ev.kind)
        if template is None:
            token = (mod.id, ev.kind)
            if token not in self._missing:
                self._missing.add(token)
                _logger.error("module %s emits kind %r with no text template", mod.id, ev.kind)
            return None
        try:
            body = template.format_map(mod.text_fields(ev))
        except (KeyError, IndexError, ValueError) as exc:
            token = (mod.id, ev.kind)
            if token not in self._missing:
                self._missing.add(token)
                _logger.error("module %s template for %r does not match its fields: %s",
                              mod.id, ev.kind, exc)
            return None

        marker = ""
        if self._highlight is not None and ev.data.get("entry") == self._highlight:
            marker = f"  <-- entry {self._highlight}"
        lines = body.split("\n")
        head = f"  {_stamp(ev)}{lines[0]}{marker}"
        return "\n".join([head, *(_CONT_INDENT + line for line in lines[1:])])

    # -- sink --------------------------------------------------------------

    def handle(self, ev: Event, mod: Any) -> None:
        line = self._render(ev, mod)
        if line is None:
            return
        if self._layout == "stream":
            self._out.write(line + "\n")
        else:
            self._sections.setdefault(ev.module_id, []).append(line)

    def close(self) -> None:
        if self._layout == "stream":
            self._out.flush()
            return
        for mod in self._modules:
            lines = self._sections.get(mod.id, [])
            self._out.write(f"\n=== {self._titles[mod.id]} ===\n")
            self._out.write("\n".join(lines) + "\n" if lines else "  (no records)\n")
        self._out.flush()
