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

## Python dependencies (installed by pip, not bundled)
- MCP Python SDK (MIT), pydantic (MIT), Pillow (MIT-CMU), NumPy (BSD-3-Clause): `bridge/`
- MCP Python SDK, pywinauto (BSD-3-Clause), psutil (BSD-3-Clause), Pillow: `windows/`
- PyFFI (BSD-3-Clause) and setuptools (MIT) for `bridge/sprite_flipbook.py` and `bridge/tools/creature_skeleton.py`

## Not included
- No Bethesda files: no GECK, no `.esm`/`.esp`/`.bsa` game data and no data derived from them.
  Each user builds the master index and record templates from their own copy of the game.
- Fallout, Fallout: New Vegas and G.E.C.K. are trademarks of Bethesda Softworks / ZeniMax Media.
