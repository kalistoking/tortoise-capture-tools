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
| **Game client** | The latest publicly released [Turtle WoW](https://turtle-wow.org) client, whatever that is at the time — Turtle ships new content patches regularly. It wraps an unmodified vanilla **`WoW.exe` (1.12.1, build 5875)** — verified by reading that executable's own `FileVersion` resource — which is what actually speaks the wire protocol this project decodes, regardless of which Turtle patch is current. |
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

The module system works end to end. Nine modules cover ten opcodes: creature
query, monster chat, party kill log, spell go, AI reaction, update object,
monster move (+ transport) and the two compressed containers.

Support grows one module at a time — see
[docs/adding-an-opcode.md](docs/adding-an-opcode.md) — and the current extent
is reported by `tct opcodes --coverage`. The design behind it is in
[ARCHITECTURE.md](ARCHITECTURE.md).

The working prototype this project supersedes lives outside the repository
(`C:\WOW\source\extract_wow_data`) and is deliberately **not** imported: its
validated wire knowledge is documented in [docs/wire-format.md](docs/wire-format.md)
and re-implemented against the architecture here.

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
```

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
  emit/      text / SQL / JSONL sinks
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
