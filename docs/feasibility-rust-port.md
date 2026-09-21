# Feasibility: porting this toolkit to Rust

`trt` (the interactive editor that consumes this tool's output) is written in
Rust. This document asks whether this toolkit should follow, so that one
person maintains one toolchain instead of two.

The answer is not "can it be done" — it can, and with less friction than a
rewrite usually implies. The question worth answering is **what a shared
language actually buys, what it costs while opcode coverage is still growing,
and what would have to be true before the trade turns positive.**

## What is actually here

Measured, not estimated:

| part | lines | what it is |
|---|---|---|
| `modules/` | 2 143 (22 files) | one decoder per opcode; a large share is the wire-layout docstrings that cite the server's own C++ |
| `core/` | 1 064 | module contracts, registry, dispatch, `ByteReader` |
| `author/` | 766 | one rule per world table, with provenance and confidence gates |
| `emit/` | 557 | text / SQL / JSONL / migration / JSON sinks |
| `analyze/` | 546 | patrol and behaviour correlation |
| `wire/` | 423 | pcap read, header crypto, framing, opcode table |
| `fields/` | 227 | UpdateFields tables and value typing |
| top level | 944 | CLI, config, logging, world-database access |
| **source total** | **6 670** | |
| tests | 2 508 | synthetic packets plus real-capture validation |

Roughly 9 200 lines. Small enough that the size is not the obstacle.

## The dependency story gets *better*, not worse

This is the finding that most changes the shape of the answer. A port does
not need to replace a stack of Python libraries, because there is almost no
stack to replace:

- **`scapy` is the only third-party dependency**, it is confined to one file
  (`wire/pcap.py`), it is imported lazily, and it is used for four symbols:
  `rdpcap`, `IP`, `TCP`, `Raw`. Reading a pcap file offline and pulling TCP
  payloads out of it is what `pcap-file` + `etherparse` do in a few hundred
  lines, and the pcap container format itself is a 24-byte global header plus
  a 16-byte record header per packet.
- **The crypto needs no library at all.** `AuthCrypt` is byte arithmetic —
  `plain = ((cipher - j) & 0xFF) ^ key[i % 40]` (`wire/crypt.py:40-48`) — and
  the session-key recovery is a subtraction over known-plaintext header bytes.
  Both port essentially verbatim and run faster.
- **The world database is already reached by shelling out** to the
  `mysql`/`mariadb` client rather than a driver (`world.py:11`), a decision
  taken for its own reasons that happens to remove an entire class of port
  work: `std::process::Command` does the same job.
- **Parsing the server's C++ headers** for opcode numbers and field indices is
  four regexes (`fields/tables.py:31-32`, `wire/opcodes.py:28-29`) — the
  `regex` crate, or hand-rolled scanning.

So the crate list for a port is roughly: a pcap reader, a TOML parser, a regex
engine. That is a **smaller** external surface than today, and it removes
`scapy`, which is by far the heaviest thing this project installs.

## What ports nearly verbatim

The majority of the code. `ByteReader` and its named-field reads
(`r.u32("gold")`) are a better fit for Rust than for Python. The 22 decoders
are mechanical struct parsing with the layout already documented and
source-cited in each file — and those docstrings are language-independent
knowledge that carries straight over as comments. The header crypto, the
framing loop, the field tables, the opcode resolution and the synthetic-packet
tests all translate close to one-for-one.

One thing actively improves. This project currently compares authored values
at **float32 precision** while computing in Python's f64 (see the drift tables
in [`feasibility-ralthas-pr.md`](feasibility-ralthas-pr.md) and
[`feasibility-rakameg-pr.md`](feasibility-rakameg-pr.md)); Rust has a native
`f32` and would represent the wire's own values exactly, rather than
round-tripping them through a wider type and comparing with a tolerance.

## What does not port: three Python-shaped things

### 1. Registry and discovery

