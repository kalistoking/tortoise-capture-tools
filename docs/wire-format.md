# Wire format reference

Every layout below was read out of the `tortoise-wow` server checkout's own
C++, with the file reference given inline. Nothing here comes from
third-party documentation, and nothing may be copied in from a GPLv3 project
-- re-derive instead (see README).

This is the living reference a module cites in its docstring when it
implements an opcode. Verified content carried over from the prototype's
handoff notes; extend it as opcodes are added.

## Framing
- `ServerPktHeader` (`src/framework/Network/MangosSocket.h`): `uint16 size`
  **big-endian** + `uint16 cmd` **little-endian** = 4 bytes.
  `body_len = size - 2`. There is **no** large/3-byte-size header variant in
  this fork — the struct is plain and fixed (checked explicitly).
- `ClientPktHeader`: `uint16 size` BE + `uint32 cmd` LE = 6 bytes.
  `body_len = size - 4`.
- Endianness trap: `iSendPacket` (`MangosSocketImpl.h`) does
  `EndianConvertReverse(header.size)` but leaves `cmd` native. Assuming `>HH`
  for both yields garbage opcodes (`0xEC01` instead of `0x1EC`).

## Crypto (`src/shared/Auth/AuthCrypt.cpp`)
- Running stream cipher, independent `(i, j)` state per direction.
- `EncryptSend` (SMSG, 4 header bytes): `cipher = (plain ^ key[i]) + j; j = cipher`.
- `DecryptRecv` (CMSG, 6 header bytes): `plain = (cipher - j) ^ key[i]; j = cipher`.
- Decoding a passive capture uses the same formula in both directions:
  `plain = (cipher - j) ^ key[i % 40]; j = prior ciphertext byte`.
- **Session-key recovery**: CMSG header bytes 4–5 (high bytes of the 32-bit
  opcode) are always plaintext `0`, so `key[i] = (cipher[i] - cipher[i-1]) & 0xFF`
  over ~20 encrypted client messages after `CMSG_AUTH_SESSION` recovers all
  40 bytes.
- **Handshake skip**: `SMSG_AUTH_CHALLENGE` (0x1EC) and `CMSG_AUTH_SESSION`
  (0x1ED) are plaintext, and each header's own `size` field gives the exact
  byte length to skip — their bodies never need parsing. (Believing
  otherwise is what made the prototype's C2S direction decode 0 packets for
  a while.)

## Opcode table
Parsed live from `src/game/Protocol/Opcodes_1_12_1.h` (`SYMBOL = 0xNNN`)
cross-referenced with `Opcodes.cpp`'s `StoreOpcode(SYMBOL, "NAME", ...)`.
825 entries; highest opcode is `0x33B`. A decrypted opcode above that is a
reliable desync signal (cheaper than waiting for a body-length overrun).
Never hardcode the table — the point is that it re-derives per checkout.

## Compressed containers — the framing trap
Both are `uint32 uncompressed_size` + zlib blob, but the **inner framing
differs**:
- `SMSG_COMPRESSED_MOVES` (0x2FB, `MovementData::AddPacket`): a batch of
  independently-opcoded micro-packets, repeated
  `[uint8 len_incl_opcode][uint16 opcode][payload]`.
- `SMSG_COMPRESSED_UPDATE_OBJECT` (0x1F6, `UpdateData::BuildPacket`): the
  raw `SMSG_UPDATE_OBJECT` body **verbatim**, no per-block opcode — one
  logical message, not a batch.

Treating the second like the first misparses it silently (it yields a
plausible-looking frame or two, then overruns).

## GUIDs (`src/game/ObjectGuid.h`)
- `packGUID`: mask byte then present bytes; each present byte belongs at
  `mask_bit_index * 8`, **not** at read order. Getting this wrong produces a
  plausible but wrong 64-bit value.
- High word = bits 48–63: `UNIT 0xF130`, `PET 0xF140`, `GAMEOBJECT 0xF110`,
  `TRANSPORT 0xF120`, `DYNAMICOBJECT 0xF100`, `CORPSE 0xF101`,
  `ITEM`/`CONTAINER 0x4000`, `MO_TRANSPORT 0x1FC0`, `PLAYER 0x0000`.
- `GetEntry() = (guid >> 24) & 0xFFFFFF` (only when the high type carries an
  entry). This is how a target NPC is identified from wire data — no
  spatial guessing needed.

