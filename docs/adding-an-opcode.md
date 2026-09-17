# Adding an opcode

One new file in `src/tortoise_capture/modules/`. Nothing else in the codebase
changes — no registration list, no import, no runner edit.

## 1. Get the layout from the source, not from a guess

Read the server's own writer for that opcode in the `tortoise-wow` checkout
(`src/game/...`), and note the file and line in the module docstring. If the
layout is reusable knowledge, add it to [wire-format.md](wire-format.md).

Never copy a layout out of WowPacketParser or HermesProxy: both are GPLv3 and
this project stays clear of them by re-deriving.

## 2. Write the module

```python
@module(id="my_thing", opcodes=("SMSG_MY_THING",), order=45)
class MyThing(BaseModule):
    """SMSG_MY_THING: what it means (SourceFile.cpp:123).

        uint64 guid (raw) ; uint32 something
    """

    def decode(self, pkt: Packet, ctx: DecodeContext) -> Iterator[Event]:
        r = ByteReader(pkt.body, pkt.name)
        guid = r.u64("guid")
        yield self.event(pkt, "my_thing", guid=guid, entry=guid_entry(guid),
                         something=r.u32("something"))
```

Rules that matter:

- **Read through `ByteReader`**, giving each field a name. An overrun then
  reports which field of which packet ran out of bytes.
- **Use the conventional keys.** `entry` and `guid` in `Event.data` are what
  `--entry` and `--guid` filter on. Omit them when the payload genuinely
  cannot supply them (`SMSG_MESSAGECHAT`'s emote form carries no sender GUID)
  — the events then correctly drop out of a filtered run.
- **One `kind` per shape**, not per opcode: `monster_move` emits `move_stop`,
  `move_linear` and `move_spline` because each prints differently.
- **Decoding part of a payload is fine.** `spell_go` stops after the spell id.
  The runner never requires a module to consume every byte.
- **Raise `WireError`** (or let `ByteReader` raise it) when the payload does
  not match the layout. The runner logs it against your module and carries on.

## 3. Declare the text form

```python
    text_section = "SMSG_MY_THING (what it is)"
    text_templates = {"my_thing": "entry={entry:<7} something={something}"}
```

The template is a `str.format` string over `text_fields(event)`, which
defaults to `event.data`. Override `text_fields` when the template needs a
derived value (a flattened coordinate, a pre-rendered multi-line block) —
see `monster_move` and `update_object`.

Timestamps, section headers, the `--entry` marker and ordering are the core's
job. A module never prints.

## 4. Declare the SQL form

```python
    sql_tables = (TableSpec(name="capture_my_thing", columns=(...), key=(...)),)

    def sql_rows(self, ev, ctx):
        yield Row("capture_my_thing", {"capture": ctx.capture_id, **ev.data})
```

- Every column in `key` must also be in `columns` — the DDL is generated from
  the spec.
- `managed=False` for a table that already exists in `tw_world`: inserts are
  emitted, DDL is not.
- Never build a SQL string. Quoting, batching and dialect are `emit/sql.py`.

## 5. Container opcodes only: `expand`

A module that unpacks other packets implements `expand()` instead of (or as
well as) `decode()`. Its children re-enter the same stream, so they get
decoded by whichever module handles them. `modules/compressed.py` is the
worked example — and a reminder that the two compressed containers do *not*
share an inner framing.

## 6. Test it

Add a test to `tests/test_modules.py` that builds the payload byte by byte
with the helpers in `tests/support.py` and asserts the decoded event. No
capture is involved, so the test runs anywhere and versions cleanly.

```bash
python tests/run_tests.py
```

(or `pip install -e ".[dev]" && pytest` — the same files.)

## 7. Check it against a real session

```bash
tct decode out/Ralthas.jsonl --only my_thing --report
```

`--only` narrows decoding to your module while leaving container expansion
intact, and `--report` shows what is still uncovered.
