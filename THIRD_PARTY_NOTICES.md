# Third-party notices

## GECK MCP (Fallout 3), by maksimka2432fr23 (MIT)
- Source: https://github.com/maksimka2432fr23/codex-skill-mcp-fallout3 (commit `b3c1044a`, 2026-09-06)
- Used in: `windows/` (ported to Fallout: New Vegas; changes listed in `windows/PORT-NOTES.md`)
- Licence: MIT, full text in `windows/LICENSE`

## Engine knowledge and crash doctor (`bridge/engine_map/`, `bridge/geck_mcp/fnv_*.py`)
Nothing from these projects is bundled. The engine map is generated on the user's machine from the
user's own checkouts and game files; the code here only reads them.
- fnv_decomp, the Fallout: New Vegas decompilation (https://github.com/momusdigital/fnv_decomp): read
  for function addresses, symbols, source locations and class headers. Its licence and CREDITS apply to it.
- fnvsym `proofs/verified.csv` (HUNK2): read for proven function names and their evidence.
- xNVSE (https://github.com/xNVSE/NVSE): the `CommandInfo`/`ParamInfo` record layout and the 1.4.0.525
  vanilla command table addresses follow its `CommandTable.h`/`CommandTable.cpp`; its source tree is read
  for xNVSE command definitions. JIP LN NVSE (jazzisparis and contributors) source is read the same way.
- Yvile's Crash Logger (Yvileapsis, fork of DavidJCobb's Crash Logger; https://github.com/yvileapsis/yUI-NVSE):
  `fnv_crash.py` parses the log format it writes. Cobb's Crash Logger (DavidJCobb, llde) logs are parsed best effort.

## Live tools and the engine companion (`bridge/native/engine/`, `bridge/geck_mcp/fnv_*.py`, `bridge/tools/fnv_setup_dev_profile.py`)
- fnv_decomp states that no licence is granted, for the reconstruction or for contributions. Nothing generated
  from it is in this repository: `engine_map.json` is built by each user from their own checkout and is
  git-ignored. The code here names some engine functions, addresses and structure offsets (in
  `fnv_facts.py`, comments and docs) as facts about FalloutNV.exe 1.4.0.525, with the evidence for each.
- xNVSE (no licence file in its repository): the companion talks to the xNVSE plugin interface through the
  declarations in `bridge/native/runtime/nvse_abi.h` (from upstream geck-mcp-nv); `fnv_damage_log` decodes
  hit data with the `ActorHitData` field offsets and the body part and hit flag names of its `GameProcess.h`
  and `GameForms.h`. No xNVSE code is copied.
- JIP LN NVSE (jazzisparis and contributors, GPL-3.0): its script commands are called at runtime
  (`CloseActiveMenu`...); `fnv_effects` and `fnv_input` use the `MagicTarget`/`ActiveEffect`/`EffectItem`
  offsets and control codes it documents. No JIP LN code is copied; its command definitions are read from its
  source when the engine map is generated.
- `fnv_patch_map` compares the running game with the user's own unpacked FalloutNV.exe (`fnv.clean_exe`),
  read locally; no executable is included or copied.
- `tools/fnv_setup_dev_profile.py` installs xNVSE, JIP LN NVSE and Yvile's Crash Logger from archives the user
  downloaded; it downloads nothing.

## Python dependencies (installed by pip, not bundled)
- MCP Python SDK (MIT), pydantic (MIT), Pillow (MIT-CMU), NumPy (BSD-3-Clause): `bridge/`
- MCP Python SDK, pywinauto (BSD-3-Clause), psutil (BSD-3-Clause), Pillow: `windows/`
- PyFFI (BSD-3-Clause) and setuptools (MIT) for `bridge/sprite_flipbook.py` and `bridge/tools/creature_skeleton.py`

## Not included
- No Bethesda files: no GECK, no `.esm`/`.esp`/`.bsa` game data and no data derived from them.
  Each user builds the master index and record templates from their own copy of the game.
- Fallout, Fallout: New Vegas and G.E.C.K. are trademarks of Bethesda Softworks / ZeniMax Media.
