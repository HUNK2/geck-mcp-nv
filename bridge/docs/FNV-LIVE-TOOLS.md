# fnv_* live tools: game control, scenario tests, live inspection and full control

These tools work on the **running game** and its plugins. They are the fnv-mcp milestones 4-8 on top of the offline
engine tools in [FNV-ENGINE-TOOLS.md](FNV-ENGINE-TOOLS.md):

- **Game control and tests**: launch Fallout: New Vegas through a portable MO2 dev profile, load a save or
  a cell with no clicks, quit and restart, install plugins, read logs, and run repeatable YAML tests.
- **Live inspection**: references, actors' AI, quests, actor values, active effects, extra data,
  inventories, script variables, the crosshair, and memory by `Class::field` path.
- **Full control**: console lines with their output, script snippets, memory writes, engine function
  calls, form edits, game settings, actor actions, bounded player input. Every change is journaled and
  can be undone.
- **Deep tools** (milestone 7): a script profiler, a line-by-line script trace, a damage log, crash
  context, a hang doctor and a map of the engine code other plugins patched.
- **Data and authoring** (milestone 8): the load order, records across all plugins with their overrides,
  a script lint against the game's command tables, compiling with the running game's own compiler, and
  replacing a running script's code without a restart.

They need the **fnvmcp engine companion**, an opt-in xNVSE plugin separate from geck-mcp-nv's own
companions. Unlike those, it works with engine addresses, so it lives in this fork's fnv-mcp work and
is not part of upstream's address-free design.

## How it works

```
fnv_* tool (Python) ──named pipe \\.\pipe\fnvmcp-engine──▶ fnvmcp_engine.dll (in the game)
                                                            pipe thread: queues one request
                                                            xNVSE MainGameLoop: serves it on the main thread
```

The DLL (`native/engine/fnvmcp_engine.c`) hard-codes no engine addresses. It offers primitives and the
bridge decides what to do with them:

| Op | What it does |
| --- | --- |
| `rd` `rs` `rp` `ls` | Read memory, a C string, a pointer chain, or a linked list (`{data, next}` nodes) |
| `wr` | Write memory; returns the old bytes for the undo journal |
| `call` | Call a function with explicit ecx/edx and stack words; returns eax, edx and st0 |
| `con` | Run a console line through xNVSE's console interface and return what it printed |
| `eval` / `run` | Evaluate an xNVSE expression or a script snippet; typed result (number, form, string, array) |
| `sched` | Run a console line later on the main thread (used to release held keys, and to quit) |
| `every` | Run a compiled script snippet every N ms for a while (closes arrival message boxes, keeps tests unpaused) |
| `events` | xNVSE messages seen: loads, saves, new game, exit to menu, script errors with text |
| `hook` `unhook` `hookctl` `hookfilter` `hooks` | Watch hooks on engine functions (below) |
| `compile` | Compile script text with xNVSE's CompileScript into a new Script object (not run, not cached) |

All ops of one request run in the same frame, in order. Reads and writes go through
`ReadProcessMemory`/`WriteProcessMemory` on the game's own process, so a bad address fails cleanly.
Engine calls are not guarded: a wrong call can crash the game, and the crash doctor explains it.

The companion refuses to load except on FalloutNV 1.4.0.525 with xNVSE 6.x. Only the current Windows user
(and SYSTEM) can open its pipe, and remote clients are rejected.

Console output capture uses the game's own mechanisms (decomp references in `geck_mcp/fnv_facts.py`):
the companion routes console output to `%LOCALAPPDATA%\fnvmcp\console.log` with the vanilla `scof`
command, and the bridge sets the vanilla "verbose messages" byte and the console-visible byte for the one
frame a line runs, so commands print their results while the console is closed.

### Watch hooks

The bridge picks a proven decomp function, reads its first bytes and decodes whole instructions covering
five bytes (`fnv_deep.decode_prologue`: MSVC prologues, and a jump another plugin already put there, which is
relocated so both hooks run). The companion replaces them with a jump to a small stub:

```
target:      jmp entry                          (the moved bytes run from the trampoline)
entry:       push hook; jmp enter_common        pushad/pushfd, record the call, ret into the trampoline
trampoline:  moved bytes; jmp target+n
exit stub:   (optional) the caller's return address is swapped for it; records eax/edx and the time,
             then returns to the real caller kept on a per-thread shadow stack
```

