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

**Open question**: class 2 (Paladin) sits oddly next to the fact that he
casts spell 1449 (Arcane Explosion). Most likely correct — `unit_class` only
drives stat scaling for creatures and does not restrict spells — but it was
never checked against `creature_template.unit_class` because no mysql client
was available. Verify before trusting stat extraction for authoring.


## Environment facts

- Target server port **8090** (this fork's `WorldServerPort`). The unrelated
  `wow_decrypt2` test used 8085; `wow_session_key.py` still defaults to 8085.
- Source repo for all live-parsed tables:
  `C:\WOW\source\tortoise-wow_AIBot\tortoise-wow`.
- Python 3.14, `scapy` is the only third-party dependency.
- **Windows encoding**: the console/locale is cp1250. Always pass
  `encoding="utf-8"` to `open()`/`read_text()`, and avoid non-ASCII
  characters in printed output (an unescaped `→` is exactly what crashed the
  third-party `wow_decrypt2.py` on its final summary line).
- The `mysql` client is **not** on PATH in this environment, so DB
  cross-checks could not be automated from the agent; plan for that (config
  for a DB connection, or accept manual verification).
- Some Bash commands touching session-key extraction were previously blocked
  by the auto-mode classifier; the user ran them in their own terminal
  instead. Worth designing CLIs that are easy to hand over verbatim.

