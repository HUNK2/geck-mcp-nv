# Setting up fnv-mcp

This is the one setup guide. It takes a Windows machine from nothing to `fnv_status` answering from the running
game. Run every command from the `bridge` folder of your clone (`cd bridge` first), in PowerShell.

Tool references: [FNV-ENGINE-TOOLS.md](FNV-ENGINE-TOOLS.md) (offline engine knowledge, crash doctor) and
[FNV-LIVE-TOOLS.md](FNV-LIVE-TOOLS.md) (everything that touches the game). A worked example of a mod built and
tested end to end: [examples/hello-quest](../examples/hello-quest/README.md).

## What you need

| | Where it comes from |
| --- | --- |
| Windows 10 or 11 | |
| Fallout: New Vegas (Steam), FalloutNV.exe 1.4.0.525 | Steam |
| Python 3.12 (the `py` launcher) | python.org |
| An MCP client (Claude Code, Claude Desktop, Codex...) | |
| xNVSE 6.4.9 or newer | its GitHub releases (you download it) |
| JIP LN NVSE | Nexus Mods (you download it) |
| Yvile's Crash Logger | Nexus Mods (you download it) |
| Mod Organizer 2 (portable instance) | its GitHub releases (you download it) |
| Zig 0.14.1 (builds the engine companion) | ziglang.org (you download it) |
| A checkout of [fnv_decomp](https://github.com/momusdigital/fnv_decomp), for the engine map | its repository |
| Optional: your own unpacked FalloutNV.exe (Steamless), for `fnv_patch_map` and planning hooks offline | you make it locally |
| Optional, for the `geck_*` tools only: the GECK and the helper `build_helper.py` builds | [bridge/README.md](../README.md) |

Nothing here downloads files or ships game files, the decompilation or anything generated from them.

## Steps

1. **Python environment.**

   ```powershell
   cd bridge
   py -3.12 -m venv .venv
   .venv\Scripts\python.exe -m pip install -r requirements.lock.txt
   ```

2. **Project file.** Copy the example and edit the copy:

   ```powershell
   Copy-Item projects\example-fnv.json projects\my-fnv.json
   ```

   Set `game_root` to your game folder. The other values work as they are; the settings table below lists every
   key. Keep `engine_control` at `"read"` until you want the agent to change the game (see "Safety levels").

3. **MO2 dev instance.** `tools\fnv_setup_dev_profile.py` sets up a portable MO2 instance with a `Dev` profile
   from archives you downloaded. It is a dry run by default; `--apply` does it and records what it added to the
   game folder:

   ```powershell
   .venv\Scripts\python.exe tools\fnv_setup_dev_profile.py --mo2 C:\Modding\FNV-Dev --downloads C:\Modding\downloads
   .venv\Scripts\python.exe tools\fnv_setup_dev_profile.py --mo2 C:\Modding\FNV-Dev --downloads C:\Modding\downloads --apply
   ```

   It writes `bAlwaysActive=1` (the game keeps running unfocused), skips the intro, uses a 1280x720 window,
   installs xNVSE into the game folder (it cannot go through MO2) and JIP LN NVSE and Yvile's Crash Logger as MO2
   mods. Then set `fnv.mo2_root` to `C:/Modding/FNV-Dev` and `fnv.mo2_profile` to `Dev` in `my-fnv.json`.

4. **Engine map.** Generate it from your fnv_decomp checkout into the path `fnv.engine_map` names (about 45
   seconds; it stays on your machine):

   ```powershell
   .venv\Scripts\python.exe -m engine_map.generate --decomp C:\src\fnv_decomp --out state\engine_map.json
   ```

   [FNV-ENGINE-TOOLS.md](FNV-ENGINE-TOOLS.md) has the optional inputs (`--exe` for the full call graph, the
   script-extender sources) and the symbol database.

5. **Zig.** Unpack Zig 0.14.1 and set `fnv.zig` to its `zig.exe` (or set the `GECK_MCP_ZIG` environment variable).

6. **Register the MCP server** with your client. With Claude Code (use your own absolute paths):

   ```powershell
   claude mcp add fnv -- C:\path\to\bridge\.venv\Scripts\python.exe C:\path\to\bridge\run_mcp.py --project C:\path\to\bridge\projects\my-fnv.json
   ```

   For a client with a JSON file, the entry is:

   ```json
   "fnv": {
     "command": "C:/path/to/bridge/.venv/Scripts/python.exe",
     "args": ["C:/path/to/bridge/run_mcp.py", "--project", "C:/path/to/bridge/projects/my-fnv.json"]
   }
   ```

   Restart the client. If the project file is not valid, the server stops at once with one line saying which key
   is wrong (the client shows it in its MCP log).

7. **Check the machine.** Ask the agent to call **`fnv_setup_check`**: it lists each prerequisite with what to do
   when one is missing.
8. With the game closed, call **`fnv_companion_install`**: it builds the engine companion with Zig and installs it
   as its own MO2 mod.
9. **`fnv_launch start=coc:GSProspectorSaloonInterior`** (or `continue` for your newest dev save), then
   **`fnv_status`**: it answers from the running game. From here, `fnv_guide` has step-by-step guides.

A first prompt to give the agent: *"Call fnv_setup_check and fix what it reports with me, then launch the game
with fnv_launch and show me fnv_status."*

## Safety levels

`fnv.engine_control` has two levels.

- **`"read"`** (the default): look at the game, start and stop the dev game (only the game `fnv_launch` started),
  and install the engine companion.
- **`"full"`**: change the game and run code in it: memory writes, engine calls, console changes, hot-swap, hooks,
  and anything that puts agent-chosen code or files into the game or its mod profile (`fnv_plugin_build` build
  and install, `fnv_install`, `fnv_profile_set` without `dry_run`). **`"full"` means code execution as your
  Windows user**: `fnv_native` compiles C and loads it into the game process. Changes to the running game are
  journaled and undoable; files put into the mod profile are not.

The tools act on the MO2 profile named in `fnv.mo2_profile`, so point it at a profile of its own, not the one you
play with.

## What each tool family needs

| Tool family | Needs |
| --- | --- |
| `fnv_guide`, `fnv_setup_check`, `fnv_crash_latest`, `fnv_crash_explain` (without engine names), `fnv_save_inspect`, `fnv_save_diff` | nothing beyond the project file |
| Engine knowledge: `fnv_func_*`, `fnv_class_*`, `fnv_symbolize`, `fnv_global_find`, `fnv_setting_info`, `fnv_string_xrefs`, `fnv_command_info`, `fnv_script_explain` | the engine map |
| `fnv_symbols`, source lines and named locals in other tools | the symbol database (optional) |
| `fnv_name_*` (the naming tools, an optional extra) | the fnvsym proof folder (`FNVSYM_ROOT`) |
| Live tools: `fnv_status`, `fnv_player`, `fnv_ref`, `fnv_photo`, tests, profiling, debugging... | a running game with the engine companion |
| Act tools and actions (they change the game or the mod profile) | a running game and `fnv.engine_control = "full"` |
| `fnv_patch_map`; hook plans while the game is closed | your unpacked FalloutNV.exe (`fnv.clean_exe`) |
| `geck_*` | the GECK, the helper and the GECK settings (`plugin`, `helper`, `records` or `spec`); without them they answer `NOT_CONFIGURED` |

## Settings

Paths in the project file may be relative to the project file. `fnv_*` keys go in the `fnv` block.

| Key or variable | Default | What it is |
| --- | --- | --- |
| `project_id` | (required) | Lower-case name of the project; used in state file names |
| `game_root` | (required) | The game folder (with `FalloutNV.exe` and `Data`) |
| `state_dir` | (required) | Where the tools keep their state, logs, builds and journals |
| `launcher` | `native` | The only launcher (Windows) |
| `legacy_tools` | `false` | Registers the legacy `geck_runtime_*` and `geck_console_*` tools (or `run_mcp.py --legacy`) |
| `plugin` | none | GECK: the `.esp` the `geck_*` tools work on |
| `helper` | none | GECK: the 32-bit helper `build_helper.py` builds |
| `records` | `[]` | GECK: the STAT records the record tools may edit |
| `spec` | none | GECK: the JSON spec the plugin is built from |
| `source_roots` | `[]` | GECK: extra folders the spec's sources are read from |
| `helper_timeout_seconds` | `45` | GECK: how long one helper call may take (5 to 90) |
| `transport` | `auto` | GECK: `persistent`, `oneshot` or `auto` (persistent when the helper supports it) |
| `fnv.engine_control` | `read` | `read` or `full` (see "Safety levels") |
| `fnv.engine_map` | `<state_dir>/engine_map.json` | The engine map from `engine_map.generate` |
| `fnv.decomp_root` | the path in the map | Your fnv_decomp checkout |
| `fnv.symdb` | `<state_dir>/symdb.sqlite` | The symbol database (optional) |
| `fnv.research_dir` | none | A folder of your own Markdown notes that `fnv_guide` searches |
| `fnv.crash_dirs` | `[]` | More folders for crash logs |
| `fnv.mo2_root` | none | The portable MO2 instance |
| `fnv.mo2_profile` | none | The MO2 profile the tools act on (one of its own) |
| `fnv.mo2_executable` | `NVSE` | The MO2 executable entry that starts the game |
| `fnv.zig` | none | Zig 0.14.1 `zig.exe` |
| `fnv.engine_pipe` | `fnvmcp-engine` | Name of the engine companion's pipe |
| `fnv.tests_dir` | none | Folder of scenario tests for `fnv_test_run_all` |
| `fnv.clean_exe` | none | Your unpacked FalloutNV.exe 1.4.0.525 (read locally, never copied) |
| `FNV_ENGINE_MAP`, `FNV_DECOMP`, `FNV_SYMDB`, `FNV_CRASH_DIRS`, `FNV_RESEARCH_DIR` | none | The same as the `fnv` keys above; the project file wins |
| `FNV_CLEAN_EXE` | none | The same as `fnv.clean_exe`; `fnv_plugin_build` then falls back to `%USERPROFILE%\.fnvdecomp\FalloutNV.exe` |
| `FNVMCP_PIPE` | none | The same as `fnv.engine_pipe` |
| `FNVSYM_ROOT` | none | The fnvsym checkout the `fnv_name_*` tools need; without it they answer `NO_PROOF_FOLDER` |
| `GECK_MCP_ZIG` | none | Zig, when `fnv.zig` is not set |
| `GECK_MCP_RELOAD` | on | `0` serves without the reloading supervisor (same as `run_mcp.py --no-reload`) |
| `GECK_SUPERVISOR_LOG` | `<state_dir>/supervisor.log` | The supervisor's log file; empty for none |
| `GECK_MCP_TRANSPORT` | none | `oneshot` forces one helper launch per call (GECK) |

## Known issues

The open problems are listed in [KNOWN-ISSUES.md](KNOWN-ISSUES.md). The ones met during setup:

- Turn Discord's in-game overlay off globally (Discord > User Settings > Game Overlay): its hook crashed the
  game at startup with JIP LN loaded on the test machine, even with the per-game switch off.
- Every quit leaves a Crash Logger log (a plugin faults in its exit cleanup, with or without the companion);
  the crash doctor marks such logs `duringShutdown`.
- The game opens its pause menu when its window loses focus. Test runs keep the world running (the companion
  closes the pause menu and message boxes during a run; `allow_menus: true` opts out).
- MCP clients that put their server in a job object without breakaway stop MO2 from starting the game; the
  bridge then opens a launch shortcut through Explorer. Claude Code's launcher allows breakaway.
- The running game keeps no quest EditorIDs, so scripts with `Quest.variable` references compile only in GECK
  (not with `fnv_compile_ingame` / `fnv_scripts_compile`).
