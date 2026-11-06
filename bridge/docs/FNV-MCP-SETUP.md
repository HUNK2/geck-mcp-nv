# Setting up the fnv_* tools (fnv-mcp)

The fnv_* tools give an MCP client (Claude Code, for example) engine knowledge from the Fallout: New Vegas
decompilation, a crash doctor, game control and scenario tests, live inspection, full control of the running
game, deep tools (watch hooks, hang doctor, patch map) and data/authoring tools. They run on Windows with the
Steam FalloutNV.exe 1.4.0.525 and an MO2 dev instance kept apart from your own game setup.

Tool references: [FNV-ENGINE-TOOLS.md](FNV-ENGINE-TOOLS.md) (offline engine knowledge, crash doctor) and
[FNV-LIVE-TOOLS.md](FNV-LIVE-TOOLS.md) (everything that touches the game). A worked example of a mod built and
tested end to end: [examples/hello-quest](../examples/hello-quest/README.md).

## What you need

| | Where it comes from |
| --- | --- |
| Fallout: New Vegas (Steam), FalloutNV.exe 1.4.0.525 | Steam |
| xNVSE 6.4.9 or newer | its GitHub releases (you download it) |
| JIP LN NVSE | Nexus Mods (you download it) |
| Yvile's Crash Logger | Nexus Mods (you download it) |
| Mod Organizer 2 (portable instance) | its GitHub releases (you download it) |
| Python 3.12 and this repository's bridge (`bridge/`, see [MCP-QUICKSTART.md](MCP-QUICKSTART.md)) | |
| Zig 0.14.1 (builds the engine companion) | ziglang.org (you download it) |
| A checkout of the fnv_decomp decompilation, for the engine map | its repository |
| Optional: your own unpacked FalloutNV.exe (Steamless), for `fnv_patch_map` | you make it locally |

Nothing here downloads files or ships game files, the decompilation or anything generated from them.

## Steps

1. **MO2 dev instance.** `tools/fnv_setup_dev_profile.py` sets up a portable MO2 instance with a `Dev`
   profile from archives you downloaded (dry run by default, `--apply` to do it; it records what it added to
   the game folder):

   ```
   py -3.12 tools\fnv_setup_dev_profile.py --mo2 C:\Modding\FNV-Dev --downloads C:\Modding\downloads
   ```

   It writes `bAlwaysActive=1` (the game keeps running unfocused), skips the intro, uses a 1280x720 window,
   installs xNVSE into the game folder (it cannot go through MO2) and JIP LN NVSE and Yvile's Crash Logger as
   MO2 mods.
2. **Engine map.** Generate it from your fnv_decomp checkout (see FNV-ENGINE-TOOLS.md). It stays on your
   machine.
3. **Project file.** Add an `fnv` block to your geck-mcp-nv project file:

   ```json
   "fnv": {
     "decomp_root": "C:/path/to/fnv_decomp",
     "mo2_root": "C:/Modding/FNV-Dev", "mo2_profile": "Dev",
     "zig": "C:/tools/zig-x86_64-windows-0.14.1/zig.exe",
     "engine_control": "full",
     "tests_dir": "../tests",
     "clean_exe": "C:/path/to/your/unpacked/FalloutNV.exe"
   }
   ```

   `engine_control` is `"read"` by default (the act tools refuse); `"full"` allows memory writes, engine calls,
   console changes and hot-swap, all journaled and undoable. `clean_exe` is optional.
4. **Register the MCP server** with your client (MCP-QUICKSTART.md) and call **`fnv_setup_check`**: it lists
   each prerequisite with what to do when one is missing.
5. With the game closed, call **`fnv_companion_install`** (builds the engine companion with Zig and installs it
   as its own MO2 mod).
6. **`fnv_launch start=coc:GSProspectorSaloonInterior`** (or `continue` for your newest dev save), then try
   `fnv_player`, `fnv_script_profile`, `fnv_test_run_all` on `examples/fnv-tests`.

## Known issues

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
