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
- **Agent tools** (after P1): what an agent needs to test and debug a mod with no person at the keyboard:
  tracing any proven engine function, watching values over time, screenshots, menus and message boxes
  pressed through the engine, test saves, time and RNG control, spawning and moving, condition evaluation
  for any record, quest markers, running script instances, leveled-list rolls and crash history.

## Renamed tools (0.5.0b1 tool merge)

Tools that did the same job became one tool with actions. The old names no longer work. The notes further down
(test logs from before the merge) use the new names.

| Old tool | Now |
|---|---|
| `fnv_crash_explain`, `fnv_crash_latest`, `fnv_crash_history` | `fnv_crash` (explain, the default), `fnv_crash latest`, `fnv_crash history` |
| `fnv_crash_context` latest / enable / disable / status | `fnv_crash` context / capture_on / capture_off / capture_status |
| `fnv_tracepoint` | `fnv_trace stack=true` (or several `when` conditions) |
| `fnv_vars` | `fnv_script_instances of=<quest or ref>` |
| `fnv_func_callers`, `fnv_func_callees` | `fnv_func action=callers`, `action=callees` |
| `fnv_time_control` | `fnv_time` with the same actions (`calendar` is the old `fnv_time`) |
| `fnv_diff` | `fnv_snapshot diff` |
| `fnv_revert_all` | `fnv_undo all=true` |
| `fnv_launch`, `fnv_quit`, `fnv_restart` | `fnv_game launch`, `quit`, `restart` |
| `fnv_ui_tiles` | `fnv_menu tiles` (`fnv_menu` alone is `state`) |
| `fnv_menu_control` press / close / close_all | `fnv_ui_do` message / close / close_all |
| `fnv_spawn` | `fnv_place at export=false` (with `count`, `distance`, `direction`) |
| `fnv_test_run`, `fnv_test_run_all`, `fnv_do` | `fnv_test` (run, the default), `fnv_test run_all`, `fnv_test steps` |
| `fnv_func_find`, `fnv_func_code`, `fnv_string_xrefs` | `fnv_func` (find, the default), `action=code`, `action=strings` |
| `fnv_class_layout`, `fnv_class_tree`, `fnv_class_vtable` | `fnv_class` (layout, the default), `action=tree`, `action=vtable` |
| `fnv_record_show`, `fnv_record_find`, `fnv_record_users` | `fnv_record` (show, the default), `action=find`, `action=users` |
| `fnv_quest_targets` | `fnv_quest action=targets` (`fnv_quest` alone is `state`) |
| `fnv_dialogue_tree` | `fnv_dialogue action=tree` (`fnv_dialogue` alone is `now`) |
| `fnv_hang_doctor` | `fnv_threads action=hang` (`fnv_threads` alone is `list`) |
| `fnv_heap` | `fnv_memory action=address_space` (`fnv_memory` alone is `pools`) |
| `fnv_frame_profile` | `fnv_profile action=frame` (`fnv_profile` alone is `sample`) |
| `geck_preview_open`, `geck_preview_close` | `geck_preview action=open`, `action=close` |

A merged tool refuses a parameter its chosen action does not use (`INVALID_ARGUMENT`), so a call with a leftover
argument from another action is not silently half-done.

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
A watch hook without an action only observes: every call goes on unchanged. The stub code never touches the x87
stack (a float result in st0 survives) and keeps the thread's LastError. One exception: if a hook's return finds its
shadow stack inconsistent (a frame it pushed is gone), there is no safe address to return to, and the companion ends
the game on purpose (exit code `0xF4C0DE01`) instead of jumping somewhere wrong. Unhooking restores the original bytes if the jump is
still ours; stub memory stays allocated so calls already inside finish normally.

The companion also installs a vectored exception handler that only observes: at a hardware fault it writes
`%LOCALAPPDATA%\fnvmcp\crash-context-<pid>-<n>.json` (the hooked calls active on the faulting thread with
their `this`/argument form ids, the op being served, the console line being run, recent events). A fault at an
address that faulted before was survived (the game recovered), so it gets no new file. The first 7 fault
addresses get numbered files; each later new one overwrites `crash-context-<pid>-latest.json`, so a real crash
late in a session always leaves its context. It writes nothing once xNVSE has announced the game is exiting.
What the companion cannot say over its pipe (the pipe could not be created) goes to
`%LOCALAPPDATA%\fnvmcp\companion.log`.

### Mass probes and the action atlas

Watch hooks are rich but installed one at a time; to answer "which of the 43,000 unnamed functions runs when
the player does X", the companion installs thousands of lightweight call counters at once. The planner
(`fnv_probe.py`, offline, capstone against the clean exe) sorts each target into an **entry** probe (a
5-byte jump over whole prologue instructions, when the stolen bytes fit the function, have ≤3 relocs and no
branch in the body lands inside them) or, when the function is too small or branches into its own first five
bytes, **site** probes: the rel32 of every direct call or jump to it and the vtable slots that hold it. A
stub increments a counter and, one call in 2ⁿ, records a sample (this/edx/caller/stack/vtables; the exit
reads eax and detects an st0 float with `fxam`/`fnstsw`). Arming suspends the other threads (Toolhelp
snapshot) and refuses while any thread's EIP is inside the bytes being moved. Counters are read in bulk with
`ReadProcessMemory`, no pipe traffic per function.

The atlas (`fnv_atlas.py`) turns counts into attribution by dose and response: it reads the counters, runs a
scripted action a set number of times (0, 1, 2, 4), reads again, and least-squares fits each function's
delta against the dose. A clean slope through the origin is `per_unit` (n calls per action); a constant rise
only when the action happens at all is `with_action`; noise is `background`. Functions whose counts track a
`console_noop` dose are marked `driver` (console machinery, not the action). Runs, segments, counts and fits
are kept in `state/atlas.sqlite`, and an existing Frida `fnvsym-trace/1` capture can be imported as a run.

## Setup

The full guide, from an empty machine, is [FNV-MCP-SETUP.md](FNV-MCP-SETUP.md); `fnv_setup_check` says what is
missing. In short:

1. A portable MO2 instance with a dev profile, xNVSE 6.4.9+ and JIP LN NVSE (`tools\fnv_setup_dev_profile.py`
   creates it; see the setup guide). In the profile's `Fallout.ini`: `bAlwaysActive=1` under `[General]`, so the
   game keeps running unfocused.
2. In the project file's `fnv` block:

   ```json
   "fnv": {
     "mo2_root": "C:/Modding/FNV-Dev", "mo2_profile": "Dev",
     "zig": "C:/tools/zig-x86_64-windows-0.14.1/zig.exe",
     "engine_control": "full",
     "tests_dir": "../state/tests"
   }
   ```

   `engine_control` is `"read"` by default: the act tools then refuse. Set `"full"` to allow changes.
   Since 2026-11-12 (PLAN-3 S1.5, bug T1) "act" covers every action that changes the running game or leaves
   something installed in it, listed in `MUTATING_ACTIONS` in `geck_mcp/fnv_live_tools.py`. With `"read"` these
   now refuse too (they used to run as read or control):
   - `fnv_hooks remove`; `fnv_trace`, `fnv_trace stack=true`, `fnv_script_profile`, `fnv_script_trace`, `fnv_damage_log`
     start and stop, and read for the three that install their hook on the first read (`fnv_script_profile`,
     `fnv_script_trace`, `fnv_damage_log`); `fnv_profile action=frame`; `fnv_crash` capture_on and capture_off;
     `fnv_memory` alloc_trace and refcount;
     `fnv_events` subscribe and unsubscribe; `fnv_watch frames=`; `fnv_systems` formula, sweep and barter;
     `fnv_perk_entries` start, stop and evaluate; `fnv_leveled_roll seed=`. These tools are now annotated as act
     (not read-only). (barter and the perk recording install the perk hooks: round-4 audit Y2.)
   - `fnv_engine_get` stays read but runs only a getter the purity check proves stores to the stack alone (its body
     and callees, on the clean exe), always through the guarded call; anything else answers `MAY_WRITE` and goes to
     `fnv_engine_call` under `"full"` (round-4 Y1). It needs `fnv.clean_exe` and capstone (the `naming` extra:
     `pip install "capstone==5.0.7"`); without them it answers `CLEAN_EXE_MISSING` or `CAPSTONE_MISSING`.
   - `fnv_probe` install, remove, clear, config and harvest; `fnv_name_oracle` run, calibrate and sweep;
     `fnv_name_test` test and battery; `fnv_field_watch`; `fnv_atlas` run and import_frida.
   - The scenario runners, whose steps change the game: `fnv_test`, `fnv_test run_all`, `fnv_test steps`,
     `fnv_session` start and end, `fnv_crawl`, `fnv_bisect`, `fnv_replay` record, stop and play.

   Reads in the same tools (list, read, status, report) still work with `"read"`. `fnv_game launch`, `fnv_game quit` and
   the installs stay control.
   For `fnv_patch_map`, add `"clean_exe"`: your own unpacked FalloutNV.exe 1.4.0.525 (the Steam exe keeps its
   code encrypted on disk; Steamless unpacks it). It is read locally and never copied.
3. With the game closed, `fnv_companion_install` builds the DLL with Zig and installs it as the MO2 mod
   `fnvmcp engine companion` (it never writes into the game folder).
4. `fnv_game launch start=continue` starts the game and loads the newest dev-profile save.

## Tools

Every answer is an envelope: `ok`, `outcome`, `data`, `error`. For an `fnv_*` call that worked, `outcome` is
`acknowledged` when a control or act tool made its change (the game or the companion confirmed it), and
`unchanged` for a read, or when the change was already in place. A failure has `ok: false`, an `error.code` and an
`error.recovery` sentence.

