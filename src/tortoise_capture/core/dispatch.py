"""The runner: Packet stream -> Event stream -> sinks.

This is the whole of the "iterate over detected opcodes" requirement, and the
whole of what the launcher depends on. It never names a module, never
branches on an opcode, and never looks inside an Event beyond the two
conventional filter keys. A module that raises is isolated and counted; the
run continues, because over 825 opcodes a forgiving runner is worth more than
a strict one.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Protocol

from .. import log as _log
from .contracts import DecodeContext, Event, Packet
from .registry import Registry
from .reader import WireError

_logger = _log.get_logger("dispatch")


class Sink(Protocol):
    def handle(self, ev: Event, mod: Any) -> None: ...
    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class Filters:
    """Cross-cutting filters, applied to the conventional Event.data keys."""

    entry: int | None = None
    guid: int | None = None

    @property
    def active(self) -> bool:
        return self.entry is not None or self.guid is not None

    def accept(self, ev: Event) -> bool:
        if self.entry is not None and ev.data.get("entry") != self.entry:
            return False
        if self.guid is not None and ev.data.get("guid") != self.guid:
            return False
        return True


@dataclass(slots=True)
class RunStats:
    packets: int = 0
    events: int = 0
    emitted: int = 0
    findings: int = 0        # events produced by analyzers, not by decoding
    errors: int = 0
    seen: Counter = field(default_factory=Counter)        # opcode -> count
    covered: Counter = field(default_factory=Counter)     # opcode -> count
    uncovered: Counter = field(default_factory=Counter)   # opcode -> count

    def report(self, table) -> list[str]:
        lines = [
            f"packets {self.packets}, events {self.events}, emitted {self.emitted}, "
            f"findings {self.findings}, errors {self.errors}",
            f"opcodes seen {len(self.seen)}, covered {len(self.covered)}, "
            f"uncovered {len(self.uncovered)}",
        ]
        for opcode, count in self.uncovered.most_common(10):
            lines.append(f"  uncovered 0x{opcode:03X} {table.name(opcode) or '?':<40} {count}")
        return lines


class Runner:
    def __init__(self, registry: Registry, ctx: DecodeContext, sinks: Iterable[Sink],
                 filters: Filters | None = None, only: set[str] | None = None,
                 analyzers: Iterable[Any] = ()) -> None:
        self._registry = registry
        self._ctx = ctx
        self._sinks = list(sinks)
        self._filters = filters or Filters()
        # `only` narrows decoding, never expansion: dropping a container module
        # would silently hide every packet inside it.
        self._only = only
        self._analyzers = list(analyzers)
        self._contexts: dict[str, DecodeContext] = {}

    def _context_for(self, module_id: str) -> DecodeContext:
        """Each module logs under its own name, so --debug says who spoke."""
        ctx = self._contexts.get(module_id)
        if ctx is None:
            ctx = replace(self._ctx, log=_log.get_logger(f"mod.{module_id}"))
            self._contexts[module_id] = ctx
        return ctx

    def run(self, packets: Iterable[Packet]) -> RunStats:
        stats = RunStats()
        for pkt in packets:
            stats.packets += 1
            stats.seen[pkt.opcode] += 1
            modules = [m for m in self._registry.for_opcode(pkt.opcode)
                       if self._only is None or m.id in self._only]
            if not modules:
                stats.uncovered[pkt.opcode] += 1
                continue
            stats.covered[pkt.opcode] += 1
            for mod in modules:
                self._dispatch(mod, pkt, stats)

        # Analysis runs once the whole stream has been seen: its findings are
        # ordinary events and go to the same sinks, so nothing downstream has
        # to know the difference.
        for found in self._drain_analyzers(stats):
            stats.findings += 1
            for sink in self._sinks:
                self._to_sink(sink, found, self._analyzer_by_id(found.module_id), stats)

        for sink in self._sinks:
            sink.close()
        return stats

    def _analyzer_by_id(self, module_id: str) -> Any:
        return next((a for a in self._analyzers if a.id == module_id), None)

    def _drain_analyzers(self, stats: RunStats) -> list[Event]:
        out: list[Event] = []
        for an in self._analyzers:
            try:
                out.extend(an.finish(self._context_for(an.id)))
            except Exception as exc:
                stats.errors += 1
                _logger.error("analyzer %s failed: %s: %s", an.id, type(exc).__name__, exc)
        return out

    def _dispatch(self, mod: Any, pkt: Packet, stats: RunStats) -> None:
        try:
            events = list(mod.decode(pkt, self._context_for(mod.id)))
        except WireError as exc:
            stats.errors += 1
            _logger.error("module %s could not decode %s: %s", mod.id, pkt.describe(), exc)
            return
        except Exception as exc:
            stats.errors += 1
            _logger.error("module %s raised on %s: %s: %s",
                          mod.id, pkt.describe(), type(exc).__name__, exc)
            return

        for ev in events:
            stats.events += 1
            if not self._filters.accept(ev):
                continue
            stats.emitted += 1
            # Analyzers see the filtered stream, so --entry scopes analysis
            # the same way it scopes output.
            for an in self._analyzers:
                try:
                    an.feed(ev)
                except Exception as exc:
                    stats.errors += 1
                    _logger.error("analyzer %s failed on %s/%s: %s: %s",
                                  an.id, mod.id, ev.kind, type(exc).__name__, exc)
            for sink in self._sinks:
                self._to_sink(sink, ev, mod, stats)

    def _to_sink(self, sink: Sink, ev: Event, mod: Any, stats: RunStats) -> None:
        if mod is None:
            return
        try:
            sink.handle(ev, mod)
        except Exception as exc:
            stats.errors += 1
            _logger.error("sink %s failed on %s/%s: %s: %s",
                          type(sink).__name__, ev.module_id, ev.kind, type(exc).__name__, exc)
