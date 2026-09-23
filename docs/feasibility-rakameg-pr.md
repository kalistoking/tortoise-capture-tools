# Feasibility: a second creature, to test generalisation rather than fit

Target: [`8e8b12c1`](https://github.com/tortoise-wow/tortoise-wow/commit/8e8b12c1bb2a03b1502b2af8e648c5dc0d711124)
— *"Adding Temple of Agamaggan - Razorfen Downs"*, which adds four creatures
(62676–62679). This capture recorded one of them, **Death Prophet Rakameg**
(entry 62679); the other three are visible in the same PR but were not the
capture's subject and are not scored here.

## Why this document exists, and how it differs from the Ralthas one

[`feasibility-ralthas-pr.md`](feasibility-ralthas-pr.md) established what
this toolkit *can* reproduce from a capture. It could not, on its own,
establish whether that result generalises, or whether it fit one creature's
particular shape by coincidence. This is the second, independently-chosen
test case the project's own methodology called for: capture a creature whose
PR is already known, author it blind, then diff against the PR — same
discipline, different creature, to see what breaks.

Something did break, and it is more informative than a clean pass would have
been.

**Status: fixed.** Both weaknesses below were found, then closed the same
session, against this exact capture — see the "Fixed" note at the end of
each. This document keeps its original findings rather than editing them
away, because the fix is only meaningful in light of what it replaced.

## Headline finding: the capture technique that helped spell timing hurt movement and respawn inference

Both Ralthas and Rakameg were recorded with the same deliberate technique:
stay near the creature, re-engage it repeatedly, so `analyze/behaviour.py`
gets enough repeat-cast samples to bound `delayRepeatMin/Max` (see the
Ralthas document's own §7 on why one sample cannot). For Rakameg that meant
**14 separate engagements** — and that same repetition, for a creature this
PR authors as a *stationary* boss (`movement_type = 0`, `wander_distance = 0`
on its one spawn, guid 2591199), fed the `patrol` analyzer enough
combat-repositioning movement to construct a confident-looking, entirely
spurious patrol route:

| | PR (hand-authored) | this toolkit, before the fix | after |
|---|---|---|---|
| `movement_type` | **0** (stationary) | **2** (waypoint) | *not proposed* |
| `creature_movement` rows for guid 2591199 | **0** | **11**, from 54 hops across 43 clusters | **0**, named as a gap |
| `spawntimesecsmin/max` | **86400 / 86400** (effectively "does not naturally respawn") | **25 / 25** | *not proposed*, named as a gap |

Both wrong values carry the *same* provenance labels (`derived`,
`convention`) this toolkit uses for its genuinely-validated Ralthas output —
nothing in the authored file distinguishes "confident and right" from
"confident and wrong" here. That is the finding worth acting on, more than
either number individually: **`analyze/patrol.py` cannot currently tell a
real patrol loop apart from scattered combat-repositioning hops gathered
across many artificial re-engagements with a stationary creature**, and
`analyze/behaviour.py`'s respawn timer cannot tell a genuine death-to-respawn
gap apart from whatever produced this one 24.6-second gap — almost certainly
not this boss's real (24-hour-plus) respawn behaviour, but some
reset/re-kill during a recording session built to maximise spell samples,
not to observe a natural death.

Neither is a decoding error. The wire data says what it says; `move_linear`
events genuinely came from entry 62679's own guid, and the health-reset that
triggered the respawn finding genuinely happened. The gap is interpretive:
both analyzers assume a stream of "natural" behaviour, and a capture
deliberately engineered to be combat-dense is not that, for a creature that
does not patrol.

## Verdict, by table

| table | result |
|---|---|
| `creature_template` (UPDATE, 7 comparable fields) | **7/7** float32-close, same ~1e-6 relative broadcast drift as Ralthas (§1) |
| `creature` (spawn fields) | **guid, position, orientation, map: exact.** `spawntimesecsmin/max` and `movement_type` were wrong before the fix below; now correctly withheld as gaps (§2) |
| `creature_movement` | **0/0**, matching the PR — this toolkit fabricated 11 before the fix, now proposes none (§2) |
| `creature_equip_template` | gap — no database configured this session, same mechanism as Ralthas §4, not a new limitation |
| `broadcast_text` | **2/2** rows, text exact, `chat_type` correctly read as **YELL** (Ralthas's was SAY) — first real evidence the chat-type distinction generalises (§4) |
| `creature_ai_events` + `creature_ai_scripts` | correlation clean, but `creature_ai_scripts.datalong` was wrong until a later run *with a database* exposed it (§4a) |
| `creature_spells` | both spells' `delayRepeatMin/Max` bounded and *tighter* than Ralthas's own (more samples); `delayInitial` now inside the authored range for both — one spell was wrong until mid-fight re-aggros stopped counting as engagement starts (§3) |

## 1. `creature_template` — the drift pattern holds

| column | PR (authored) | wire (broadcast) |
|---|---|---|
| `scale` | 1.60000002384186 | 1.60000002 — float32-identical |
| `dmg_min` | 242.87294 | 242.873001 |
| `dmg_max` | 312.931793 | 312.932007 |
| `attack_power` | 124 | 124 — exact |
| `unit_class` | 2 | 2 — exact |
| `ranged_dmg_min` | 45.505024 | 45.5050011 |
| `ranged_dmg_max` | 62.569408 | 62.5694008 |

Same order of magnitude of drift as Ralthas's own stats (§1.1 of that
document) — a few parts in a million, consistent with the same cause
(damage is broadcast after the stat system runs, not as the authored
literal). No database was configured this session, so these ship as `wire`
provenance rather than `confirmed`-restated; the mechanism that would close
that gap is already built (Ralthas §1.1), just not exercised here.

The PR's UPDATE also touches `speed_walk`, `speed_run` and `unit_flags`,
which this toolkit does not decode into `creature_template` at all — not a
regression, just outside what a `CREATE` block's UpdateFields currently
covers, and worth naming rather than leaving implicit. `spell_list_id`
matches by the same authoring convention as Ralthas (`= entry`, confirmed
against this PR's own `UPDATE ... SET spell_list_id = entry WHERE entry IN
(62676, 62677, 62678, 62679)`).

## 2. `creature` and `creature_movement` — where this capture broke the tool

Position, orientation, `guid` (2591199) and `map` (129) are exact, same as
Ralthas. The two derived fields are not, and the reason is the same for
both: this specific creature does not naturally do the things the analyzers
assume every creature does.

**`movement_type` / `creature_movement`.** The PR spawns Rakameg with
`movement_type = 0, wander_distance = 0` — a boss that stands still except
when fighting. `analyze/patrol.py` (`Patrol.feed`) collects every
`move_linear` hop for a guid regardless of *why* the creature moved; across
14 engagements, combat repositioning produced 54 hops that happened to
cluster into 43 groups and chain into an 11-point loop the loop-closing
heuristic accepted. Nothing in `patrol_route`'s provenance (`derived`) flags
this as lower-confidence than Ralthas's own real, independently-verified
41-waypoint route (that document's own baseline, cross-checked against the
live database). A reviewer reading only the authored file has no way to
tell the two apart.

**Fixed.** `_Route` now records each hop's timestamp alongside its position,
and independently tracks the same aggro→death "engagement" window
`analyze/behaviour.py` already uses for spell timing (kept separate, not
shared state — analyzers each see the raw stream and keep their own view of
it). `combat_hop_fraction()` is the share of a route's hops whose timestamp
falls inside one of those windows; a `patrol_route` finding now carries this
fraction and a `confident` flag (`MAX_COMBAT_HOP_FRACTION = 0.5`). Checked
against both real captures rather than picked blind: Ralthas's own validated
route is **5%** combat hops, Rakameg's fabricated one is **100%** — a gap
wide enough that 0.5 is nowhere near either edge. `author/spawn.py` now
withholds `movement_type`/`wander_distance`/`creature_movement` entirely
when `confident` is false, and reports why as a gap instead, the same
refusal `delayRepeatMin/Max` already gets from too few samples.

**`spawntimesecsmin/max`.** `analyze/behaviour.py`'s respawn finding
(`respawn_timer`) is built on exactly one death→next-sighting gap: 24.6
seconds. The PR's own value is 86400 (24 hours — the "does not really
respawn" convention this project's own `spawn.py` docstring already
anticipates as a possibility it cannot detect from wire data alone). One
sample cannot be told apart from a fixed timer's own noise (the same
caveat Ralthas's single-sample case already carried, worded in
`spawn.py`'s own notes) — and here that structural weakness produced a
number over 3000x smaller than reality, not a close miss.

**Fixed.** `respawn_timer` gained a `confident` flag (`samples >= 2`,
`MIN_RESPAWNS_FOR_CONFIDENCE` in `behaviour.py` — a lower bar than
`spell_repeat_delay`'s five, because a respawn timer is typically fixed
rather than randomised, so two independent gaps already cross-validate each
other). `spawn.py` now withholds `spawntimesecsmin/max` outright when not
confident, protects the columns from `fill_schema_defaults` via `skip`, and
reports the single observation as a gap instead of a number. Ralthas's own
two-sample case (299–300s, still `confident=True`) is unaffected.

**What this does not mean.** It does not mean the decoders are wrong, or
that Ralthas's own validated 41/41 waypoint match was luck — that result is
independently checked against a live database with sub-centimetre accuracy,
which this capture's numbers cannot claim. It means the *inference* layer's
assumptions (creatures patrol peacefully, deaths are natural, one sample
bounds a range) hold for a genuinely patrolling, genuinely-repeatedly-killed
creature and do not hold for a stationary boss re-engaged by hand for
sample-gathering. Both are real creature types in this content, so the tool
needs to say which situation it is in, not assume the Ralthas case
universally.

## 3. `creature_spells` — the repeat-delay bound holds up, and gets tighter

| PR field | spell 22417 | spell 28447 |
|---|---|---|
| `delayRepeatMin/Max` (authored) | 18 / 22 | 11 / 17 |
| `delayRepeatMin/Max` (derived) | **20 / 27** (29 samples) | **12 / 18** (38 samples) |
| `delayInitialMin/Max` (authored) | 0 / 0 | 4 / 5 |
| `delayInitialMin/Max` (derived) | **0 / 0** — exact | **4 / 4** — was 2 / 2, see *Corrected* below |

Same shape of disagreement Ralthas's own repeat delay showed (§7 of that
document): the observed bound sits close at the floor and wider at the
ceiling, because an observed cast-to-cast gap is the random parameter plus
whatever overhead (cast time, GCD, re-targeting) happened to occur, and
overhead only ever adds delay. With more samples than Ralthas had (29 and 38
against Ralthas's 30, but now on two independent spells rather than one),
the bound is *tighter* on both spells, not just present — a genuine
capability confirmation, not a coincidence.

`delayInitial` for spell 28447 is the one number here that looks like a real
methodological gap rather than expected noise: derived 2s against an
authored 4–5s is *below* the authored range, which the "overhead only adds
delay" model does not predict. The likely cause: this creature has two
spells competing for the AI's first cast after aggro, and "the first time
spell 28447 was seen cast after an engagement" is not the same measurement
as "how long after engagement spell 28447's own slot becomes eligible" if
the AI sometimes casts spell 22417 first. `analyze/behaviour.py` does not
currently distinguish these, and Ralthas — with only one spell — could not
have surfaced this gap.

**Corrected: that cause was wrong, and measuring it said so.** The paragraph
above is kept because the correction only means something beside it. The task
that followed it opened with *confirm the cause on the capture rather than
assuming it*, and the confirmation disproved it.

There is no competition between the two spells. The analyzer took every
`SMSG_AI_REACTION` as the start of an engagement, and that opcode fires again on
every re-aggro mid-fight — a target switch, the player re-engaging after kiting
— when nothing about a spell's timer restarts. This capture has **14 aggros over
2 deaths**. Split by whether each aggro started a fight from rest:

| aggro | started from rest | 22417 first cast | 28447 first cast |
|---|---|---|---|
| #1 (first in the capture) | **yes** | **0.09 s** | **4.00 s** |
| #12 (first after the first death) | **yes** | **0.09 s** | **4.03 s** |
| the other 12 | no — mid-fight | 4.8 s – 56.7 s | 1.68 s – 60.6 s |

Both fresh fights give **both** spells their authored value — 22417 at 0,
28447 inside 4–5. Each spell is right independently, which is exactly what
competition for the first cast would have prevented. The 1.68 s that `min()`
picked came from aggro #5, eight seconds into a fight that had started
long before.

This is the **third** consequence of the capture technique §2 describes, after
the fabricated patrol route and the single-sample respawn timer: re-engaging a
stationary boss fourteen times by hand produces aggros that a naturally-pulled
creature never would. Ralthas, pulled fresh on each of its three lives, gave
three fresh engagements out of three and could not have shown it.

A second defect sat underneath it: the search for "the first cast after an
aggro" had no upper bound, so a creature that died before casting a spell would
have lent that engagement a cast from its **next life**. Now the search ends at
the next death.

**Fixed.** `analyze/behaviour.py` measures `delayInitial` only from the first
aggro of each life — the first in the capture, and the first after each death —
and only up to the death that ends it. A full evade-and-reset also restarts the
timers and is not detected, so repeated evades yield fewer samples than they
could; fewer, never wrong. `author/spells.py` now skips `delayInitialMin/Max`
and names a gap when no fresh fight ever reached a spell, rather than letting
the schema's 0 claim it casts the instant it aggroes.

After: 28447 authors **4 / 4** against the PR's 4 / 5. The minimum is exact.
The maximum is a second short because two samples cannot reveal the top of a
random range — the same *bound, not value* limit the repeat delay has — but the
derived value now sits **inside** the authored range, where 2 / 2 sat below it.

## 4. `broadcast_text`, `creature_ai_events`, `creature_ai_scripts` — clean, and one real cross-check

Both lines' text is exact ("Agamaggan shall be reborn!" / "No! No! I cannot
die!"), aggro/death event-type inference is unambiguous (2 samples each,
same caveat about small-sample confidence as Ralthas §6), and — the one
genuinely new thing this creature exercises — `chat_type` was correctly read
as **YELL** (`SMSG_MESSAGECHAT` type `0x0C`), where Ralthas's own lines were
**SAY** (`0x0B`). The PR's own `creature_ai_scripts` comments ("Yell on
aggro" / "Yell on death") confirm it. Ralthas alone could not have shown the
SAY/YELL distinction actually holds; this is the first real evidence it does.

## 4a. What only a database-backed comparison could see

Everything above was produced without a world database configured. A later
run *with* one, comparing against the live server rows, found three more
defects that no capture-only run could have surfaced — two of them in the
tables §4 had called clean:

| | authored by the PR | this toolkit, before | after |
|---|---|---|---|
| `creature_ai_scripts.datalong` | **1** (yell) | **0** | **1** |
| `broadcast_text.sound_id` | **60640 / 60641** | **0 / 0** | *not proposed*, named as a gap |
| `creature_spells` slot order | 22417, then 28447 | 28447, then 22417 | 22417, then 28447 |

**`datalong`.** For `SCRIPT_COMMAND_TALK` the server reads the say/yell
distinction from `datalong` (`ScriptMgr.h:80`, numbered by `enum ChatType` at
`Creature.h:122-123`), and this toolkit already computed exactly that value
one column over, for `broadcast_text.chat_type` — it simply never carried it
across. Worth being precise about the severity: `ScriptCommands.cpp:65` passes
`datalong > 0 ? datalong : -1`, and `DoScriptText` treats a negative override
as "use the text's own chat type" (`ScriptMgr.cpp:2816`), so the creature would
still have yelled. The row was textually wrong against the hand-authored one,
not behaviourally broken.

**`sound_id`.** This one is the more interesting failure, because the module
was already *trying* to do the right thing: `dialogue.py`'s docstring states
that an unattributed sound is left out rather than written as `0`, precisely
because a zero meaning "not observed" and a zero meaning "silent" are
indistinguishable once stored. But `fill_schema_defaults` fills any column the
rule did not set, and it is **a no-op without a database** — so the stated
intent held in every capture-only run and was silently violated the moment a
schema became readable. The fix is to declare the exclusion as `skip`, the
same mechanism `spawntimesecsmin/max` already uses, and to emit a gap.

**Slot order.** `creature_spells` is one wide positional row, so which slot a
spell lands in is authored content. Ordering slots by observed cast count made
that content depend on how long the capture happened to run — the same
creature recorded twice could author the same two spells into swapped slots,
and a positional diff then reports every column of both as changed. That is
also why the raw comparison against the server looked worse than it was. Now
ordered by spell id, which is stable across captures and happens to match both
hand-authored PRs.

The pattern across all three is the same one this document already names for
`patrol` and `respawn_timer`: **a wrong value carrying the same provenance
label as a right one**. What is new is the discovery channel — two of these
were invisible not because the logic was subtle but because the code path that
produced them only runs with a database attached, which no validation run in
this document ever had.

## 5. `creature_equip_template` — same gap mechanism as Ralthas, not a new one

The wire carries a virtual item display id (25382); resolving it to the
PR's authored `equipentry1 = 14845` needs the `item_template` database
lookup (`author/equipment.py`, same mechanism as Ralthas §4), which was not
configured for this run. Named as a gap, not guessed at — no different from
what Ralthas would show without a database either.

## Bottom line

The decoding layer generalises cleanly: every field read straight off the
wire (stats, position, guid, map, text, chat type) matched a creature this
toolkit had never seen, to the same precision Ralthas showed. The two
analyzers that infer rather than decode — `patrol` and `behaviour`'s respawn
timer — did not generalise to a creature whose real behaviour (stationary,
effectively non-respawning) differs from the assumption their heuristics
were built and tested against (patrols, dies and respawns naturally). Both
produced a specific, wrong, *confidently-labelled* answer rather than a
named gap, which was the actual defect: the honesty this toolkit's authoring
output otherwise insists on (§7's refusal to guess a repeat delay from one
sample, §2's Ralthas-side ground-snap caveat) did not extend to these two
values here.

Both are fixed now, in the same session this document's findings came from,
each checked against both real captures rather than merely made to pass a
unit test: `patrol_route` carries a `confident` flag from a combat-hop
fraction (5% for Ralthas's genuine route, 100% for Rakameg's fabricated
one — nowhere near the 0.5 cutoff on either side), and `respawn_timer`
carries one from a sample-count threshold, mirroring `spell_repeat_delay`'s
own. `author/spawn.py` withholds `movement_type`/`creature_movement` and
`spawntimesecsmin/max` respectively when either flag is false, reporting a
gap instead of a wrong number. Re-run after the fix: Rakameg's authored file
proposes no `creature_movement` rows and no `spawntimesecsmin/max`, matching
the PR's own silence on both; Ralthas's already-validated output is
unchanged.

## 6. What two creatures still could not show

A later audit asked the obvious follow-up question — which parts of this
toolkit encode a property of *these two creatures* rather than a general rule
— and found that the fix above is one of them. `combat_hop_fraction` measures
a specific *cause* of a fabricated route (combat repositioning), not the thing
that actually matters, so it is structurally blind to the other common way to
produce one: **a creature with `movement_type = 1` that wanders at random**.
Wandering happens out of combat, so its hops look peaceful, pass the combat
check, and would author exactly the waypoint route this document's headline
finding is about.

`closes_loop` is no help there either, and measurement says so: Rakameg's
fabricated route reports `closes loop: True` just as Ralthas's genuine one
does, because a most-common-successor walk returns to its anchor almost
regardless. The signal that does separate them is repetition, which is what
the word *patrol* means in the first place:

| | walked waypoints | seen exactly once | median visits |
|---|---|---|---|
| Ralthas (genuine) | 41 | **0%** | 6 |
| Rakameg (fabricated) | 11 | **73%** | 1 |

A waypoint visited once is a destination, not a waypoint — and random
wandering lands near 100% single-visit by construction, since every
destination is new. `MAX_SINGLE_VISIT_FRACTION = 0.5` now gates
`patrol_route` alongside the combat check; both real captures keep their
existing verdicts (Ralthas confident, Rakameg refused), and the wanderer case
is refused by a signal that does not depend on having seen one.

Three other two-example assumptions came out of the same audit:

- **`creature_equip_template` authored only slot 1 of 3.**
  `UNIT_VIRTUAL_ITEM_DISPLAY` is Size:3 (`UpdateFields.h:95`) — mainhand,
  offhand, ranged — but only a base index carries a name, so a shield or a bow
  arrived unnamed and was dropped silently. Both creatures validated here
  carry a single weapon, which is why it never showed. Now read by offset,
  with an unresolvable slot skipped rather than defaulted to 0 ("no item").
- **`creature_template` stats take the first `CREATE` and assume it was
  clean.** Nothing guarantees the first sighting caught an unmodified
  creature; one already enraged or buffed would author modified numbers as
  `wire`. A later `CREATE` disagreeing on an authored column is now reported
  instead of discarded. Narrowed to authored columns only — the first run
  against real data flagged `UNIT_FIELD_FLAGS`, which moves between sightings
  by design and says nothing about stats.
- **`SMSG_MESSAGECHAT`'s emote form is dropped under `--entry`** — checked and
  left alone. That one is not a defect: `core/contracts.py` documents it as
  deliberate, because the emote form carries no sender guid and so cannot be
  attributed to any creature for a per-entry migration to claim.

The finding that matters going forward is not "patrol and respawn were
buggy" — it is that **the discipline this toolkit already had in one place
(`creature_spells` refusing an unbounded value) did not automatically apply
everywhere it should have**, and a second, independently-chosen test
creature is what surfaced the gap. Ralthas alone, being a genuinely
patrolling, genuinely multiply-respawning creature, could not have shown
either weakness existed.