| Tool | Kind | What it does |
| --- | --- | --- |
| `fnv_status` | read | Running? Phase (main_menu, loading, in_game, in_menu, paused_menu), cell, loaded plugins, companion ping |
| `fnv_game launch` / `fnv_game quit` / `fnv_game restart` | control | Start at `menu`, `continue`, `save:<name>` or `coc:<cell>`; quit via `qqq`, then window close, then terminate |
| `fnv_install` | control | A built plugin into the dev profile as its own MO2 mod, enabled (game closed) |
| `fnv_companion_install` | control | Build and install the engine companion |
| `fnv_logs` | read | New lines of nvse.log, the console log, CrashLogger.log, JIP and MO2 logs |
| `fnv_test` / `fnv_test run_all` | control | Run YAML scenarios (below) |
| `fnv_snapshot` / `fnv_snapshot diff` | act | Capture chosen state and compare |
| `fnv_player` `fnv_ref` `fnv_refs_near` `fnv_crosshair` | read | Player and references: base, position, cell, flags, runtime class (RTTI), extra data |
| `fnv_actor_ai` `fnv_actor_values` `fnv_effects` | read | AI package and procedure, process level, combat, detection; actor values with their modifiers and the effects behind them; active effects with source items |
| `fnv_quest` `fnv_script_instances of=` | read | Stage list with done flags, objectives with text and state, script variables by name |
| `fnv_extra_data` `fnv_inventory` | read | A reference's extra data decoded; items with counts and equipped state |
| `fnv_script_errors` `fnv_events` `fnv_formid_live` | read | Script errors with script/mod; companion events; a plugin's load-order prefix |
| `fnv_mem_read` | read | Memory at an address or field path (`m_pBaseProcess.m_eProcessType`), decoded with the decomp layouts |
| `fnv_console` `fnv_run_script` | act | A console line with its output; an expression or script snippet with its result |
| `fnv_mem_write` `fnv_form_edit` | act | Write memory / a form field by path, typed by the layout |
| `fnv_engine_call` | act | Call a proven engine function by name (convention from the decomp signature) |
| `fnv_setting_set` `fnv_script_set` | act | Game/INI settings; quest stages, objectives, variables, globals |
| `fnv_actor_control` `fnv_input` | act | Actor actions (combat, packages, idles, move, kill, items, actor values); bounded player input |
| `fnv_journal` `fnv_undo` `fnv_undo all=true` | read/act | Every change with its old value; undo one, several, or all |
| `fnv_script_profile` | read | Calls and time per script (quest, object, effect) with the owning quest: hooks on `Script::Run` and `Script::RunScriptEffectStart/Finish/Update` |
| `fnv_script_trace` | read | The statements of one script as they run: kind (set, if, command name), block names, branches taken, failures, time; with the source, line numbers and the lines that did not run. Hook on `ScriptRunner::EvaluateLine` filtered to the script |
| `fnv_damage_log` | read | Applied damage (`Actor::DamageHealthAndFatigue`) with the hit behind it: attacker, target, weapon, body part, health/limb/fatigue/armor damage, base damage, multiplier, critical/sneak/fatal flags |
| `fnv_crash context` | read | The newest crash-context files, named with the engine map; `enable` adds the script hooks so they name the running script. `fnv_crash` attaches the matching file |
| `fnv_threads action=hang` | read | The main thread's (or every thread's) stack sampled from outside the process and named: where a hang is stuck. Works when the companion does not answer |
| `fnv_patch_map` | read | Engine code, imports and vtable slots other modules changed, by owner, with jump chains and functions patched by several owners |
| `fnv_hooks` | read | The watch hooks installed; remove one or all |
| `fnv_loadorder` | read | The dev profile's plugins in order: index, the real file behind MO2's virtual folder, masters, problems |
| `fnv_record action=find` | read | Records in every active plugin by EditorID, name, form id or `Plugin.esp:LOCALID`; defining and winning plugin, override count |
| `fnv_record action=users` | read | Everything that references a record: placed refs, scripts (SCRO), leveled lists, containers, conditions (typed by the game's condition-function table), packages, form lists, any form field; whether the winning version still does |
| `fnv_conflicts` | read | Records several active plugins edit, worst first: plugins in load order, winner, subrecords each override changes, earlier changes the winner loses |
| `fnv_asset_check` | read | Meshes, textures, icons and sounds a plugin names that no enabled mod, the Data folder or any BSA has; textures inside its loose meshes too |
| `fnv_profile_set` | control | Enable or disable MO2 mods and plugins in the dev profile (game closed), with a backup of the three profile files |
| `fnv_record` | read | One record decoded (strings, form references with EditorIDs, script header and source, common DATA layouts), every version in load order, and what each override changed |
| `fnv_script_lint` | read | Script source checked offline: structure, block types, variables, unknown commands, argument counts, EditorIDs of the wrong type or in no plugin |
| `fnv_compile_ingame` | read | Compile with the running game's compiler: its errors with line numbers, or the variables, references, statements and bytecode |
| `fnv_script_hot_swap` | act | Replace a running script's code with a new compile; instances keep their variables; undoable |
| `fnv_scripts_compile` | control | Compile the project spec's scripts with the running game into geck-mcp-nv's compiled-script cache, so `geck_plugin_build` emits compiled scripts without GECK |
| `fnv_dialogue action=tree` | read | The responses a speaker could give for a topic (GREETING by default): prompt, quest and priority, flags, response text, every condition with its value, the engine's verdict, and the response it would pick now |
| `fnv_dialogue_choose` | act | Talk to an NPC and pick responses by index, prompt text or info id; each step's options, the pick and the answer |
| `fnv_ai_packages` | read | An actor's packages in order: type, flags, schedule against the game clock, location, conditions with values; which one runs, which are eligible or shadowed, and why the others are not |
| `fnv_form_live_diff` | read | A form in memory versus its winning plugin record (name, value, weight, health, damage, script, global value, quest data): what changed at runtime |
| `fnv_trace` | read | Calls to any proven engine function while the game runs: `this` and arguments decoded with the decomp signature (forms named), result, time, caller by name; counts by caller and `this`; filter by `this`, caller or a parameter |
| `fnv_watch` | act | Values sampled over time (expressions, globals, actor values, positions, memory fields by class layout): first, last, min, max and a timeline of changes with frames; stops early on `until` |
| `fnv_screenshot` | read | The current frame as PNG, returned as an image: the engine's own back-buffer capture (`SaveScreenshot`, works with the window covered), else a window capture |
| `fnv_test_save` | act | Named test saves (`fnvmcp_test_*`) in the dev profile: save, load (waits for the game's post-load message), list, delete |
| `fnv_crash history` | read | Every crash log explained and grouped by signature: count, first/last time, likely culprits, new signatures flagged, crashes during exit cleanup marked |
| `fnv_move` | act | Teleport the player or a ref to a ref, a cell (coc, or JIP `MoveToCell` with a position) or a position; waits for the cell to load; undoable |
| `fnv_place at export=false` | act | Place a base form near a ref (`PlaceAtMe`) and return the new refs; undo disables and deletes them |
| `fnv_condition_eval` | read | Every condition list of a record (INFO, PACK, PERK and its entry tabs, SPEL/ENCH/ALCH/INGR effects, QUST quest, stage log entries and objective targets, RCPE, IDLE, MESG buttons, CPTH) evaluated against chosen refs, with what fails |
| `fnv_quest action=targets` | read | Each objective's targets: ref, cell, position, target conditions, the door a marker points at, the player's target cache, and every reason a marker is not shown |
| `fnv_script_instances` | read | Running quest scripts and the object scripts of every loaded cell (the lists `TES::RunScripts` walks), variables, instance counts per script, and whether scripts run at all |
| `fnv_menu` | read | Interface mode, active menu, menu stack, visible menus by name, loading screen, and an open message box with title, text and buttons |
| `fnv_ui_do` | act | Press a message box button by index or label through the engine's click handler (`GetButtonPressed` sees it), close the open menu, close all menus |
| `fnv_rng` | act | The game's one Mersenne Twister: state, or seed it like `MersenneTwister::Seed` |
| `fnv_time` | act | Read the calendar, jump to an hour, advance hours (day/month/year roll over), change the timescale; `engine_wait` runs the game's own one-hour wait step |
| `fnv_leveled_roll` | read | Roll a leveled list many times with `TESLeveledList::CalculateCurrentForm` at a level: share of each form and count, and of nothing; repeatable with a seed |
| `fnv_detection` | read | Why an NPC sees someone or not: the detection records on its process (lost/unseen/noticed/seen, score, line of sight in the view cone), both directions, light, sneak, distance, settings; for NPC observers the game's `CalculateDetectionLevel` recomputes and the 25 inputs of its detection formula are captured |
| `fnv_reaction` | read | How two actors relate: hostile or not (the engine's `IsHostileTo`, behind `GetShouldAttack`), fight reaction, the faction pair that decides it, disposition, crime anger, teammates, aggression, and the rule branch that decided |
| `fnv_combat` | read | An actor's combat controller: target, procedure, queued procedure, action, goal, weapon, combat style, and the combat group's targets (last known position, attackers) and members |
| `fnv_perk_entries` | act (read for action=read) | Record perk entry-point checks (value before, each perk entry that applied, value after), or `evaluate` one value entry point now with a value and its filter forms |
| `fnv_pathing` | read | An actor's path: mover state, the pathing request (start, goal, navmesh, door rules), the solution's points, the path handler's state (waiting, blocked, avoidance failed) and the reasons found |
| `fnv_raycast` | read | A ray through the game's Havok world (`TES::Pick`) between points or refs: hit, hit point, hit object and its reference; `ground_at` gives the terrain height |
| `fnv_cell_grid` | read | Loaded exterior grid, current interior, interior and exterior cell buffers: each cell with its grid coordinates, load state and why it is loaded |
| `fnv_hook_set` | act | Change an engine function live: skip it with a result (float results in st0), replace its result, change one argument, or run a script line on each call; filter, hit limit, read the changed calls, journaled |
| `fnv_patch` | act | Bytes, NOPs, a ret or a jmp/call into the game's code; refuses over companion hooks and foreign patches (clean exe compare) unless forced; journaled with the original bytes |
| `fnv_trace stack=true` | read | Record a function's calls that meet conditions, with arguments as objects, result, time, caller and the call stack; groups hits by path |
| `fnv_engine_get` | read | Call a proven getter (Get/Is/Has/Can/...) whose machine code and callees write no memory but their own stack (fnv_oracle.Purity on the clean exe), always guarded, with typed arguments; pointer results described |
| `fnv_globals` | read | Global variables from the data handler's list, filtered; set values (journaled) |
| `fnv_dialogue` | read | The conversation on screen: speaker, state, spoken topic and info, the line and name as shown, the choices in order with topic, info, quest and heard flag |
| `fnv_memory action=address_space` | read | The game's address space (2 GB unless large address aware, largest free block), memory pools by use, form and process-level actor counts; growth over N seconds |
| `fnv_world_edit` | act | Move, rotate, scale, enable, disable, delete or duplicate a ref with the functions the console commands use; force or release weather; trigger or stop an image-space modifier; undoable |
| `fnv_scene_graph` | read | A loaded ref's node tree: classes (MSVC RTTI or Gamebryo NiRTTI), names, hidden flag, transforms, geometry counts, skinning, shader and texture paths, controllers and sequences, extra data |
| `fnv_menu tiles` | read | A menu's live tile tree with trait values named by the game's trait table |
| `fnv_vats_odds` | read | VATS hit chance per target and limb from CombatFormulas::GetVATSChanceToHit and the ep 8 perk layer, the percent the menu shows, and the gun formula term by term |
| `fnv_profile action=frame` | read | Time per frame by subsystem from timing hooks on the main loop's children, removed afterwards |
| `fnv_window` | control (act for release_mouse, capture_mouse, unpause) | Free the user's mouse while the game runs (DirectInput non-exclusive background mode on the game window), capture it again, close the pause menu; persist writes `bBackground Mouse=1` to the dev profile's Fallout.ini (checked: launch starts with it, live toggles return S_OK) |
| `fnv_probe` | act | Count calls of thousands of unnamed functions at once. The probe kind is chosen offline per function against the clean exe (entry jump when the prologue is whole instructions, ≤ the function, ≤3 relocs and nothing branches into the moved bytes; otherwise the rel32 of direct calls and the vtable slots that hold it; otherwise none): `plan` classifies a selection, `install` arms with other threads suspended (none mid-instruction), `read` returns counts and decoded samples (this/edx/caller/stack/vtables, result int or st0 float) with a per-caller table, `remove` disarms and restores the bytes. Journaled; a function that crashed the game while probed is quarantined |
| `fnv_atlas` | act | Dose and response over the probe set: run a scripted game action 0/1/2/4 times, read the counters before and after each segment, least-squares fit the counts to the dose to classify each function `per_unit` (runs n× per action), `with_action` (once per action) or `background`; functions that scale with `console_noop` are marked `driver`. Stored in `state/atlas.sqlite`; `import_frida` loads an existing `fnvsym-trace/1` run |

| `fnv_name_dossier` | read | One function's case for naming: engine-map status, static callers/callees, the vtable slots it fills (folded across unrelated classes?), strings and settings, fnvsym claims and rows; from harvested probe sets: calls, atlas fits, runtime callers with the call resolved (direct; vtable class and slot, also through `mov reg,[vt+disp]` / `call reg`; callback), `this` classes and their common base, form types, the result's shape masked to the declared width, threads, caller chains; suggested tests |
| `fnv_name_oracle` | act | Does it compute a value the game already knows? Results from samples, or from calls on ~760 objects (references, base forms, quests, cells, worldspaces, packages) when a disassembly check proves the code writes nothing but its stack frame; compared with script commands, base-form and form commands, and form header fields; match = ≥ 20 objects, no mismatch, exact chance of a random match ≤ 1e-6, shuffled control; `calibrate` runs it blind on proven getters, `sweep` on every not-proven pure getter |
| `fnv_name_test` | act | Experiments: skip / force the result of / change an argument of a function during an action, with a control in every repetition (≥ 3); skips pop what the function's own `ret n` pops; `battery` re-identifies functions blind from the console effects their skip blocks |
| `fnv_field_watch` | act | Hardware watchpoints (DR0..DR3, main thread or all threads): `find` lists the fields of an object (and one level into its pointers) a console action changed, named from the layouts; `watch` names each function that wrote or read a field, with the instruction, values and callers |
| `fnv_warnings` | control (act for capture start/stop) | The engine's own warnings (`MASTERFILE:`, `SCRIPTS:`, `MODELS:` lines, Havok messages) from xNVSE's `falloutnv_error.log` and `falloutnv_havok.log`. Actions `status`, `enable`, `read`, `summary`, `clear` (see below) |
| `fnv_guide` | read | Which tool for which problem: `list`, `topic` (a step by step procedure), `find` (search tool names and descriptions), `engine` (saved research notes and recorded facts). Needs no game (see below) |
| `fnv_name_export` | control | Everything recorded, as `fnvsym-trace/2` into `fnvsym/traces`, with each method's measured calibration, the layout squeeze (owner class when the observed-class bound and the field-declaration bound meet) and re-check flags for proven names the runtime contradicts; fnvsym's `namer.runtime2` turns it into evidence. With nothing recorded it writes no file (`changed: false`, `exported: 0`). A `path` must be a `.json` under `fnvsym/traces` or `<state>/traces`, and an existing file is replaced only when it is a trace (else `BAD_PATH`) |

`fnv_probe` also takes `int3` (a one-byte trap probe for functions with no entry jump and no call site, so every
not-proven function is probeable; `int3_budget` calls and it removes itself) and `harvest` (copy counts, samples and
caller tables into `state/atlas.sqlite`).

Act tools journal every change in `state/fnv/journal.jsonl` and back up the dev profile's saves before the
first change of a game session (`state/fnv/save-backups`, newest 10 kept). Console lines, scripts and
engine calls cannot be undone automatically; the journal says which entries can.

## Warnings and guides: fnv_warnings and fnv_guide (Plan 2, first wave)

The conventions these two tools follow (and every later action-style tool) are in
[FNV-TOOL-CONVENTIONS.md](FNV-TOOL-CONVENTIONS.md): an `action` argument, the verdict first, paged lists, next steps.

**`fnv_warnings`.** The engine calls its warning function from about 845 places, but in the retail game the
function is empty. xNVSE hooks it when `[Logging] EnableGameErrorLog=1` is set in `Data/NVSE/nvse_config.ini`.
It then writes each warning as one line (the formatted text, no time, no caller) to `falloutnv_error.log`,
and Havok messages to `falloutnv_havok.log`, both in the game folder. The game rewrites both files at each start
unless `bAppendErrorLogs=1`.

| Action | What it does |
| --- | --- |
| `status` | Is the setting on, which config file wins, the two files with size and time, the mark, and what to do next |
| `enable` | Sets `EnableGameErrorLog=1` in the config file the game reads and keeps every other line. It takes effect at the next game start (a control action: it changes a file). With MO2 it edits the winning `mods/<mod>/NVSE/nvse_config.ini` or `overwrite/NVSE/nvse_config.ini` (a copy is saved in `state/warnings-backups`). When only the real `Data/NVSE` file exists, it copies that file into `overwrite/NVSE` and adds the line there, so the real game folder is not touched. The result names the file written |
| `read` | Lines since the mark (`since`: `mark`, `start`, or a line number). Filters `category` (`MASTERFILE`, `SCRIPTS`, `MODELS`, `TEXTURES`, `ANIMATION`, `AI`, `Audio`, `other`), `text`, `plugin`, `source` (`engine`, `havok`). Paged. Each row has `seq` (its line number in its file), `source`, `category`, `text`, `plugins` |
| `summary` | Counts per category and per plugin, and the most repeated messages (hex ids and numbers are masked, so lines that differ only in those group together) |
| `clear` | The game holds the files open, so they are not emptied: the current end of each file becomes the mark and `read` starts there. A mark is dropped when the game rewrites the file |

The verdict reads, for example, "37 engine warnings since the mark, 31 of them MASTERFILE". `fnv_logs` can also
return the two files (sources `error` and `havok`). The engine writes through a buffer, so the last lines before a
crash can be missing.

**Live capture (`capture mode=start|stop|status`).** While the game runs it keeps both files open with no read sharing,
so nothing can read them (seen live: `Permission denied`). `capture` hooks the engine's `Warning` 0x005B5E40 itself,
chained behind xNVSE's jump: every call (any thread) records its eight stack words, the caller and the thread, and the
bytes behind word 1 are copied at the call (usually the first `%s`, which often sits in a reused buffer). The bridge
formats the printf text with the format string read from the game. `read` and `summary` use the capture while the files
are locked; rows carry `caller` and `thread`. `fnv_session start` turns it on. Checked live: two real ANIMATION
warnings (a SpecialIdle .kf) and a direct call with a freed string argument came out right. The Havok log can only be
read after the game quits.

**`fnv_guide`.** Guides are Markdown files in `geck_mcp/guides/` (`crash`, `script`), also served as the MCP
resource `fnv://guide/{name}`. `engine` searches the Markdown files of `fnv.research_dir` (or the environment
variable `FNV_RESEARCH_DIR`) by heading section, and the facts in `fnv_facts.py`; with no folder set it searches
the shipped guides instead and points to `fnv_func` for the decomp. A test fails when a guide names
an `fnv_*` or `geck_*` tool that does not exist.

## Game time: fnv_time and fnv_wait_for (Plan 2, W1)

Addresses come from `D-time.md` section 1. Each timer function is looked up in the engine map by address and must
have the expected signature; otherwise the tool refuses with `ENGINE_MAP_MISMATCH` and calls nothing. Frame waits
and predicates run inside the game in one request (`pred set`, `until`, `pred clear`), so the game does not run
between the base frame and the wait.

**`fnv_time`** (kind `act`; `status` is a read). Actions:

| Action | What it does |
| --- | --- |
| `pause` / `resume` | Disables and enables the game clock (`BSTimer` Disable/Enable through the `call` op). The tool keeps its own flag and resumes only its own pause. A focus loss or another user's hold is left alone |
| `hold` / `release` | Closes and opens the frame gate. While closed, nothing runs between frames; requests are still served. The gate reopens by itself after `timeout_seconds` (default 120) with no calls |
| `step frames=N [fps=F]` | Advances exactly N frames and holds the game there. With `fps`, the fixed frame step is set first (`ClampAt`) |
| `fixed fps=F` | Every frame takes 1000/F ms of game time. `fps=0` turns it off |
| `speed x=F` | Writes both time multiplier floats (0.05 to 10), so the change does not ramp |
| `status` | Gate state, clock counter, fixed step, multiplier, frame, and the real frame rate from two readings |

Changing actions need `fnv.engine_control` set to `full`. Without a running game the tool answers `GAME_NOT_RUNNING`
with the next step (`fnv_game launch`).

**`fnv_wait_for`** (kind `read`). Exactly one condition:

| Argument | Meaning |
| --- | --- |
| `frames=N` | N frames pass, counted from the frame at the start of the wait |
| `seconds=S`, `clock=game` or `real` | S game seconds (divided by the time multiplier) or S wall seconds. The rate is the fixed step, else measured, else 60 assumed (when the gate is held) |
| `predicate=TEXT` | Compiled predicate (see below), waited on for up to `max_frames` |
| `loaded=true` | The game has finished loading: loading flag and menu mode clear, frame delta above 0, two frames run, and the player has a parent cell. Checked in rounds until `timeout` |

`timeout` is in real seconds. The wait runs frames whether the gate is held or open; when it was open before, it is
opened again at the stop. The verdict says the frame reached, the frames waited and whether the gate was held.

**Predicate text** (`fnv_pred.py`). Numbers (`12`, `0x1F`, `1.5`); absolute memory `[0x011F6394+0x0C]:f32`; pointer
chains `[[0x011DEA0C]+0x07]:u8` (each `[...]` is one read); hook bases `this.[..]`, `argN.[..]`, `reg.eax.[..]`;
`frame`, `main`; comparisons `== != < <= > >=`; `and or not`; `+ - & >> <<`; parentheses. Types: `u8 i8 u16 i16 u32
i32 f32`. Limits: 64 operations, stack depth 16. Errors name the column (`BAD_PREDICATE`). Field paths through the
symbol database are not supported yet.

## Frame-exact control: fnv_input frames, fnv_test steps, fnv_session, fnv_health (Plan 2, W1)

As of Plan 2 W2 the bridge registers 178 tools: 44 geck tools (unchanged) and 134 `fnv_*` tools. This wave adds `fnv_test steps`,
`fnv_session` and `fnv_health`.

**Input with frames.** `fnv_input` takes `frames=N` (1 to 10000) for a press that lasts exactly N frames. Controls 0
to 12 go through the engine's own force call. Other controls go to their bound key, or to their mouse button
(256 + button for a mouse-only binding). Keys are DirectInput scan codes (0 to 255), buttons are 0 to 7, and a mouse
move is `[dx, dy]` per frame (signed; sent as two's complement). The injector is installed once per game process with
`inpcfg`. Before that, the poll and force functions are checked against the engine map. A prologue with a relative
jump or call is refused (`PROLOGUE_RELOCATION`), because `inpcfg` takes no relocation list. Each job ends by itself
after its frames. The call waits for them unless it is told not to. Needs `fnv.engine_control` set to `full`.

**fnv_test steps** (control). Runs steps given inline, with the same keys as a YAML scenario. Arguments: `steps` (1 to 200),
`name`, `start` (`menu`, `continue`, `save:<name>` or `coc:<cell>`), `plugins`, `setup`, `teardown`, `deterministic`,
`repeat` (1 to 20), `timeout_seconds` (20 to 900) and `relaunch_on_crash`. It returns the scenario report.

Scenario additions:

| Key | Example | What it does |
| --- | --- | --- |
| `step` | `step: {frames: 30, fps: 60}` | Advances exactly 30 frames and holds the game there. `fps` sets the fixed frame step first |
| `wait` for `loaded` | `wait: {for: loaded, timeout: 60}` | The game has finished loading (the same check as `fnv_wait_for loaded`) |
| `wait` for an event | `wait: {for: script_error, timeout: 10}` | A bare lowercase word is an event type from the event ring. Only events that arrive during the wait count |
| `wait` for a predicate | `wait: {for: "frame >= 200", timeout: 30}` | Runs frames until the predicate is true (see Predicate text below) |
| `input` with frames | `input: {control: 3, frames: 12}`, `{key: 30, frames: 5}`, `{button: 0, frames: 10}`, `{mouse: [-4, 0], frames: 20}` | A frame-exact press. `action` defaults to `tap` |
| `as` | `- as: door` on a step, then `$door` in a later step | Keeps the step's result; `$name` is replaced in later steps |
| `deterministic: true` | top-level key | The frame step is fixed at 60 fps and the random generator is seeded with 1 for the run. Both are reset after |
| `repeat: N` | top-level key | Runs the scenario N times, each from its `start`. The comparison lists the steps whose result (passed and actual value) differs |
| `on_crash` | `on_crash: report` | The default. `hold` is `NOT_AVAILABLE_YET` |
| `photo`, `play`, `ui`, `break`, `flow` | any step with one of these keys | `NOT_AVAILABLE_YET`: planned for later waves, refused at load |

**fnv_session** (control). Actions:

| Action | What it does |
| --- | --- |
| `start` | Checks the setup (`SETUP_MISSING` names what is missing). Launches the game or loads `start`, as a scenario does. Turns the warning log on (a change takes effect at the next game start, and the result says so). Clears the warning log to a mark. Turns the crash context on. With `deterministic`, fixes the frame step and seeds the generator. Records the baseline frame rate and NiRefObject count. Refused when a session is open (`SESSION_OPEN`) |
| `mark` | A named point (`label`). Health reports from here on, and the warning log is cleared to here |
| `end` | Runs the health check, reverts this session's journal entries, releases the frame control, turns the crash context off, and quits the game when `quit_game` is true. Returns the health report |
| `status` | Whether a session is open, since when, and the last mark |

**fnv_health** (read). Needs an open session. Returns `ok` and `items`; each item has `kind`, `what` and `next` (the
tool to call next):

| Kind | Reported when |
| --- | --- |
| `script_errors` | Script errors since the mark, from the event ring |
| `warnings` | Engine warnings since the mark, by category |
| `crash` | A crash log since the mark (marked as during shutdown when the game was exiting) |
| `crash_context` | Crash-context files newer than the mark |
| `frame_time` | The frame time is more than 1.5 times the session start's |
| `objects` | The NiRefObject count is more than 1000 above the session start's |

Notes: the NiRefObject count (0x011F4400) and the OSInputGlobals pointer (0x011F35CC) are statics from the decomp
units. The engine map does not list them yet, so the results say so. The companion's `inp` reply is assumed to carry
`ok`, `job`, `frame`, `jobs` and `installed`. The C side of `inp` is finished in another wave, so the live game has not
run these tools yet.

---

## Source-level debugger: fnv_debug (Plan 2, W2)

**`fnv_debug`** (kind `act`; `status`, `stack` and `locals` are reads, and so are `break` with `op=list`). It sets
breaks in the running game and reads what the game stopped on, with names and source lines from the engine map and
the symbol database. Every stop is parked by the engine companion (`stops`, `resume`); the game runs on only when
the debugger resumes it.

**Breaks** (`action=break`):

| Form | What it does |
| --- | --- |
| `op=set location=<name>` | A function by name (`Actor::ApplyDamage`) or by address (`0x00401000`). At the function's first byte it is a hook hold (`hookact hold`), so the thread parks before the body runs |
| `op=set location=<file>:<line>` | The first address of that source line (symbol database, by path suffix). A trap: a hardware breakpoint for the first four, `int3` after that |
| `op=set location=fault` | The companion's crash hold: a real fault parks the faulting thread (`faulthold on`) |
| `when="<predicate>"` | Stops only when the predicate holds (`fnv_pred` text, such as `frame >= 100`) |
| `do=log log=["<expr>"]` | Does not stop; records one expression at each hit (the companion's log ring) |
| `op=list` / `op=clear break_id=N` | Lists breaks with their mechanism and hits; clears one, or all when `break_id` is left out |

`hits=N` stops from the Nth hit that passes `when` (companion `bp after`; a function break then uses a trap
breakpoint, not a hook hold). Several expressions in `log` are not built yet and are refused with `NOT_YET`.

**Stops and reads** (all need a stopped game unless noted):

| Action | What it returns |
| --- | --- |
| `status` | Running, or stopped with the function, `file:line`, the source lines around it (current one marked), why it stopped and the thread. Also the breaks while running |
| `stack depth=N with_locals=true` | Frames from the EBP chain. Frame 0 is the stop; callers show the call site (`file:line` from the return address) |
| `locals frame=N` | Parameters and locals in scope at that address, decoded by type size. Pointers show their address and the class named by RTTI when the vtable is known. A value that cannot be read gives a note |
| `eval expression=...` | Reads a value: a local, `this->field` or `obj.field` through the class layout, `[0x011DEA0C]:u32`, a register as `$eax`. Casts and calls are refused (`CALL_UNSUPPORTED`) |
| `set expression=... value=...` | Writes a local, a field or a register of the stop. Takes effect when the game resumes |
| `threads` | Every thread of the game with its eip, whether it is the main thread, and whether it is parked at a stop |

A hook stop has no frame of its own yet: its parameters come from the saved stack arguments, and its locals that
live in the frame show `not set up` until the function's prologue has run.

**Running on** (`action=...`):

| Action | What it does |
| --- | --- |
| `continue` | Resumes every stop (`resume ff run`) |
| `step mode=instruction` / `into` | One instruction (`resume step`) |
| `step mode=over` | Runs a call at the current line as one step (a temporary `to` after the call) |
| `step mode=out` | Runs to the return address of the current function |
| `step mode=line` | Single-steps until the source line changes; a call is run over |
| `until location=...` | Runs to a location once (`resume to`) |
| `return value=N` | Returns N from the current function without running the rest (`resume ret`) |
| `skip location=file:line` | Moves execution to another line of the same function (`resume eip`) |
| `freeze` / `thaw` | Suspends every game thread (from outside the process), and resumes them. Nothing runs in the game until `thaw` |
| `dump file=name` | Writes a minidump into `bridge/state/dumps/` (the threads are frozen while it is written) |

Limits, for now: a hook stop cannot be stepped (`BAD_MODE`; set a break on a line inside the function instead). A
break is not kept across a restart of the game, and breaks are not journaled, so `fnv_undo` does not remove them.
Stepping after a hook-hold stop needs the companion's native breakpoint modes.

## Script-level tools: fnv_flow, fnv_blame, fnv_script_debug, fnv_script_log (Plan 2, W2)

These four sit on the engine companion's hooks, breakpoints, watch rings and trace op, and on the symbol database and
the engine map. Every address is resolved through the engine map first; a missing or different address refuses with
ENGINE_MAP_MISMATCH and the map to refresh. Actions that change the game need `fnv.engine_control` = full; reads do
not.

**fnv_flow** (`trace`, `call`). `trace function=<name or 0x..> when=<predicate> depth=0 max_steps` arms a breakpoint
on the function's entry, then runs `resume trace` over the function's range with a log program that reads its scalar
locals at their EBP offsets (at most 16). The result lists each executed source line in order with its hit count, the
source text and the locals that changed (the change is attributed to the line before it). The function needs symbol
database lines (NO_LINES otherwise) and a hook-free entry (ALREADY_HOOKED). `call` and `depth 1`: see "Plan 2
completion pass" below.

**fnv_blame** (`field`, `ref`, `report`, `form`). `field address=<0x...> | <ref> + 0x..` sets a hardware write watch
(`dbg` set, 1 to 64 bytes, `write` or `readwrite`, scope `main` or `all`, up to 600 seconds). It shares the four
hardware slots with fnv_debug. Each hit records the thread, the stack with source lines, and the script line when the
script runner is on the stack. `ref ref=<ref> what=<presets>` hooks the B-debug section 3 presets (comma-separated, or
all), filtered to the reference pointer, with the log flag so the ring keeps every call. `report` groups hits by
culprit: the script line, else the writing function. `form`: see "Plan 2 completion pass" below.
Deviation: the script context of a blame hit is the last statement the thread ran, read from the statement ring; it is
approximate, not the statement that wrote.

**fnv_script_debug** (`break`, `status`, `vars`, `set`, `eval`, `step`, `continue`, `freeze`, `thaw`). A break is a hold
on the hook of `ScriptRunner::EvaluateLine` (0x005E1550): the predicate tests the script's form id through arg0 (set
with `script`), and a companion hook filter tests one raw argument, the line (arg6) or the opcode (arg1). The
predicate language cannot read a raw argument, so only one line or opcode filter is in force at a time, and `when`
is a predicate over the script's variables that is ANDed with the script test. `status` shows the script, line, opcode,
reference and if-depth; `vars` lists the stopped script's variables by name (names from the script's variable list);
`set` writes a variable's value (it does not mark the reference changed, as the engine's own set does); `eval` runs a
snippet on the stopped reference; `step line` runs to the next line of the same script run; `continue` resumes;
`freeze` and `thaw` turn the process-scripts byte (0x0118C685) off and back on. A statement reached during save or load
is passed on with `resume next` and counted in `skipped`, because it cannot be refused at hold time.

The statement hook is shared with fnv_script_log. fnv_script_trace uses a different label on the same address, so a
break refuses with HOOKED_ELSEWHERE while fnv_script_trace is on: stop it first. A break cleared to none re-hooks the
statement so the ring keeps going.

**fnv_script_log** (`commands`, `history`, `events`, `raise`, `errors`). `commands mode=start` installs the statement
hook as a record-only hook (no hold); `commands mode=read script=<name> ref=<ref> opcode=<n> since=<seq>` lists the
statements in the ring with the command name from the engine map (the ring holds the last 200 records; `dropped`
counts the rest); `commands mode=stop` removes it unless a break still uses it. `history ref=<ref>` lists the
world-changing commands (Disable, Enable, MoveTo, AddItem and the like) on a reference from the same ring.
`events mode=start` hooks `Script_SetActionFlag` (0x005AC750) and `ScriptLocals::SetActionFlag` (0x005A8E20);
`events mode=read` lists the block flags raised with their names (OnAdd, OnEquip, OnDrop, ..., OnTriggerLeave) and the
raising function. `raise ref=<ref> flag=<name>` calls `Script_SetActionFlag` through the engine call path, so the flag
is only marked on the reference; the block runs when the scripts next run. Deviation: the second argument of
`Script_SetActionFlag` is taken as the reference's extra data list (ref + 0x44), which is not yet confirmed against the
decompiled signature. `errors since=last|start` wraps fnv_script_errors.

## Eyes: fnv_camera, fnv_look, fnv_photo, fnv_hud, fnv_visibility (Plan 2, W3)

Checked in the game on 2026-11-11 (Prospector Saloon). Code: `geck_mcp/fnv_eyes.py`.

| Tool | Kind | What it does |
|---|---|---|
| `fnv_camera` | act | `get` the view; `set` a free camera (position, yaw, pitch, `look_at`, `fov`); `orbit` a ref (relative yaw, pitch, distance or `fit`); `pov first\|third`; `restore` |
| `fnv_look` | read | References on screen, largest first: screen rectangle in pixels, distance, line of sight, flags; crosshair, prompt, corner message, subtitle, open menus |
| `fnv_photo` | act | `ref` from set angles in one sheet (world paused, view restored); `cell`; `view` (labels); `sequence` with the frame gate; `compare` against a baseline |
| `fnv_hud` | read | HUD mode, visible parts, crosshair and prompt, message on screen and queued, subtitle; `post` a corner message |
| `fnv_visibility` | read | Why a ref does not draw: no 3D, deleted, disabled, culled, zero scale, empty bound, first person, other cell, outside the view, occluded |

How:

- The camera is the scene graph's NiCamera (`*(0x011DEB7C)` +0xAC; FOV at +0xBC). Its world rotation (+0x68) has
  the view direction in column 0; +0x9C is the world-to-clip matrix the tools project with.
- A free camera is the engine's fly camera: `tfc 1` (the world stops) or `tfc`, flag at OSGlobals
  (`*(0x011DEA0C)`) +6; the pose is on the player at +0x7E0 (yaw, pitch in radians, position). Writing it moves
  the view on the next frame, and `SaveScreenshot` captures it. `restore` puts back the saved fly state and FOV.
- First/third person is player +0x64A; `pov` presses the POV control for four frames (one frame does not switch).
- References come from the loaded cells' own lists (cell +0xAC; the interior, or the exterior grid from TES).
  xNVSE `GetRefs` returns at most 200 per call and stops listing a reference after Disable and Enable.
- A reference's 3D root is +0x64 → +0x14; its world bound sphere is behind the pointer at +0x20, flags at +0x30.
- HUD: `*(0x011D96C0)`: queue +0x18C (text at +0 of each node), crosshair +0x1B8, flags +0x1BC, mode +0x1C4,
  tiles 26 (message), 28 (subtitle), 29 (prompt), 32 (name). The message tile keeps its last text after the
  message leaves the screen. `post` calls `QueueUIMessage` 0x007052F0.

## Hands: fnv_play, fnv_ui_do, fnv_input v2 (Plan 2, W3)

Code: `geck_mcp/fnv_hands.py`.

| Tool | Kind | What it does |
|---|---|---|
| `fnv_play` | act | `goto` a ref or position on foot; `look_at`; `activate` (or why not); `talk`; `loot`, `equip`, `use`, `drop`; `stop` |
| `fnv_ui_do` | act | `click` a tile or `button` by its label (the menu's HandleClick); `wait` N hours; `fast_travel`; `message`; `close` |
| `fnv_input` | act | Now also `key_name` (E, Space, F1, ...), `button` (mouse) and `mouse` [dx, dy] with `frames` |

How:

- `goto` plans a route with weighted A* on a 64-unit grid. Each step is checked with three knee-high path-pick
  rays as wide as the player (the centre ray, and side rays from 40% along the step so they do not start inside
  the player's own body). Doors are passable nodes, opened by `activate` on the way. Some floors are not
  pickable from above (seen in Prospector Saloon); a step without a floor hit keeps its height. Walking is the
  forward control through the injector, turned with `SetAngle Z`. When the player stops gaining ground, the spot
  ahead is marked blocked and the route is planned again from where the player stands (up to four times).
- `activate` reads the refusal states first (menu mode, controls-disabled byte player +0x680, disabled, locked,
  combat, unnamed door). It then turns to the target, trying up to five points of its bound until the crosshair
  picks it, and presses activate for one frame. What happened comes from the menus, the HUD message queue, the
  door state and the player's position. Only reference functions are evaluated on the target: actor functions on
  a container crash the game (seen live). `direct=true` schedules `Activate player` on the main loop, never
  inside a request: an NPC's dialogue would hold the request until the conversation ends.
- Menu clicks call the menu's vtable slot 3 `HandleClick(id, tile)` with the tile's id trait (0xFAA), as a
  mouse-up does; `button` finds the tile by its text and walks up to the nearest tile with an id.
- `wait` presses the wait key for four frames (one frame does not open the menu). When SleepWaitMenu opens,
  waiting is allowed there; the hours then run through the engine's own one-hour wait (`fnv_time`).
- `fast_travel` asks `CanFastTravel` 0x0093D660 first (its refusal message is the reason), then
  `FastTravel(marker)` 0x0093CDF0.
- `type`: the text entry menu (TextEditMenu 1051, the character name): its string (+0x34) set, then its Done click
  0x007E66A0 (tile 1), which copies the text to 0x011DAED8 for the menu that asked; New Vegas asks for no other menu
  text (see "Live checks of the leftovers").
- Doors: path-pick rays pass through door collision, so a closed door is not in the plan (seen live). When the walk
  stops gaining ground with a closed, non-load door within 140 units ahead, `goto` opens it and plans again through
  the door's centre. Checked live: the bathroom door of Prospector Saloon opened on the way. Not solved: a door that
  swings open into a narrow corridor can wedge the player against its panel (the rays do not see the panel either).
- Checked live (evening run): `talk` on Easy Pete opened DialogMenu ("Howdy. What can Easy Pete do for you?");
  `fast_travel` from Goodsprings to Primm (an hour passed); TrudyRef is disabled at a fresh start and `talk` refuses
  with `disabled`. `activate direct=true` on a load door does not move the player (use the crosshair press or
  `fnv_move`).
- `fnv_test steps` takes the eyes and hands as steps: `photo {action, ref, angles}`, `play {action: goto|talk|activate|look_at,
  ref | position}` (passes when it arrived, a dialogue opened, the target is in the crosshair, or activate was pressed
  with no refusal) and `ui {action, label, ...}`. `break` and `flow` stop the main thread and are not scenario steps.

## Creation loop: fnv_asset, fnv_form, fnv_place, fnv_author (Plan 2, W4)

Code: `geck_mcp/fnv_create.py`.

| Tool | Kind | What it does |
|---|---|---|
| `fnv_asset` | act | `reload` a model or texture from disk; `watch` a folder; `file_log` start/read/stop; `where`; `queue`; `models`; `archives` |
| `fnv_form` | act | `create` a form or a clone; `reload` a rebuilt plugin's changed records into the live forms; `changes` |
| `fnv_place` | act | `at`, `array` (row, grid, circle), `nudge`, `rotate`, `scale`, `snap`, `check`, `list`, `remove` |
| `fnv_author` | control | `export` the session as a spec part; `check` a plugin; `scaffold`; `package` |

How:

- Model reload: disable every loaded reference that uses the model, call `ClearUnusedModels(true)` 0x00448620
  on the ModelLoader (`*(0x011C3B3C)`; it drops every cached model nobody uses), then enable them again. The
  file log shows the model read again. A texture reloads with the loose models that name it.
- File log: two record hooks. `FileFinder::GetFile` 0x00AFDF20 covers lookups by name, including files that do
  not exist anywhere (null result); the name is copied at the call. The archive open by entry 0x00AF6340 is what
  queued loaders use for BSA files; it gets no name, so the hook copies the file entry, and the bridge maps the
  name hash back to the path with the hashes stored in the archives.
- `fnv_form create` uses xNVSE `TempCloneForm` for a clone (until the game quits), else `CreateFormForType` +
  `AddFormToDataHandler`. `reload` applies the rebuilt record's name, models, value, weight, health, armour
  rating and threshold with the engine's setters (`SetName`, `SetModelPathEX`, `SetBipedModelPathEX`,
  `SetBaseItemValue`, `SetWeight`, `SetHealth`, `SetArmorAR`, `SetArmorDT`), then rebuilds the 3D of the refs that
  use a changed model. New records and other types are listed as needing a restart. `CloneForm` (JIP) is not
  present in this setup.
- Placement uses `PlaceAtMe`, `SetPos`, `SetAngle`, `SetScale`; every placement and change is journaled, and
  `remove` disables and marks for deletion. `snap` and `check` cast rays down and pass through the object's own
  collision.
- `export` writes `state/fnv/author/<name>.spec-part.json`: created forms as records (template + FULL), placed
  references as cell objects with `@EditorID` bases where the record index knows them.
- `package` copies only the plugin and the files it names that are loose in its own mod folder, never files of
  the game's Data folder or of other mods.

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

Files are a YAML subset or JSON; no YAML library is needed. The rules are below. Examples:
`examples/fnv-tests/`.

### Scenario file format

A scenario file is either JSON (the whole file starts with `{` or `[`) or the YAML subset described here. A
file a full YAML parser reads differently is read by these rules, not YAML's.

- **Structure.** Block mappings (`key: value`) and block lists (`- item`), nested by indentation. Indent with
  spaces; a tab in the indentation is refused. A list may sit at the same indentation as its key. `- key: value`
  starts a mapping inside a list item. `---` lines are ignored.
- **Flow values.** `{a: 1, b: x}` and `[1, 2]`, nested, on one line. Text after the closing bracket is refused.
- **Plain scalars.** `true` `yes` `on` and `false` `no` `off` (any case) are booleans; `null` and `~` are null.
  Whole numbers become integers and decimals (`1.5`, `.5`, `1.5e3`) become floats. A number with a leading zero
  (`00138562`), hex (`0x1F`) and `1E5` (no decimal point) stay text, so form ids can be written bare. Quote a word
  you want as text: `'yes'`.
- **Quoted scalars.** `'single'` takes the text as written (`''` is one quote). `"double"` takes JSON escapes
  (`\t`, `\n`, `\"`, `\u00e9`); any other backslash is refused, so write a Windows path in single quotes.
- **Comments.** `#` at the start of a line, or after a space or tab, starts a comment, except inside quotes.
  `x#y` is text.
- **Block scalars.** `|` joins the more-indented lines that follow with line breaks, `>` with spaces. Unlike
  YAML, blank lines are dropped, each line's own indentation is dropped, and a ` #` inside starts a comment.
  Chomping and indentation indicators (`|-`, `|2`) are not supported: the lines after them are refused.
- **Not supported.** Anchors and aliases (`&x`, `*x`) and tags (`!!str`) are read as plain text. Keys are text.
  No multi-document files and no multi-line flow values.

### Agent steps and checks

```yaml
name: agent wave end to end
start: {test_save: agentwave}         # a save made with fnv_test_save
guard: {message_boxes: keep}          # keep message boxes for the test (default: close them)
trace_script: HelloQuest              # report which statements of this script ran and which never did
steps:
  - spawn: {form: 0006B53D, as: ammo} # $ammo in later steps
  - expect: {exists: $ammo}
  - trace: {function: "TESQuest::RunScript"}
  - wait: {seconds: 1}
  - expect: {called: "TESQuest::RunScript", at_least: 10}
  - run: ShowMessage 0011E661
  - expect: {message_box: Hardcore}
  - menu: {press: "No"}
  - expect: {menu_open: MessageMenu, is_false: true}
  - expect: {condition: 00138562, list: requirements, passes: false}
  - time: {advance_hours: 1}
  - watch: {items: [GetCurrentTime], seconds: 1}
  - expect: {marker: 03002FCA}
  - screenshot: agent-wave-end
  - expect: {no_script_errors: true}
teardown:
  - trace: {function: "TESQuest::RunScript", action: stop}
  - revert_all: true                  # this test's changes only
```

Steps: `spawn` (`as:` names the new ref), `move`, `save` / `load` (test saves), `menu` (`press`, `close`,
`close_all`), `screenshot`, `watch` (fails when `until` never becomes true), `trace`, `time`, `rng`.
Checks: `called` (calls recorded by a running trace, `at_least` / `at_most`), `condition` (a record's
condition lists, optionally one `list`, `passes`), `message_box` (text in the open box), `marker` (a quest
target is shown), `menu_open`. `revert_all: true` in a test undoes only the changes made since its steps
began (`revert_all: process` for every change of the game process).

A failing run adds a debug bundle: the open menus and message box, the console's last 40 lines and a
screenshot. With `trace_script` the report has `scriptTrace`: statements run, and the lines that never ran.

## Data and authoring

`fnv_record action=find` and `fnv_record` index every active plugin into `state/fnv/records.sqlite` (record
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

## Dialogue, AI packages and runtime changes

Conditions are evaluated the way `TESConditionItem::ContainsTrait` (0x00681600) does it: pick the reference
the function runs on (subject, target, a reference, the combat target or the linked reference), call the
command's eval handler from the running game's own command table (`CommandInfo.eval` at +0x20, cdecl
`(ref, param1, param2, double &)`), compare with the value or global, and combine (an OR item joins the next
one; groups are ANDed). So each condition shows its own value, not only pass or fail. Handler calls are
batched, 250 per frame: Trudy's GREETING (5579 responses of every quest) takes about 10 s.

`fnv_dialogue action=tree` asks the engine for each response's verdict with `TESTopicInfo::CheckCondition`
(0x0061E600, proven; with the speaker and the player), which also applies say-once, quest conditions and the
dummy-node rules; a response where the engine and the per-condition check disagree gets a note. `wouldSay`
is the first available response of the highest-priority quest in list order (random responses are chosen
among consecutive ones at run time). `scope=speaker` keeps the responses whose positive speaker tests
(`GetIsID X == 1`, `GetIsVoiceType`, `GetInFaction`...) pass for this speaker, plus any available now;
`available` and `all` widen or narrow that. Response text comes from the plugin record (NAM1), since the game
loads it only when a line is spoken.

`fnv_dialogue_choose` opens the conversation by having the player activate the speaker, waits until the
speaker has finished (DialogMenu state `speech_idle`), reads the options from the menu's
TopicMenuController list and its response ListBox (decomp layouts), and picks one with
`DialogMenu::HandleSelection(-1, tile)` (0x007624F0), the call a mouse click makes. The menu ignores clicks
within 500 ms of filling its list; the tool waits for that.

`fnv_ai_packages` reads the base form's `TESAIForm` package list and each `TESPackage` (type at +0x20, flags
at +0x1C, location, schedule PSDT at +0x38, conditions at +0x40), checks the schedule against `GameHour`,
`GameDay`, `GameMonth` and `GetDayOfWeek`, and evaluates the conditions with the actor as subject. The first
eligible package is the expected one; `matchesEngine` compares it with `GetCurrentPackage`. A running package
that is not on the list came from elsewhere (a script package, a quest, combat, dialogue, a template NPC).

`fnv_form_live_diff` reads the form's components at the offsets the decomp's class layouts give for its
runtime class (`TESFullName`, `TESValueForm`, `TESWeightForm`, `TESHealthForm`, `TESAttackDamageForm`,
`TESScriptableForm`), plus `TESGlobal` and `TESQuest` data, and compares them with `fnv_record`'s
decoding of the winning record.

## Engine code: fnv_native, fnv_code, fnv_plugin_build (Plan 2, W5)

Code: `geck_mcp/fnv_native.py`, `geck_mcp/fnv_code.py`, `geck_mcp/fnv_plugin_build.py`; companion
`native/engine/w5_mod.c`, `w5_code.c`; module API `native/sdk/fnvmcp_module.h`; standalone runtime
`native/runtime/fnvmcp_rt.c`. Checked in the game on 2026-11-11.

| Tool | Kind | What it does |
|---|---|---|
| `fnv_native` | act | C modules inside the game: `headers` (module API + `fnv_engine.h`: `FNV_ADDR_<name>`, typed pointers `FNV_<name>` with their calling convention, padded structs), `build` (Zig), `load` (init runs on the main thread, requested hooks installed), `call` an export, `log`, `reload`, `unload`, `list` |
| `fnv_code` | act | Change an engine function in its decomp source and run it: `checkout`, `edit`, `build` (the unit's own compiler and flags, then a link against the game's addresses), `compare` (old and new side by side), `swap`, `unswap`, `list`, `export` (local patch set) |
| `fnv_plugin_build` | control | A module as a normal xNVSE plugin with no companion: `scaffold`, `build` (hook prologues planned from the running game or the local game image), `install` (own MO2 mod in the dev profile), `log` |

How:

- The module API (`FnvmcpApi`, 44 bytes, version 1): `log`, `read`, `write` (put back at unload), `hook(target,
  handler, user, pop)`, `on_frame`, `console`, `nvse`, `alloc`/`free` (the game's MemoryManager). A hook handler gets
  the registers, return address and stack arguments (`FnvHookFrame`); `FNV_SKIP` returns the frame's `eax` and pops
  `pop` bytes. API calls find their module by the caller's return address.
- Hooks: the module asks; the bridge plans the stolen prologue (as for every hook), installs a plain hook, and
  `hooknative` attaches the handler. Unload removes the hooks (original bytes back), waits for running handlers, runs
  `fnvmcp_module_exit`, puts writes back and frees the DLL.
- Checked live: a module logged `activate 00107C77 by 00000014` from `TESObjectREFR::Activate` 0x00573170; reloaded
  with a skip for that door, the activation was refused and the door stayed open; after unload the door closed and the
  entry bytes were the original `55 8b ec`.
- `fnv_code`: the unit's source is copied into `state/fnv/code/<address>/` (the decomp checkout is never edited) and
  compiled with `tools/msvc.py` and the unit's recipe. The function's COMDAT is taken from the object; each
  relocation is bound through the unit's `symbols`, the unit's functions, or a name bound to one address anywhere in
  the recipe; read-only sections it needs are copied into the blob. Refused: exception handling (`.text$x`,
  `.xdata$x`), new writable statics, copied sections with relocations. The blob goes into `code alloc` memory and the
  entry gets a `jmp` (`code patch`, other threads suspended).
- S3 checked live: `AIFormulas::CalculateSkillValue` 0x00643C20 with `float result = base + 10.0f;` built to 103
  bytes (original 97, 7 relocations, all bound); `compare` (15, 5, 5) gave 28 and 38; after `swap` a call of the
  original entry returned 38; after `unswap` 28 again; the game ran on.
- `fnv_plugin_build`: `fnvmcp_rt.c` provides the same API as a plugin (hooks written at plugin load, before the game
  runs; frame callbacks on xNVSE's main-loop message; log next to the DLL, which MO2 puts in `overwrite/NVSE/Plugins`).
  Checked live: the activation logger as a plugin logged the door and an NPC activation with no companion hook.
- Generated structs come from the symbol database's best definition of a class; for `TESForm` that definition is a
  36-byte placeholder of `unk` fields (the real class is 0x18 bytes), so check a struct before trusting it.
- Built in the completion pass (below): `fnv_orig_<address>`, `compare mode=shadow`, call-site and vtable redirects
  for functions under five bytes, a loader module for an exported patch set. Not built: `compare ab` (decided: shadow
  answers "does the new code agree", and alternating the entry per call would need a hook on our own jump).

## Performance and inspectors (Plan 2, W6)

Code: `geck_mcp/fnv_perf.py` (profile, threads, memory, object), `geck_mcp/fnv_coverage.py`, `geck_mcp/fnv_inspect.py`
(anim, audio, physics, systems); companion `native/engine/w6_cov.c`. Checked in the game on 2026-11-11.

| Tool | Kind | What it does |
|---|---|---|
| `fnv_profile` | read | `sample` the thread stacks from outside the game (nothing patched): self, inclusive and innermost-game-function time per function and per decomp source line, callers, frame rate, a folded-stack file; `diff` two saved profiles |
| `fnv_coverage` | act | `start` one-shot traps at every line start of decomp files or functions (verified only), `stop` lines run and never run, `diff` two runs |
| `fnv_threads` | read | Every thread with its start routine and current function; the four known spin locks with their owner |
| `fnv_memory` | read | `pools` (block size, committed bytes, peak; live NiRefObjects), `alloc_trace` (allocations per call site, named) |
| `fnv_object` | read | `identify` a pointer (pool block, class by RTTI, form), `find` live objects of a class |
| `fnv_anim` | act | `state` per body section (group name, weapon, move, power armour, sequence file), `play` a group or an idle |
| `fnv_audio` | act | `log` every sound created (form or file, and the asking function), `play` a sound |
| `fnv_physics` | read | `controller` (fall time and distance), `body` (3D root to Havok body with classes) |
| `fnv_systems` | read | `formulas`, `formula`, `sweep`, `progression`, `needs` |

How and what was seen:

- Profiling suspends the thread about 190 times a second (Wow64GetThreadContext, then the EBP chain), names each
  address once (engine map and symbol database, module + offset outside the exe). `gameFunctions` attributes each
  sample to its innermost game function, so a wait inside the driver counts for `PresentFrame`.
- S5 checked live: an idle saloon runs at 61 fps with 81% of the main thread in `PresentFrame` (vsync). A module that
  spends 25 ms in front of TES::RunScripts every frame: the profile put 83% of the main thread under
  `FUN_008705d0` (`main.cpp:908`, the call into RunScripts) with the module's code as self time; after unloading it
  the diff read 34.3 -> 57.0 fps and that function -82.9 points.
- Coverage on `TESObjectREFR::Activate` while opening a door: 32 of 67 lines ran, the others listed.
- Memory: 37 pools, 63.5 MB committed, 242,268 NiRefObjects; in two idle seconds 5,227 allocations from 17 call sites
  (most from `BSAudioManager::SetVoiceState`). Pool blocks are laid out per 4 KB page (Character blocks of 0x1E0:
  eight per page); the page tail after the last whole block is not a block.
- Systems: `skill_value(15, 5, 5)` = 28; XP for levels 1 to 5 = 0, 200, 550, 1050, 1700. The game's actor value is
  spelled `SleepDeprevation`; the hardcore flag is player +0x7BC.
- Audio log in the saloon: a dog's panting and tongue sounds, with files and `BSAudio::GetSoundHandleByFilename`.
- Built in the completion pass (below): everything listed here before, except `fnv_memory pressure` (decided: the
  engine's free-memory steps unload cells and textures mid-game; `pools` covers the reading) and thread names from
  the thread-name exception (the game names few threads; `fnv_threads` names them by start routine).

## Whole-plugin checks: fnv_crawl, fnv_bisect, fnv_selftest (Plan 2, W7)

Code: `geck_mcp/fnv_crawl.py`, `geck_mcp/fnv_suite.py`; guides `slow`, `change-engine`, `check-plugin` in
`geck_mcp/guides/`. Checked in the game on 2026-11-11 except `fnv_bisect` (see below).

| Tool | Kind | What it does |
|---|---|---|
| `fnv_crawl` | control | `cells plugin`: every cell the plugin adds or edits, visited and settled, then script errors, engine warnings, files not found, references with no 3D, dead load doors, frame rate and a photo, worst first. `dialogue npc`: every line written for the NPC with topic, quest, the engine's verdict and failing condition, and the voice and lip files with whether they exist |
| `fnv_bisect` | control | `level=mods\|plugins` and the failing `fnv_test steps` steps: checks it fails with everything on and passes with every candidate off, then halves the suspects each round (the game is quit and relaunched) and names the culprit; the profile is put back |
| `fnv_selftest` | read | What each family relies on, against the running game: ok, degraded or broken with the fact |

What was seen:

- `fnv_crawl cells` on HelloWasteland.esp (7.8 s): one row, a real finding: the engine asks for
  `data\sound\fx\fx\fire\fx_fire_barrel_lp.ogg` (a doubled `fx\fx`), which does not exist. HelloQuest.esp: clean.
- `fnv_crawl dialogue` on Easy Pete: each line with its topic (GREETING, PLAYERTHROWGRENADE, ...), quest
  (VFreeformGoodsprings), the engine's verdict (one GREETING line blocked by `GetQuestVariable 00104EAE 14 == 1`) and
  its voice and lip files, all found. The file names follow the archives' rule, read from Fallout - Voices1.bsa:
  `sound\voice\<plugin>\<voice type>\<quest EditorID, 10 chars>_<topic EditorID, 15 chars>_<info id>_<n>.ogg`.
  Calling `VFDialogue::BuildVoiceFilePath` 0x00616FA0 directly faulted at 0xC (guarded): it needs a this pointer the
  decomp does not show.
- `fnv_selftest`: 21 ok, 1 degraded (`Warning` starts with xNVSE's jump, as expected), 0 broken. It found a companion
  bug on its first run: `code free 0` matched an empty slot of the block table (fixed: 0 is refused).
- The companion's build hash listed only some source files, so a change to `w5_code.c` alone reused a stale build;
  every `engine/*.c` and the module header are hashed now (a test checks it).
- `fnv_bisect` is tested against a fake profile and runner (eight mods, the culprit in three rounds, the profile put
  back). Its live run needs the MO2 launch of the MCP server (launching from an in-process test client fails), so it
  is not checked in the game yet.
- Built in the completion pass (below): replay, `fnv_crawl quest` and `items`, bisect `records`. Decided against:
  parallel game instances (WinMain refuses a second process through the named mutex whose name is at 0x01082AB8, and
  refuses Remote Desktop sessions; each worker would also need its own MO2 instance, INI, saves and about 2 GB) and
  bisect `hooks` (putting another DLL's patch back in a running game is unsafe; level `mods` names the DLL).

## Acceptance scenarios S1, S2, S4, S6 (Plan 2, run in the game 2026-11-11 night)

Each scenario was run against a plugin built for it with the spec builder (`examples/hello-bug` is in the
repository; the other three needed game files it cannot ship), its scripts compiled by the running game. What failed on the way was fixed and is listed.

**S1, build a quest mod and play it as a player** (one `fnv_test steps` call): PASSED, 23 steps in 64 s, no console and no teleport. The player walks to a contact, talks,
picks "Sure, I'll look for them." (stage 20, set by the line's result script), walks to and picks up three tokens
(stage 30 from the quest script), talks again, hands them in (stage 40, the tokens taken, 100 caps given by the
result script), and a photo sheet of the contact is taken. Fixed on the way:
- The builder writes dialogue: topics (`DIAL`), lines (`INFO`) with responses, conditions (`CTDA`), choices
  (`TCLT`), flags and result scripts, and new lines on a master topic (`GREETING`) through an override copied from
  its template (`geck_mcp/esp/dialogue.py`). `fnv_scripts_compile` compiles result scripts in the game inside a
  one-block wrapper and keeps the block's statements, the layout of FalloutNV.esm's result scripts.
- A line's result script runs only when the line's record flag `0x2000` is set: all 6,277 lines of FalloutNV.esm
  with a compiled result script carry it and none of the 16,970 others (`TESTopicInfo::GetResultScript` reads the
  scripts from the file only then). Without it the line was said and its script never ran.
- `fnv_test steps` has a `dialogue` step: `{choose: [option, ...], speaker: ref}`, the call a click on a response makes.
- `fnv_play goto` waits up to 3 s for a message box that something closes (the DLC "items added" boxes, closed by
  `fnv_test steps`) and walks on, instead of stopping.

**S4, a script misbehaves** (`examples/hello-bug`): PASSED. HelloBugQuest counts the global HelloBugLevel up to
100 and never gets there. `fnv_script_debug` stopped on source line 8 (`if HelloBugLevel >= 100`), eval read 39,
step went to line 10. `fnv_blame field HelloBugLevel + 0x24` named "script 0D000803 line 8: set HelloBugLevel to
0", the janitor quest's script. `fnv_script_hot_swap` swapped in the fix; the regression test
(`tests/hello-bug-finishes.yaml`) failed before the fix and passed after, and the quest reached stage 10. Fixed on
the way:
- The game's script runner counts statements, not source lines (its "line 7" was the 7th statement, an endif on
  source line 10). `fnv_script_debug` takes and shows source lines, mapped through the script's text (the game's,
  else its plugin record's SCTX), with the statement number beside them. Code swapped in from an in-game compile
  keeps xNVSE's extra scriptname statement, so it counts one more; the mapping reads that from the code.
- `fnv_blame field` named the wrong scripts: it read "the last statement the thread ran" after the watch ended. The
  companion now stamps each main-thread watch hit with the newest hook record the main thread wrote (hit words 8
  and 9); field reads its hits every 0.2 s while watching (the statement ring wraps in about a second) and reads
  that one record: the exact statement, with its source line.
- In-game compiles replaced global EditorIDs with form ids when a first compile failed for another reason
  (`set <form id> to` is no variable). Globals keep their names (the running game keeps them).
- Quest scripts do not run while the pause menu is open (it opens when the game window loses focus): the guide says
  to close it with `fnv_ui_do`.

**S6, a new mesh looks wrong**: PASSED. A crate mesh shipped loose in its mod names a
misspelled texture (`CrateLarge0l.dds`). The photo sheet (four angles) showed it without its paint beside the
vanilla crate; `fnv_asset_check` named the missing texture and the mesh; the mesh file was fixed in the mod folder;
`fnv_asset reload` took 0.66 s with no restart; the sheet after shows both crates alike. Fixed on the way:
- `fnv_photo` of a base form (the STAT's EditorID instead of the placed reference) crashed the game in
  `Cmd_GetPos_Eval`. The eyes and hands tools refuse a base form now (`NOT_A_REFERENCE`, with the next step).
- The builder's asset check searched only Data: a mesh in the plugin's MO2 mod folder failed the build. It searches
  the enabled mods and the plugin's own mod folder now.
- Not fixed: `fnv_asset file_log` did not see the missing texture (the engine never asked `FileFinder::GetFile` for
  it); `fnv_asset_check` finds it offline.

**S2, a load order crashes on entering a cell**: PASSED. The crate mesh of HelloCrashRoom
has its root node's child pointing at its strip data. With the crash hold on (`fnv_debug break location=fault`),
`coc HelloCrashRoom` faulted on a loader thread in `NiAVObject::AttachParent` (from `NiNode::SetChildAt`,
`ReadChildListFromStream`, `NiStream::Load`, `QueuedModel::Run`); the game stayed alive, held. `fnv_debug status`
named the fault and `hellocrash\crate.nif` on the faulting thread's stack; `fnv_asset where` gave the HelloCrash mod
folder and `fnv_record action=find plugin=HelloCrash.esp` the STAT HelloCrashCrate that uses it. Fixed on the way:
- A fault on a loader thread during a load leaves the main thread waiting for it, and the companion answers only
  on the main thread: every call timed out. `fnv_debug status` and `stack` now fall back to reading the held
  fault from outside: the registers of the fault from the companion's context file (the first fault of that thread;
  a crash logger faulted again later), the stack above them named with the engine map, and file paths found on that
  stack.
- `fnv_threads action=hang` samples, besides the main thread, the threads that use the most CPU and the threads held at a
  fault, and reads a stack that sits near the top of its region in smaller pieces.
- Seen on the way: a mesh whose root claims 268 million children does not crash; the load job never finishes and the
  game hangs in `TES::LoadInterior`.

Not run: `fnv_bisect` in the game (the MCP server must launch the game; it was not reconnected with the new tools).


## Plan 2 completion pass (2026-11-11 late night)

Everything the waves left as "not built" was built or decided against; each item below was run in the game
(Prospector Saloon, a spawned Trudy, the Hello plugins) unless it says otherwise. Tool count at the time: 209 (165 `fnv_`,
44 `geck_`).

**Inspectors**
- `fnv_anim groups` (the group map at animation +0xDC, values are AnimSequenceBase: a single one holds its
  BSAnimGroupSequence at +4): 1,057 groups for the player with their KF files. `biped` (Character +0x1B4, slots of 16
  bytes at +0x2C): the player's upper body, hands, Pip-Boy; Trudy's NCR outfit. `face` (process +0x178, keyframe
  blocks with values at +0xC and count at +0x10): counts 15/15/16/1/17/16/1 as the decomp says; Trudy's mood
  expression 5 = 1.0. `idle_tree`: the roots come from IdleAnimationManager::GetIdleArray 0x00600690 **on the base
  form** (the manager's own lookup by reference returned no folder); each root is asked with
  TESIdleForm::FindIdleFormForObject 0x005FF2E0 in order. Approximate: replay delays and the caller's filters
  (HighProcess::StartIdleAnimation) are not applied, so the LOOSE root idles with no conditions answer first.
  `events`: record hook on Actor::SetAnimAction 0x008A73E0 (no actions in a quiet saloon).
- `fnv_audio playing` (BSAudioManager 0x011F6EF0 +0x54 map; BSGameSound flags +8, state +0x10, volume +0x24, file
  +0x36, sound form +0x134): 12 sounds, 10 playing. The position at +0x17C read as garbage, so positions are not
  reported. `stop` by form sends message 4 through 0x00ADB610 (two clock loops stopped). `music`: the state block
  0x011DD0F0; `file=` plays a track as special media (type 6) and `stop` fades it (both seen). `radio`: one station,
  radio off. `hear`: CanPlaySoundAt 0x0082ECA0 (static, cdecl) says yes next to the player, no 90,000 units away.
  `subtitles`: record hook on Interface::ShowSubtitle 0x00705210 (not run with a speaker). `voice_check` = `fnv_crawl
  dialogue`: Trudy's 60 lines all found with the corrected naming rule.
- Voice file names (fixed in fnv_crawl): quest and topic EditorIDs share 25 characters; when they do not fit, the
  quest keeps at most 10 and the topic the rest; the id is the record's own 6 hex digits with a `00` prefix in every
  plugin (DLCs with one master too); then the TRDT response number. Checked on every FalloutNV.esm response that has
  a file in Fallout - Voices1.bsa (28,767 of 28,768). The old rule (always 10/15, prefix = number of masters) matched
  67% of vanilla and every mod plugin wrongly.
- `fnv_physics body` adds the velocity (hkpRigidBody +0x1B0, Havok units x 6.9991); `set`: bhkRigidBody
  SetLinearVelocity 0x005615D0 takes an NiPoint3 **in game units** and converts it itself (a bottle jumped 12 units).
  `controller` adds the state (+0x3F0: on ground ...). `triggers`: record hook on the script event raiser 0x005AC750
  (action ref, the trigger's extra list = trigger + 0x44, flag): OnTrigger runs merged per frame span, two actors in
  two scripted triggers seen. `projectiles` (ProcessLists 0x011E0E80 +0x70, kept by RTTI class), `query` (six rays),
  `layers`.
- `fnv_systems perk`: perk DATA at +0x38 plus its conditions ("Educated: needs level 4, player 1"). `barter`:
  GetSkillPriceMultipliers 0x00649050 with scratch out-floats: Barter 15 gives x1.482 / x0.517, a 9mm pistol (value
  100) 148 / 52; not compared with an open barter menu. `GetValue` returns 0 for ingestibles (their value is in
  ENIT). `zone`: GetCalculatedLevel 0x00526190 returns an unsigned short (mask it): Vault11Zone level 6. `crime`:
  karma; New Vegas keeps no bounty.
- `fnv_object referrers`: every writable region read from outside (VirtualQueryEx), 601 MB in 0.7 s; a Havok body
  pointer was held by its bhkCollisionObject and its native body. `fnv_memory refcount`: a write watchpoint on +4
  through `fnv_blame field`.

**Creation loop**
- `fnv_asset nif` (offline): header, root, the child links of every node block (NiAVObject flags are a u32 in New
  Vegas files, user version 2 = 34), collision, skin, texture paths. It names the S2 crash mesh's bad link (root child
  10 -> NiTriStripsData) without the game and passes vanilla meshes, the skeleton included. `dds`: size, format, mip
  chain (read from the archive). `users path`: the records that name a file (the S2 culprit HelloCrashCrate from its
  mesh path). One FalloutNV.esm record (00150FC0) has compressed data zlib refuses; it is listed as unreadable.
- `file_log` gains source `texture` (NiSourceTexture::Create 0x00A5FD70; textures never pass FileFinder::GetFile):
  the crate's diffuse and normal map seen when it was placed.
- `reload` of a menu XML (`menu=HUDMainMenu`): RemoveMenuEntries 0x00A1CA20, then MenuManager::ReloadMenu 0x00720AA0
  with the code of `&HUDMainMenu;` from 0x00A01860 (1004), as the console's `reload <menu>` does. `.kf`: ClearUnused-
  Models also drops unused KF files; `ref=` rebuilds an actor that holds one.
- `fnv_form bytes`: vtable slot 0xB on the live form, the record buffer 0x011C54CC read, freed (FormHeap_Free) and the
  globals restored. WEAP has a writer (the research had found none in the decomp): the 9mm pistol came back as 820
  bytes. `delete`: flags 0x20 | 0x4000 on a form this session created (no engine function to unlist it was found).
- `fnv_place check` adds overlaps: bound spheres compared, a candidate confirmed by a ray between the centres.

**Debugger and code**
- `fnv_flow call`: a small module (native/modules/fnvmcp_flowcall.c) makes the call from its frame callback, where
  breakpoints stop (they are ignored while the companion serves a request); the bridge fills its slot. XP for level
  5 and CalculateSkillValue(15, 5, 5) traced in the game. A module left loaded by an earlier bridge is found through
  its export table in memory. A call that times out disarms the slot (a stale armed slot once fired later into a
  breakpoint).
- `fnv_flow depth 1`: companion op `trace ranges <hex>` (up to 16 extra ranges, cleared when the trace ends); the
  bridge passes the direct callees with source lines from the engine map's call table. Each line names its function.
- `fnv_blame form`: record hook on BGSSaveLoadGame::NotifyFormIDChanged 0x0084A690 filtered to the form; flags named
  by GetChangeFlagName 0x0083FEF0 ("Moved", "Havok Moved" for a thrown bottle, 67 calls in 4 s, summarised).
- `fnv_code`: `fnv_orig_<8 hex digits>` (C name or a block-scope C++ declaration's mangled name) binds to a trampoline
  (the original's first instructions, then a jump back): new = original + 1 gave 29 for 28 while swapped. `export`
  writes a loader module (checks the expected bytes, places the code, binds relocations, jumps the entry): loaded as
  a hot module it applied the change (38), unloaded it put it back (28). `compare mode=shadow`: record hook on the
  original, each live call replayed on the new code (no live calls of the formula in an idle game). Functions under
  five bytes: their E8 call sites (engine map call table) and vtable slots (exe read-only data) are redirected (fake
  tested only).
- `fnv_script_debug step over|out`; `fnv_script_coverage` (statement ring read every 0.2 s in the background:
  4,500 statements in 11 s, 55 lost; HelloBug's scripts 75% and 89% with the lines never run);
  `fnv_script_decompile` (xNVSE's DecompileScript command, the file read back and removed: GSTrudyScript).

**Eyes, hands, time**
- `fnv_render toggle` (the console toggles; flag before and after where known: borders 0 -> 1 -> 0), `imagespace`,
  `shaders`, `lights` (the world shadow scene node's list: nodes of next, prev, ShadowSceneLight; its NiPointLight at
  +0xF8): 26 lights with positions.
- `fnv_overlay text` (DebugText::Print 0x00A0F8B0 on DebugText_Instance 0x00A0D9E0): shows once the debug text layer
  is on (`fnv_render toggle debug_text`); `marker`, `clear`.
- `fnv_replay`: two held controls recorded with their frames from a save, replayed under the frame gate: the player
  ended within 2 units of the recorded position.
- `fnv_game launch start=cell:<interior EditorID>` (SStartingCell in the INI): run 2026-11-12, crashed the game at startup
  in every cell tried (HelloWasteland, GSProspectorSaloonInterior): TES::LoadInterior -> LoadingMenu setup 0x00788AB0
  -> Tile::Set on InterfaceManager's root tile, still null before the main menu builds it. Removed; `coc:` goes
  through the main menu. Intros were already skipped (SIntroSequence blank).
- `fnv_ui_do type`: not run (no name entry menu could be opened without a new game).

**Whole-plugin checks**
- `fnv_crawl quest` (HelloCourier, stages 10 and 20: stage reached, objective shown, no unresolved target) and
  `items` (HelloCourier's token added, model found, taken back; the console takes the load-order form id, not the
  EditorID). `fnv_bisect level=records plugin=`: variants written with the codec (fake tested); `fnv_bisect` itself
  is still not run in the game (it needs the MCP server's launch, which the in-process runner cannot do).

## Loose ends of W1 to W4 (2026-11-11 evening)

Built and checked in the game in this run:

| Piece | What changed | Checked live |
|---|---|---|
| `fnv_watch frames=N` (`every=k`) | The companion sampler: one program of up to 16 values run in the main-loop message every k frames, kept in a ring in the game, read once. Items are predicate text (`[[0x011DEA3C]+0x30]:f32`) or `player.x/y/z/yaw`, `frame` | 120 consecutive frames, none missed |
| `fnv_events subscribe=[...]` | xNVSE event sinks (`onactivate`, `onhit`, `ondeath`, `onadd`, `onload`, ...): frame, reference and the event's argument words; words that point to forms come back as form ids. `sink_since`, `ref`, `unsubscribe` | OnActivate recorded when the player opened a door |
| `fnv_debug hits=N` | The companion counts hits that pass `when` and stops from the Nth (`bp after`, sent before `bp set`: set first, the next frame's hit stopped at once, seen live) | TES::RunScripts stopped on hit 120 exactly |
| A stop during a wait | A breakpoint that parked the main thread while `fnv_wait_for` or a step ran frames left the request open and the one pipe instance busy: the game hung (seen live). The companion now ends that request with `stopped`; the tools say `STOPPED` and name fnv_debug | 120th-hit break during a 5 s wait: the wait returned, the game resumed |
| `fnv_flow trace` | The trace is read under the step stop that ends it (it read the breakpoint's seq: `no_trace`); hex trace values decoded; the result field is `endedBy` (a duplicate `reason` argument made every finished trace fail) | TES::RunScripts: 114 quests visited, 52 scripts run, locals per line |
| `fnv_blame field` | Plugin code in a stack is named `module+offset` | GameHour is written every frame by `jip_nvse.dll+0x10FA4` |
| Wire numbers | The companion parses numbers as hex; several calls sent decimal (`resume` seq, `bp set` id, `ret` pop, `trace read`, `dbg read`): wrong values from 10 up. All send hex now | (found while checking hits=120) |
| `fnv_session start` | Failed on the locked warning log; it now turns the live warning capture on | session start, health ok |
| `fnv_photo` | Refused with `MENU_OPEN` while a menu is open (all frames were the pause menu's backdrop) | |
| `fnv_form reload` | Takes the rebuilt plugin's path: the running game locks its plugin files against writing | HelloRedFlash renamed in the running game from a rebuilt copy |

Still not done: S6 end to end (a changed texture file and a misspelled mesh in a test plugin), `type` into menus,
a door panel swinging into a corridor, `fnv_flow call` and depth 1, `fnv_blame form`, several `log` expressions.

## Agent tools: testing and debugging with no one at the keyboard

The point is a closed loop: an agent changes a mod, runs it, sees what the game did, and finds out why,
without a person clicking. Where the game has a function or a data structure for it, the tools use that.

- **fnv_trace** hooks a proven function (status proven or class proven only) with the companion's observe-only
  watch hook (see "Watch hooks" for its one exception) and decodes each call with the decomp signature: `this`, stack and register arguments by calling
  convention (fastcall: integer and pointer arguments in ecx/edx), forms named with RTTI and the plugin
  indexes, the result (float results are in st0, not recorded), the caller named from the engine map. The
  ring keeps 4096 calls; `overwritten` says how many fell out between reads. Example: in two seconds
  `TESQuest::RunScript` ran for every running quest, all from `TES::RunScripts` (proven) on the main thread.
- **fnv_screenshot** calls the engine's `SaveScreenshot(path)` (0x00878860; a decomp name, identified by its
  own log strings, and checked against the unpacked exe's bytes). It copies the back buffer, so it works
  when the window is covered. It needs the device to allow locking the back buffer (`bAllowScreenShot=1`,
  and multisampling off or a driver that allows it); otherwise the tool falls back to a window capture.
- **fnv_menu / fnv_ui_do** read `InterfaceManager` (0x011D8A80, read by `InterfaceManager::Instance`,
  proven): mode +0x0C, active menu +0xD0, menu stack +0x114, and the visibility byte per menu id
  (0x011F308F + id). The message box is `MessageMenu` (0x011DA4F0): its tiles are found by name (`MM_Title`,
  `MM_MessageText`) and the buttons are the items of its list box (+0x40), each label the `ListItemText`
  child's string. A press calls `MessageMenu::HandleTileSelection(7, button tile)` (class proven), the call a
  click makes: it stores the button's value where `GetButtonPressed` reads it (0x0118C684), runs the
  message's callback and shows the next queued box. Close uses `MessageMenu::Close` (proven). The active-menu
  field stays empty for message boxes, so the top of the menu stack is taken as the open menu.
- **fnv_condition_eval** reads the condition lists where each record keeps them (decomp layouts): INFO +0x18,
  PACK +0x40, PERK +0x40 and its entry-point entries (conditions array +0x10, one list per tab: tab 0 is the
  perk owner, other tabs' subjects are given), effect items +0x1C (effect list after the `EffectItemList`
  vtable, checked by each item's `EffectSetting`), QUST +0x54, stage log entries, objective targets (run on
  the target ref), RCPE +0x34, IDLE +0x30, MESG buttons (+0x30, conditions +0x08), CPTH +0x18. Each
  condition runs with the game's own condition function from the command table, combined like
  `TESConditionItem::ContainsTrait`.
- **fnv_quest action=targets** follows how the game builds markers: the quest must run and be the player's active
  quest (`PlayerCharacter` +0x6B8), the objective must be displayed and not completed and in the player's
  list (+0x6BC), the target ref must exist and its conditions pass (subject: the target ref). Targets in
  another cell point at the first door of the path the game built (target +0x20). It also says whether the
  game's own target cache (+0x6C4) holds the target.
- **fnv_script_instances** walks what `TES::RunScripts` (proven) walks: the quest list
  (`TESDataHandler` +0x118; quests with the enabled bit run their script) and, for each loaded cell (the
  interior, else the grid from `TES` +0x08), the cell's scripted-ref list (loaded data +0x4C). It reports
  `scriptsRunning` from the byte `Script::GetProcessScripts` (proven) returns.
- **fnv_rng** writes the state `MersenneTwister::Seed` would leave (mt[0] = seed, mt[i] = mt[i-1] * 69069,
  index 624) into the one generator `BSRandom::QInstance` (proven) returns; leveled lists, VATS, combat, AI,
  lockpicking and `GetRandomPercent` all draw from it, on every thread. Rolls in the same request repeat
  exactly; across frames other systems keep drawing numbers.
- **fnv_time** reads the calendar (0x011DE7B8: year, month, day, hour, days passed, timescale globals).
  `globals` mode moves the clock forward with the game's month lengths and sets the calendar's dirty byte,
  then re-evaluates nearby actors' packages. `engine_wait` calls the game's one-hour wait step (0x0094DF80,
  unnamed, so it needs `allow_unproven`) once per hour: process lists catch up, healing and hardcore needs
  run, as when the player waits. The step counts the player's wait counter (+0x654) down and fully heals at
  0, so the tool keeps it above 0 and restores it. Time changes are not undone: the game never runs its
  clock backwards; load a test save to go back. (A pathfinding crash followed a rewind once; later crashes point
  to the 2 GB address space instead, see "Tested on".)
- **fnv_leveled_roll** gets the list with `TESLeveledList::GetFormAsLeveledList` and rolls it with
  `TESLeveledList::CalculateCurrentForm` (both proven) at the level asked, nested lists resolved.
- **fnv_move** uses `MoveTo`, `coc`, `SetPos` / `SetAngle` and JIP LN's `MoveToCell` (FNV has no
  `PositionCell` or `PositionWorld`); undo moves the ref back with `MoveToCell` and its old heading.

## Engine AI tools: detection, relations, combat, perks, paths, rays, cells

Built by reading the game's own code in the decomp, including functions nobody had named. Each result lists
the functions it used and their status (`uses`); each was checked in the running game against what the game
itself reports.

- **fnv_detection** reads the observer's committed detection list through the process vtable
  (`HighProcess::GetDetectionState`, +0x504) and decodes each `DetectionViewData` (level, score, line of sight
  in the cone and in any direction, last seen position). With `recompute` and an NPC observer it calls
  `Actor::CalculateDetectionLevel` (0x008A0D10) with `calc=1` and no record writes, while a hook on the
  detection formula (0x00642ED0, 25 arguments; the decomp calls it `ComputeDamageResult`) captures the inputs
  the game passed. Arguments 8 and up are read with the companion's stack snapshot (selector 14). The argument
  names come from the formula's call site. The player as observer is never recomputed (that path writes
  records).
- **fnv_reaction** calls `Actor::IsHostileTo` (0x008B06D0) as `GetShouldAttack` does, the faction fight
  reaction (0x008B87A0), `Actor::GetFactionReaction` (the deciding faction pair), `GetDisposition`,
  `IsAngryWithPlayer` and `IsInCombatWithActor`, and re-derives which branch of `IsHostileTo` decided
  (teammates, frenzied, crime, already fighting, aggression against the fight reaction).
- **fnv_combat** gets the controller with `Actor::GetCombatController` and reads it: +0x84 procedure, +0x88
  queued procedure, +0xA8 action, +0xAC goal, +0xBC self, +0xC0 target, +0xF0 weapon, +0xF4 style. Procedure,
  action and goal are named by their RTTI class (`CombatProcedureAttackRanged`, `CombatGoalKillTarget`...).
  The combat group's targets (+8, 0x68 bytes each) and members (+0x18) follow.
- **fnv_perk_entries** hooks `ApplyPerkModifiers` (0x005E58F0, ep and actor and the value before) and
  `BGSEntryPointFunction::ApplyFunction` (0x005E5B40, one call per matching entry), matches each apply to its
  check by thread and time, and finds the perk by scanning the player's perk ranks (+0x87C) for the entry's
  function data. `evaluate` calls the dispatcher itself with a scratch value. Only the player (and teammates,
  through the player) has perk entries; abilities that change actor values (Strong Back) are not entry points.
- **fnv_pathing** reads `Actor+0x190` (ActorMover: state, path failed, request, solution, handler), the
  `PathingRequest` (start and goal locations, door rules), the `PathingSolution`'s detailed nodes (0x3C bytes,
  position at +8) and the `DetailedActorPathHandler` (state, avoidance, blocked). The request's locations are
  never resolved to a triangle (the builder resolves its own copies), so only "no navmesh at all" is reported
  from them. The failure reason code is not stored; trace 0x009DC5E0 (`HandlePathResult`) to catch it.
- **fnv_raycast** builds a `bhkPickData` in game memory and calls its constructor, `SetFrom`, `SetTo`,
  `SetFilter` (fastcall: the layer goes in EDX) and `TES::Pick` (0x00458420). The hit fraction is at +0x40 and
  the hit collidable at +0x80 (Havok's ray-cast output at +0x30); `FindReferenceFor3D` names the reference.
  `ground_at` calls `TES::GetLandHeightAt`.
- **fnv_cell_grid** reads `TES` (grid +0x08, center +0x24, interior +0x34, buffers +0x38/+0x3C), the
  `GridCellArray` (dimension +0x0C, cells +0x10) and each cell's state byte at +0x26
  (`TESObjectCELL::GetState`; the decomp's helper is misleadingly called `GetByteAtOffset38`).

## Phase 2 tools (2026-11-07 night)

Built from the decomp (three Sonnet research passes over the main loop, VATS, scene graph, UI tiles, dialogue,
memory and world edits) and checked in the running game on the dev profile:

- **Behaviour hooks** (`fnv_hook_set`): the companion's `hookact` op adds an action to a watch hook. skip returns
  through a per-hook stub (`mov eax, value` or `fld dword [value]`, then `ret n` with the bytes the calling
  convention pops), result replaces eax at exit, arg rewrites a stack word or ecx/edx before the trampoline, script
  queues a console line for the main thread (at most 8 per frame). A filter and a hit limit apply to every action.
  Checked: Actor::GetLevel skipped with 42 for the player only (engine call and `player.GetLevel` gave 42, a gecko
  kept 7), result 30 with a player filter, fnv_undo removing the hook.
- **Patches** (`fnv_patch`): `B8 05 00 00 00 C3` over GetLevel's first six bytes made every actor level 5; a second
  patch with a wrong `expect` was refused; fnv_undo restored `55 8B EC 51 89 4D`.
- **Tracepoints** (`fnv_trace stack=true`): the hook record's snapshot area holds up to 24 stack words that point into the
  exe (`HF_STACK`); the bridge keeps those preceded by a call instruction. GetLevel on the gecko: 67 hits, all from
  CalculateDetectionLevel (0x008A0D10). Deeper frames can be stale stack values.
- **fnv_engine_get**: Actor::GetLevel on the player returned 1.
- **fnv_globals**: TimeScale 30 set to 60 (`GetGlobalValue` agreed) and undone.
- **fnv_dialogue**: a conversation with a spawned Sunny Smiles: GREETING, the line on screen, quest
  VFreeformGoodsprings and her choices in order.
- **fnv_memory action=address_space**: 1856 of 2048 MB of address space used after loading the test save, largest free block 14 MB; 133552
  forms; 18 high-process actors. Windows reserves the region above 2 GB for a non-large-address-aware 32-bit process,
  so the walker stops at the limit the loaded image's header gives.
- **fnv_world_edit**: transform (position, Z angle, scale 2.0), duplicate, disable on a gecko, then undo of all three;
  HVDustStorm forced (seen in a screenshot) and undone (current weather back, override cleared).
- **fnv_scene_graph**: the player's 97 nodes (BSFadeNode, NiNode, BSFaceGenNiNode, NiTriShape, NiTriStrips), skinned
  shapes with vertex and triangle counts, PPLighting texture sets (HandMale.dds, _n, _sk), locomotion sequences, BSX
  flags. Gamebryo classes have no MSVC RTTI: their names come from NiObject::GetRTTI (vtable slot 2).
- **fnv_menu tiles**: HUDMainMenu with 401 trait names read from the game's own trait map (0x011F32F4).
- **fnv_vats_odds**: 9mm pistol on a gecko at 210 units: head 0.4907 from the engine, and the recomputed terms give
  49.07% too; the torso term is 147% and caps at 95. The menu number uses single precision (Direct3D's x87 control
  word), so 0.95 shows as 95. Not yet compared with an open VATS menu: injected input did not open VATS here.
- **fnv_profile action=frame**: 60 fps, 16.4 ms frames, render 13.7 ms (vsync wait included); with iNumHWThreads 12 the AI
  and process-list children run off the main thread and are listed as not called.
- Offline: record users of GSSunnySmiles (her ACHR, 159 dialogue conditions, a package, a quest) and Caps001 (the
  first index of the whole load order takes about 10 s); conflicts in the vanilla DLC set (76 with lost changes);
  6 missing assets in HonestHearts.esm; profile_set dry run.

Hazard seen: reverting the session right after closing a conversation with the spawned Sunny Smiles copy (disable and
MarkForDelete of her and of a spawned gecko in one frame) crashed the game in
Script::RunDelayedScriptActionsOnReferences (0x005AA720) with an illegal instruction. The undo lines come from the
spawn tool; the cause is not pinned down.

## Saves: fnv_save_inspect and fnv_save_diff (offline)

Read a `.fos` exactly as the game writes it (format from `BGSSaveLoadGame::SaveGame` 0x00847850 and the
change-form writers): header, thumbnail, plugin list, player location, every global variable and the
change-form table with each form's change flags by name. Reference positions (initial data) and quest bodies
(flags, stages done with log dates, objectives, script variables) are decoded; quest script variables and
globals are named from the plugins' records. `fnv_save_diff` compares two saves: header, plugins, player
location, globals, and forms added, removed or changed (decoded fields compared one by one, other bodies byte
for byte). Every value in a save buffer is followed by a `|` spacer; form ids are 3-byte indexes into the
save's form id array (0x800000 = created in game).

## Building a mod without GECK

geck-mcp-nv's builder emits a script uncompiled until its compiled-script cache (`state/scripts/<project>.json`)
has it; normally GECK fills that cache. `fnv_scripts_compile` fills it from the running game instead: with the
last build installed and loaded, it compiles each spec script in game, drops the scriptname statement xNVSE
writes before compiling (GECK's bytecode starts with a single one), maps runtime form ids to the plugin's
master-relative ids, builds `SCHR`/`SCDA`/`SCTX`/`SLSD`/`SCVR`/`SCRO`/`SCRV`, checks them with the builder's own
`verify_script`, and stores them with an in-game provenance tied to the build's compile context. The next
`geck_plugin_build` includes them. Scripts with `Quest.variable` references still need GECK. The worked
example is [examples/hello-quest](../examples/hello-quest/README.md).

Dialogue: a `DIAL` record in the spec takes `infos`, lines with `key`, `quest`, `responses`, `conditions`, `choices`,
`flags` and a `result` script (`geck_mcp/esp/dialogue.py` has the format). `"override": true` on a master topic
(`GREETING`) adds lines to it; the topic is copied from its template dump (`tools/esm_research.py dump`).
`fnv_scripts_compile` compiles the result scripts too.

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
- Over MCP from a closed game: `fnv_test hello-wasteland.yaml` launched the game and passed in 9 s;
  `fnv_test run_all` on the three examples took 34 s (one pass, the broken one failed with the actual
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
- `fnv_threads action=hang`: main thread sampled 3 times, in `Renderer::PresentFrame` (vsync) each time.
- `forced-crash.yaml`: the report now carries the companion's context (main thread, read of 0x1C, the op).
- Every quit leaves a Crash Logger log on the test machine, with the companion off too: a plugin faults in
  its exit cleanup (`ExitProcess` in the call trace). The crash doctor marks such logs `duringShutdown`.

Milestone 8 (2026-11-07):

- `fnv_loadorder`: 11 plugins (FalloutNV.esm, 5 DLC masters, 4 packs, GunRunnersArsenal, HelloWasteland.esp).
- `fnv_record action=find Trudy`: `GSTrudy` (NPC_), `TrudyRef`, her note, topics, packages and script, across the
  load order, 3.2 s including the first index build.
- `fnv_record Weap10mmPistol`: defined in FalloutNV.esm, winner GunRunnersArsenal.esm, which changes
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
`fnv_threads action=hang`); other cells take 10-15 s.

Dialogue, AI packages and live diffs (2026-11-07, a fresh game started in the Prospector Saloon):
- `fnv_dialogue action=tree TrudyRef`: 5579 GREETING responses checked in about 10 s; 10 kept for Trudy (her 7
  Goodsprings lines plus 3 shared ones), the engine and the per-condition check agreeing on every one;
  `wouldSay` her say-once greeting ("Well, you've been causing quite a stir...", `GetDeadCount JoeCobb < 1`).
- `fnv_dialogue_choose`: Trudy's menu listed 7 options with their info ids; "Does Goodsprings get many
  visitors?" played her two lines, "Goodbye." forced the goodbye and the menu closed.
- `fnv_ai_packages` on a Goodsprings settler: `GoodspringsSettlerSaloonPackage` expected and running
  (`matchesEngine`); the sleep package out of schedule ("runs 22:00 for 8 h, it is 12.7"), flee and ambush
  packages failing their quest-variable conditions. On Trudy, early in a new game: no package eligible, every
  one gated on a Goodsprings quest variable.
- `fnv_form_live_diff GameHour`: plugin 12.0, live 12.81; `GSTrudy`: name and script unchanged.
  `WeapNV9mmPistol`: name, value 100, health 150, weight 1.5 and damage 16 all equal to the record; after
  `fnv_form_edit iValue 999` it reported value 100 -> 999, and nothing after `fnv_undo`.
- About 15 s after that conversation, with Trudy force-enabled while no package of hers was valid, the game
  crashed in a pathfinding tasklet (`RouteBuilder::BuildRoute` > `AStarSearch` > scrap heap); no request was
  being served. A state normal play never makes; not traced further.

- Agent tools (2026-11-07, Goodsprings save): `fnv_menu` read the Gun Runners' Arsenal message box (title,
  text, OK); `fnv_ui_do message` pressed OK and the next queued box appeared; on the hardcore prompt
  (`ShowMessage 0011E661`) "No (Recommended)" and "Yes" were pressed by label and `GetButtonPressed`'s byte
  became 0 and 1 (hardcore stayed off: the result belonged to the console). `fnv_trace TESQuest::RunScript`
  decoded every quest and named the caller. `fnv_watch` sampled at 250 ms with frame numbers.
  `fnv_script_instances`: 229 running quest scripts, 25 loaded cells, 68 scripted refs. `fnv_quest action=targets`:
  VMQ01 listed every reason its markers are hidden; the active Old World Blues quest's target was shown and in
  the game's cache. `fnv_condition_eval`: the Cowboy perk's skill requirement failed (15 against 45), the
  Super Stimpak's effects split on `IsHardcore` / `HasPerk`. `fnv_leveled_roll Ammo5mm75`: nothing in 23% of
  300 rolls against chance none 25%; the same seed gave the same rolls twice. `fnv_time` advanced 2 hours
  (globals) and 2 hours (`engine_wait`); `fnv_place at export=false`, `fnv_move` and their undo; `fnv_test_save` save, list
  and load. The engine screenshot worked with the window in the background. A YAML test with every new step
  passed, 17 steps, unattended; a failing one carried the debug bundle; `trace_script` listed the four lines
  of the Hello Quest script that never ran.
- About 40 s after a test advanced the clock and its teardown set it back, the game crashed in low-process
  pathfinding (`LowProcess::ProcessPatrol` > `Pathing::FindPathDistance` > door search > scrap heap), the same
  family as the crash after Trudy was force-enabled. Since then time changes only go forward.
- One fresh start hung before the main menu (main thread waiting in ntdll under
  `LoadedNode::ReadFromStream`, 4 s of CPU, no hooks installed); `fnv_game restart` terminated it and the next
  start was normal (14.6 s to the save).

- Engine AI wave (2026-11-07 evening): `fnv_detection` on Sunny Smiles against the player: the recomputed
  score (-12, lost) matched the record the game keeps; the formula inputs matched the game (distance =
  `GetDistance`, light 100 = the process light, levels 2 and 1). `fnv_combat` on a gecko fighting a bighorner:
  target = `GetCombatTarget`, goal "hide from target", six geckos in its group, two group targets.
  `fnv_reaction`: very aggressive against neutral gave hostile, as the rule says. `fnv_raycast` straight down
  hit the terrain at 8368.3 where `GetLandHeightAt` gives 8368.31, and hit the player's spine bone above the
  player. `fnv_perk_entries evaluate 9` with Swift Learner: 250 XP became 275 (multiply 1.1), as the game
  computed. `fnv_pathing` on a raven: six detailed path points from its position to its goal. `fnv_cell_grid`:
  the 5x5 grid around cell (-17, 0), all attached. `fnv_save_inspect` / `fnv_save_diff` on the dev saves: all
  4507 change forms read, quests named (`NVDLC03MQ00` running, stage 10 done).
- The game crashed four times on 2026-11-07 (14:50, 15:49, 16:22, 20:51), each in an engine worker thread
  (pathfinding tasklet or AI linear task thread), three writing through a null pointer inside the scrap-heap
  allocator, one with "not enough memory" as the last error; two happened with no tool request or hook
  active. The game's exe is not large address aware (2 GB address space) and used 1860 MB of it right after
  loading the test save with all DLCs. Running out of address space explains these crashes better than the
  clock rewind blamed earlier. The usual fix is a 4 GB patch of FalloutNV.exe (the community "FNV 4GB
  Patcher"); it is not installed on the test machine.

## Launch notes

- MO2 is started with ShellExecute. A detached `CreateProcess` gave the game no window focus and its
  renderer failed to start ("Failed to initialize renderer").
- The Python MCP client puts its server in a kill-on-close job object without breakaway; inside it MO2's
  hooked start of `nvse_loader.exe` fails with `ERROR_ACCESS_DENIED`. The bridge detects such a job and then
  opens a launch shortcut through Explorer instead. Claude Code's launcher (libuv) allows breakaway.
- From the shell tools of a Claude desktop session, MO2's start of `nvse_loader.exe` failed with
  `ERROR_ACCESS_DENIED` too, although the enclosing jobs allowed breakaway (a test child started with
  `CREATE_BREAKAWAY_FROM_JOB` came up fine); cause not found. Starting the MCP client through WMI
  (`Win32_Process.Create`) launched the game normally.
- MCP clients may pass the server a short allow-list of environment variables; MO2's `basic_games` plugin
  then fails on a missing `ProgramW6432`. The bridge adds the user's missing standard variables
  (`CreateEnvironmentBlock`) before launching, never overriding any.
- A leftover `plugin_loadcheck.tmp` (from an MO2 that was killed) makes MO2 stop at a "Plugin error"
  dialog; when MO2 is not running the bridge moves it into `state/fnv/mo2-backups`.
- An MO2 of the dev instance left running without the game (after a crash) takes the next launch request,
  and a failed launch leaves it behind a modal "Cannot launch program" dialog that nobody clicks. Before a
  launch the bridge closes such an MO2 (WM_CLOSE, then terminate); while waiting for the game it watches for
  MO2's blocking dialogs and fails at once with `MO2_LAUNCH_FAILED`, closing the dialog.

## Credits

- [xNVSE](https://github.com/xNVSE/NVSE): the plugin interface, console/script/array interfaces, script
  commands used by the reads, the extra data type list, and the `ActorHitData` field offsets, body part
  names and hit flags `fnv_damage_log` decodes (`GameProcess.h`, `GameForms.h`). xNVSE has no licence file;
  nothing of its code is copied, only interface declarations (`native/runtime/nvse_abi.h`, upstream
  geck-mcp-nv), offsets and names.
- [JIP LN NVSE](https://github.com/jazzisparis/JIP-LN-NVSE): `CloseActiveMenu`, `MoveToCell`, and the layouts of
  `MagicTarget`, `ActiveEffect` and `EffectItem` and the control codes used by `fnv_effects` and
  `fnv_input` (offsets only, checked against the decompilation's class sizes); the `TES` singleton address
  and its grid and interior fields used by `fnv_script_instances`.
- xNVSE's menu ids, `Tile` / `Tile::Value` layouts and the data handler address (`g_dataHandler`), used by
  `fnv_menu` and `fnv_script_instances` (offsets and names only).
- The Fallout: New Vegas decompilation and fnvsym's proof table: addresses, layouts and proven names.

## Live checks of the leftovers (2026-11-12)

Every item the completion pass left as "built, checked offline" was run in the game (Prospector Saloon, the Hello
plugins). Five were wrong and are fixed; one was removed. Tests: 858 pass.

- `fnv_game launch start=cell:` **removed**. SStartingCell in the INI crashes the retail game at startup in every cell
  tried (a plugin interior and GSProspectorSaloonInterior): TES::LoadInterior -> LoadingMenu setup 0x00788AB0 ->
  Tile::Set on InterfaceManager's root tile, still null because the main menu builds it. `coc:` (through the main
  menu) reaches a cell in 11 s. A `cell:` start now answers CELL_START_UNSUPPORTED with the `coc:` to use.
- `fnv_ui_do type` **fixed**: calling PlayerNameEntryMenu::Finish 0x007AB9A0 on the TextEditMenu crashed the game
  (Finish belongs to the name entry menu object, whose +0x28 is the player's NPC). The TextEditMenu is generic: its
  Done handler 0x007E66A0 copies its string (+0x34) to 0x011DAED8 and closes it, and the name entry menu takes the
  text on its next update (PlayerNameEntryMenu::UpdateDialog 0x007AB7E0). Run: the menu opened through the engine's
  queue (9 written to 0x011D8950, HandleQueuedMenuOpen builds "Player Name Entry Menu"), `type` "Courier Six", the
  menu closed and the player's base form is named Courier Six.
- `fnv_anim idle_tree` **fixed** (was approximate and wrong: it named SpecialIdle_Shrug for every actor). The answer
  now is the engine's own IdleAnimationManager::FindIdleFormForObject 0x00600950 (what HighProcess::StartIdleAnimation
  0x008DAB40 asks), with the actor's replay delays (animation +0x134). The walk uses the same folder as the engine:
  TESObjectREFR::GetModelFilePath 0x005715D0 -> BuildIdlePath 0x006007F0 -> GetFormArrayForIdleDir 0x00600560 (20
  roots for a male NPC, not the one-entry array GetIdleArray gives for the base form). Seen: Easy Pete (sitting) 0006F8C1
  from engine and walk alike; Sunny and a settler "no idle": the walk reaches a group with no file of its own, which
  the engine's last check drops (reported as walkAnswer with a note).
- `fnv_systems barter` **fixed** and compared with open barter menus: 28 of 28 prices equal (14 the player sells to
  Sunny, 14 Trudy sells). The value now comes from the player's inventory entry (ExtraContainerChanges via
  GetByType 0x00410220; entry value 0x004BD400 with condition and weapon mods: a worn 10mm pistol is 1,197, not
  1,200) or, for items not carried, the form (0x0048E8A0, which also prices ingestibles; the console's GetValue
  gives 0 for them). Rounding is half up (0x004BD510; Python's round() is not), and the menu holds a buy price at or
  over the value and a sell price at or under it (0x0072ED00).
- `fnv_code compare mode=shadow` **fixed** and run: FUN_00404010 (max of two floats): 200 live calls in 3 s, 39
  distinct, all agree; with max turned into min, 37 of 38 differ (original 5, new 1 for 1 and 5). The replay passed
  the recorded ecx as `this` to every function, which a cdecl function takes as an extra first argument (the
  originals then read 32916.06 for max(1, 5)); `this` now goes only to thiscall and fastcall functions.
- Short-function swaps run: FUN_00a62660 (4 bytes, an NiTexture getter): 19 call sites and 3 vtable slots
  redirected, frames kept coming, unswap put all 22 back (checked byte by byte). **Fixed**: the swap list lived only in
  the bridge process, so a restarted MCP server could not undo a swap the game still had. It is now kept in the
  bridge state (`fnv/code/swaps.json`) with the game's pid and start time; a new game process starts empty.
- `fnv_script_debug step over|line|out` run on HelloBugQuestScript and VCG01SCRIPT. `out` **fixed**: the script
  runner is a stack object, so scripts the game runs one after another reuse its address and "a different runner"
  never came (4,096 statements, then STEP_LIMIT). A run now also ends when another script runs or the same script
  starts again (its offset goes back). Also **fixed**: after the last break was cleared the EvaluateLine hook stayed,
  recording every statement of every script for the rest of the session (1.46 million records seen); it is now
  removed unless `fnv_script_log commands` started it. Breaks and the stop live in the server process: a restarted
  server drops its hooks (a held game resumes). Inside a block the runner skips (a MenuMode block outside that menu)
  the stop's opcode field showed the previous statement's opcode; the line mapping was right.
- `fnv_audio subtitles`: a spoken line outside dialogue gets a subtitle only when General Subtitles is on
  (bGeneralSubtitles 0x011D8928, value at +4, off by default; VoiceHost::PlayVoiceTopic 0x00578A80 checks it). The
  result now says when it is off, and `enable=true` turns it on for the recording only (seen on, then back to 0). No
  subtitle was caught: after Sunny's first Say (her greeting voice file played), no NPC spoke again on Say or SayTo
  (PlayVoiceTopic was never called; the hooks themselves recorded 752 calls of a test function), so the capture of a
  subtitle is still not seen.
- `fnv_asset reload .kf` with `ref=`: Sunny's animation object was rebuilt (0x1C5D2888 -> 0x1B293158) with her 287
  groups; without `ref` only unused copies are dropped.
- **Rays were broken everywhere** (found while building surface alignment): bhkPickData::SetFilter 0x004A3F70 takes
  its filter on the stack (`mov [ebp-4],ecx`, the filter read at `[ebp+8]`, `ret 4`), but both ray callers
  (fnv_hands, fnv_engine_ai) passed it in edx, so the filter was whatever the stack held (seen: 0x05EAC25D, layer
  93) and rays missed indoors and out. Fixed: a downward ray now meets the saloon floor at the player's feet (3,456)
  and a sight line to Sunny stops on the wall between (NVGSWallONLY). `fnv_raycast`, `fnv_place snap/check`, the walk
  planner and `fnv_physics query` all use these rays. Outdoor Havok coordinates have no origin shift (a crate's
  body is its position / 6.9991).
- `fnv_place snap align=true` built: the slope from five rays 24 units apart (the pick output has no normal), then
  SetAngle X and Y. Measured from the 3D's world rotation (+0x68; translation +0x8C): the up axis is
  (-sin Y, sin X cos Y, cos X cos Y) whatever the heading, so X = asin(n_y / cos Y), Y = -asin(n_x). Three crates
  on a 22 to 46 degree slope at headings 0, 37 and 200: up axis = surface normal (dot 1.0). A side that rises
  more than 96 units over the 48 is a step or a wall and is left out (a 71 degree rock face was laid flat that way).
- `fnv_script_debug` **fixed**: the script's variable list is embedded in the Script at +0x4C (its first node), not a
  pointer there, so every variable name came back empty (vars, set by name). Types come from ScriptVariableInfo
  +0x10 (0 float, 1 integer); a local variable is 16 bytes and has no type byte (the old "string" flag read past
  it). **Built**: a `when` that names script variables (`fTimer >= 0 and bChooseSex == 0`), checked by the bridge at
  each stop because the predicate language cannot read doubles; a stop whose condition is false is passed on like
  the refused save/load stops (VCG01SCRIPT: false condition, 2 then 4 refused stops and no stop; true condition,
  a stop with the 12 variables named; an unknown name lists the script's variables). The stop's opcode is read
  from the compiled script at the stop's offset - 4 (it equals arg1). In VCG01SCRIPT's inactive MenuMode 1036
  block the statement counter still runs one ahead of the source mapping; not resolved (that block was not
  running, and no stop in an active block of that script came while checking).
- `fnv_audio subtitles` caught live: "Evening." from a spawned Trudy greeting the player, through the tool with
  `enable=true`. The text is a temporary string, so the tool now reads it while recording (records polled every
  0.15 s); read after the recording it was garbage. The call carries no speaker form (argument 1 is the sound id,
  argument 2 part of the voice handle), so the result is text, frame and sound id. `Say` on an NPC that has already
  spoken does not reliably speak again; a fresh NPC's own greeting does.
- MCP protocol: an error the SDK writes itself (arguments failing the input schema, an unknown tool) now carries the
  same structured result as the tools' errors (BridgeServer in server.py), so every result matches the declared
  output schema (checked over raw stdio, 8 calls including errors). `fnv_globals`, `fnv_hud`, `fnv_physics` are
  annotated as acting on the game.
- Code reloads without a restart: `run_mcp.py` is now a supervisor that reloads the server when `geck_mcp/`
  changes and sends the client a tool-list change (MCP-QUICKSTART.md, "Code changes reload by themselves"). Checked
  in the Claude app (Claude Code 2.1.321): the app speaks 2026-07-28 (`server/discover`, then `subscriptions/listen`
  for tools, prompts and resources), the server declares `tools.listChanged`, reloads swap the process between calls,
  and the stamped notices reach the listen stream. The app never sends `tools/list` itself: it keeps a saved tool
  list, which is why new tools needed a reconnect. On connections the app re-made after the server process was
  killed, it did not refetch on the notice; whether it refetches on a session's first connection (the normal case
  now) is not yet checked. The supervisor writes its events to `state/supervisor.log` (the app shows any server
  stderr as an error, so routine events go only to that file).
- Still to run: `fnv_bisect` (mods, then records), from the MCP server (the in-process runner cannot launch through
  MO2).
