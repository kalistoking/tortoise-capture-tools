"""Text templates and SQL generation -- the two declarative output paths."""

from __future__ import annotations

import io
import logging
import tempfile
from pathlib import Path

from support import make_packet
from tortoise_capture.core.contracts import Column, Event, Row, SqlContext, TableSpec
from tortoise_capture.emit.jsonl import EventSink
from tortoise_capture.emit.sql import SqlSink, create_table, literal
from tortoise_capture.emit import text as text_sink
from tortoise_capture.emit.text import TextSink

MANAGED = TableSpec(name="capture_demo",
                    columns=(Column("capture", "VARCHAR(64)", nullable=False),
                             Column("entry", "INT UNSIGNED")),
                    key=("capture", "entry"))
WORLD = TableSpec(name="creature_movement",
                  columns=(Column("id", "INT UNSIGNED"), Column("point", "INT UNSIGNED")),
                  managed=False)


class DemoModule:
    """A module as the sinks see it: templates and mappings, nothing else."""

    id = "demo"
    text_section = "Demo"
    text_templates = {"demo": "entry={entry} name={name}"}
    sql_tables = (MANAGED, WORLD)

    def text_fields(self, ev):
        return ev.data

    def sql_rows(self, ev, ctx):
        yield Row("capture_demo", {"capture": ctx.capture_id, "entry": ev.data["entry"]})
        yield Row("creature_movement", {"id": 1, "point": 1})


def _event(kind="demo", **data):
    return Event(packet=make_packet(1, b"", t=12.5), module_id="demo", kind=kind, data=data)


def test_a_float_with_no_literal_is_written_null():
    """A wire f32 can carry NaN or infinity; written bare, `inf` and `nan` read
    as column names and the INSERT failed."""
    for dialect in ("mysql", "sqlite"):
        assert literal(float("nan"), dialect) == "NULL"
        assert literal(float("-inf"), dialect) == "NULL"


def test_an_event_carrying_nan_is_still_json():
    """json.dumps writes NaN as a bare NaN token, which strict JSON refuses."""
    import json

    out = io.StringIO()
    EventSink(out).handle(Event(packet=make_packet(0x1, b""), module_id="m", kind="k",
                                data={"speed": float("nan"), "points": [(1.0, float("inf"))]}))

    def refuse(token):
        raise ValueError(f"not JSON: {token}")

    record = json.loads(out.getvalue(), parse_constant=refuse)
    assert record["data"]["speed"] is None and record["data"]["points"] == [[1.0, None]]


def test_literals_quote_and_escape_per_dialect():
    assert literal(None, "mysql") == "NULL"
    assert literal(7, "mysql") == "7"
    assert literal("it's", "mysql") == "'it''s'"
    assert literal("back\\slash", "mysql") == "'back\\\\slash'"
    assert literal("back\\slash", "sqlite") == "'back\\slash'"
    assert literal(b"\x01\x02", "mysql") == "0x0102"


def test_a_guid_past_two_to_the_63_survives_sqlite_exactly():
    """SQLite has no unsigned 64-bit integer: a creature's guid, 0xF130...,
    overflowed into a REAL, neighbouring guids rounded to one value, and
    INSERT OR IGNORE dropped a third of capture_unit_field as "duplicates".
    Written as its two's-complement value it is stored exactly."""
    import sqlite3
    guids = (0xF13000F4AB2787EA, 0xF13000F4AB2787EB)
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE t (guid BIGINT UNSIGNED NOT NULL, PRIMARY KEY (guid))")
    for guid in guids:
        db.execute(f"INSERT OR IGNORE INTO t (guid) VALUES ({literal(guid, 'sqlite')})")
    stored = sorted(g & 0xFFFFFFFFFFFFFFFF for (g,) in db.execute("SELECT guid FROM t"))
    assert stored == sorted(guids)
    assert literal(guids[0], "mysql") == str(guids[0])          # MySQL holds it as it is


def test_ddl_carries_the_primary_key():
    ddl = create_table(MANAGED, "mysql")
    assert "CREATE TABLE IF NOT EXISTS `capture_demo`" in ddl
    assert "PRIMARY KEY (`capture`, `entry`)" in ddl
    assert "`capture` VARCHAR(64) NOT NULL" in ddl


