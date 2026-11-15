# fnv-mcp

**MCP tools that let an AI agent build, run, debug and change Fallout: New Vegas: the GECK, the running game and the
engine itself.**

fnv-mcp is a standalone continuation of [geck-mcp-nv](https://github.com/2Shay96/geck-mcp-nv) by **2Shay**, which
it is built on and keeps the full history of. Everything 2Shay made (the GECK bridge, the plugin builder, the script
compiler, the console companion) is here unchanged in purpose; see the original README below.

What fnv-mcp adds, on top of the 44 `geck_*` tools: **160 `fnv_*` tools** for the running game (204 in all).

- **Engine knowledge**: function and class lookup from the decompilation, a symbol database with source lines and
  named locals for the retail exe, crash explanation, engine warnings.
- **Live control**: time control (pause, step, frame advance), breakpoints and a debugger, screenshots and photo
  sheets ("eyes"), walking, talking, using doors and fast travel ("hands"), forms and scripts changed in place.
- **Engine changes**: C modules hooked into the running game, decomp functions recompiled with the game's own
  compiler and swapped in, both exportable as normal xNVSE plugins.
- **Performance and checks**: a sampling profiler, code coverage, thread, memory and object views, animation, audio,
  physics and formula inspectors, whole-plugin crawls, load-order bisection and a self-test.

Start with [bridge/docs/FNV-MCP-SETUP.md](bridge/docs/FNV-MCP-SETUP.md) and
[bridge/docs/FNV-LIVE-TOOLS.md](bridge/docs/FNV-LIVE-TOOLS.md). The `fnv_guide` tool has step-by-step guides.

Nothing from the game, the GECK or the decompilation is in this repository: each user builds the engine map and
symbol database from their own copies.

## Credits

- **2Shay**: author of [geck-mcp-nv](https://github.com/2Shay96/geck-mcp-nv), the project this repository is built
  on: `bridge/` (GECK bridge, plugin builder, script compiler, runtime diagnostics, console companion).
- **HUNK2**: the `fnv_*` live, engine and debugging tools and the engine companion.
- The [fnv_decomp](https://github.com/momusdigital/fnv_decomp) project, xNVSE, JIP LN NVSE and Yvile's Crash Logger,
  whose work these tools read at runtime; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

---

## Original project: GECK MCP for New Vegas 0.4.0 Beta (2Shay)


**0.4.0 Beta is a major upgrade:** the bundled companion source lets your AI agent **enter in-game console commands** and **query live mod indices/load-order prefixes with GetModIndex**, including explicitly named loaded plugins. Typed inventory and equipped-condition readback help verify effects; resumable compilation improves the editor workflow.

**Still in Beta:** Windows live tests passed, but broader GECK workloads and remote-session validation remain open. Use disposable plugins and saves.

**Source-only download: no precompiled DLL or EXE.** Your AI agent can follow [local setup and compilation](docs/SOURCE-SETUP.md) to build the helper and optional xNVSE companion. [Download 0.4.0 Beta](https://github.com/2Shay96/geck-mcp-nv/releases/tag/v0.4.0-beta.1).

Previous stable prototype: **0.3.0** on [Nexus Mods](https://www.nexusmods.com/newvegas/mods/99851?tab=files) and [GitHub Releases](https://github.com/2Shay96/geck-mcp-nv/releases/tag/v0.3.0). GitHub gets development updates first, so main may be newer than the Nexus download. See [upgrading and release checks](docs/RELEASING.md).

**Let an AI agent (Claude, Codex or any MCP client) build and edit Fallout: New Vegas plugins and drive the GECK.**

> ⚠️ **Work-in-progress prototype.** The `geck` bridge is tested end to end on **Windows 11** with the vanilla
> Steam GECK (see [docs/TESTING.md](docs/TESTING.md)). Linux/Proton is written but untested. Read the status table
> first, back up your Data folder, and use a throwaway plugin.

## What's inside

An optional read-only xNVSE companion adds automatic quest snapshots and comparisons
without console typing or screen control. See [setup and validation limits](bridge/docs/AUTOMATIC-RUNTIME.md).

The bridge now exposes **44 tools**, including four user-driven vanilla runtime
diagnostics tools and a shared CLI. They prepare safe query batches, collect fresh
checkpoint output, compare values and evaluate declarative assertions. Windows vanilla item-count/delta/error capture passed on 18 October; see [usage and limits](bridge/docs/MCP-QUICKSTART.md#runtime-diagnostics-29-bridge-tools).

| Folder | MCP server | What it does | Tested |
| --- | --- | --- | --- |
| [`bridge/`](bridge/README.md) | `geck` (44 tools) | **Builds whole plugins from a JSON spec** (records, cells, placed objects, stable FormIDs) without GECK. **Compiles scripts with GECK's own compiler** and caches the bytecode. Loads plugins in GECK, checks cells against the build, sets models, saves with backups. Also actor photos and animated sprite billboards. | ✅ Windows 11 (vanilla GECK) · ❓ Linux/Proton · also tested on Mac via CrossOver |
| [`windows/`](windows/README.md) | `geck_nv` (46 tools) | **FNV port of [GECK MCP for Fallout 3](https://github.com/maksimka2432fr23/codex-skill-mcp-fallout3)** by maksimka2432fr23. Windows UI automation: launch GECK through NVSE, navigate Cell View and the Object Window, drag objects into the Render Window, load order, transactions and rollback, placed-reference patches. | ❓ Windows (not yet run against the FNV GECK) |

You can run either server on its own, or both at once (their tool names don't overlap).

## Why it exists

The GECK is a 2010 GUI editor with no scripting interface. Other AI modding kits edit plugins through xEdit
and leave the GECK to you. This project gives an agent the GECK itself: loading, verifying, saving and
compiling scripts. It also adds an offline builder, so the agent can write a whole mod as a readable spec.

It is being used to make a real mod: *Salvatore Ganacci*, a sprite-based creature boss with its own skeleton,
scripts and quests. It was built from a spec and compiled through the bridge, and the boss already pursues and
fights the player in game.

## Quick start

1. Read [`bridge/README.md`](bridge/README.md) (any OS), [`windows/README.md`](windows/README.md) (Windows), or both.
2. Install Python 3.12+ and create a virtual environment in the folder you'll use.
3. Nexus download: build the GECK helper once with `build_helper.py` (see [`bridge/README.md`](bridge/README.md), step 3).
   Release downloads contain source only: **no DLL or EXE**. Build the helper locally; see [AI-assisted source setup](docs/SOURCE-SETUP.md). Optional console/telemetry DLLs are built locally for your project.
4. Register the server in your MCP client (examples are in each README).
5. First prompt: *"Run geck_status (or geck_doctor) and tell me what you see. Don't change anything."*

## Requirements

- Fallout: New Vegas and its GECK (Steam: Library → Tools). This package contains **no** game files, GECK or ESM data.
  Each user builds their own master index locally.
- Python 3.12+ and an MCP client.
- Optional automatic quest diagnostics: native Windows Steam FNV 1.4.0.525, xNVSE 6.4.9 or newer 6.x, and Zig 0.14.1 to build a companion for your project. See [setup](bridge/docs/AUTOMATIC-RUNTIME.md). Vanilla batch diagnostics need no extender.
- Optional: NVSE for `geck_launch(via_nvse=true)`. PyFFI for sprite tools installs with `bridge/requirements.lock.txt`.

## Safety

- Every tool that writes makes backups first. Read-only tools are labelled.
- The bridge refuses to load plugins over unsaved GECK work. The Windows server refuses file edits while GECK or the game runs.
- Windows UI tools move your mouse and type. Don't use the PC while they run.
- Report problems. Don't trust results you haven't checked in GECK or in game.

## Status and roadmap

The [v1.0 vision](docs/V1-VISION.md) records the proposed Windows build/compile/playtest/fix workflow and future release presentation requirements.

v0.3.0 adds runtime diagnostics to the existing bridge server and remains a prototype release.
The v0.2 series fixed what broke while building a real mod on Windows: tools now work
through Claude's Cowork device proxy, hidden GECK windows are restored, stale recovery barriers clear themselves after
a GECK restart, and the Render Window can be captured on Windows. Next steps: a GECK Extender pass, running the
`windows/` server against the FNV GECK, a Linux/Proton test, DLC masters, and one combined installer. See
[CHANGELOG.md](CHANGELOG.md).

## Licence and credits

- MIT. See [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
- `bridge/` © 2026 2Shay. `windows/` © 2026 maksimka2432fr23, with the FNV port by 2Shay.
- The `fnv_*` tools and the engine companion (fnv-mcp) © 2026 HUNK2.
- Made with AI coding agents (Anthropic Claude and OpenAI Codex), directed and tested by 2Shay; the fnv-mcp additions
  directed and tested by HUNK2.
- Not affiliated with or endorsed by Bethesda Softworks, ZeniMax, Microsoft, Anthropic or OpenAI.
  Fallout and G.E.C.K. are trademarks of their owners.

## Resumable compilation and readiness (0.4.0 candidate)

Build/install/load through the bridge, then use `geck_compile_start`,
`geck_compile_status` and `geck_compile_resume`. The shared CLI is
`bridge/compile_scripts.py`. Pending/blocked retain work and the editor reservation;
only verified means saved-script/cache/rebuild/install checks passed. Quiet UI
polling or input dispatch is provisional. Unknown prompts require review.
See [compile recovery](bridge/docs/MCP-QUICKSTART.md#resumable-compilation-040-candidate).

`geck_runtime_readiness` separates per-adapter support and installed files from
fresh process/build/mapping proof. Automatic sampling supports a single quest's
variables; item counts and reference state use vanilla manual batches. Setup
preflight previews exact files/backups and restart checks. No compiler download,
DLL install or console command runs without an explicit request.

## In-game console companion (0.4.0 Beta)

The bundled companion source provides opt-in `geck_console_setup`, `geck_console_readiness`,
`geck_console_query`, `geck_console_execute`, `geck_console_status` and `geck_console_release` tools.
Read-only telemetry is unchanged. See [console setup, commands and outcome semantics](bridge/docs/CONSOLE-COMMANDS.md).
Real FNV console smoke acceptance passed; broader GECK/Shadow gates remain
pending. This published version remains Beta.
