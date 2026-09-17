# Known-good baseline: the `Ralthas` session

The reference capture every regression is measured against. The capture
itself is **not** in this repository and never will be (see README, Data
policy); it lives on the local machine and tests find it through
`TCT_TEST_CAPTURE`. What follows is the expected *result* of processing it,
which is safe to version and is what the golden tests assert.

Target: **Ralthas**, `creature_template.entry = 62635`, spawn `guid 2590698`
in this fork's own `tw_world`.

## Validation evidence

- Reconstructed patrol X/Y matched the 42 authored `creature_movement`
  waypoints to **sub-centimetre** precision; Z differed by tenths of a yard,
  consistent with runtime mmap ground-snap rather than a decode error.
- All matched movement packets arrived via `SMSG_COMPRESSED_MOVES`, so the
  zlib/inner-framing path is exercised.
- After fixing the C2S direction, request/response counts balanced exactly:
  `CMSG_ITEM_QUERY_SINGLE` 256 = `SMSG_ITEM_QUERY_SINGLE_RESPONSE` 256, and
  likewise for creature/gameobject queries and pings.
- Latest capture: 8212 records (S2C 6910, C2S 1302), zero desyncs, zero
  parse errors across the whole file including players and gameobjects.
- `UNIT_FIELD_HEALTH` from `VALUES` blocks reached exactly 0 at t=90.307 s —
  the same timestamp `SMSG_PARTYKILLLOG` reported the death. Three
  independently decoded opcodes agreeing.
- Respawn gap death 90.307 s → create 389.841 s ≈ **299.6 s**, matching the
  DB's `spawntimesecsmin/max = 300`.
- Field offsets independently matched Xian55/HermesProxy's build-5875 table.

Decoded Ralthas stats (from the `CREATE` block): level 13, health/maxhealth
342, mindamage 20.2119, maxdamage 24.4671, attack power 44, ranged attack
power 36, scale 1.0, bounding radius 0.306, combat reach 1.5, displayid /
nativedisplayid 13091, faction template 17, base mana 488,
`bytes_0` → race 0, **class 2 (Paladin)**, gender Male, power Mana.

~~**Open question**: class 2 (Paladin) sits oddly next to the fact that he
casts spell 1449 (Arcane Explosion)... never checked against
`creature_template.unit_class` because no mysql client was available.~~
**Resolved below** — a local `tw_world` became reachable and every field
matched, including `unit_class = 2`.

## Independent DB verification (this toolkit, against live `tw_world`)

Re-run once a local database became reachable (`127.0.0.1:3306`, via the
`mariadb-10.3.39-winx64` client bundled in the `tortoise-wow_AIBot` server
install — not on `PATH`, invoked by full path). This is this toolkit's own
decode output compared field-by-field and point-by-point against the live
tables, not a re-statement of the prototype's earlier claim.

**`creature_template` (entry 62635) — every field matches exactly**: `name`
Ralthas, `level_min/max` 13, `health_min/max` 342, `mana_min/max` 488,
`dmg_min/max` 20.2119 / 24.4671, `attack_power` 44, `ranged_attack_power` 36,
`scale` 1, `unit_class` **2**, `faction` 17, `rank` 0, `display_id1` 13091.
The open question above is closed: `unit_class = 2` (Paladin) is exactly what
the `CREATE` block decoded, not a misread.

**`creature_movement` (id 2590698) — all 42 waypoints present**, matching
the spawn (`creature.guid 2590698`, position `(-9129.66, -1098.79, 73.6607)`,
`spawntimesecsmin/max = 300`). Each DB waypoint matched against its nearest
decoded hop (113 linear hops decoded from the capture — roughly 2.7 patrol
loops over the ~450 s session, vs. 42 authored points):

| | value |
|---|---|
| mean XY error | **0.005 yards** (≈ 0.5 cm) — sub-centimetre, as claimed |
| max XY error | 0.200 yards, one point (#11) — a nearest-hop mismatch across loop iterations, not a decode error |
| max Z error | 0.354 yards — consistent with runtime mmap ground-snap, as claimed |

Confirms the prototype's original claim independently, with this codebase's
own decoder and a live database instead of a manual cross-check.

## Environment facts

- Test client: Turtle WoW distribution **1.18.1-7272-Hotfix-2026-04-12**,
  installed at `C:\WOW\1.18.1-7272-Hotfix-2026-04-12`. Its `WoW.exe` reports
  `FileVersion 1,12,1,5875` — the wire protocol is unmodified vanilla
  1.12.1 build 5875; Turtle WoW's own version number is only a content-patch
  label on top of it.
- Target server port **8090** (this fork's `WorldServerPort`). The unrelated
  `wow_decrypt2` test used 8085; `wow_session_key.py` still defaults to 8085.
- Source repo for all live-parsed tables:
  `C:\WOW\source\tortoise-wow_AIBot\tortoise-wow`.
- Python 3.14, `scapy` is the only third-party dependency.
- **Windows encoding**: the console/locale is cp1250. Always pass
  `encoding="utf-8"` to `open()`/`read_text()`, and avoid non-ASCII
  characters in printed output (an unescaped `→` is exactly what crashed the
  third-party `wow_decrypt2.py` on its final summary line).
- A local `tw_world` is reachable at `127.0.0.1:3306` (user `mangos`,
  database `tw_world`, matching `mangosd.conf`'s `WorldDatabase.Info`). No
  `mysql`/`mariadb` client is on `PATH`, but one is bundled at
  `tortoise-wow_AIBot/server/mariadb-10.3.39-winx64/bin/mysql.exe` and works
  invoked by full path. DB cross-checks are no longer blocked (see
  [Independent DB verification](#independent-db-verification-this-toolkit-against-live-tw_world)
  above); consider wiring a DB connection into the toolkit's own config
  rather than always shelling out.
- Some Bash commands touching session-key extraction were previously blocked
  by the auto-mode classifier; the user ran them in their own terminal
  instead. Worth designing CLIs that are easy to hand over verbatim.