A hook records into a ring (4096 calls: registers, up to 8 arguments, a 96-byte copy of what an argument
points to, the frame, the result and the time) and/or a per-key table (calls, total and worst time), which
the bridge reads with `rd`. A filter records only calls where a register or argument has a given value.
Hooks only observe: every call goes on unchanged. The stub code never touches the x87 stack (a float result
in st0 survives) and keeps the thread's LastError. Unhooking restores the original bytes if the jump is
still ours; stub memory stays allocated so calls already inside finish normally.

The companion also installs a vectored exception handler that only observes: at a hardware fault it writes
`%LOCALAPPDATA%\fnvmcp\crash-context-<pid>-<n>.json` (the hooked calls active on the faulting thread with
their `this`/argument form ids, the op being served, the console line being run, recent events). It writes
nothing once xNVSE has announced the game is exiting.

## Setup

The full guide, from an empty machine, is [FNV-MCP-SETUP.md](FNV-MCP-SETUP.md); `fnv_setup_check` says what is
missing. In short:

1. A portable MO2 instance with a dev profile, xNVSE 6.4.9+ and JIP LN NVSE (fnv-mcp `SETUP-NOTES.md`
   lists the steps; `scripts/setup_dev_profile.py` in fnv-mcp creates it). In the profile's
   `Fallout.ini`: `bAlwaysActive=1` under `[General]`, so the game keeps running unfocused.
2. In the project file's `fnv` block:

   ```json
   "fnv": {
     "mo2_root": "C:/Modding/FNV-Dev", "mo2_profile": "Dev",
     "zig": "C:/tools/zig-x86_64-windows-0.14.1/zig.exe",
     "engine_control": "full",
     "tests_dir": "../state/fnv-tests"
   }
   ```

   `engine_control` is `"read"` by default: the act tools then refuse. Set `"full"` to allow changes.
   For `fnv_patch_map`, add `"clean_exe"`: your own unpacked FalloutNV.exe 1.4.0.525 (the Steam exe keeps its
   code encrypted on disk; Steamless unpacks it). It is read locally and never copied.
3. With the game closed, `fnv_companion_install` builds the DLL with Zig and installs it as the MO2 mod
   `fnvmcp engine companion` (it never writes into the game folder).
4. `fnv_launch start=continue` starts the game and loads the newest dev-profile save.

## Tools

| Tool | Kind | What it does |
| --- | --- | --- |
| `fnv_status` | read | Running? Phase (main_menu, loading, in_game, in_menu, paused_menu), cell, loaded plugins, companion ping |
| `fnv_launch` / `fnv_quit` / `fnv_restart` | control | Start at `menu`, `continue`, `save:<name>` or `coc:<cell>`; quit via `qqq`, then window close, then terminate |
| `fnv_install` | control | A built plugin into the dev profile as its own MO2 mod, enabled (game closed) |
| `fnv_companion_install` | control | Build and install the engine companion |
| `fnv_logs` | read | New lines of nvse.log, the console log, CrashLogger.log, JIP and MO2 logs |
| `fnv_test_run` / `fnv_test_run_all` | control | Run YAML scenarios (below) |
| `fnv_snapshot` / `fnv_diff` | read | Capture chosen state and compare |
| `fnv_player` `fnv_ref` `fnv_refs_near` `fnv_crosshair` | read | Player and references: base, position, cell, flags, runtime class (RTTI), extra data |
| `fnv_actor_ai` `fnv_actor_values` `fnv_effects` | read | AI package and procedure, process level, combat, detection; actor values with their modifiers and the effects behind them; active effects with source items |
| `fnv_quest` `fnv_vars` | read | Stage list with done flags, objectives with text and state, script variables by name |
| `fnv_extra_data` `fnv_inventory` | read | A reference's extra data decoded; items with counts and equipped state |
| `fnv_script_errors` `fnv_events` `fnv_formid_live` | read | Script errors with script/mod; companion events; a plugin's load-order prefix |
| `fnv_mem_read` | read | Memory at an address or field path (`m_pBaseProcess.m_eProcessType`), decoded with the decomp layouts |
| `fnv_console` `fnv_run_script` | act | A console line with its output; an expression or script snippet with its result |
| `fnv_mem_write` `fnv_form_edit` | act | Write memory / a form field by path, typed by the layout |
| `fnv_engine_call` | act | Call a proven engine function by name (convention from the decomp signature) |
| `fnv_setting_set` `fnv_script_set` | act | Game/INI settings; quest stages, objectives, variables, globals |
| `fnv_actor_control` `fnv_input` | act | Actor actions (combat, packages, idles, move, kill, items, actor values); bounded player input |
| `fnv_journal` `fnv_undo` `fnv_revert_all` | read/act | Every change with its old value; undo one, several, or all |
| `fnv_script_profile` | read | Calls and time per script (quest, object, effect) with the owning quest: hooks on `Script::Run` and `Script::RunScriptEffectStart/Finish/Update` |
| `fnv_script_trace` | read | The statements of one script as they run: kind (set, if, command name), block names, branches taken, failures, time; with the source, line numbers and the lines that did not run. Hook on `ScriptRunner::EvaluateLine` filtered to the script |
| `fnv_damage_log` | read | Applied damage (`Actor::DamageHealthAndFatigue`) with the hit behind it: attacker, target, weapon, body part, health/limb/fatigue/armor damage, base damage, multiplier, critical/sneak/fatal flags |
| `fnv_crash_context` | read | The newest crash-context files, named with the engine map; `enable` adds the script hooks so they name the running script. `fnv_crash_explain` attaches the matching file |
| `fnv_hang_doctor` | read | The main thread's (or every thread's) stack sampled from outside the process and named: where a hang is stuck. Works when the companion does not answer |
| `fnv_patch_map` | read | Engine code, imports and vtable slots other modules changed, by owner, with jump chains and functions patched by several owners |
| `fnv_hooks` | read | The watch hooks installed; remove one or all |
| `fnv_loadorder` | read | The dev profile's plugins in order: index, the real file behind MO2's virtual folder, masters, problems |
| `fnv_record_find` | read | Records in every active plugin by EditorID, name, form id or `Plugin.esp:LOCALID`; defining and winning plugin, override count |
| `fnv_record_show` | read | One record decoded (strings, form references with EditorIDs, script header and source, common DATA layouts), every version in load order, and what each override changed |
| `fnv_script_lint` | read | Script source checked offline: structure, block types, variables, unknown commands, argument counts, EditorIDs of the wrong type or in no plugin |
| `fnv_compile_ingame` | read | Compile with the running game's compiler: its errors with line numbers, or the variables, references, statements and bytecode |
| `fnv_script_hot_swap` | act | Replace a running script's code with a new compile; instances keep their variables; undoable |
| `fnv_scripts_compile` | control | Compile the project spec's scripts with the running game into geck-mcp-nv's compiled-script cache, so `geck_plugin_build` emits compiled scripts without GECK |

