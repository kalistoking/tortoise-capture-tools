"""Registry resolution and the runner's isolation and filtering behaviour."""

from __future__ import annotations

from support import make_ctx, make_packet, make_tables
from tortoise_capture.core.base import BaseModule
from tortoise_capture.core.dispatch import Filters, Runner
from tortoise_capture.core.registry import Registration, Registry

OPCODE = 0x123


class Good(BaseModule):
    id = "good"

    def decode(self, pkt, ctx):
        yield self.event(pkt, "good", entry=62635, guid=7)


class SessionScoped(BaseModule):
    """A fact about the whole session (e.g. which map), not about one creature."""

    id = "session_scoped"

    def decode(self, pkt, ctx):
        yield self.event(pkt, "world_transfer", scope="session", map_id=0)


class Broken(BaseModule):
    id = "broken"

    def decode(self, pkt, ctx):
        raise ValueError("this module is wrong about the layout")


class Collector:
    def __init__(self):
        self.events = []
        self.closed = False

    def handle(self, ev, mod):
        self.events.append(ev)

    def close(self):
        self.closed = True


def _registry(*instances) -> Registry:
    reg = Registry()
    for order, inst in enumerate(instances):
        reg.add(Registration(id=inst.id, opcodes=(OPCODE,), order=order, instance=inst))
    reg.resolve(make_tables())
    return reg


def test_symbols_resolve_against_the_checkout_table():
    tables = make_tables(opcode_names={0x0DD: "SMSG_MONSTER_MOVE"})
    reg = Registry()
    reg.add(Registration(id="good", opcodes=("SMSG_MONSTER_MOVE",), order=0, instance=Good()))
    reg.resolve(tables)
    assert [m.id for m in reg.for_opcode(0x0DD)] == ["good"]
    assert reg.coverage() == {0x0DD: ["good"]}


def test_an_unknown_symbol_leaves_the_opcode_uncovered_without_raising():
    reg = Registry()
    reg.add(Registration(id="ghost", opcodes=("SMSG_NOT_IN_THIS_FORK",), order=0, instance=Good()))
    reg.resolve(make_tables())
    assert reg.coverage() == {}


def test_a_raising_module_is_isolated_and_counted():
    sink = Collector()
    stats = Runner(_registry(Broken(), Good()), make_ctx(), [sink]).run(
        [make_packet(OPCODE, b"")])
    assert stats.errors == 1                 # the broken one
    assert len(sink.events) == 1             # the good one still ran
    assert sink.closed


def test_uncovered_opcodes_are_counted_not_errors():
    stats = Runner(_registry(Good()), make_ctx(), []).run([make_packet(0x999, b"")])
    assert stats.errors == 0 and stats.uncovered[0x999] == 1


def test_entry_filter_uses_the_conventional_data_key():
    sink = Collector()
    runner = Runner(_registry(Good()), make_ctx(), [sink], Filters(entry=1))
    stats = runner.run([make_packet(OPCODE, b"")])
    assert stats.events == 1 and stats.emitted == 0 and sink.events == []


def test_only_narrows_decoding():
    sink = Collector()
    runner = Runner(_registry(Good(), Broken()), make_ctx(), [sink], only={"good"})
    stats = runner.run([make_packet(OPCODE, b"")])
    assert stats.errors == 0 and len(sink.events) == 1


def test_session_scoped_events_bypass_the_entry_filter():
    """world_transfer (the map) is a session-wide fact, not about any one
    creature -- it must still reach sinks under --entry, unlike an ordinary
    entry-tagged event for a *different* entry, which must still be dropped."""
    sink = Collector()
    runner = Runner(_registry(SessionScoped()), make_ctx(), [sink], Filters(entry=999))
    stats = runner.run([make_packet(OPCODE, b"")])
    assert stats.emitted == 1 and len(sink.events) == 1
    assert sink.events[0].kind == "world_transfer"


def test_an_ordinary_event_for_a_different_entry_is_still_dropped():
    """Regression guard: the session-scope escape hatch must not become a
    general bypass for the --entry filter."""
    sink = Collector()
    runner = Runner(_registry(Good()), make_ctx(), [sink], Filters(entry=999))
    runner.run([make_packet(OPCODE, b"")])
    assert sink.events == []
