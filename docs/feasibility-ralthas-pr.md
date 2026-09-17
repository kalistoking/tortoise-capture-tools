# Feasibility: reproducing a hand-authored PR from a capture alone

Target: [`37a2392e`](https://github.com/tortoise-wow/tortoise-wow/commit/37a2392e1efdaf40f9062d3dabc5f73db2faed3c)
— *"Adding Ralthas guid, spawn point, spells, scripts, events, equipment, path,
and stats"*, 301 lines of hand-authored SQL across 7 tables.

The question: **how much of that could this toolkit have produced from the
`Ralthas` capture instead of a human doing it by hand?**

Every verdict below was measured against the capture and the live `tw_world`,
not estimated. Nothing here is aspirational.

## Verdict

| table | derivable from capture | blocker |
|---|---|---|
| `creature_template` (UPDATE, 9 fields) | **9/9** — exact | none |
| `creature` (spawn row) | **13/18** — rest are constant defaults | `map` id |
| `creature_movement` (42 waypoints) | **42/42** X/Y exact | Z within ~0.35 yd; needs loop detection |
| `creature_equip_template` | **1/1** — exact | needs an `item_template` lookup |
| `broadcast_text` (2 texts) | text, `chat_type`, `language_id` — exact | `sound_id`/`emote_id*` unobserved (are 0 here) |
| `creature_ai_events` (2 events) | event types by correlation | needs cross-opcode analysis |
| `creature_ai_scripts` (2 scripts) | generated from the two above | none |
| `creature_spells` | `spellId`, `delayInitial*` | **`delayRepeat*`, `castTarget`** |

Roughly **85–90 % of the PR's data content is recoverable**. The rest is
either a constant the authoring convention fixes anyway, or the one genuine
blocker in the last row.

---

## 1. `creature_template` — fully recoverable, verified exact

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

The raw uint32 slots carry full float precision (`1101115897` →
`20.211887…`); only the text report rounds to four places. SQL output must
emit the full value, not the display string.

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

Both deltas are float32 round-trip noise, not decode error.

`guid` is recoverable too, and exactly: the wire GUID
`0xF13000F4AB2787EA` masked to its low 24 bits is **2590698** — the PR's
`creature.guid`. (`entry` is bits 24–47, which is how the toolkit already
identifies creatures.)

`spawntimesecsmin/max = 300` follows from death t=90.307 → respawn
t=389.841 = **299.534 s**, rounded. `movement_type = 2` (waypoint) is
implied by the closed movement loop. The rest (`id2..4`, `wander_distance`,
`health_percent`, `mana_percent`, `spawn_flags`, `visibility_mod`) are the
constants every spawn row carries.

**Only `map` is genuinely absent** from the packets decoded so far — it
arrives at login (`SMSG_LOGIN_VERIFY_WORLD` / `SMSG_NEW_WORLD`), so it is one
small module away rather than unknowable.

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

## 5. `broadcast_text` — text exact, sound and emote unobservable here

| PR field | from capture |
|---|---|
| `male_text` / `female_text` | **exact strings**, both of them |
| `chat_type = 0` | `SMSG_MESSAGECHAT` msgtype `0x0B` (MONSTER_SAY) |
| `language_id = 0` | the language field, currently read and discarded |
| `sound_id = 0` | no `SMSG_PLAY_SOUND` in this capture |
| `emote_id1..3 = 0` | the two `SMSG_EMOTE` packets belong to the **player**, not Ralthas |
| `entry = 6263501/02` | authoring convention (`entry*100 + n`), not wire data |

Sound and emote are zero here, so nothing is lost — but a creature that does
play a sound would need an `SMSG_PLAY_SOUND` module to capture it.

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
`event_chance = 100` is a weak inference from two samples — defensible, but it
should be emitted as an assumption, not a measurement.

## 7. `creature_spells` — the one real blocker

| PR field | status |
|---|---|
| `spellId_1 = 1449` | **exact** — `SMSG_SPELL_GO` |
| `name = 'Ralthas'` | **exact** — `SMSG_CREATURE_QUERY_RESPONSE` |
| `delayInitialMin/Max = 0` | **supported** — aggro→first cast was +0.047 s, twice |
| `probability_1 = 100` | weak inference (it cast on every engagement) |
| `castTarget_1 = 1` | **not decoded** — needs the `SMSG_SPELL_GO` target block |
| `delayRepeatMin/Max = 12 / 17` | **not derivable from this capture** |

The repeat delay is the honest failure. This capture contains exactly **one**
repeat interval: 60.026 → 78.317 = **18.291 s**, which is not even inside the
PR's 12–17 s range. An observed interval is `random(min,max)` plus cast time,
GCD and target availability — recovering the authored bounds needs many
samples across a long fight, and even then it is statistical inference, not
extraction. A capture of one 30-second fight cannot produce those two numbers,
and pretending otherwise would be inventing data.

`castTarget` is different: the layout is known (`Spell.cpp:4662` target block),
it is simply not implemented yet.

---

## 8. What the toolkit would need

Ordered by value per unit of work:

1. **`analyze/` layer** (already designed, deferred) — waypoint loop detection,
   aggro/death correlation, per-entry timelines. Unlocks `creature_movement`,
   `creature_ai_events`, `creature_ai_scripts`.
2. **SQL authoring emitter** — a second consumer of the same `TableSpec`s that
   writes *world* rows (`managed=False`) instead of capture rows, with the
   `entry*100+n` id convention and `INSERT` ordering that satisfies foreign
   keys. See §9.
3. **`item_template` lookup** — the first case needing the world DB as an
   *input*. Either a read-only connection in config, or an exported
   display→entry map. This is a deliberate architectural change: the toolkit
   is file-in/file-out today, on purpose.
4. **Small decoder gaps**, each one file:
   - `SMSG_SPELL_GO` target block → `castTarget`
   - keep `language` in `messagechat` → `language_id`
   - `SMSG_LOGIN_VERIFY_WORLD` / `SMSG_NEW_WORLD` → `map`
   - `SMSG_PLAY_SOUND`, `SMSG_EMOTE` → `sound_id`, `emote_id*`
5. **Longer captures** for anything statistical (`delayRepeat*`,
   `probability`). Not a code problem.

## 9. Proposed SQL output shape

The existing SQL path writes `capture_*` observation tables. Authoring output
is a different thing and should stay separate rather than overloading it:

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
  the point is a reviewable delta, as §10 of ARCHITECTURE.md argues.

## 10. Bottom line

For a creature like Ralthas — one that spawns, patrols, aggros, talks, casts
one spell, dies and respawns inside the capture — this toolkit could have
produced **everything in that PR except the spell repeat delays and
`castTarget`**, with stats and geometry matching the hand-authored values to
float precision.

The work is not in the decoding, which already reaches these numbers. It is in
the `analyze/` layer that turns 113 hops into 42 waypoints and three
timestamps into two AI events, and in an authoring emitter honest enough to
mark its own guesses.