## `SMSG_MONSTER_MOVE` (0x0DD) / `_TRANSPORT` (0x2AE)
`packet_builder.cpp` `WriteCommonMonsterMovePart` + `WriteMonsterMove`:
```
packGUID unit [+ packGUID transport for _TRANSPORT]
Vector3 start_pos
uint32  splineId
uint8   moveType   0 Normal | 1 Stop | 2 FacingSpot(+Vector3) | 3 FacingTarget(+uint64) | 4 FacingAngle(+float)
-- Stop ends the packet here --
uint32  flags
uint32  duration
flags & Mask_CatmullRom (0x200):  uint32 count ; Vector3[count]   (cyclic dup point already included)
else:                             uint32 lastIdx ; Vector3 dest ; packedOffset[lastIdx-1]
```
`appendPackXYZ`: 11/11/10-bit signed fields, ×0.25 scale, offsets are
`dest - point`.

A patrol is sent as a series of point-to-point **linear hops**, so the
ordered hop destinations *are* the waypoint list.

## `SMSG_UPDATE_OBJECT` (0x0A9) — outer framing
`UpdateData::BuildPacket`:
```
uint32 blockCount
uint8  hasTransport
[if out-of-range guids: uint8 type=4 ; uint32 count ; count × packGUID]   (counts as a block, always first)
blockCount × block
```
Block types (`ObjectUpdateType`): `VALUES=0`, `MOVEMENT=1`,
`CREATE_OBJECT=2`, `CREATE_OBJECT2=3`, `OUT_OF_RANGE_OBJECTS=4`,
`NEAR_OBJECTS=5`.
- `VALUES`: `packGUID` + UpdateMask + fields. **No object-type byte** — the
  object's type must come from the GUID high word.
- `MOVEMENT`: **raw 8-byte guid** (not packed!) + `BuildMovementUpdate`.
- `CREATE_OBJECT`/`CREATE_OBJECT2`: `packGUID` + `uint8 objectTypeId` +
  `BuildMovementUpdate` + UpdateMask + fields.

`Object::BuildMovementUpdate` (`Object.cpp:415`):
```
uint8 updateFlags
if LIVING (0x20):        MovementInfo::Write ; 6 × float speeds (walk,run,run_back,swim,swim_back,turn)
                         if moveFlags & SPLINE_ENABLED: PacketBuilder::WriteCreate
elif HAS_POSITION (0x40): 4 × float (x,y,z,o)
if HIGHGUID (0x08):      uint32
if ALL (0x10):           uint32
if MELEE_ATTACKING (0x04): packGUID (victim; empty packguid = single 0x00)
if TRANSPORT (0x02):     uint32
```
`ObjectUpdateFlags` (`UpdateData.h`, comment says "checked for 1.12.1"):
`SELF 0x01, TRANSPORT 0x02, MELEE_ATTACKING 0x04, HIGHGUID 0x08, ALL 0x10,
LIVING 0x20, HAS_POSITION 0x40`.

`MovementInfo::Write` (`Object.cpp:146`):
```
uint32 moveFlags ; uint32 stime ; float x,y,z,o
if ONTRANSPORT (0x02000000):  uint64 t_guid (RAW) ; float tx,ty,tz,to
if SWIMMING (0x00200000):     float s_pitch
uint32 fallTime                          <-- unconditional
if JUMPING (0x00002000):      float zspeed, cosAngle, sinAngle, xyspeed   (note: cos before sin)
if SPLINE_ELEVATION (0x04000000): float
```

`Movement::PacketBuilder::WriteCreate` (`packet_builder.cpp:154`) — the
embedded spline snapshot, emitted when the unit was **already mid-move** the
moment the client first saw it. For a patrolling creature this is the normal
case, not an edge case; without it you cannot reach the fields payload at
all:
```
uint32 splineFlags
if final_angle (0x40000):  float
elif final_target (0x20000): uint64
elif final_point (0x10000):  float x,y,z
uint32 timePassed ; uint32 duration ; uint32 splineId
uint32 nodes ; Vector3[nodes]
Vector3 finalDest            (zero vector when cyclic)
```
Caveat: the C++ writes **nothing at all** if `move_spline.Initialized()` is
false, so a flagged-but-uninitialised spline would desync the parse. Not
observed in practice; flag it if it ever appears.

## UpdateMask + fields (`UpdateMask.h`, `Object::BuildValuesUpdate`)
```
uint8 blockCount ; blockCount*4 mask bytes ; one uint32 per set bit, ascending index
```
Bit `i` lives at byte `i >> 3`, bit `i & 7`. `blockCount = ceil(fieldCount/32)`.

