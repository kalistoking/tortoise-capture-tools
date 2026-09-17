"""Text templates and SQL generation -- the two declarative output paths."""

from __future__ import annotations

import io
import tempfile
from pathlib import Path

from support import make_packet
from tortoise_capture.core.contracts import Column, Event, Row, SqlContext, TableSpec
from tortoise_capture.emit.sql import SqlSink, create_table, literal
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


def test_literals_quote_and_escape_per_dialect():
    assert literal(None, "mysql") == "NULL"
    assert literal(7, "mysql") == "7"
    assert literal("it's", "mysql") == "'it''s'"
    assert literal("back\\slash", "mysql") == "'back\\\\slash'"
    assert literal("back\\slash", "sqlite") == "'back\\slash'"
    assert literal(b"\x01\x02", "mysql") == "0x0102"


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
    out = io.StringIO()
    sink = TextSink(out, [DemoModule()])
    sink.handle(_event(kind="unknown", entry=1), DemoModule())   # must not raise
    sink.close()
    assert "(no records)" in out.getvalue()
