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
| **Game client** | World of Warcraft **1.12.1 (build 5875)**, vanilla wire protocol. |
| **Recorded server** | Any vanilla-protocol server reachable by that client, including third-party ones. |

This repository is **standalone**. It is not a fork, submodule or mirror of
any other repository and carries no upstream remote. It only *references* a
`tortoise-wow` checkout at runtime, as a read-only source of truth for opcode
numbers and update-field indices (see [Source-derived tables](#source-derived-tables)).

## Status

Bootstrapping. The architecture is specified in [ARCHITECTURE.md](ARCHITECTURE.md);
opcode support is added one module at a time and its current extent is
reported by `tct opcodes --coverage`.

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
tct key Ralthas.pcap --port 8090

# 2. decrypt and frame the whole session into records
tct dump Ralthas.pcap --port 8090 --repo C:/WOW/source/tortoise-wow_AIBot/tortoise-wow

# 3. run every registered opcode module over those records
tct decode Ralthas.jsonl --entry 62635 --format text,sql
```

`--debug` turns on the detailed log level; errors always go to stderr.

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

## Data policy

**Captures and extracted records are never committed.** `*.pcap`, `*.pcapng`,
`*.jsonl`, session keys, logs and generated SQL are ignored by git. A real
capture contains the recorded account name (in `CMSG_AUTH_SESSION`) and is
treated as private test material.

Test captures (e.g. the `Ralthas` session) stay on the local machine and are
pointed at through `TCT_TEST_CAPTURE`; tests that need one skip themselves
when it is unset.

## Licensing discipline

All wire knowledge here is derived from the user's own `tortoise-wow` C++
source. No code or table is taken from GPLv3 projects such as
WowPacketParser or HermesProxy — they were assessed as cross-reference only.
Re-derive, do not copy.
