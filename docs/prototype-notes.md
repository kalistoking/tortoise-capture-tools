# Prototype notes (frozen)

The working prototype this toolkit supersedes lives outside the repository
at `C:\WOW\source\extract_wow_data` and is not imported here. These notes
record what it was and what was already ruled out, so neither is
rediscovered later.

Its verified protocol knowledge lives on in [wire-format.md](wire-format.md),
its measured results in [baseline-ralthas.md](baseline-ralthas.md), and the
open design questions it left are answered in [../ARCHITECTURE.md](../ARCHITECTURE.md).

## What the prototype was (`C:\WOW\source\extract_wow_data`)

Not under version control. Plain files, no packaging, no tests. Works.

| file | role |
|---|---|
| `wow_session_key.py` | Derives the 40-byte session key from a capture. `-keyOnly` prints the bare hex key (stdout contract for subprocess use). |
| `dump_capture.py` | Decrypts the whole session (both directions) → JSONL. Derives the key itself by shelling out to `wow_session_key.py -keyOnly`. |
| `extract_creature_path.py` | Reconstructs one creature's `creature_movement` waypoints; emits ready-to-run SQL. Takes the key as an argument. |
| `decode_extras.py` | Decodes specific opcodes out of the JSONL (chat, kill log, spell casts, queries, AI reaction, movement, update-object combat stats). |
| `update_fields.py` | `SMSG_UPDATE_OBJECT` block/field decoder (newest piece). |
| `logsetup.py` | Tees stdout+stderr into `logs/<target-stem>.log`, echoes the invoking command line. |

Data present there: `Ralthas.pcap` (3,766,846 B), `Ralthas.jsonl` (8212
records), `logs/Ralthas.log`, `supported_opcodes.txt` (825 lines).
`export/Ralthas.jsonl` is **stale** (holds an older capture) — regenerate or
drop it.

Known prototype shortcomings to fix in the real architecture:
- Four CLIs with ad-hoc `argparse`, no shared entry point, inconsistent
  argument names (`--repo` means the same thing in two of them, absent in
  others; the key is a positional arg in one and auto-derived in another).
- `reassemble_stream`, `read_packguid`, the header readers and the
  compressed-container unpacker are **duplicated** across
  `extract_creature_path.py`, `dump_capture.py` and `decode_extras.py`.
- No tests at all. Validation was manual cross-checking (section 6).
- Field tables exist only for `EObjectFields`/`EUnitFields`.
- Output is print-to-stdout; nothing is structured for programmatic reuse
  except `dump_capture.py`'s JSONL.


## Third-party projects already assessed

| project | verdict |
|---|---|
| [TrinityCore/WowPacketParser](https://github.com/TrinityCore/WowPacketParser) (GPLv3, C#) | **Not usable.** Current `master` models modern retail dynamic update fields (`ITEM_FIELD_ARTIFACT_XP` etc). `ClientVersionBuild.V1_12_1_5875` exists as an enum value only — there is no wired-up vanilla field decoder. |
| [Xian55/HermesProxy](https://github.com/Xian55/HermesProxy) (GPLv3, C#) | **Useful as cross-reference only.** Has a real `World/Enums/V1_12_1_5875/UpdateFields.cs` offset table (cross-validated against WPP in its own test suite) and legacy vanilla crypt/update handlers. Our source-derived offsets matched it exactly. |
| `C:\WOW\source\wow_decrypt2_py_test\wow_decrypt2.py` (local, independent) | Different goal: decrypts a pcap and **rewrites a new pcapng** with cleartext headers spliced back into the original TCP segments, for viewing in Wireshark. Worth keeping as a capability. Contributed the opcode-ceiling desync check. Its "large header" (3-byte size) support is **inapplicable** to this fork. It crashes on its final summary print under cp1250. |

**Licensing**: both C# projects are GPLv3. Everything in the prototype was
deliberately derived from the user's own tortoise-wow source instead, so
there is no GPL entanglement. Keep it that way — re-derive, don't copy.

