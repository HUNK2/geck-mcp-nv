# GECK bridge (`geck`): spec-to-plugin builder + GECK control

The bridge is the original part of GECK MCP for New Vegas. It has two layers:

1. **Offline plugin authoring (any OS, no GECK needed).** You describe a mod in a JSON *spec*: records,
   interior cells and placed objects. The bridge writes a valid `.esp` with stable FormIDs, checks every
   `@EditorID` reference against your own masters, and installs it with backups.
2. **GECK control.** A small 32-bit helper (`Probe.Mcp.exe`) runs next to GECK and drives it mostly through
   Windows messages; only the photo camera uses real input. The bridge uses it to load plugins, verify cells
   against the build, set models, save, **compile scripts with GECK's own compiler**, and take actor photos.

## Tested where?

| Launcher (`"launcher"` in the profile) | Platform | Status |
| --- | --- | --- |
| `native` | Windows | **Tested** on Windows 11 with the vanilla Steam GECK: status, build, install, plugin load and reload, cell verify, Render Window capture, preview, script compile, crash recovery ([docs/TESTING.md](../docs/TESTING.md)) |
| `wine` | Linux (Wine/Proton) | **Untested.** The helper must run in the same prefix as GECK |
| `crossover` | macOS + CrossOver | Tested on Mac via CrossOver (not a supported platform) |

Also untested: GECK Extender, and non-English GECK. The unit tests (plus 6 parser tests) pass on Linux and Windows 11;
CI runs them on Ubuntu, macOS and Windows.

## Setup

Requirements: Python 3.12+, Fallout: New Vegas with GECK, and an MCP client (Claude Desktop/Code, Codex, ...).

```sh
cd bridge
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt      # Windows: .venv\Scripts\python.exe
```

1. **Build the master index** (read-only, run once per game update). Set `FNV_DATA` if your Data folder is elsewhere.
   ```sh
   .venv/bin/python esm_index.py FalloutNV.esm
   ```
2. **Make a profile.** Copy `projects/example-<your OS>.json` to `projects/hellowasteland.json` and fix the paths.
3. **Build the helper for this version.** Release downloads ship source only, with no DLL or EXE. Rebuild even if an
   older Git checkout contains a helper. See [AI-assisted source setup](../docs/SOURCE-SETUP.md). On Windows this uses the C# compiler that comes with
   the .NET Framework 4 (already part of Windows):
   ```sh
   .venv\Scripts\python build_helper.py --project projects\hellowasteland.json
   ```
4. **Register the MCP server** in your client:
   ```json
   "geck": {
     "command": "/path/to/bridge/.venv/bin/python",
     "args": ["/path/to/bridge/run_mcp.py", "--project", "/path/to/bridge/projects/hellowasteland.json"]
   }
   ```
5. Ask your agent: *"Validate the spec, build the plugin and show me what it contains."*
   This uses `geck_spec_validate`, then `geck_plugin_build`, then `geck_plugin_inspect`, and touches nothing in the game.

## The 44 tools

| Group | Tools |
| --- | --- |
| Offline authoring | `geck_spec_validate`, `geck_plugin_build`, `geck_plugin_inspect`, `geck_master_lookup`, `geck_plugin_install` |
| Runtime diagnostics | `geck_runtime_prepare`, `geck_runtime_collect`, `geck_runtime_report`, `geck_runtime_close` (user-run vanilla batches; Windows item-count/delta/error acceptance passed) |
| Automatic runtime (optional xNVSE) | `geck_runtime_snapshot`, `geck_runtime_compare` (read-only quest telemetry; [setup and validation limits](docs/AUTOMATIC-RUNTIME.md)) |
| Compile workflow | `geck_compile_start`, `geck_compile_status`, `geck_compile_resume` |
| Readiness/setup | `geck_runtime_readiness`, `geck_runtime_setup_preflight`, `geck_runtime_save_context` |
| In-game console (opt-in) | `geck_console_readiness`, `geck_console_setup`, `geck_console_query`, `geck_console_execute`, `geck_console_status`, `geck_console_release` |
| Editor lifecycle | `geck_editor_close` (normal clean-session close only) |
| Editor state | `geck_status`, `geck_inspect_windows`, `geck_show_windows`, `geck_capabilities`, `geck_project_attach` |
| Load & verify | `geck_plugin_load`, `geck_plugin_load_status`, `geck_cell_verify` |
| Records | `geck_records_find`, `geck_record_read`, `geck_record_set_model`, `geck_preview_open`, `geck_preview_close` |
| Save & check | `geck_plugin_save`, `geck_plugin_validate` |
| Recovery | `geck_operation_get`, `geck_recovery_acknowledge` |
| Pictures | `geck_render_capture` (Windows and macOS), `geck_image_cutout`, `geck_actor_photo` (macOS only for now) |

