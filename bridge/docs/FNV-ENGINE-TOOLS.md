# fnv_* tools: engine knowledge and crash doctor

Fifteen read-only tools that use the game's own code, through the Fallout: New Vegas decompilation.
They work offline: no game, GECK or companion needs to run, and they change nothing. The tools that work
on the running game (control, tests, live inspection, full control) are in [FNV-LIVE-TOOLS.md](FNV-LIVE-TOOLS.md).

| Tool | What it answers |
| --- | --- |
| `fnv_func_find` | Which engine functions match these words, this address, or this text in their code? |
| `fnv_func_code` | The decompiled C++ of one function; calls to unnamed functions are annotated with proven names |
| `fnv_func_callers` / `fnv_func_callees` | Who calls it, what it calls (direct calls read from the exe; several levels) |
| `fnv_symbolize` | Address → function + offset, a setting/static object, or DLL + offset |
| `fnv_name_status` | Is this name proven, and by what evidence? |
| `fnv_class_layout` / `fnv_class_vtable` / `fnv_class_tree` | Fields with offsets and types; each vtable slot's function; base and derived classes (RTTI) |
| `fnv_command_info` | A console/script command: parameters, return type, handlers and what they call |
| `fnv_global_find` / `fnv_setting_info` | Settings and static objects: address, default value, which functions use them |
| `fnv_string_xrefs` | Functions whose code contains a string |
| `fnv_crash_latest` / `fnv_crash_explain` | The newest crash log, parsed, and explained in engine terms |
| `fnv_save_inspect` / `fnv_save_diff` | A `.fos` save decoded with the game's save format (header, plugins, location, globals, changed forms, quests), and two saves compared |

Every result carries the engine map's identity (game build, decomp commit, proof file hash, whether the
exe passed the build check). Names have a status: **proven** names come from fnvsym's proof table and are
evidence; **decomp** names are the decompilation's own (often machine-made); **unnamed** are placeholders.

## Setup

The repository ships no decompilation data. Point the generator at your own copies:

```powershell
cd bridge
.venv\Scripts\python.exe -m engine_map.generate `
    --decomp C:\src\fnv_decomp `
    --verified C:\src\fnvsym\proofs\verified.csv `
    --exe $env:USERPROFILE\.fnvdecomp\FalloutNV.exe `
    --nvse-src C:\src\NVSE --plugin-src "JIP LN=C:\src\JIP-LN-NVSE" `
    --out state\engine_map.json --header state\fnv_engine_addresses.h
```

- `--decomp` (required): a checkout of [fnv_decomp](https://github.com/momusdigital/fnv_decomp).
- `--verified`: fnvsym's `proofs/verified.csv`. Without it no name counts as proven.
- `--exe`: an **unpacked** FalloutNV.exe 1.4.0.525 (the Steam exe is DRM-packed; Steamless unpacks it).
  It is checked against the decomp's 16 image probes and then gives the full call graph, Windows imports,
  setting addresses and defaults, RTTI class hierarchy and vtables, and the vanilla command tables. Only addresses, names and hashes are
  written. Without it the call graph only follows `FUN_xxxxxxxx` references in the source.
- `--nvse-src`, `--plugin-src LABEL=PATH`: script-extender source trees for their command definitions.
- `--header`: a C header of proven function addresses for native code.

Takes about 45 seconds and writes a ~40 MB JSON. Regenerate after pulling the decomp (class layouts are
still moving while it merges shared class headers) or a new `verified.csv`.

Then add an `fnv` block to the project profile (all optional):

```json
"fnv": {
  "engine_map": "../state/engine_map.json",
  "decomp_root": "C:/src/fnv_decomp",
  "crash_dirs": [],
  "mo2_root": "C:/Modding/FNV-Dev",
  "mo2_profile": "Dev"
}
```

Defaults: `engine_map` is `<state_dir>/engine_map.json`; `decomp_root` is the path recorded in the map.
Environment variables `FNV_ENGINE_MAP`, `FNV_DECOMP` and `FNV_CRASH_DIRS` work too.

## Example: where does the game apply limb damage?

1. `fnv_setting_info("fCombatPlayerLimbDamageMult")` → the setting lives at `0x011CFA40` (default 0.5)
   and exactly one engine function reads it: `0x009B6620`, in the hit-data code.
2. `fnv_func_code("0x009B6620")` → the decompiled body: it picks the body part that was hit (from
   the contact point, or a ray cast from the attacker's eyes), computes that part's damage with
   `FUN_00647a30(target, part, base)` and scales it by the setting. Its member and global names in
   the decomp are machine-made (status `unnamed`), so read the logic, not the names.
3. `fnv_func_callers("0x009B6620", depth=2)` → `InitHitData` and `InitFromObjects`, called from
   `ImpactProjectile::ProcessHits`.

## Crash doctor

Install [Yvile's Crash Logger](https://www.nexusmods.com/newvegas/mods/82540). It writes
`CrashLogger.log` next to `FalloutNV.exe` at each start and keeps older logs in `Crash Logs\`.
`fnv_crash_latest` picks the newest log that records a crash (the game folder, `Crash Logs`, the MO2
overwrite folder and any `crash_dirs`); pass `path` for a specific file.

`fnv_crash_explain` gives:
- each call-trace frame as an engine function with its proof status and source location, or as
  DLL + offset (the logger rebases DLL frames to 0x10000000; the tool undoes that) with the DLL's path
  and version;
- the forms in registers and on the stack, with the plugin that defines and last modified each;
- likely culprits with a confidence and the reasons: a plugin DLL at the top of the stack or hooking in
  between game frames, a non-vanilla plugin's form in a register, a mesh or texture being loaded,
  memory-manager crashes (the top frame is a victim), crashes in runtime code (look at the caller),
  NVAC or the Stutter Remover being installed, a running script.

Logs from 4.x versions of the logger (two different call-trace layouts) and Cobb's original logger are
parsed too, with less detail. The top frame is where a crash surfaced, not always its cause, and log text
is treated as data.
