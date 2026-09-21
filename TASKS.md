# tct — what is open, what it depends on

*`ARCHITECTURE.md` says why things are the way they are; this says what is left
and in what order. Every task carries its acceptance checks, and a task is not
done until they are ticked against a measurement.*

**Written from outside.** The board session assembled this file on 2026-09-21
from this repository's own README and `docs/`, so the board would have
something true to read rather than a guess. Every entry below cites where it
came from. **The tct session owns this file** — correct it, and the board
follows on the next `python board.py`.

Numbering is by area, not by priority: **M** the module system and what it
decodes, **A** authoring and what a capture can and cannot support, **D**
design questions still open, **P** housekeeping. The board
(`../board/board.html`) reads this file and trt's with one parser, so the shape
matters: a task is `### <ID> · <title> — <phase>`, its acceptance checks are
`- [ ]` boxes under it, and `**Blocked by:**` is a line of its own.

What trt has asked of this project is **not** listed here. It lives in
`../extract_wow_data/handoff/`, one file per request with the reply appended,
and the board reads those files directly — one fact in one place.

---

## M — the module system and what it decodes

### M1 · Opcode coverage, one module at a time — backlog

The part that grows. Twenty-three modules cover 47 opcodes today; the recipe
is `docs/adding-an-opcode.md` and the current extent is not a number anyone
maintains by hand.

- [ ] the next opcode a capture needs, decided by what a capture needs
- [ ] `tct opcodes --coverage` is the measure, not this line

**Blocked by:** nothing.

### M2 · A wandering creature, to check a gate built without one — blocked

`MAX_SINGLE_VISIT_FRACTION = 0.5` now gates `patrol_route` beside the combat
check, and it was chosen to refuse a case **no capture here has ever seen**: a
creature with `movement_type = 1` that wanders at random, whose hops look
peaceful and would author a fabricated waypoint route
(`docs/feasibility-rakameg-pr.md` §6).

The two captures to hand keep their verdicts — Ralthas confident (41 waypoints,
0% single-visit), Rakameg refused (11, 73%) — so the gate is measured on both.
The wanderer itself is reasoning, not a measurement, until one is recorded.

- [ ] a capture of a creature with `movement_type = 1`
- [ ] `patrol_route` refuses it, and the single-visit fraction says why
- [ ] whichever way it lands, the number is written down beside the other two

**Blocked by:** a capture of a wandering creature.

---

## A — authoring, and what a capture can support

### A1 · The route a combat capture fabricates — **done**

The headline finding of the second creature, and worth more than a clean pass
would have been: the capture technique that helped spell timing hurt movement
and respawn inference (`docs/feasibility-rakameg-pr.md`).

- [x] found against a second, independently chosen creature, authored blind
- [x] the gate refuses the fabricated route rather than authoring it
- [x] `closes_loop` measured to be no help — Rakameg's fabricated route closes
      its loop exactly as Ralthas's genuine one does
- [x] the document keeps its original finding rather than editing it away

### A2 · A spell's repeat delay, as a bound rather than a value — **done**

An observed cast-to-cast gap measures `random(min,max)` **plus** cast time, so
the gap and the authored parameter are two different quantities and no decoder
could ever close that. It went from a named gap to a named, honestly-inexact
bound — which is the §5.1 answer, not a workaround for missing one.

- [x] `delayRepeatMin/Max` proposed as a bound, labelled as one
- [x] the longer capture's three deaths give two gaps rather than one
- [x] the bound holds on the second creature and gets tighter

### A3 · The Z the server ground-snaps at runtime — **closed, not ours**

`position_z` differs from the hand-authored row by 4.6e-5 yd and `orientation`
by 1.4e-6 rad; waypoint Z sits within ~0.35 yd. The server ground-snaps at
runtime against its own mmaps, so the broadcast Z is the snapped one and the
authored one is not. Both are correct; they are not the same quantity.
Inventing agreement between them would be the failure, not the difference.

Measured: 215 of 219 comparable columns identical at float32 precision, zero
disagreements (`docs/feasibility-ralthas-pr.md`).

### A4 · Three assumptions that fit two creatures rather than a rule — **done**

An audit asked which parts encode a property of *these two captures* rather
than a general rule (`6d39ee5`, `docs/feasibility-rakameg-pr.md` §6).

- [x] `creature_equip_template` read by offset — `UNIT_VIRTUAL_ITEM_DISPLAY` is
      Size:3, and a shield or a bow used to arrive unnamed and be dropped
- [x] a later `CREATE` that disagrees with the first on an authored column is
      reported rather than discarded, narrowed to authored columns
- [x] `SMSG_MESSAGECHAT`'s emote form under `--entry` — checked and **left
      alone**: it carries no sender guid, so no per-entry migration can claim it

---

## D — design questions still open

### D1 · Whether this toolkit follows trt onto Rust — blocked

Measured, not estimated: ~6 670 source lines and 2 508 test lines
(`docs/feasibility-rust-port.md`). The recommendation is **a trigger, not a
verdict** — do not port now, do not rule it out.

- [ ] **(a)** opcode coverage plateaus — the exploratory phase ends, and
      Python's specific advantage ends with it, **or**
- [ ] **(b)** trt genuinely needs in-process typed decode, so the benefit
      changes capability rather than convenience
- [x] insurance meanwhile: the JSON contract stays stable — it is the seam a
      port would preserve
- [x] insurance meanwhile: the dependency on the dynamic event payload is not
      deepened — it is the one thing a port has to renegotiate either way

**Blocked by:** neither trigger having fired.

### D2 · A REST/API layer, if one is ever wanted — blocked

The most port-hostile kind of code to write twice. Build it **after** D1 is
settled, or accept up front that it is throwaway work. As of 2026-09-20 trt
renders this tool's authored output without one, so nothing is waiting on it.

**Blocked by:** D1.

---

## The dependency graph, in one picture

```
M1 (opcode coverage) ── open-ended, and the measure is --coverage
M2 (a wanderer) ──blocked on a capture nobody has recorded

A1, A2, A4 ── done, against two creatures authored blind
A3 ── closed: a runtime snap is not a decode error

D1 (Rust) ──blocked on a trigger: coverage plateaus, or trt needs typed decode
D2 (REST) ──blocked by D1
```