Tools that act on the running GECK take an optional `editor_session`; leave it out to use the GECK that is running
(see [docs/MCP-QUICKSTART.md](docs/MCP-QUICKSTART.md)). After changing bridge code, restart your MCP client.

Runtime tools need no editor attachment. Their shared CLI is `runtime_diagnostics.py`;
see [runtime profiles, identity, capture, assertions and lifecycle](docs/MCP-QUICKSTART.md#runtime-diagnostics-29-bridge-tools).

The same jobs are available from the command line: `build_plugin.py` (`--project` uses the profile's state and build
folders), `install_plugin.py`, `live_load.py` (`--if-needed`), `cell_verify.py`, `compile_scripts.py` (build, then load in
GECK, compile every script, save and cache the compiled bytecode), `recover.py` (list or clear recovery barriers),
`photo_cli.py`, `sprite_flipbook.py` (an animated sprite billboard NIF from PNG frames; `--collision-material organic`
sets the sound when it is knocked about) and `tools/creature_skeleton.py` (a creature's own skeleton and animations;
`--project` finds the game Data folder). Both use PyFFI from the lock file and write the same bytes for the same input.

## Using this bridge from a separate workshop

Keep project specs, assets, profiles and state in the workshop, and run this repo's commands with an explicit
workshop profile. This is the authoritative implementation; do not maintain a copied `geck_mcp` package or helper.
Version 0.2.1 includes `sprite_flipbook.py --collision-static` for fixed, unmovable collision as well as collision
material selection. The sprite manifest reports the collision material and motion system read back from the NIF.

## FormIDs in the game console

Build receipts and plugin inspection show **file-local FormIDs**, not the game's current load order. A plugin with
one master commonly stores its own records with prefix `01`. In the game, replace that prefix with the plugin's
actual runtime load index. For example, file ID `0100080F` becomes `0A00080F` when the plugin loads at index `0A`.
Do not change the plugin's stored IDs to match a particular player's load order.

For a placed reference belonging to the plugin, its ID when clicked in the game console can establish the runtime
prefix. A dynamically created reference beginning with `FF` cannot. A short `plugins.txt` alone does not establish
the index used by the running game. Verify the runtime prefix before diagnosing a writer failure from “Invalid info”
or a quest lookup error. EditorIDs may also be unavailable in the vanilla game console; use the verified numeric ID.

## Safety built in

- Every edit uses a request key. Repeats are refused or replayed, never run twice.
- Operations are journaled in SQLite. An interrupted operation blocks further edits until you review and acknowledge it,
  or until GECK has been restarted (then it is released automatically as `stale`).
- Install and save make content-addressed backups and check hashes before and after.
- The bridge refuses to load anything if GECK has unsaved changes, and never discards your work.

## Known limits (v0.1)

- `geck_plugin_load` loads your plugin with **FalloutNV.esm only**. No DLC or other masters yet.
- Editor record tools target configured STAT records. Richer records (creatures, quests, scripts, weapons ...)
  are made through the spec builder, not by editing inside GECK.
- Control IDs and menu captions are from the English vanilla GECK.
- `Probe.cs` still contains early experimental operations (`cell.load`, `window.layout.place`, `object.place`) that
  only accept the author's test objects and use real mouse input. The MCP server does not use them.
- Actor photos use macOS `screencapture`. `geck_render_capture` also works on Windows: PrintWindow at the size GECK
  renders (works when other windows cover it). PrintWindow sometimes returns only the grey window background, so a
  blank result is retried with a repaint request (about 12 s), then a DPI-aware screen copy is tried if nothing
  covers the Render Window. Look at the PNG: the tool only checks that it is not one flat colour.

Docs: [docs/MCP-QUICKSTART.md](docs/MCP-QUICKSTART.md) · [docs/PHOTO.md](docs/PHOTO.md) · [docs/M1-FAST-BRIDGE.md](docs/M1-FAST-BRIDGE.md)

## 0.4.0 candidate compile recovery

Build/install/load first, then use `compile_scripts.py --project PROFILE` or
`geck_compile_start`. Retain the operationId and resume it with
`--operation-id UUID` / `geck_compile_resume`. `--status` reads local proof;
`--once` advances one phase; `--extend-seconds 1200` explicitly extends a reviewed
expired deadline. Pending is incomplete; blocked requires its reported next action.
Only verified sets compiled=true after persisted source/bytecode/cache/rebuild/install.

Each script is saved at a clean boundary before advancing. CP1252 source files
are read as Unicode from RichEdit; only CRLF/CR versus LF is normalized for hashes.
Measured/copied lengths detect truncation. SCPT verification checks EDID/FormID/type,
SCTX, SCHR/SCDA sizes, references and semantic variable names/indices/types, ignoring
unused SLSD padding. Spec/source/FormID/compiler/index/template changes invalidate
reuse. Legacy caches without saved compile provenance require recompilation.

Reservations span CLI/MCP clients/profiles targeting the same editor installation.
Client restart resumes the same operation; GECK restart invalidates UI state.
Explicitly reload the checkpoint artifact and resume to revalidate persisted work.
Unknown dirty work/prompts remain untouched. A Save Current Script prompt is
resolved only after native owner, exact EDID/source and Yes-button checks.
Uncertain saves are never blindly repeated. Existing recovery acknowledgment can
release a reviewed reservation with a clean editor/current disk hash; it retains
historical proof and abandons that operation.

Status reports loaded/installed/built hashes separately. Unknown loaded identity
or stale loaded bytes block save. Final rebuild compares saved records/overrides
before installation. First-class master override intents remain unsupported;
incomplete roundtrips are refused. Restart MCP clients after changing server code.

## Owner-requested console execution (23 October)

The v0.4.0 candidate now includes separate opt-in `geck_console_readiness`,
`geck_console_execute`, `geck_console_status` and `geck_console_release` tools.
Read-only telemetry is unchanged. See [console setup, commands and outcome semantics](docs/CONSOLE-COMMANDS.md).
Real FNV acceptance is pending; this candidate is not release-ready.


Console wire-v2 adds named-plugin `{form}` binding, explicitly authorized `coc`,
typed inventory/condition reads, decimal-string `started` identity, and MCP setup.
See [console workflows](docs/CONSOLE-COMMANDS.md); use a fresh v2 companion after
the one-time upgrade, and never convert the process FILETIME to a JS Number.

## Engine knowledge and crash doctor (`fnv_*`, HUNK2 fork)

Fourteen read-only `fnv_*` tools look things up in the game's own code through your local
[fnv_decomp](https://github.com/momusdigital/fnv_decomp) checkout: find engine functions, read their
decompiled code, callers/callees, symbolize addresses, check whether a name is proven, class layouts,
command tables and settings; and explain crash logs (Yvile's Crash Logger) in those terms. They work
offline and change nothing. Generate the engine map once with `python -m engine_map.generate`; see
[docs/FNV-ENGINE-TOOLS.md](docs/FNV-ENGINE-TOOLS.md). No decomp data ships with this repository.
