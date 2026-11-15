# Automatic runtime diagnostics (optional xNVSE adapter)

> **Legacy toolset.** The `geck_runtime_*` and `geck_console_*` tools (15) are off by default since fnv-mcp 0.5: the `fnv_*` tools do these jobs through the engine companion. Set `"legacy_tools": true` in the project file, or start the server with `--legacy`, to register them. They will be removed in a later release.

`geck_runtime_snapshot` reads fresh quest values from a small native companion;
`geck_runtime_compare` compares a previous snapshot in the same process/load/build.
Both use the same core as `runtime_companion.py`. They require no GECK attachment,
console typing, screen control or input automation. Existing vanilla batch tools
remain available without xNVSE.

## Supported scope

- Native Windows, FNV Steam runtime 1.4.0.525; xNVSE **6.4.9 or newer 6.x**.
  The DLL checks the packed extender version before accessing the script interface.
  GOG, NoGore, Wine/Proton, older extenders and xNVSE 7 are not validated here.
- `quest_variable` probes, one existing quest per compiled profile, at most 100
  distinct variables. Profile plugin must equal the configured project plugin.
- Read-only public xNVSE messaging/script interfaces. No game addresses, engine
  structure offsets, process memory writes, arbitrary script endpoint or save
  serialization. Expressions are generated from strict profile fields at build
  time; callers cannot submit console commands.
- Every 500 ms on the main game loop: resolve `GetModIndex`, verify `HasVariable`,
  read numeric values, atomically replace a project-specific telemetry JSON file.
  Snapshots are sequential observations, not a same-frame transaction.

## Build and install

Use the normal project configuration and a runtime profile, for example:

```json
{
  "schema_version": 1,
  "plugin": "MyTest.esp",
  "probes": [
    {"key":"fuel", "kind":"quest_variable", "local_form":"000803", "variable":"Fuel"},
    {"key":"idle_waste", "kind":"quest_variable", "local_form":"000803", "variable":"IdleFuelUsed"}
  ],
  "rules": [{"key":"no_idle_waste", "kind":"equals", "fields":["idle_waste"], "value":0}]
}
```

The local quest suffix comes from your own build receipt; the example is fictional.
No runtime prefix is configured. The companion resolves it in the running game.