Field indices are parsed live from `src/game/Objects/UpdateFields.h`
(`EObjectFields`, `EUnitFields`), evaluated like a C enum (sequential
auto-increment plus `=` expressions that sum named constants and literals).
`OBJECT_END = 6`, `UNIT_END = 188`. Spot values confirmed:
`UNIT_FIELD_MINDAMAGE 0x86`, `MAXDAMAGE 0x87`, `ATTACK_POWER 0xA5`,
`ATTACK_POWER_MODS 0xA6`, `DISPLAYID 0x83`, `BYTES_0 0x24`,
`OBJECT_FIELD_SCALE_X 0x04`.

Type handling: most fields are plain `uint32`; floats are raw IEEE-754 bit
patterns in the same slot (`SCALE_X`, `BOUNDINGRADIUS`, `COMBATREACH`,
min/max damage melee+offhand+ranged, the two attack-power multipliers);
`*_ATTACK_POWER_MODS` is a signed int16 pair (pos, neg) packed into one slot.
`UNIT_FIELD_BYTES_0` = `race | class<<8 | gender<<16 | powertype<<24`
(verified via `Object::SetByteValue` at `Object.cpp:1003` and
`Creature.cpp:380-407`); **race is hardcoded 0 for creatures**, class comes
from `creature_template.unit_class`.

Two hard-won points:
1. **There is no per-viewer field masking in this core.** `BuildValuesUpdate`
   special-cases only `UNIT_NPC_FLAGS`, `UNIT_FIELD_DISPLAYID` and health
   visibility; everything else falls through to a plain
   `*data << m_uint32Values[index]` (`Object.cpp:792`). So a creature's
   combat stats *are* broadcast to every observer. (Modern-retail
   `PRIVATE`/`OWNER_ONLY` semantics, which HermesProxy's table annotates, do
   not apply here.)
2. **`EUnitFields` names are only valid for units/pets.** GameObjects,
   items, corpses and dynamic objects have entirely different field layouts
   starting at the same `OBJECT_END` offset, so the same indices get
   silently mislabelled (a GameObject's field shows up as
   "UNIT_FIELD_HEALTH = 329159"). Gate on the GUID high word
   (`0xF130`/`0xF140`) before naming anything. This bug was found and fixed
   in the prototype; keep a regression test for it.

`CREATE`/`CREATE2` blocks carry the full field snapshot (that is where
`mindamage`/`maxdamage`/`attack_power`/`scale`/`bytes_0` appear, once, at
first sighting). `VALUES` blocks carry only what changed — in practice for a
fighting creature that is `health`.

## Other decoded opcodes
| opcode | layout (source) |
|---|---|
| `SMSG_CREATURE_QUERY_RESPONSE` (0x61) | `uint32 entry` + CString name + 3×`uint8(0)` + CString subname + 7×`uint32` (type_flags, type, beast_family, rank, unk, pet_spell_list_id, display_id) + `uint8 civilian` + `uint8 racial_leader` (`QueryHandler.cpp:186`). Does **not** carry unit_class/scale/damage — those are UpdateFields only. |
| `SMSG_MESSAGECHAT` | `ChatHandler::BuildChatPacket` (`Chat.cpp:2254`). MONSTER_SAY 0x0B / YELL 0x0C: raw 8-byte sender guid + `uint32 nameLen` + name + raw 8-byte target guid + `uint32 msgLen` + msg. MONSTER_EMOTE 0x0D: **no sender guid** — cannot be filtered by entry. |
| `SMSG_PARTYKILLLOG` | raw killer guid + raw victim guid (`Unit.cpp:1127`). |
| `SMSG_SPELL_GO` | packGUID (cast item or caster) + packGUID (caster) + `uint32 spellId` + `uint16 castFlags` + targets (`Spell.cpp:4662`). |
| `SMSG_ITEM_QUERY_SINGLE_RESPONSE` | `uint32 entry, class, subclass` + CString name + more (`ItemHandler.cpp:370`). Has no creature linkage — cannot be filtered by creature entry. |
| `SMSG_AI_REACTION` | raw guid + `uint32 reactionType` (`Creature.cpp:2246`). |

## Behavioural findings (semantics, not just bytes)

- **`MOVE STOP` marks combat engagement, not death.** It coincides with
  `SMSG_AI_REACTION` (aggro) and precedes `SMSG_PARTYKILLLOG` by the length
  of the fight. An earlier analysis got this backwards because the first
  capture's fights were fast enough that the timestamps nearly coincided.
- A creature's static stats never change after `CREATE`, so any stat
  extraction must read the create-time snapshot, not wait for `VALUES`.
- Respawn is observable: death → next `CREATE` for the same entry.