Act tools journal every change in `state/fnv/journal.jsonl` and back up the dev profile's saves before the
first change of a game session (`state/fnv/save-backups`, newest 10 kept). Console lines, scripts and
engine calls cannot be undone automatically; the journal says which entries can.

## Scenario tests

```yaml
name: hello wasteland cell and a reversible change
start: {coc: HelloWasteland}          # menu | continue | {save: name} | {coc: CellEditorID}
plugins: [HelloWasteland.esp]          # must be loaded
steps:
  - expect: {eval: "GetName (player.GetParentCell)", equals: "Hello Wasteland"}
  - set: {global: GameHour, value: 10}
  - wait: {frames: 10}
  - expect: {global: GameHour, approx: 10, tolerance: 0.2}
  - expect: {console: "player.GetAV Health", contains: "GetActorValue: Health >>"}
  - expect: {no_script_errors: true}
teardown:
  - revert_all: true
```

A run keeps the world going: the game opens its pause menu when its window loses focus, so while a test runs
the companion closes the pause menu and message boxes (`allow_menus: true` turns that off, for a test about a
menu). On a coc start the DLC pack quests show "items added" boxes for about a minute; the companion closes
them in the background, so the launch does not wait for them. Each run also turns on crash context, so a
crash during a test names the running script (`crash_context: false` turns it off).

Steps: `run` (console line), `script`, `wait` (`seconds`, `frames`, or `until` an expression with a
`timeout`), `expect`, `set` (stage, global, variable, objective), `actor`, `input`, `move`, `mem_write`,
`call`, `snapshot`, `revert_all`, `quit`. Expectations: `eval`, `item_count`, `stage`, `quest_running`,
`actor_value`, `variable`, `global`, `cell`, `console`, `disabled`, `dead`, `exists`, `mem`,
`no_script_errors`, compared with `equals`, `not_equals`, `gt`, `ge`, `lt`, `le`, `approx`, `contains`.

A run launches the game when needed, applies `start` (message boxes that appear on arrival, like the DLC
packs' "items added" boxes on a coc start, are closed), stops at the first failure (or not, with
`continue_on_failure: true`), and returns each step's actual value. A failing run adds a screenshot of
the game window. If the game dies, the report carries the crash doctor's diagnosis of the new crash log
and the game is relaunched. Reports are saved in `state/fnv/test-reports`.