def test_world_tables_get_inserts_but_never_ddl():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "out.sql"
        sink = SqlSink(path, capture_id="Ralthas")
        sink.handle(_event(entry=62635, name="Ralthas"), DemoModule())
        sink.close()
        sql = path.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS `capture_demo`" in sql
    assert "CREATE TABLE IF NOT EXISTS `creature_movement`" not in sql
    assert "INSERT IGNORE INTO `creature_movement`" in sql
    assert "existing world table" in sql


def test_grouped_text_renders_the_template_and_marks_the_filtered_entry():
    out = io.StringIO()
    sink = TextSink(out, [DemoModule()], highlight_entry=62635)
    sink.handle(_event(entry=62635, name="Ralthas"), DemoModule())
    sink.close()
    text = out.getvalue()
    assert "=== Demo ===" in text
    assert "[  12.500s] entry=62635 name=Ralthas  <-- entry 62635" in text


def test_a_section_with_no_events_says_so():
    out = io.StringIO()
    TextSink(out, [DemoModule()]).close()
    assert "(no records)" in out.getvalue()


def test_a_missing_template_is_reported_not_raised():
    """The sink says which module emitted which kind with no template, as an error,
    and writes no line for the event; the run goes on."""
    out = io.StringIO()
    sink = TextSink(out, [DemoModule()])
    reported = []

    class Catch(logging.Handler):
        def emit(self, record):
            reported.append((record.levelno, record.getMessage()))

    handler = Catch()
    text_sink._logger.addHandler(handler)
    try:
        sink.handle(_event(kind="unknown", entry=1), DemoModule())   # must not raise
        sink.close()
    finally:
        text_sink._logger.removeHandler(handler)
    assert reported == [(logging.ERROR, "module demo emits kind 'unknown' with no text template")]
    assert "(no records)" in out.getvalue()


def _errors_of(logger, work):
    messages = []

    class Catch(logging.Handler):
        def emit(self, record):
            messages.append(record.getMessage())

    handler = Catch(logging.ERROR)
    logger.addHandler(handler)
    try:
        work()
    finally:
        logger.removeHandler(handler)
    return messages


def test_a_template_that_cannot_format_a_value_is_reported_not_raised():
    """`{x:.2f}` over a None raises TypeError, which only KeyError, IndexError and
    ValueError were caught for: the whole run ended in a traceback."""

    class Moving(DemoModule):
        text_templates = {"demo": "x={x:.2f}"}

    out = io.StringIO()
    sink = TextSink(out, [Moving()])
    messages = _errors_of(text_sink._logger,
                          lambda: sink.handle(_event(x=None), Moving()))
    assert len(messages) == 1 and "does not match its fields" in messages[0]


def test_an_event_record_keeps_its_scope():
    """`scope="session"` is what lets an event past an --entry filter; the record dropped it."""
    from tortoise_capture.emit import jsonl

    event = Event(packet=make_packet(1, b""), module_id="m", kind="k", data={}, scope="session")
    assert jsonl.event_record(event)["scope"] == "session"


def test_a_value_json_has_no_form_for_is_reported_not_quietly_a_string():
    from tortoise_capture.emit import jsonl
    import json

    out = io.StringIO()
    event = Event(packet=make_packet(1, b""), module_id="m", kind="k", data={"ids": {1, 2}})
    messages = _errors_of(jsonl._logger, lambda: jsonl.EventSink(out).handle(event))
    assert len(messages) == 1 and "set" in messages[0]
    assert json.loads(out.getvalue())["data"]["ids"]            # still one valid line


def test_a_dump_line_that_is_not_an_object_is_reported_and_the_rest_is_read():
    from tortoise_capture.emit import jsonl

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "dump.jsonl"
        good = io.StringIO()
        jsonl.PacketSink(good).handle(make_packet(0x1EC, b"\x01\x02", "SMSG_AUTH_CHALLENGE"))
        path.write_text("[1, 2]\n3\n" + good.getvalue(), encoding="utf-8")
        found = []
        messages = _errors_of(jsonl._logger, lambda: found.extend(jsonl.read_packets(path)))
    assert len(found) == 1 and len(messages) == 2


def test_a_conflict_rule_the_sql_writer_does_not_know_is_refused():
    """An unknown rule fell through to a plain INSERT, which fails on a second run of
    the same capture where "ignore" would not: a typo changed what re-running meant."""
    TableSpec(name="t", columns=(), conflict="replace")
    try:
        TableSpec(name="t", columns=(), conflict="upsert")
    except ValueError as exc:
        assert "upsert" in str(exc)
    else:
        raise AssertionError("an unknown conflict rule was accepted")
