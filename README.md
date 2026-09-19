# tortoise-capture-tools

Offline toolkit that turns a captured WoW 1.12.1 world session into
authorable server content: `creature_movement` waypoints, `creature_template`
combat stats, and behaviour timelines (aggro, spells, chat, death, respawn).

A capture is decrypted **from the capture alone** — the 40-byte session key is
recovered by a known-plaintext attack on the encrypted client headers, so no
cooperation from the recorded server is required.

## What this repository is for

| | |
|---|---|
| **Consuming project** | `tortoise-wow` — the private vanilla-protocol server fork whose `tw_world` database the extracted content is authored into. |
| **Game client** | Turtle WoW client, **1.18.1-7272-Hotfix-2026-04-12**. |
| **Recorded server** | Any vanilla-protocol server reachable by that client, including third-party ones. |

This repository is **standalone**. It is not a fork, submodule or mirror of
any other repository and carries no upstream remote. It only *references* a
`tortoise-wow` checkout at runtime, as a read-only source of truth for opcode
numbers and update-field indices (see [Source-derived tables](#source-derived-tables)).

## Intended use and disclaimer

This tool exists to support development of `tortoise-wow` — reconstructing
authorable server content from a capture of a session the person running the
capture is themselves a party to. It is not intended for, and must not be
used for, unauthorized access to any system, interception of communications
you are not a party to, or any other unlawful purpose. Using it against a
server without that server's or account owner's permission may violate that
server's terms of service and applicable law; that is the user's
responsibility to determine before running it, not this project's.

The author provides this software "as is", under the license below, and
disclaims all liability for how anyone else chooses to use it. See
[License](#license), which carries its own no-warranty and
limitation-of-liability terms (AGPL-3.0 §§15–16).

## Status

The module system works end to end. Seventeen modules cover 19 opcodes:
creature query, monster chat, party kill log, spell go, spell start (cast
time), own cast success/failure, a cast interrupted (the only wire signal for
a *creature's* own interruption — `SMSG_CAST_RESULT` is player-only), AI
reaction, update object, object destroy (leaves visibility), monster move
(+ transport), the two compressed containers, map (login/teleport), sound,
and melee and spell damage outcomes (`SMSG_ATTACKERSTATEUPDATE`,
`SMSG_SPELLNONMELEEDAMAGELOG` — both observational only, see
[ARCHITECTURE.md §12](ARCHITECTURE.md): the wire's damage figure is
post-mitigation, not the raw `creature_template` roll).

Two analyzers answer what no single packet can: `patrol` reconstructs a
creature's route from the hops it broadcast (validated against the live
database — all 41 waypoints in the authored order, mean XY error 0.006 yards),
and `behaviour` correlates across opcodes to derive respawn timers, which
trigger fires which line of dialogue, and spell cast timing. Findings carry
their sample counts, and say so when a capture is too short to support a
conclusion.

`tct author` then turns those observations into proposed world-database rows,
matching a target table's full column width by reading its own schema
(`DESCRIBE`) rather than a second, hand-maintained copy of it — the same
never-hardcode-what-the-source-already-knows rule the opcode and field tables
follow. Checked field by field against a hand-authored content PR, at
float32-bit precision: 215 of 219 comparable columns identical, zero
disagreements; the rest are two tiny already-measured runtime deltas
(ground-snap, timing) and one spell's repeat delay bounds, named as a gap
rather than guessed at
([docs/feasibility-ralthas-pr.md](docs/feasibility-ralthas-pr.md)).

Support grows one module at a time — see
[docs/adding-an-opcode.md](docs/adding-an-opcode.md) — and the current extent
is reported by `tct opcodes --coverage`. The design behind it is in
[ARCHITECTURE.md](ARCHITECTURE.md).

The working prototype this project supersedes lives outside the repository and
is deliberately **not** imported: its validated wire knowledge is documented in
[docs/wire-format.md](docs/wire-format.md) and re-implemented against the
architecture here.

## Requirements

- Python 3.14
- `scapy` (only third-party dependency)
- A `tortoise-wow` checkout, for opcode and update-field tables

## Quickstart

```bash
pip install -e .

# 1. recover the session key from the capture
tct key capture.pcap --port 8090

# 2. decrypt and frame the whole session into records
tct dump capture.pcap --port 8090 --repo /path/to/tortoise-wow

# 3. run every registered opcode module over those records
tct decode capture.jsonl --entry <creature_template.entry> --format text,sql

# 4. propose world-database rows for one creature, provenance included
tct author capture.jsonl --entry <creature_template.entry>
```

`tct author` writes a migration for a human to review, never one that applies
itself: every value says whether it was read off the wire, inferred, resolved
against the database or fixed by convention, and anything the capture could not
support is listed at the end instead of being defaulted to zero.

## Configuration

Copy `tct.example.toml` to `tct.toml` and set what you would otherwise retype
every run. It is TOML — `parametr = hodnota`, `#` comments, `[section]`
groups — and every key is optional.

```toml
[capture]
repo = "/path/to/tortoise-wow"
port = 8090

[log]
level = "info"          # console: error | warn | info | debug
file_level = "debug"    # the log file may keep more than the screen shows

[log.modules]
update_object = "debug" # detail for one module, without the other 824
```

Precedence: built-in default → `tct.toml` → environment (`TCT_REPO`,
`TCT_PORT`, `TCT_SERVER_IP`, `TCT_LOG_LEVEL`, `TCT_CONFIG`) → command-line
flag. So `--log-level debug` overrides the file for a single run, and
`--debug` is a shortcut for it.

An optional `[database]` section points `tct author` at a read-only world
database for the things that need one: resolving an item's display id to its
entry, confirming a value before proposing to change it, and reading a target
table's own column defaults so the output matches its full width. The password
is not a config key — set `TCT_DB_PASSWORD` instead.

`tct.toml` is git-ignored (machine-specific paths); `tct.example.toml` is
versioned and documents every key.

Logging has four levels — `error`, `warn`, `info`, `debug`. **Errors and
warnings always go to stderr**, info and debug to stdout, so the text report
on stdout is never polluted by diagnostics. An error means something was
lost (the exit code becomes 2); a warning means the result is there with a
caveat.

## Layout

```
src/tortoise_capture/
  core/      module contracts, registry, dispatch loop, byte reader
  wire/      pcap reassembly, header crypto, framing, opcode table
  fields/    UpdateFields tables and value typing
  modules/   one module per opcode — the part that grows
  analyze/   cross-opcode analyzers: patrol routes, behaviour correlation
  author/    one rule per world table it can propose rows for
  emit/      text / SQL / JSONL / migration sinks
docs/        wire-format knowledge, how to add an opcode
tests/       synthetic-packet and golden tests
```

Tests run with `pytest` after `pip install -e ".[dev]"`, or without it via
`python tests/run_tests.py`.

## Documentation

| | |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | the module system and why it is shaped that way |
| [docs/adding-an-opcode.md](docs/adding-an-opcode.md) | the one-file recipe for new opcode support |
| [docs/wire-format.md](docs/wire-format.md) | verified packet layouts, with source references |
| [docs/baseline-ralthas.md](docs/baseline-ralthas.md) | the known-good numbers regressions are measured against |
| [docs/feasibility-ralthas-pr.md](docs/feasibility-ralthas-pr.md) | how much of a hand-authored content PR a capture can reproduce, measured |
| [docs/prototype-notes.md](docs/prototype-notes.md) | what the superseded prototype was, and what is already ruled out |

## Data policy

**Captures and extracted records are never committed.** `*.pcap`, `*.pcapng`,
`*.jsonl`, session keys, logs and generated SQL are ignored by git. A real
capture contains the recorded account name (in `CMSG_AUTH_SESSION`) and is
treated as private test material.

Test captures stay on the local machine and are pointed at through
`TCT_TEST_CAPTURE`; tests that need one skip themselves when it is unset.

## License

[GNU Affero General Public License v3.0](LICENSE) (AGPL-3.0-or-later) — the
same license `tortoise-wow` itself uses. Any modified version, including one
only ever run as a network service and never distributed as a binary, must
make its complete source available to users of that service (AGPL-3.0 §13).

## Licensing discipline

All wire knowledge here is derived from the user's own `tortoise-wow` C++
source. No code or table is taken from GPLv3 projects such as
WowPacketParser or HermesProxy — they were assessed as cross-reference only.
Re-derive, do not copy. Choosing AGPL-3.0 for this project's own license is a
separate decision from that discipline: it is about what this project grants
downstream, not about what it took from anyone upstream.