Files are a YAML subset (block mappings and lists, flow `{}` and `[]`, quoted or plain scalars, `|`
blocks, comments) or JSON; no YAML library is needed. Examples: `examples/fnv-tests/`.

## Data and authoring

`fnv_record_find` and `fnv_record_show` index every active plugin into `state/fnv/records.sqlite` (record
headers and the first subrecords; a file is re-indexed when its size or time changes; the 245 MB
FalloutNV.esm plus the DLC took a few seconds). Records are found by the plugin that defines them and
their local id, so a record's versions across the load order, and what each override changed, come from
one query. Single records are decoded with geck-mcp-nv's codec.

`fnv_script_lint` uses the engine map's command tables (vanilla, xNVSE and JIP LN: about 3000 commands with
their parameters) and the record index for EditorIDs. On 1500 FalloutNV.esm scripts it reports errors in 2
(both malformed lines); what the game's compiler accepts anyway (a stray `endif`, a variable declared twice)
is a warning.

`fnv_compile_ingame` compiles with xNVSE's CompileScript in the running game and returns the compiler's own
messages (the console flags are set for that frame so it prints them). The running game keeps few EditorIDs:
base forms are replaced by their form ids before compiling, and **`Quest.variable` references cannot be
resolved** at runtime (the game keeps no quest EditorIDs), so scripts using them compile only in GECK.

`fnv_script_hot_swap` compiles the new source into a fresh Script and exchanges the compiled code (header
counts, bytecode, reference and variable lists, not the script type) with the running one in one frame.
Instances keep their variables by index, so the tool refuses when existing variables would move or vanish.
Variables the new code adds do not exist in instances that were already running. `fnv_undo` swaps back.

## Building a mod without GECK

