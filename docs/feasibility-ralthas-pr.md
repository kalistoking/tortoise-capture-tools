# Feasibility: reproducing a hand-authored PR from a capture alone

Target: [`37a2392e`](https://github.com/tortoise-wow/tortoise-wow/commit/37a2392e1efdaf40f9062d3dabc5f73db2faed3c)
— *"Adding Ralthas guid, spawn point, spells, scripts, events, equipment, path,
and stats"*, 301 lines of hand-authored SQL across 7 tables.

The question: **how much of that could this toolkit have produced from the
`Ralthas` capture instead of a human doing it by hand?**

Every verdict below was measured against the capture and the live `tw_world`,
not estimated. Nothing here is aspirational.

## Verdict

Superseded by an actual field-by-field run, not an estimate — see
[§9](#9-sql-output-shape-built): every table now widens to its real column
count via the target's own schema, checked against the PR column by column,
**at float32-bit precision** (not decimal-rounded, which would have hidden a
genuine difference as a false match).

This verdict was re-measured after a second, much longer capture (several
patrol loops, multiple full fights to and past death, 3 deaths/respawns
instead of 1) specifically recorded to settle the one statistical question
the first capture could not: a spell's repeat delay. See the note under
`creature_spells` below for what that capture did, and did not, settle.

| table | result |
|---|---|
| `creature_template` (UPDATE, 9 fields) | **9/9 float32-exact** — restated as the database's own value where it already agreed (§1.1, `CONFIRMED` provenance), so this is no longer an approximation |
| `creature` (18 columns) | **16/18 float32-exact**, 2 near (`position_z` 4.6e-5 yd, `orientation` 1.4e-6 rad — real, small, already-documented deltas from runtime ground-snap and timing, not decode error; §2) |
| `creature_movement` (42 waypoints × up to 9 columns) | **42/42** rows, XY exact, Z within ~0.35 yd (ground-snap, same cause as above) |
| `creature_equip_template` (4 columns) | **4/4** |
| `broadcast_text` (2 rows × 12 columns) | **12/12** per row |
| `creature_ai_events` (2 rows × 15 columns) | **15/15** per row |
| `creature_ai_scripts` (2 rows × 22 columns) | **22/22** per row |
| `creature_spells` (90 columns) | **88/90 float32-exact, 2 now derived but not matching** — `delayRepeatMin/Max` is no longer withheld (30 samples from the longer capture bound it at 13–27s), but that bound does not bit-match the PR's authored 12–17s, and by design cannot: see below |

**215 of 219 comparable fields float32-bit-identical.** Of the remaining
four: two (`position_z`, `orientation`) are real physical noise of a few
parts in 100,000, already measured and explained rather than rounded away;
two (a spell's `delayRepeatMin/Max`) are **derived, not withheld, and not an
exact match** — an honest disagreement with a well-understood cause, not a
decode error (§7).

---

## 1. `creature_template` — fully recoverable

The PR sets nine fields. Eight come straight out of the `CREATE` block's
UpdateFields; the ninth is a convention.

| PR value | wire field | decoded |
|---|---|---|
| `scale = 1` | `OBJECT_FIELD_SCALE_X` | 1.0 |
| `dmg_min = 20.211887` | `UNIT_FIELD_MINDAMAGE` | 20.2119 |
| `dmg_max = 24.467083` | `UNIT_FIELD_MAXDAMAGE` | 24.4671 |
| `attack_power = 44` | `UNIT_FIELD_ATTACK_POWER` | 44 |
| `unit_class = 2` | `UNIT_FIELD_BYTES_0` byte 1 | 2 (Paladin) |
| `ranged_dmg_min = 17.192947` | `UNIT_FIELD_MINRANGEDDAMAGE` | 17.1929 |
| `ranged_dmg_max = 23.640301` | `UNIT_FIELD_MAXRANGEDDAMAGE` | 23.6403 |
| `ranged_attack_power = 36` | `UNIT_FIELD_RANGED_ATTACK_POWER` | 36 |
| `spell_list_id = entry` | — | authoring convention |

### 1.1 Correction: the floats are not bit-exact, and it matters

An earlier version of this document called this section "exact". Measured
against the live database, that is true of the integers and not quite true of
the floats:

| column | database (authored) | wire (broadcast) | apart |
|---|---|---|---|
| `dmg_min` | 20.2118873596 | 20.2119007111 | 6.6e-7 |
| `dmg_max` | 24.4670829773 | 24.4671001434 | 7.0e-7 |
| `ranged_dmg_min` | 17.1929473877 | 17.1928997040 | 2.8e-6 |
| `ranged_dmg_max` | 23.6403007507 | 23.6403007507 | **0** |
| `scale` | 1.0 | 1.0 | **0** |
| `attack_power`, `ranged_attack_power`, `unit_class` | 44 / 36 / 2 | 44 / 36 / 2 | **0** |

Damage is broadcast *after* the stat system has run, so what reaches the wire
is the server's computed value, not the number an author typed. The two agree
to about six significant figures, which is far past anything observable in
play — but writing the broadcast value back into the column it came from would
nudge it every time, and a capture/author/capture loop would walk it.

**Resolved, not just worked around.** The authoring emitter diffs against the
database, and when a column already agrees within `AGREEMENT_TOLERANCE`
(1e-4 relative), it restates that column as **the database's own value** —
read via a wide `DECIMAL` cast so the restatement is the true stored float32,
not MySQL's ~6-digit default display truncation — rather than the wire's.
`provenance` for it becomes `CONFIRMED`, not `WIRE`. Applying that `SET` is
therefore a genuine no-op, and — the part worth stating plainly — the
restated value is **float32-bit-identical to the PR's own**, verified above,
not merely close. A value that does not already agree still gets the wire's
own reading, written with nine significant digits (the IEEE float32 round-trip
guarantee) so it at least lands bit-identically to what the packet said.

## 2. `creature` — spawn row from the respawn, not the first sighting

The `CREATE` block at first sighting (t=12.904 s) shows Ralthas **mid-patrol**
at `(-9177.97, -1026.83)` facing `+0.590` — useless as a spawn row. The
capture also contains his **respawn** at t=389.841 s, and that one is the
spawn point:

| | PR (hand-authored) | decoded from respawn `CREATE` | delta |
|---|---|---|---|
| `position_x` | -9129.660156 | -9129.660156 | **0** |
| `position_y` | -1098.790039 | -1098.790039 | **0** |
| `position_z` | 73.660652 | 73.660698 | 4.6e-5 |
| `orientation` | -2.321688652038574 | -2.321690082550049 | 1.4e-6 |

Neither delta is float32 round-trip noise (that would be exactly 0, as X and
Y are) — both are small, genuine differences between the live runtime value
and the static authored one: `position_z` from the server's mmap ground-snap
adjusting height at runtime, `orientation` plausibly from the same kind of
runtime recompute. Real, tiny, and worth keeping honest about rather than
rounding into a false "exact".

`guid` is recoverable too, and exactly: the wire GUID
`0xF13000F4AB2787EA` masked to its low 24 bits is **2590698** — the PR's
`creature.guid`. (`entry` is bits 24–47, which is how the toolkit already
identifies creatures.)

`spawntimesecsmin/max = 300` in the PR, a single authored value with no
observable min/max spread. The first (short) capture measured this from one
death-to-respawn gap — 299.534 s, rounded — which cannot distinguish min from
max on its own. The longer capture's 3 deaths give **two** such gaps
(299.217 s, 300.022 s), tight enough (0.8 s spread) to be confident this is
sampling noise around a single fixed timer rather than genuine randomisation:
`spawntimesecsmin/max = 299 / 300`, a near-exact match to the PR's `300`.
`movement_type = 2` (waypoint) is implied by the closed movement loop. The
rest (`id2..4`, `wander_distance`, `health_percent`, `mana_percent`,
`spawn_flags`, `visibility_mod`) are the constants every spawn row carries.

Those two gaps were only visible at all because of a fix made for this
capture specifically: the observing player never lost sight of Ralthas
across any of its 3 deaths, so no fresh `CREATE` block ever followed a
respawn — each one showed up only as a `VALUES` block resetting
`UNIT_FIELD_HEALTH` off 0 (`analyze/behaviour.py`'s "a respawn is not always
a CREATE"). The position row emitted from this same capture is therefore the
*first sighting*, not a post-respawn `CREATE` — it happens to land on the
exact same coordinates as the original short capture's respawn `CREATE`
above only because Ralthas's spawn point is a fixed location the recording
began at, not because this capture found a second respawn `CREATE` of its
own.

**`map` — done.** `SMSG_LOGIN_VERIFY_WORLD` / `SMSG_NEW_WORLD` (identical
20-byte body: `uint32 mapId` + `Vector4`, `CharacterHandler.cpp:574`,
`Player.cpp:2826`) is exactly the opcode this section predicted, and the
Ralthas capture contains one: `map_id = 0`, matching the PR exactly. It is not
about the creature itself — it is the *observing player's* map — which is
why `Event.scope="session"` had to exist: a plain `--entry`-scoped run would
otherwise drop it before it ever reached the authoring rule, the same way an
emote correctly drops for lacking a sender guid. Wired into `creature.map`
(§9, ARCHITECTURE.md §17.5).

## 3. `creature_movement` — geometry yes, exact Z no

Already measured against the live table ([baseline-ralthas.md](baseline-ralthas.md)):
all 42 waypoints present, **mean XY error 0.005 yd (sub-centimetre)**, max Z
error 0.354 yd.

Two caveats for authoring:

- The capture yields **113 linear hops** (≈2.7 patrol loops), not 42 ordered
  waypoints. Collapsing them needs loop detection and de-duplication — the
  deferred `analyze/` layer, not a decoder.
- **Z is approximate.** The server ground-snaps at runtime, so the broadcast Z
  is what the creature stood on, not what an author typed. Emitting decoded Z
  into `creature_movement` would be committing sniffed drift. Either keep the
  authored Z when a row already exists, or accept a ≤0.35 yd delta and say so.

`orientation`, `waittime`, `wander_distance`, `script_id` are all 0 in the
PR — not derivable, but also not needed.

## 4. `creature_equip_template` — recoverable through one DB lookup

The PR sets `equipentry1 = 5276`. The wire carries the item's **display id**,
not its entry:

```
UNIT_VIRTUAL_ITEM_DISPLAY = 5010
UNIT_VIRTUAL_ITEM_INFO    = 285346306 = 0x11000A02
                            -> class 2, subclass 10, inventory_type 17
```

`SELECT entry FROM item_template WHERE display_id = 5010` returns exactly one
row: **5276**, *"Monster - Staff, 3 Piece Taped Staff"*, whose
`class/subclass/inventory_type` (2 / 10 / 17) match the packed
`UNIT_VIRTUAL_ITEM_INFO` bytes. The lookup is unambiguous and self-checking.

This is the first output that **needs the world database as an input**, not
just as a comparison target — worth deciding deliberately (see §8).

## 5. `broadcast_text` — text exact, sound wired up but unexercised here

| PR field | from capture |
|---|---|
| `male_text` / `female_text` | **exact strings**, both of them |
| `chat_type = 0` | `SMSG_MESSAGECHAT` msgtype `0x0B` (MONSTER_SAY) |
| `language_id = 0` | the language field (now kept by the chat module) |
| `sound_id = 0` | schema default — no `SMSG_PLAY_SOUND` in this capture |
| `emote_id1..3 = 0` | the two `SMSG_EMOTE` packets belong to the **player**, not Ralthas |
| `entry = 6263501/02` | authoring convention (`entry*100 + n`), not wire data |

`sound_id` is built now (`modules/play_sound.py` decodes the opcode,
`behaviour.py._sound_attribution` matches it to a line by timestamp,
attributing only when exactly one line across *every* creature's dialogue
falls inside the window — the same discipline `text_trigger` applies within
one, generalised across entries because the opcode has no sender to key on).
Ralthas never plays a sound, so this path stays unexercised against real
data; the synthetic tests in `test_analyze.py` are what it stands on
(ARCHITECTURE.md §16.4).

## 6. `creature_ai_events` + `creature_ai_scripts` — correlation, and it is clean

The timestamps make the event types unambiguous:

```
t= 59.979  SMSG_AI_REACTION (reaction=2)  +  SAY "The Brotherhood Stands for Justice!"
t= 60.026  SMSG_SPELL_GO 1449                          (+0.047 s after aggro)
t= 78.317  SMSG_SPELL_GO 1449
t= 90.307  PARTYKILLLOG + HEALTH=0        +  SAY "The lies of Stormwind, must be told!"
t=389.841  respawn CREATE
t=395.152  SMSG_AI_REACTION (reaction=2)  +  SAY "The Brotherhood Stands for Justice!"  (repeat)
t=395.199  SMSG_SPELL_GO 1449                          (+0.047 s after aggro, identically)
```

Text 1 fires on aggro **twice, to the millisecond** → `event_type = 4`
(`EVENT_T_AGGRO`). Text 2 fires exactly at death → `event_type = 6`
(`EVENT_T_DEATH`). `creature_ai_scripts` then follows mechanically:
`command = 0` (talk), `dataint` = the `broadcast_text` entry.

This is exactly the cross-opcode correlation the `analyze/` layer exists for.
`event_chance = 100` was a weak inference from two samples in the original
capture; the longer capture's 3 full engagement cycles show the same pair of
lines firing at the same two triggers every time (`3x, offset +0.000s` for
both), a somewhat firmer basis for "always" — but 3 is still a small sample,
so this stays an assumption made explicit, not a measurement.

## 7. `creature_spells` — one bounded rather than matched, one resolved differently than planned

| PR field | status |
|---|---|
| `spellId_1 = 1449` | **exact** — `SMSG_SPELL_GO` |
| `name = 'Ralthas'` | **exact from the short capture** (`SMSG_CREATURE_QUERY_RESPONSE`); the longer capture never contains this opcode at all — the client had the template cached from the earlier session — so `name` falls back to the numeric entry there. Expected, honest fallback, not a regression in the decoder. |
| `delayInitialMin/Max = 0` | **supported** — aggro→first cast was +0.046–0.047 s, consistently across all 3 engagements in the longer capture |
| `probability_1 = 100` | authoring convention, matches the table's own default |
| `castTarget_1 = 1` | **matches**, but as a schema default with a caveat — see below |
| `delayRepeatMin/Max = 12 / 17` | **derived from the longer capture: 13 / 27 — bounds it, does not bit-match it** |

### The repeat delay: resolved into an honest bound, not an exact match

The original (short) capture contained exactly **one** repeat interval:
60.026 → 78.317 = **18.291 s**, not even inside the PR's 12–17 s range — one
sample cannot support min *or* max, so `tct author` correctly left both
columns out entirely rather than guess.

A second, deliberately longer capture was recorded specifically to settle
this: several patrol loops, full fights carried past multiple deaths, staying
near the creature so more of its cast cycle would be sampled. That capture
gives **30** repeat intervals, and `analyze/behaviour.py` now reports
`13.0–27.3 s`, which `tct author` writes into
`delayRepeatMin_1 = 13, delayRepeatMax_1 = 27`.

That is *not* the PR's `12 / 17` — and by the mechanism itself, it cannot be.
An observed cast-to-cast gap measures `random(min,max)` **plus** cast time,
global cooldown and however long retargeting/line-of-sight took, so it is a
noisy upper bound on the true parameter, not the parameter itself. The data
matches that prediction exactly: the observed **minimum** (13 s) lands within
a second of the authored minimum (12 s) — there is little room below it for
overhead to hide — while the observed **maximum** (27 s) sits 10 s above the
authored maximum (17 s), because occasional real-world delay (one bad angle,
one longer reposition) pulls the sample's ceiling up but nothing pulls a
sample's floor down. Thirty samples turned "no data" into a real, bounded,
honestly-labelled estimate; they did not, and structurally could not, turn
into the PR's own hand-tuned numbers. `tct author`'s note says exactly this
rather than presenting `13–27` as if it settled the question.

`castTarget` was never a pending decoder — that framing was wrong, and worth
correcting rather than quietly dropping. `Spell.cpp:4662`'s target block
carries who a spell *hit*: the resolved guid, after the AI already chose it.
`castTarget` is a different thing — the *rule* the AI used to make that
choice (`GetTargetByType()`, `CreatureAI.cpp:230`: nearest enemy, self,
whoever provoked it) — and that rule is consulted server-side before the cast
and never serialized in any packet. No capture, however complete, can recover
it; there is no wire representation to decode.

That this column's own `DESCRIBE creature_spells` default is `1`, and that
the PR itself writes `1` into every one of its eight slots including the
seven it never uses, is the tell: whoever authored that migration could not
read it off a capture either, and used the common default. This toolkit does
the same, schema-filled with an explicit note that it is a default, not a
measurement — which is the ceiling of what is possible here, not an interim
step before a decoder that could never exist.

---

## 8. What the toolkit would need

Ordered by value per unit of work. The first three have since been built.

1. ~~**`analyze/` layer**~~ — **done**. `patrol` reconstructs the route,
   `behaviour` correlates aggro/death/respawn and cast timing
   (ARCHITECTURE.md §16).
2. ~~**Authoring emitter**~~ — **done**. `tct author --entry N` writes a world
   migration with per-field provenance (§9, ARCHITECTURE.md §17).
3. ~~**`item_template` lookup**~~ — **done**. A read-only accessor shelling out
   to the `mysql` client, configured under `[database]`; no driver dependency.
4. ~~**Full-width authoring output**~~ — **done**. `fill_schema_defaults`
   reads a target table's own `DESCRIBE`, so an unused `creature_spells` slot
   or a `creature_ai_scripts` boilerplate column is proposed from the
   table's own default instead of being left out (§9,
   ARCHITECTURE.md §17.3). Confirmed the same way as everything else here:
   checked field by field against the PR.
5. ~~**`SMSG_LOGIN_VERIFY_WORLD` / `SMSG_NEW_WORLD` → `map`**~~ — **done**
   (`modules/world_transfer.py`). Confirmed against the real capture:
   `map_id = 0`, matching the PR exactly. Needed a real architecture fix
   alongside it: the map is a fact about the *session*, not about any one
   creature, so it has no `entry` to filter by -- under a plain `--entry` run
   it would have been dropped the instant it reached the filter, the same
   way `Filters` correctly drops an emote with no sender guid. `Event.scope`
   ("entry" | "session") is the fix, checked at the dispatch level, not just
   unit-tested against the rule in isolation (ARCHITECTURE.md §17.5).
6. ~~**`SMSG_SPELL_GO` target block → `castTarget`**~~ — **withdrawn**, not
   built. Verified against the server source that this was never a pending
   decoder to begin with: `castTarget` is the AI's target-*selection rule*
   (`CreatureAI.cpp:230`), consulted before a cast and never put on the wire
   in any form. What `Spell.cpp:4662`'s target block carries is who the
   spell *hit* -- the resolved outcome, not the rule that picked it. No
   decoder could ever close this gap; §7 above corrects the earlier framing.
   ~~keep `language` in `messagechat`~~ — **done**
7. ~~**`SMSG_PLAY_SOUND` → `broadcast_text.sound_id`**~~ — **done**
   (`modules/play_sound.py`). The opcode is a clean 4-byte body (`Object.cpp`
   `PlayDirectSound`) with no sender guid at all, so attribution is
   timestamp-coincidence across *every* creature's dialogue at once, matching
   only when exactly one line qualifies (`behaviour.py._sound_attribution`,
   ARCHITECTURE.md §16.4). The Ralthas capture contains no `SMSG_PLAY_SOUND`,
   so this path ships with zero real-capture validation — synthetic tests
   only, said plainly rather than left implicit.
8. ~~**Longer captures** for anything statistical (`delayRepeat*`)~~ — **done,
   with an honest ceiling.** A capture recorded specifically for this (several
   patrol loops, fights carried past multiple deaths) gave 30 repeat
   intervals, enough to bound `delayRepeatMin/Max` at `13–27 s` against the
   PR's `12/17` — closer at the floor than the ceiling, exactly as the
   overhead-adds-to-the-gap model in §7 predicts. This was never a code
   problem, and the result confirms it: more samples produced a tighter,
   still-honest bound, not the authored numbers themselves, because an
   observed gap and a cast parameter are two different quantities.

## 9. SQL output shape (built)

The existing SQL path writes `capture_*` observation tables. Authoring output
is a different thing and stays separate rather than overloading it:

- **`tct author --entry N`** — a distinct subcommand, so nobody mistakes
  observations for a migration.
- Emits a single migration file shaped like the PR itself:
  `sql/database_updates/world/<timestamp>_world.sql`, sections in dependency
  order (`broadcast_text` → `creature_ai_scripts` → `creature_ai_events` →
  `creature_equip_template` → `creature_template` → `creature` →
  `creature_movement` → `creature_spells`).
- **Every field that is inferred rather than read carries a SQL comment
  saying so**, and every field that could not be derived is left out of the
  statement rather than defaulted silently. A reviewer must be able to see
  which numbers came off the wire and which are assumptions.
- Values that exist in the DB already are diffed, not blindly re-inserted —
  the point is a reviewable delta, as §10 of ARCHITECTURE.md argues. For
  Ralthas this means all eight stat columns come back "already correct in the
  database, so not re-stated" and only `spell_list_id` is proposed.

## 10. Bottom line

For a creature like Ralthas — one that spawns, patrols, aggros, talks, casts
one spell, dies and respawns inside the capture — this toolkit produces
**215 of the PR's 219 comparable fields float32-bit-identical**: 53 rows
across 8 tables, at the PR's own full column width, ids matching the
hand-authored ones. Of the four it does not reproduce bit-for-bit: two
(`position_z`, `orientation`) are real, small, already-measured runtime
differences (ground-snap, timing) rather than decode error; two (one spell's
`delayRepeatMin/Max`) went from a named gap to a named, honestly-inexact
estimate once a longer capture supplied enough samples — `13–27 s` bounding
the PR's `12/17`, closer at the floor than the ceiling because an observed
cast-to-cast gap measures more than the pure random parameter it is standing
in for.

The work was never only in the decoding, which already reached most of these
numbers from the first, short capture. It was in the `analyze/` layer that
turns dozens of hops into 41 waypoints and a handful of timestamps into two
AI events; in `fill_schema_defaults` reading each table's own column defaults
instead of a second copy of its schema; in restating a confirmed value as the
database's own reading (a true no-op) rather than the wire's computed one,
closing what had been a ~3e-6 approximation into an exact match; in detecting
a respawn from a `VALUES` health reset once a longer capture's player never
lost sight of the creature, so no fresh `CREATE` ever came; and in an emitter
honest enough to mark its own guesses as guesses — including the one that
matters most, stating plainly that 30 samples produced a real bound on a
repeat delay, not the authored parameter itself, and why those are not the
same thing.
