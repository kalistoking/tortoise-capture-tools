# Architecture

How this toolkit is put together, and why. The driving requirement is not
throughput or feature count — it is that **support for one more opcode must
cost one new file and nothing else**, for 825 opcodes, over a long time.

---

## 1. Requirements this design answers

| # | Requirement | Where it is met |
|---|---|---|
| R1 | Modular: one unit of work per opcode | [§3 Pattern analysis](#3-pattern-analysis), [§6 Module contracts](#6-module-contracts) |
| R2 | The runner iterates over *detected opcodes*, not over hand-written per-packet code | [§7 The dispatch loop](#7-the-dispatch-loop) |
| R3 | The runner must not depend on the body of any interface method | [§7.1 The independence rule](#71-the-independence-rule) |
| R4 | Text output format is declared by the module (templates), mapped by the core | [§9 Text emission](#9-text-emission) |
| R5 | SQL output: the module declares the table shape and the row mapping | [§10 SQL emission](#10-sql-emission) |
| R6 | Log levels — error / warn / info / debug, error always on stderr | [§8.1 Four levels](#81-four-levels) |
| R6b | Verbosity set from a config file, per run and per module | [§8.2 The config file](#82-the-config-file) |
| R7 | Incremental opcode coverage, measurable | [§12 Coverage and roadmap](#12-coverage-and-roadmap) |
| R8 | Captures and records never reach the repository | [§13 Testing](#13-testing) |
| R9 | Answer questions no single packet can, without special-casing the runner | [§16 The `analyze` layer](#16-the-analyze-layer) |
| R10 | Propose world-database rows, with every value's provenance visible | [§17 The `author` layer](#17-the-author-layer) |

---

## 2. Layering

Five layers, each usable on its own and testable without the one above it.
Data flows one way; no layer imports a layer above it.

```
 capture file
     |
 [wire]     pcap read -> TCP reassembly -> session-key recovery -> header
     |      decryption -> framing                       ==> Packet stream
     |
 [core]     registry lookup per opcode -> expansion (containers)
     |      -> decode                                   ==> Event stream
     |
 [modules]  one module per opcode: decode + how to print + how to store
     |
 [emit]     text / SQL / JSONL sinks consume Events
     |
 [analyze]  cross-opcode correlation: patrol routes, behaviour timelines
     |      -- findings re-enter the Event stream and reach the same sinks
     |
 [author]   world-table rows with per-value provenance   ==> migration file
            -- rules are sinks, so they see events and findings alike
```

Two data types cross every boundary, and only those two:

```python
Packet   # one framed message: seq, t, direction, opcode, name, body, via
Event    # one decoded fact: packet provenance, kind, data mapping
```

`Event.data` is a plain `Mapping[str, Any]` rather than a per-opcode class.
That is deliberate: the core, the templates, the SQL mapper and the JSONL
writer all treat it uniformly, and no core code ever needs to import a type
a module defined. A module may still build that mapping from a dataclass
internally.

---

## 3. Pattern analysis

The question posed was "maybe Strategy?". Strategy is the right *shape* for
one module, but on its own it does not solve the problem — it describes
swapping **one** interchangeable algorithm, and we need to select among
hundreds, keyed by a value read off the wire.

Options considered:

| Option | Adding an opcode costs | Verdict |
|---|---|---|
| **A. `if/elif` chain** (the prototype) | edit the core, forever growing | Rejected — violates R3 directly; the core ends up knowing every payload. |
| **B. Plain Strategy** (one context, one swappable algorithm) | n/a | Insufficient alone — no selection mechanism for ~825 candidates. |
| **C. Registry of strategies** (dispatch table + plugin discovery) | one new file | **Chosen.** Strategy stays the per-module contract; a registry keyed by opcode does the selection; discovery makes registration automatic. |
| **D. Visitor** | change the visitor interface, then every implementor | Rejected — grows in the wrong direction; a new opcode should not touch existing code. |
| **E. Chain of Responsibility** | one new file, but O(n) per packet and order-dependent | Rejected as the primary mechanism — no direct lookup, no coverage introspection. Fan-out of one opcode to several modules is handled by the registry instead. |
| **F. Declarative struct DSL** (a table describing each layout) | one table entry | Rejected as primary. Vanilla payloads are heavily conditional — flag-gated optional blocks, embedded splines, update masks — so the DSL would have to grow into a small language. A typed cursor (`ByteReader`) gets most of the brevity with none of the lost expressiveness. |

**Chosen: Registry of Strategies + segregated capability interfaces.**

Supporting patterns, each pulling its weight:

- **Interface Segregation.** A module *must* decode. Printing, SQL and
  container expansion are separate optional capabilities. A module that only
  knows how to decode declares nothing else, and the core notices the absence
  rather than requiring a stub.
- **Null Object.** A missing capability resolves to a no-op, so sinks contain
  no `if module is X` branching — only "does this module offer this
  capability".
- **Data Mapper.** Modules map an `Event` to rows; they never write SQL text.
  Quoting, batching and dialect live in one place.
- **Observer / Sink.** Emitters subscribe to the event stream; adding an
  output format does not touch decode code.
- **Template Method (optional convenience).** `BaseModule` supplies defaults
  so a short module stays short — but it is a convenience, not a requirement:
  the contracts are `Protocol`s, structurally typed.

### 3.1 Why registration is by symbol, not by number

Modules declare `opcodes = ("SMSG_MONSTER_MOVE",)`. Numbers are resolved at
load time from the opcode table parsed out of the server checkout
([§11](#11-source-derived-tables)). A fork that renumbers an opcode keeps
working; a symbol that no longer exists is reported at startup as a coverage
gap instead of silently decoding the wrong payload. Numeric registration is
accepted too, for opcodes absent from a given checkout.

---

## 4. Package layout

```
src/tortoise_capture/
  cli.py              subcommands: key, dump, decode, author, opcodes
  config.py           tct.toml + environment + flags, in that precedence
  log.py              four-level logging (error/warn/info/debug)
  core/
    contracts.py      Packet, Event, TableSpec, the Protocols
    registry.py       @module decorator, discovery, symbol resolution
    pipeline.py       framing + expansion -> Packet stream
    dispatch.py       Packet stream -> Event stream (the runner)
    reader.py         ByteReader: typed cursor, packGUID, cstring, GUID helpers
  wire/
    pcap.py           capture read, TCP reassembly, timestamps
    crypt.py          HeaderCrypt, known-plaintext session-key recovery
    framing.py        header layouts, session walk, desync detection
    opcodes.py        opcode table from the server checkout (+ cache)
  fields/
    tables.py         UpdateFields.h enum evaluation, object-type gating
    values.py         field value typing (float / two-short / bytes_0 / int)
  modules/            <-- the part that grows; core never imports from here
  emit/
    text.py           template renderer + section grouping
    sql.py            TableSpec -> DDL/INSERT, dialect handling
    jsonl.py          raw record / event dump
    migration.py      AuthoredRow -> world migration, with provenance
  analyze/            <-- also grows; patrol reconstruction, behaviour correlation
  author/             <-- also grows; one rule per world table it can propose
  world.py            read-only world-database lookups (no driver dependency)
```

---

## 5. Module anatomy

A complete, working module — this is the whole cost of supporting an opcode:

```python
@module(id="ai_reaction", opcodes=("SMSG_AI_REACTION",))
class AiReaction(BaseModule):
    """SMSG_AI_REACTION: a unit entered combat (Creature.cpp:2246)."""

    # --- decode: wire bytes -> facts -----------------------------------
    def decode(self, pkt: Packet, ctx: DecodeContext) -> Iterator[Event]:
        r = ByteReader(pkt.body)
        guid = r.u64()                      # raw guid here, not packed
        yield self.event(pkt, "ai_reaction",
                         guid=guid,
                         entry=guid_entry(guid),
                         reaction=r.u32())

    # --- text: format declared here, mapping done by the core ----------
    text_section = "AI reactions (aggro)"
    text_templates = {
        "ai_reaction": "entry={entry:<7} guid=0x{guid:016X} reaction={reaction}",
    }

    # --- sql: table shape declared here, writing done by the core ------
    sql_tables = (
        TableSpec(
            name="capture_ai_reaction",
            columns=(Column("capture", "VARCHAR(64)"),
                     Column("t", "DOUBLE"),
                     Column("guid", "BIGINT UNSIGNED"),
                     Column("entry", "INT UNSIGNED"),
                     Column("reaction", "INT UNSIGNED")),
            key=("capture", "t", "guid"),
        ),
    )

    def sql_rows(self, ev: Event, ctx: SqlContext) -> Iterator[Row]:
        yield Row("capture_ai_reaction",
                  {"capture": ctx.capture_id, "t": ev.packet.t, **ev.data})
```

Nothing in that file is referenced by name anywhere else in the codebase.
Dropping it into `modules/` is the entire integration step.

---

## 6. Module contracts

`core/contracts.py`, all `typing.Protocol` — structural, so a module can
implement them without inheriting anything.

```python
class Decoder(Protocol):                       # required
    id: str
    opcodes: Sequence[str | int]
    def decode(self, pkt: Packet, ctx: DecodeContext) -> Iterable[Event]: ...

class Expander(Protocol):                      # optional — containers
    def expand(self, pkt: Packet, ctx: DecodeContext) -> Iterable[Packet]: ...

class TextRenderer(Protocol):                  # optional
    text_section: str
    text_templates: Mapping[str, str]          # event kind -> format template
    def text_fields(self, ev: Event) -> Mapping[str, Any]: ...   # default: ev.data

class SqlEmitter(Protocol):                    # optional
    sql_tables: Sequence[TableSpec]
    def sql_rows(self, ev: Event, ctx: SqlContext) -> Iterable[Row]: ...
```

Conventional `Event.data` keys, honoured by the core for cross-cutting
filters and by nothing else:

| key | meaning |
|---|---|
| `entry` | `creature_template.entry` this event is about — feeds `--entry` |
| `guid` | full 64-bit wire GUID — feeds `--guid` |

A module that cannot supply them simply omits them; filtered runs then skip
its events, which is the correct behaviour (a chat emote carries no sender
GUID and genuinely cannot be attributed to an entry).

---

## 7. The dispatch loop

The entire runner, in essence:

```python
for pkt in pipeline.packets(capture, cfg):        # wire layer + expansion
    for mod in registry.for_opcode(pkt.opcode):   # 0..n modules
        for ev in mod.decode(pkt, ctx):           # the module's own business
            if not filters.accept(ev):
                continue
            for sink in sinks:
                sink.handle(ev, mod)              # text / sql / jsonl
```

An opcode with no module is counted as *seen but uncovered* and reported at
the end; it is never an error. That is what makes incremental coverage
comfortable: an unsupported opcode is data, not a failure.

### 7.1 The independence rule

The runner depends only on the contracts, never on any implementation:

1. `core/`, `wire/`, `emit/` and `cli.py` **never import from `modules/`**.
   Enforced by a test over the import graph.
2. The core **never branches on an opcode value or a module id**. The only
   opcode constants outside `modules/` are the two session-bootstrap opcodes
   in `wire/framing.py` (`SMSG_AUTH_CHALLENGE`, `CMSG_AUTH_SESSION`), which
   are a transport concern — they delimit the plaintext handshake that exists
   before any decoding does — and are documented as such where they are
   defined.
3. A module's output is consumed only through the contract: `Event`s are
   mappings, `Row`s are mappings, templates are strings. The core cannot be
   broken by *how* a module computes them, only by a module returning
   something the contract does not allow.
4. A module that raises is isolated ([§14](#14-failure-handling)); the run
   continues.

---

## 8. Logging and configuration

### 8.1 Four levels

`log.py`. A closed vocabulary of four, where the dividing line between `error`
and `warn` is **whether anything was lost**.

| level | content | stdout | stderr | log file |
|---|---|---|---|---|
| `error` | the work failed: desync, module exception, unparsable payload, row for an undeclared table | no | **yes** | yes |
| `warn` | suspicious, but the run continued: zero-filled TCP gap, missing checkout falling back to numeric-only, container size mismatch, module declaring an opcode this fork lacks | no | **yes** | yes |
| `info` | where the run has got to: files read, stream sizes, counts, milestones | yes | no | yes |
| `debug` | per-packet / per-field detail, offsets, decisions taken | yes | no | yes |

Rules:

- **Warnings share stderr with errors.** stdout carries the text report; a
  diagnostic line in the middle of it would corrupt that output.
- Warnings and errors are counted separately. Only errors change the exit
  code — a warning means the result is there, with a caveat.
- Every module gets its logger through `ctx.log`, named `tct.mod.<id>`, which
  is what makes per-module verbosity possible.
- The log file (`logs/<capture-stem>.log`) is appended per run with a
  timestamp separator and the invoking command line, so one file holds a
  target's whole processing history. It can be kept more verbose than the
  console.
- Output is ASCII-only and written UTF-8: the local console is cp1250 and a
  stray arrow character is a real, previously observed crash source.

Levels are thresholds on *handlers*, not on loggers: loggers stay permissive
and each destination filters per record. A level set on a logger could not
express "info everywhere, debug for this one module", and would outlive the
call that set it.

Exit codes: `0` clean, `2` completed with errors, `1` fatal (could not start).

### 8.2 The config file

`tct.toml`, next to the repository. TOML, so it reads as the plain
`parametr = hodnota` with `#` comments that it looks like, but values keep
their types — `port = 8090` is an integer, `file = true` is a boolean,
`[log.modules]` is a table — and `tomllib` is in the standard library, so it
costs no dependency. Everything in it is optional.

```toml
[log]
level = "info"          # console: error | warn | info | debug
file_level = "debug"    # the file may keep more than the screen shows

[log.modules]
update_object = "debug" # one module in detail, without the other 824
```

Precedence, lowest to highest: **built-in default → `tct.toml` → environment
→ command-line flag**. A config file holds what you would otherwise retype
every run (checkout path, port, verbosity); a flag overrides one of them for
one run.

Two consequences worth stating:

- `tct.toml` is **git-ignored** — it holds machine-specific paths.
  `tct.example.toml` is versioned and documents every key.
- Problems in the file (unknown key, bad level name) are collected while
  parsing and logged as warnings *after* the handlers exist — the file is what
  configures logging, so it cannot log during its own parse. The run continues
  on defaults; only a config path named explicitly and missing is fatal.

---

## 9. Text emission

The module declares the shape; the core does the mapping and all the
surroundings.

- `text_templates[kind]` is a `str.format` template. The core renders it with
  `text_fields(event)`, defaulting to `event.data`.
- The core owns everything around the template: the timestamp column, the
  section header, the `--entry` highlight marker, ordering, and the
  "(no records)" note for an empty section.
- Two layouts, `--text-layout`:
  - `grouped` (default) — one section per module, in registration order;
    reads like the prototype's report.
  - `stream` — strictly chronological across modules; reads like a timeline.
- A missing template for an emitted kind is an error naming the module and
  the kind, not a crash.

A module never calls `print`. That keeps output ordering, log interleaving
and file redirection a single concern.

---

## 10. SQL emission

Also declarative: the module states the table and the mapping, the core
writes the statements.

```python
TableSpec(name, columns, key=(), managed=True, conflict="ignore", comment="")
Column(name, type, nullable=True)
Row(table, values)
```

- `managed=True` (default) — a table this toolkit owns; the writer emits
  `CREATE TABLE IF NOT EXISTS` from the spec, then the inserts.
- `managed=False` — a table that already exists in `tw_world`
  (e.g. `creature_movement`): **no DDL is emitted**, only inserts. That is
  the difference between "here is what I captured" and "here is a migration
  for your world database", and getting it wrong would hand the user a
  `CREATE TABLE` for a table the server owns.
- `conflict` selects the insert form (`ignore` / `replace` / `plain`).
- Dialect: MySQL by default (the `tw_world` target), SQLite selectable for
  local analysis. Quoting, escaping, batching and `NULL` handling live in
  `emit/sql.py` alone — a module never builds a SQL string.
- Output is a file (`out/<stem>.sql`), not a live connection: this
  environment has no `mysql` client on PATH, and a reviewable file is the
  right artifact for something that will be applied to a world database.
  A direct-connection sink can be added later behind the same interface.

---

## 11. Source-derived tables

Opcode names/numbers and update-field indices are parsed at runtime from the
`tortoise-wow` checkout (`Opcodes_1_12_1.h` + `Opcodes.cpp`,
`UpdateFields.h`), never hardcoded — that is what keeps the toolkit correct
against a fork that renumbers anything.

Decision: **parse at runtime, cache on disk, never commit the result.**

- The cache key is the source files' size and mtime; a changed checkout
  invalidates it automatically.
- The cache lives under `.cache/` (git-ignored). A generated table checked
  into the repository would silently drift from the checkout it claims to
  describe — precisely the failure mode the live parsing exists to prevent.
- A missing or moved checkout is an error naming the path it looked at, and
  degrades to numeric-only output rather than aborting.
- `tct opcodes` prints the resolved table; `--coverage` adds which opcodes
  have a module.

`EUnitFields` names are valid **only** for units and pets. `fields/tables.py`
gates naming on the GUID high word (`0xF130`/`0xF140`); other object types
resolve to numeric indices until their own layouts are added. This was a real
bug in the prototype (a GameObject field printed as `UNIT_FIELD_HEALTH`) and
carries a regression test.

---

## 12. Coverage and roadmap

Support arrives one module at a time; the tool reports where it stands:

```
tct opcodes --coverage        # opcode -> module, and the gaps
tct decode ... --report       # end of run: seen / decoded / uncovered / failed
```

Order followed so far, highest content value first:

1. `SMSG_MONSTER_MOVE` / `_TRANSPORT` — waypoints (validated in the prototype)
2. `SMSG_UPDATE_OBJECT` — create/values blocks, combat stats
3. `SMSG_DESTROY_OBJECT` — the other half of the respawn story `analyze/
   behaviour.py` already documents: `Object.cpp:2685`'s `DestroyForNearby
   Players` (called from `Creature::DisappearAndDie`) is corpse fade-out, so
   a creature that leaves visibility this way needs a fresh CREATE to come
   back, and one that never does (confirmed empirically: zero records for
   Ralthas in the whole capture) explains why only VALUES-based health
   resets were ever seen for it
4. `SMSG_COMPRESSED_MOVES`, `SMSG_COMPRESSED_UPDATE_OBJECT` — containers
5. `SMSG_AI_REACTION`, `SMSG_PARTYKILLLOG`, `SMSG_SPELL_GO`, `SMSG_SPELL_START`
   — behaviour, the latter giving the cast's exact duration (`m_timer` read
   once at cast start, `Spell.h:432`), not one measured from packet gaps
6. `SMSG_CAST_RESULT` — the recording player's own cast success/failure;
   player-only and guid-less on the wire (`Spell.cpp:4569`), so it never
   carries an entry/guid key (same as `SMSG_PLAY_SOUND`) but is useful as a
   diagnostic for why a spell sample run looks sparse (out of range, not
   ready, interrupted, ...)
7. `SMSG_SPELL_FAILED_OTHER` — a cast in progress was cancelled, no reason
   given; unlike `SMSG_CAST_RESULT` this fires for *any* caster type
   (`Spell::cancel()`, `Spell.cpp:4926`), so it is the only wire signal a
   creature's own interrupted cast ever produces. In the Ralthas capture
   both observed occurrences are the player's own Fireball, paired exactly
   with `SMSG_CAST_RESULT(SPELL_FAILED_INTERRUPTED)` as the source predicts
   (`Spell.cpp:3706`) — the creature-caster case ships unexercised against
   real data, same honesty as `SMSG_PLAY_SOUND`'s own gap
8. `SMSG_MESSAGECHAT`, `SMSG_CREATURE_QUERY_RESPONSE` — identity, script text
9. `SMSG_LOGIN_VERIFY_WORLD`/`SMSG_NEW_WORLD`, `SMSG_PLAY_SOUND` — map, sound
10. `SMSG_ATTACKERSTATEUPDATE` — melee swing outcomes, observational only (see
    `modules/attacker_state.py`'s docstring: the damage on the wire is
    post-armor-mitigation *and* post-attack-power-bonus, empirically higher
    than `dmg_min/dmg_max` in the real capture, not lower as armor alone would
    predict — reversing it needs the target's armor, which needs a player
    field table this toolkit does not have yet, so it stays raw combat-log
    data rather than a stat-refinement source it cannot honestly be yet)
11. `SMSG_SPELLNONMELEEDAMAGELOG` — spell damage outcomes, same observational
    caveat as above but sharper: `modules/spell_damage_log.py`'s docstring
    traces `Unit::CalculateAbsorbResistBlock` (`Unit.cpp:2333`), which clamps
    the wire's `damage` to *zero* (not melee's floor of 1) once block+absorb+
    resist exceed it — so a fully-resisted hit and a barely-landing one are
    wire-indistinguishable at the low end, on top of needing the target's
    resistance at cast time to reverse at all
12. everything else, as the content being authored demands it

---

## 13. Testing

- **Synthetic packets.** Each module ships a test that builds its payload
  byte by byte from the documented layout and asserts the decoded `Event`.
  No capture needed, no privacy question, runs anywhere.
- **Golden output.** Small committed expected-text / expected-SQL files guard
  the template and mapping layers.
- **Regression tests** for the two known traps: object-type gating of unit
  field names, and the differing inner framing of the two compressed
  containers.
- **Opt-in integration.** Tests that want the real `Ralthas` session read
  `TCT_TEST_CAPTURE`; unset means skipped. **No capture, JSONL, log or
  extracted record is ever committed** — a capture contains the recorded
  account name, and `.gitignore` covers those formats by extension.
- **Architecture test.** Fails if anything under `core/`, `wire/`, `emit/` or
  `cli.py` imports `modules/`.

---

## 14. Failure handling

| failure | handling |
|---|---|
| module raises while decoding | caught per packet, logged as error with module id and packet seq, counted, run continues |
| stream desync (opcode above the fork's ceiling, or body length overrun) | error, that direction stops, the other direction still completes |
| unknown opcode | not an error — counted as uncovered, reported at the end |
| unsupported update-block type | error, that message stops (the offset is unrecoverable past it), the run continues |
| missing checkout or table | **warn** naming the path, numeric-only fallback — output is degraded, not lost |
| gap in the TCP stream | **warn** saying where and how many bytes; zero-filled, decode continues until it desyncs |
| container size mismatch | **warn**; the inflated body is used, since it parsed |
| module declares an opcode this fork lacks | **warn** at startup; that opcode stays uncovered |
| unknown key or level in the config file | **warn**, that key ignored, default used |

The principle: a single bad packet must never cost the run. Half a session
decoded with an accurate error count is far more useful than a traceback.

---

## 15. Decisions taken

| # | Decision | Rationale |
|---|---|---|
| D1 | Registry of strategies with auto-discovery | One file per opcode, zero core edits (R1–R3) |
| D2 | `Event.data` is a plain mapping | Uniform across text/SQL/JSONL; core never imports module types |
| D3 | Compressed containers are modules, not core code | Removes the last opcode branch from the runner |
| D4 | Registration by opcode symbol, resolved from source | Survives a fork renumbering; gaps are reported, not silently mis-decoded |
| D5 | Tables parsed at runtime, cached on disk, never committed | A committed table drifts from the checkout it describes |
| D6 | SQL to a file, MySQL dialect, `managed` flag per table | No mysql client available; world tables must not receive DDL |
| D7 | Four log levels; error vs warn = was anything lost | Keeps the vocabulary decidable; warnings do not change the exit code |
| D7b | TOML config file, flags override it, never committed | Typed values without a dependency; machine-specific paths stay local |
| D8 | Module errors are isolated and counted | Incremental development over 825 opcodes needs a forgiving runner |
| D9 | Python 3.14 + scapy only | Same as the prototype; no new runtime to maintain |

| D10 | Analyzers are Event-in, Event-out | Findings reach the text and SQL sinks through the module contracts, so the emit layer never learns analysis exists |

Built since: **`analyze/`** (see [§16](#16-the-analyze-layer)).

Deferred, with the seam already in place:

- **Live DB comparison** — diffing decoded content against `tw_world` and
  emitting only the delta. A second consumer of the same `TableSpec`s.
- **pcapng rewriting** — splicing decrypted headers back into the capture for
  Wireshark, a capability worth keeping from the local `wow_decrypt2`
  experiment. A sink over the `Packet` stream, below the module layer.

---

## 16. The `analyze` layer

A decoder answers "what does this packet say". An analyzer answers "what does
the session as a whole say" — the patrol behind 113 hops, the respawn timer
behind a death and the creature's next sighting, the trigger behind a line of
creature dialogue.

### 16.1 Shape

```python
class Analyzer(Protocol):
    id: str
    def feed(self, ev: Event) -> None: ...          # every decoded event, in order
    def finish(self, ctx: DecodeContext) -> Iterable[Event]: ...   # findings, at the end
```

The return type is the point: **a finding is an ordinary `Event`**. An
analyzer therefore declares `text_templates` and `sql_tables` exactly like an
opcode module, and its output reaches the same sinks through the same
contracts. The emit layer contains no analysis-aware code at all, and adding
an analyzer costs one file in `analyze/` — the same bargain `modules/` offers.

Discovery, ordering and the independence rule work the same way too:
`analyze/` is imported by string at runtime, nothing in `core/`, `wire/`,
`emit/` or `cli.py` may import it, and the architecture test enforces both
packages together.

Analyzers see the **filtered** stream, so `--entry` scopes analysis exactly as
it scopes output. `--no-analyze` skips them.

### 16.2 Why findings carry their evidence

An analyzer's output is inference, not decoding, and the two must not be
presented as if they were the same thing. So every finding carries the sample
count behind it, and refuses to round a guess into a conclusion:

- A text is attributed to a trigger only when **every** occurrence coincides
  with it. One coincidence out of three is reported as `text_untriggered`.
- A spell's repeat delay carries `confident: false` until enough intervals
  exist to bound it — the Ralthas capture holds exactly one, and one interval
  cannot recover an authored `min/max` (see
  [feasibility-ralthas-pr.md](docs/feasibility-ralthas-pr.md) §7).
- Intervals spanning a death are dropped: two engagements are not one cooldown.

### 16.3 Patrol reconstruction

Worth stating because the obvious approach does not work. Cutting the hop
stream at its first return to the start needs one uninterrupted lap to exist,
and in a real session there often is none — the Ralthas capture holds 113 hops
over ~2.7 laps with a fight, a death and a respawn in the middle, and not one
clean lap.

Instead: **cluster** hop destinations by proximity (one cluster is one
waypoint, since every lap re-broadcasts the same authored position), **count
the transitions** between clusters, then **walk** from the waypoint nearest the
spawn point along the busiest outgoing edge until the walk closes. Combat
detours drop out on their own — they are never the busiest edge — and a
capture that starts mid-route still numbers from the spawn, because the
respawn sighting pins the origin.

Validated against the live `tw_world`: all 41 distinct waypoints of Ralthas's
route, in the authored order, mean XY error 0.006 yards.

### 16.4 Attributing a sound with no sender

`SMSG_PLAY_SOUND` (`modules/play_sound.py`) carries a sound id and nothing
else — no caster, no target — so `behaviour.py` attributes a captured sound to
a line of dialogue purely by timestamp, and does it **across every
creature's dialogue at once**, not per entry: a sound is attributed only when
exactly one line, from any creature in the whole session, falls inside the
coincidence window. Two creatures talking in the same instant makes the
sound's owner a coin flip, and `_sound_attribution` leaves it unattributed
rather than guess — the same discipline as `text_trigger`'s
one-trigger-explains-every-occurrence rule, applied across entries instead of
within one.

This needed `_sounds: list[tuple[float, int]]` collected ahead of the usual
`entry is None -> return` guard in `feed()`, since a session-scoped fact by
definition has no entry to key on — the same category `Event.scope="session"`
exists for ([§17.5](#175-not-every-fact-is-about-a-creature)), though here the
cross-entry matching happens inside one analyzer rather than at the filter.

Ralthas's own capture contains no `SMSG_PLAY_SOUND` at all, so this path has
no real-capture validation — only the synthetic tests in `test_analyze.py`.
Said plainly rather than left to be discovered.

### 16.5 A respawn is not always a `CREATE`

The respawn timer was first built against a capture where the player died,
left, and came back — a fresh `CREATE` block on every respawn. A second
capture, recorded specifically to get enough repeat-cast samples for
[§16.2](#162-why-findings-carry-their-evidence)'s confidence threshold, was
taken by staying in sight of the creature the whole time — and its respawn
timer came back empty, on a capture with three confirmed deaths.

The cause: `CREATE_OBJECT` is sent when an object *enters* a client's known-
objects set, not on every state change. A player who never loses sight of the
creature never has it leave that set, so the server never needs to resend a
`CREATE` on respawn — it just resets `UNIT_FIELD_HEALTH` back up through an
ordinary `VALUES` block. `_respawn`'s only sighting source was `c.creates`, so
it had nothing to find, even though the respawn timing was sitting in the
capture the whole time (confirmed by hand: two `VALUES` blocks resetting
health off 0, timed 299.217 s and 300.022 s after their deaths — both within
a second of the authored 300 s).

`_Creature.revivals` is the second sighting source this needed: a `HEALTH>0`
reading (`_health_of`, scanning a CREATE or VALUES block's field list) is
counted only when it follows a confirmed death (`alive` flag, set by
`party_kill`) — an ordinary damage-then-heal cycle while already alive is not
a respawn, and does not need to be a "sighting" at all, since nothing was
lost. `_respawn` then merges `creates` and `revivals` into one sorted list of
"alive again" timestamps. Position stays CREATE-only — a `VALUES` block
carries no coordinates — but the timer does not need to, and now does not.

---

## 17. The `author` layer

Analysis says what the session showed. Authoring says what a world database
should therefore contain — a different claim, made to a different standard,
and kept in a different output.

### 17.1 A rule is a sink

```python
class AuthorRule(Protocol):
    id: str
    table: str
    def handle(self, ev: Event, mod: Any) -> None: ...   # the sink contract
    def close(self) -> None: ...
    def rows(self, ctx: AuthorContext) -> Iterable[AuthoredRow]: ...
    def gaps(self, ctx: AuthorContext) -> Iterable[str]: ...
```

Rules are registered as **sinks**, which is what makes them free: a sink
already receives every emitted event *and* every analyzer finding, in order,
and is already closed at the end of the stream. `tct author` is therefore the
decode pipeline with a different set of sinks — not a second traversal, and
not a line of new runner code.

One rule may fill several tables (`dialogue` fills three), because
`creature_ai_events` → `creature_ai_scripts` → `broadcast_text` share an id
convention that would otherwise have to be agreed on across files. The writer
groups by each row's own table, in first-appearance order, so the migration
applies in dependency order.

### 17.2 Provenance is the product

Every value carries where it came from — `WIRE`, `DERIVED`, `LOOKUP`,
`CONFIRMED` or `CONVENTION` — and each section of the migration states the
breakdown:

```sql
-- creature_template  (1 row(s))
--   confirmed   (matches the wire, restated as the database's own value -- a no-op): scale, dmg_min, ...
--   convention  (authoring convention, not observed): spell_list_id
-- NOTE: restated as the database's own value, a no-op -- already agreed with the wire ...
```

Four rules follow from taking that seriously:

1. **Nothing undecidable is defaulted.** A column the capture cannot support is
   omitted and listed under `NOT DERIVED`, with the reason. A zero meaning
   "not observed" is indistinguishable from a zero meaning "none" once it is in
   a table.
2. **An already-correct column is restated as the database's own value, not
   omitted, and not the wire's.** Broadcast floats are the server's *computed*
   values and sit ~3e-6 from the authored ones (§1.1 of
   [feasibility-ralthas-pr.md](docs/feasibility-ralthas-pr.md)), so writing
   the wire value back would nudge the stored one on every capture/author
   cycle. Restating it as what the database (read via a wide `DECIMAL` cast,
   not a plain `SELECT`'s ~6-digit display truncation) already holds keeps the
   migration's shape complete — matching a hand-authored one, which was the
   whole point of `fill_schema_defaults` too — while making the `SET` a
   genuine no-op. `provenance=CONFIRMED` marks the difference from a column
   whose value is actually changing.
3. **Floats are written to round-trip.** Nine significant digits is the IEEE
   guarantee for float32, so a proposed value lands in the column
   bit-identically instead of "tidily" moving.
4. Checked against tortoise-wow `37a2392e` at float32-bit precision (not
   decimal-rounded, which would hide a real difference as a false match):
   215 of 219 comparable fields identical, two real-and-small runtime deltas
   already measured and explained rather than rounded away, two honest gaps.

### 17.3 Matching a table's full width without a second copy of its schema

A hand-written migration sets every column a table has, including the ones
that are always `0` for this kind of content — `event_flags`,
`creature_ai_scripts.x/y/z/o`, seven unused `creature_spells` slots. Retyping
that shape here would be a second copy of the schema, liable to drift from
whatever the real table looks like (the same failure D5 already rejects for
opcode and field tables).

So `fill_schema_defaults` reads it from the table that will receive the row —
`DESCRIBE <table>`, cached per table — and widens with whatever a column's own
default is, tagged `CONVENTION`. A rule opts a column *out* by passing it in
`skip`, for the narrow case where a default is a real value that could be
wrong rather than harmless boilerplate:

- `creature.map` — default `0` is Eastern Kingdoms, a specific place, not
  "unknown". Always skipped; always a gap.
- `creature_spells.delayRepeatMin/Max` for a spell this capture watched
  actually repeat — default `0` means "does not repeat", which is known
  false. Skipped only while the observation stays low-confidence
  ([§16.2](#162-why-findings-carry-their-evidence)); a capture that clears
  `behaviour.py`'s sample threshold gets its measured range proposed instead.
- `creature.wander_distance` for a waypoint mover is set explicitly to `0`
  rather than left to the schema's default of `5`: the column has no reader
  at all for `WAYPOINT_MOTION_TYPE` (verified against `Creature.cpp`), so the
  default is inert but reads as a real value sitting next to `movement_type`.

Checked against the hand-authored PR this was validated on
([docs/feasibility-ralthas-pr.md](docs/feasibility-ralthas-pr.md)): 207 of 210
comparable fields identical, zero disagreements, and the three differences are
exactly the skipped columns — present in the PR's output as the numbers a
longer capture or a decoded target block would supply, and present in this
migration as a named gap instead.

### 17.4 The database as an input

`world.py` is the first place the toolkit reads the world database rather than
just comparing against it — resolving an item's display id to its entry needs
`item_template`. It shells out to the `mysql`/`mariadb` client instead of
taking a driver dependency (D9), is read-only by construction, and takes its
password from `TCT_DB_PASSWORD` rather than a config file or a command line.

Every lookup that can be cross-checked is: the item resolved from
`UNIT_VIRTUAL_ITEM_DISPLAY` must match the class, subclass and inventory type
packed into `UNIT_VIRTUAL_ITEM_INFO`, and a disagreement withholds the row
rather than guessing between two answers.

Without a database the command still runs; the lookups become gaps.

### 17.5 Not every fact is about a creature

Wiring `creature.map` up (from `SMSG_LOGIN_VERIFY_WORLD` /
`SMSG_NEW_WORLD` — `modules/world_transfer.py`) surfaced a real gap in the
filter model, not just a missing decoder: that event is not about any
creature at all — it is which map the *observing player* was on — so it has
no `entry` for `Filters.accept` ([§7.1](#71-the-independence-rule)) to match
against. Under a plain `--entry`-scoped `tct author` run, it would have been
silently dropped at the same line that correctly drops a sender-less emote —
both look identical to the filter (`entry` key absent), but mean opposite
things: one is *a fact about an unidentifiable creature*, correctly excluded
from a run scoped to a different one; the other is *not a fact about a
creature in the first place*, and has nothing for `--entry` to judge.

`Event.scope` is the fix: `"entry"` (the default) keeps the existing
behaviour exactly, `"session"` bypasses `--entry`/`--guid` entirely. It is a
narrow escape hatch, not a general one — `Filters.accept` checks it first and
returns `True` immediately, so a module opts a *specific* event into it
(`self.event(pkt, kind, scope="session", ...)`) rather than a whole class of
output becoming unfilterable by accident. Caught at the dispatch level, not
just unit-tested against one rule in isolation: `test_dispatch.py` proves a
session-scoped event reaches a sink under a non-matching `--entry`, and that
an ordinary entry-tagged event for a different entry still does not — the
regression that would matter is the escape hatch turning into a bypass.

### 17.6 What it does not do

It does not apply anything. The output is a file for a human to read, argue
with and run — which is why the gaps and the provenance are in the file itself
rather than in a log the reviewer will never see.