`@module(id=..., opcodes=(...))` plus a runtime `pkgutil` walk
(`core/registry.py:32-42`) is what makes "drop a file into `modules/` and
nothing else in the codebase names it" mechanically true — a property
ARCHITECTURE.md §7.1 treats as load-bearing, not cosmetic.

Rust reaches the same place with `inventory` or `linkme` (link-time
registration, no central table to edit). The ergonomic loss is one line:
`modules/mod.rs` gains a `mod foo;`. Small, but it is a real weakening of the
exact property the current design is proud of, and it should be acknowledged
rather than discovered later.

### 2. Text templates

Modules declare `text_templates[kind]` and the sink renders them with
`template.format_map(mod.text_fields(ev))` (`emit/text.py:53`) — runtime
substitution of names into a string chosen at runtime. Rust's `format!` is
compile-time only, so this needs a small runtime formatter (a `strfmt`-style
crate, or about forty lines).

Worth noting is the *error path*, not just the happy one. `emit/text.py:58`
logs "template does not match its fields" — a runtime mismatch that this
project has already hit for real (the `object_destroy` template referencing
`{entry}` when the key was absent for a non-entry-bearing GUID). A Rust port
should convert that class of bug into a startup or test-time check instead of
reproducing it as a runtime log line.

### 3. The dynamic event payload — the real fork in the road

`Event.data` is a `dict[str, Any]` that flows from `decode()` through the
text, SQL, JSONL, analyze and author layers. There are **83 read sites** of
that payload across the source. This is the load-bearing translation
decision, and it splits into two genuinely different projects:

**(a) `HashMap<String, Value>`.** Fastest route, keeps the architecture
one-for-one, every current design property survives. The cost is `Option`
friction at all 83 sites, and — more importantly — it *preserves* the
missing-key error class rather than eliminating it. That class is not
hypothetical here; it has already produced one real bug.

**(b) Typed events per kind.** The Rust-shaped answer: the missing-key bug
becomes a compile error, and the decoders get materially clearer. But the
generic sinks currently work precisely *because* they can iterate an unknown
payload's keys; typed events mean trait-based dispatch through every sink, and
the author rules — which read fields across event kinds — need rethinking too.
This is a redesign wearing a port's clothes, and it should be planned as one.

The estimate differs by a large factor between (a) and (b). Choosing between
them is the first decision of any port, not something to settle mid-flight.

A fourth, smaller item: there are **100 `yield` sites** (52 in `modules/`, 24
in `author/`). Rust has no stable generators, so each becomes either an
`impl Iterator` or — simpler and entirely adequate for an offline batch tool —
a returned `Vec`. Mechanical, but it touches every decoder.

## What a shared language actually buys

**It does not buy integration.** `trt` runs this tool as a subprocess and
reads JSON from it. That boundary works, and it works identically whether the
two sides share a language or not. "One tech stack" on its own is a
cosmetic property here.

**It does buy two real things:**

1. **One toolchain for one maintainer.** One `cargo build`, one test runner,
   one set of idioms to hold in mind. For a solo maintainer moving between two
   projects daily, this is a genuine, recurring cost saved — not a large one
   per instance, but paid every single time.
2. **The actual prize: `trt` could depend on a `tct-core` crate.** No
   subprocess, no JSON round-trip, typed access to decoded events, and live
   re-decode inside the editor. This is the only benefit here that changes
   what is *possible* rather than what is *convenient*.

But (2) deserves an explicit decision rather than arriving by drift: the
two-project split — this tool stays a pure analysis CLI, `trt` stays the
editor — was chosen deliberately. A shared `tct-core` crate does not
necessarily violate it (core as a library, CLI as a thin binary, `trt` as a
second consumer), but it does couple the two repositories far more tightly
than a documented JSON contract does. That coupling is the thing to weigh,
and it should be weighed on purpose.

## What it would cost, measured against this project's own standards

Every one of the 22 decoders earned its correctness the same way: verify the
layout against the server's C++ with a `file:line` citation, write the failing
synthetic test, implement minimally, validate against a real capture. The
traps are specific and unforgiving — packed versus raw GUIDs on the same
opcode family, a `randomPropertyId` that is signed despite being written
through an unsigned cast, an unconditional `fallTime`. A port must re-earn
every one of those guarantees; translating the code is not the same as
inheriting its correctness.