geck-mcp-nv's builder emits a script uncompiled until its compiled-script cache (`state/scripts/<project>.json`)
has it; normally GECK fills that cache. `fnv_scripts_compile` fills it from the running game instead: with the
last build installed and loaded, it compiles each spec script in game, drops the scriptname statement xNVSE
writes before compiling (GECK's bytecode starts with a single one), maps runtime form ids to the plugin's
master-relative ids, builds `SCHR`/`SCDA`/`SCTX`/`SLSD`/`SCVR`/`SCRO`/`SCRV`, checks them with the builder's own
`verify_script`, and stores them with an in-game provenance tied to the build's compile context. The next
`geck_plugin_build` includes them. Scripts with `Quest.variable` references still need GECK. The worked
example is [examples/hello-quest](../examples/hello-quest/README.md).

Scenario expressions (`until`, `eval`, `console`, `run`) and references accept EditorIDs from any active
plugin: names the game does not keep are replaced by their form ids from the record index.

## Tested on (2026-11-06/07, FalloutNV 1.4.0.525 Steam, xNVSE 6.4.9, JIP LN 57.30, Crash Logger 5.17)

- Launch through MO2 to the main menu in 5.5 s, a save loaded with no clicks in 25 s; quit via `qqq` 3 s.
- `hello-wasteland.yaml` passes (8 steps, 3.3 s on a running game); `broken-expectation.yaml` fails with
  "global GameHour: expected equals 99, got 12.18"; `forced-crash.yaml` crashes the game through
  `TESContainer::IsGold` with a bad pointer, the report names FUN_0084e3a0+0xA called from
  `TESContainer::IsGold`, and the game is back in the test cell 8.7 s later.
- Over MCP: every read tool on the player and a placed NPC; memory write and undo; a form edit of a
  weapon's damage checked with `GetAttackDamage`; a setting change reverted; an unproven function refused;
  holding "forward" for 800 ms moved the player 262 units.
- Over MCP from a closed game: `fnv_test_run hello-wasteland.yaml` launched the game and passed in 9 s;
  `fnv_test_run_all` on the three examples took 34 s (one pass, the broken one failed with the actual
  value, the forced crash was diagnosed and the server relaunched the game in 8.5 s). The game keeps
  running after the MCP client closes.
- Discord's in-game overlay (`DiscordHook`) crashed the game at startup with JIP LN loaded, in every
  launch for a while, even with its per-game switch off. With the overlay turned off globally (Discord >
  User Settings > Game Overlay) no launch crashed. The Steam overlay and ShadowPlay were loaded without
  trouble.

Milestone 7 (2026-11-07), live in the Prospector Saloon and over MCP:

- `fnv_script_profile`: 238 scripts in 180 frames, 0.09 ms of script time per frame, top scripts named with
  their quests (`VCG01SCRIPT` / "Ain't That a Kick in the Head", `GSSunnySmilesScript`, DLC quest scripts).
- `fnv_script_trace GoodspringsRadioScript`: each frame `scriptname`, `begin OnLoad` (not taken),
  `begin OnActivate` (not taken), `begin GameMode`, `set`, `if` (not taken), `endif`... with offsets and
  times of 0.2-1 us per statement.
- `fnv_damage_log`: an NPC fight in the saloon; e.g. Easy Pete's .357 hit the player's torso for 13.04 health
  and 5.43 limb damage (base 13.04, multiplier 1.0); his dynamite hit a settler for 39.12 (base 42.12).
- `fnv_patch_map`: 424 patched places (JIP LN 166, xNVSE 86, Crash Logger 17, our watch hooks), 26 engine
  functions patched by more than one module (for example `Expression::Eval`: JIP LN and xNVSE).
- `fnv_hang_doctor`: main thread sampled 3 times, in `Renderer::PresentFrame` (vsync) each time.
- `forced-crash.yaml`: the report now carries the companion's context (main thread, read of 0x1C, the op).
- Every quit leaves a Crash Logger log on the test machine, with the companion off too: a plugin faults in
  its exit cleanup (`ExitProcess` in the call trace). The crash doctor marks such logs `duringShutdown`.

Milestone 8 (2026-11-07):

- `fnv_loadorder`: 11 plugins (FalloutNV.esm, 5 DLC masters, 4 packs, GunRunnersArsenal, HelloWasteland.esp).
- `fnv_record_find Trudy`: `GSTrudy` (NPC_), `TrudyRef`, her note, topics, packages and script, across the
  load order, 3.2 s including the first index build.
- `fnv_record_show Weap10mmPistol`: defined in FalloutNV.esm, winner GunRunnersArsenal.esm, which changes
  `DNAM`; `DATA` value 750, health 80, weight 3, damage 22, clip 12; ammo list resolved to `AmmoList10mm`.
- `fnv_script_trace GoodspringsRadioScript` mapped each statement to its source line from the plugin's
  `SCTX` and listed the lines that did not run.
- `fnv_compile_ingame`: `set undeclared to 1` fails with the game's "Unknown variable 'undeclared'", line 3.
- `fnv_script_hot_swap GoodspringsRadioScript` with a line `set Button to 42`: the radio's `Button` read 42
  while swapped and -1 again after `fnv_undo`; a version that reordered the variables was refused.

Milestone 9 (2026-11-07): Hello Quest built with `geck_plugin_build`, its script compiled in game with
`fnv_scripts_compile`, rebuilt, installed and proven by `hello-quest.yaml` (16 steps passed, twice: after the
first build and after a spec change that invalidated the cached script). Open: launching from the main menu
straight into that room takes 2-3.5 minutes (the main thread is in DirectSound buffer creation, per
`fnv_hang_doctor`); other cells take 10-15 s.

## Launch notes

- MO2 is started with ShellExecute. A detached `CreateProcess` gave the game no window focus and its
  renderer failed to start ("Failed to initialize renderer").
- The Python MCP client puts its server in a kill-on-close job object without breakaway; inside it MO2's
  hooked start of `nvse_loader.exe` fails with `ERROR_ACCESS_DENIED`. The bridge detects such a job and then
  opens a launch shortcut through Explorer instead. Claude Code's launcher (libuv) allows breakaway.
- MCP clients may pass the server a short allow-list of environment variables; MO2's `basic_games` plugin
  then fails on a missing `ProgramW6432`. The bridge adds the user's missing standard variables
  (`CreateEnvironmentBlock`) before launching, never overriding any.
- A leftover `plugin_loadcheck.tmp` (from an MO2 that was killed) makes MO2 stop at a "Plugin error"
  dialog; when MO2 is not running the bridge moves it into `state/fnv/mo2-backups`.

## Credits

- [xNVSE](https://github.com/xNVSE/NVSE): the plugin interface, console/script/array interfaces, script
  commands used by the reads, the extra data type list, and the `ActorHitData` field offsets, body part
  names and hit flags `fnv_damage_log` decodes (`GameProcess.h`, `GameForms.h`). xNVSE has no licence file;
  nothing of its code is copied, only interface declarations (`native/runtime/nvse_abi.h`, upstream
  geck-mcp-nv), offsets and names.
- [JIP LN NVSE](https://github.com/jazzisparis/JIP-LN-NVSE): `CloseActiveMenu`, and the layouts of
  `MagicTarget`, `ActiveEffect` and `EffectItem` and the control codes used by `fnv_effects` and
  `fnv_input` (offsets only, checked against the decompilation's class sizes).
- The Fallout: New Vegas decompilation and fnvsym's proof table: addresses, layouts and proven names.