Obtain Zig 0.14.1 from [the official download page](https://ziglang.org/download/0.14.1/).
The Windows x86_64 archive SHA256 is
`554f5378228923ffd558eac35e21af020c73789d87afeabf4bfd16f2e6feed2c`.
The CLI does not download or install a compiler. Use an absolute compiler path:

```powershell
.venv\Scripts\python.exe runtime_companion.py --project projects\my_mod.json build --profile my-runtime.json --zig C:\tools\zig\zig.exe
```

This prints a build receipt. DLL, generated header, receipt and fake host are
stored under the project's ignored state directory. Native source is in
`bridge/native/runtime/`; no game files or Bethesda assets are needed to compile.

Quit FNV normally, determine the SHA256 of the checked installed ESP, then:

```powershell
.venv\Scripts\python.exe runtime_companion.py --project projects\my_mod.json install --build-receipt PATH\TO\build.json --expected-plugin-hash YOUR_64_CHARACTER_SHA256
```

Installation refuses any running FNV process, a changed build/ESP, or an unowned
or modified destination DLL. It backs up an owned previous DLL and receipt.
It installs only a project-specific `geck_mcp_<project hash>.dll` under
`Data/NVSE/Plugins`; game activation lists and existing mod assets stay untouched.

Launch through xNVSE or the mod manager's configured xNVSE entry and load a test
save. Installed files or `nvse_steam_loader.dll` alone are insufficient proof:
the reader verifies the loaded companion and `nvse_1_4.dll` in the actual process.
A normal launch that skips xNVSE cannot produce valid automatic observations.

## Read and compare

```powershell
.venv\Scripts\python.exe runtime_companion.py --project projects\my_mod.json snapshot --profile my-runtime.json --expected-plugin-hash YOUR_64_CHARACTER_SHA256 > baseline.json
.venv\Scripts\python.exe runtime_companion.py --project projects\my_mod.json snapshot --profile my-runtime.json --expected-plugin-hash YOUR_64_CHARACTER_SHA256 --baseline baseline.json
```

For MCP, pass `profile` and `expected_plugin_hash` to `geck_runtime_snapshot`;
pass its returned `data` as `baseline` to `geck_runtime_compare`. Restart the MCP
client after updating the server code to discover the two new tools (31 total).
Keep the game advancing frames; paused/background-suspended games can become stale.

The reader checks receipt/project/profile identity, installed ESP/DLL hashes,
running executable, process creation time, loaded modules, sample age (3 seconds),
numeric completeness and a game-resolved load index. Missing values, query errors,
nonfinite numbers or stale output are errors, never zero or pass. Declarative
rules reuse the vanilla evaluator. If a quest has not initialized yet, use an
explicit `Initialized == 1` rule rather than treating zero-valued locals as ready.

Load/new-game/menu events change the epoch. A different process, epoch, profile,
ESP/DLL hash or non-advancing sequence refuses comparison and needs a new baseline.
After changing the ESP: quit, install the checked build, re-run companion install
to bind its hash, restart and capture a fresh baseline. Hot ESP replacement is
not supported. The adapter only observes; edits still go through the usual source
and GECK compile/install workflow.

To remove it, quit normally and move only the unchanged owned DLL and telemetry
JSON/.tmp to a local archive, using `state_dir/companion/install.json` for ownership
and backups. Do not remove other plugins. No save/co-save records need migration.

## Evidence and limits

The initial project-scoped adapter was tested live on Windows/xNVSE 6.4.9: an
isolated disposable plugin reported an observed speed of 60 versus expected 250
and 1.5 fuel consumed over 1.5 stationary driving seconds. After source repair and
GECK recompilation, automatic fresh stdio MCP samples reported speed 250, zero
speed error and fuel unchanged at 100 over two stationary driving seconds.

The reusable adapter adds generated profiles, strict installed ownership/build
binding and integration into the main server. Its generated 32-bit DLL is tested
under a fake xNVSE host in six modes: complete values, missing plugin, missing
variable, nonfinite output, compilation failure and runtime script error, plus
load failure, epoch changes, exit and bounded expression compilation.

On 19 October 2026, the generalized adapter also passed in-game acceptance using
its generated profile/DLL, with the precursor disabled. A fresh 31-tool main MCP
server and the shared CLI verified the loaded companion, build/profile identity
and game-resolved prefix. Stationary driving reported speed 250, zero speed error,
zero idle fuel used and fuel unchanged over two seconds; all four configured
rules passed. A later stopped sample restored speed from 250 to 100 and cleared
the speed effect; comparing against the driving baseline returned the expected
deltas. A deliberately wrong expected ESP hash was rejected without observations.
These observations cover the tested isolated quest on Windows and
xNVSE 6.4.9, not every profile or supported extender release.

Portable tests cover malformed/stale data, identity changes, assertions, deltas,
receipt ownership/backups and CLI/MCP service envelopes. Windows CI downloads the
pinned official compiler, verifies its hash and exercises the generated DLL.

ABI reference: [xNVSE PluginAPI.h at 0ccd23a](https://github.com/xNVSE/NVSE/blob/0ccd23ad885ddae533c1790a3fc56cd073e38de3/nvse/nvse/PluginAPI.h).
Only the minimal public C ABI declarations are included; no upstream implementation
or SDK checkout is redistributed.

## 0.4.0 candidate readiness and context

Use geck_runtime_readiness before collection and geck_runtime_setup_preflight
before explicit setup. Readiness separates static probe support, configured and
installed status from fresh live process/build/mapping evidence. Mixed item-count
and disabled/dead profiles receive an unsupported-probe list and vanilla manual
route; automatic support remains one quest's variables. Setup preflight previews
exact files/backups, prerequisites, compiler version and restart/verification steps.
It never downloads, builds or installs a compiler/DLL. Explicit setup uses existing
CLI build/install commands, with FNV normally closed.

GetModIndex is a reliable lookup in a verified current runtime context. The
optional companion already evaluates it via xNVSE APIs without console input.
An installed DLL or a historical prefix does not prove this path is live. Current
prefix is displayed only with a fresh verified sample and process/build/sequence
evidence; otherwise mapping stays unverified. The read-only telemetry adapter
does not execute commands. The owner-requested v0.4 console actuator is separate
and requires explicit setup; see CONSOLE-COMMANDS.md.

Use geck_runtime_save_context / runtime_companion.py save-context --label LABEL
when the context explicitly changes; old samples/baselines are rejected. Companion
process/load epoch/build/profile changes also invalidate mappings. Automatic
detection of every save-context change is not implemented.

## Owner-requested console execution (23 October)

The v0.4.0 candidate now includes separate opt-in `geck_console_readiness`,
`geck_console_execute`, `geck_console_status` and `geck_console_release` tools.
Read-only telemetry is unchanged. See [console setup, commands and outcome semantics](CONSOLE-COMMANDS.md).
Real FNV acceptance is pending; this candidate is not release-ready.