The mitigation, though, is unusually strong — stronger than most rewrites get:

- **The oracle already exists.** Two ground-truth content PRs (Ralthas,
  Rakameg), a baseline document of known-good numbers, a 41/41 waypoint match
  independently verified against a live database at sub-centimetre accuracy,
  and a habit of checking byte-identical output before and after a refactor.
- **Both implementations can be run against the same capture at every step.**
  A port of correctness-critical wire code is only safe at all because of
  this; it is what turns "rewrite and hope" into "differential testing".

So the technical risk is low. The cost is time and attention, not uncertainty.

## The argument against doing it now

The dominant activity in this project is still adding one opcode at a time
through a TDD cycle: read the C++, write a failing test, decode, validate
against a real capture, repeat. Coverage is 47 opcodes and still growing.

That workflow is exactly where Python's fast edit-run loop pays and where
Rust's compile step and type friction tax hardest. Rust's return on investment
arrives in the maintenance phase — refactoring a large stable codebase without
fear. **This project is not in that phase yet**, and porting now would trade
the advantage it currently needs for one it cannot yet use.

## Recommendation: decide a trigger, not a verdict

Do not port now, and do not rule it out. Port when either condition holds:

- **(a) Opcode coverage plateaus.** The exploratory phase ends, Python's
  specific advantage ends with it, and the maintenance phase where Rust pays
  begins.
- **(b) `trt` genuinely needs in-process typed decode.** The benefit becomes
  something that changes capability rather than convenience, and the
  `tct-core` crate justifies itself on its own merits.

Until one of those is true, two pieces of cheap insurance keep the option
open: keep the JSON contract stable (it is the seam a port would preserve),
and avoid deepening the dependency on the dynamic event payload, since that is
the one thing a port has to renegotiate either way.

One interaction worth flagging: if a REST/API layer is ever added here, it
should be built **after** the port question is settled, or accepted up front
as throwaway work. It is the most port-hostile kind of code to write twice,
and — as of 2026-09-20 — `trt` already renders this tool's authored output
without one.

## If it happens, the order that de-risks it

1. **`tct-core` crate first, decoders last.** `ByteReader`, header crypto,
   framing, pcap reading, opcode and field tables. Acceptance: recovers the
   same session key and frames the same packet count from the same capture.
2. **Decoders in the existing TDD rhythm**, one module at a time, each
   validated against its own synthetic tests *and* a real capture — the same
   discipline that produced them, not a bulk translation.
3. **`analyze/` and `author/` last.** They hold the heuristics and confidence
   thresholds (`MIN_RESPAWNS_FOR_CONFIDENCE`, `MAX_COMBAT_HOP_FRACTION`,
   `MIN_INTERVALS_FOR_CONFIDENCE`), which are the least mechanical and most
   judgement-laden part of the codebase.
4. **Acceptance gate: byte-identical `tct author` output for both Ralthas and
   Rakameg**, including the gap lists, which are as much a part of the
   contract as the values.

No big-bang cutover. The two implementations must be diffable on the same
capture at every step; that property is the entire reason this is safe.

## Bottom line

Technically feasible and lower-risk than a typical rewrite: the codebase is
small, the external dependencies would *shrink*, the crypto and database
access need no libraries at all, and a strong validation oracle already
exists. Three things need real design rather than translation — module
discovery, runtime text templates, and above all the dynamically-typed event
payload that 83 sites read.

The case against is not technical, it is about timing. A shared language buys
one maintainer one toolchain, and buys `trt` the option of linking this tool
as a crate instead of shelling out to it — but it buys nothing for
integration, which already works across the process boundary. Meanwhile the
exploratory, one-opcode-at-a-time phase that Python suits best is still the
phase this project is in.

**The honest framing of this decision is not "can we port it" — it is "is the
exploratory phase over?". Right now it is not.**
