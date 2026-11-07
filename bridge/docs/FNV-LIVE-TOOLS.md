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
| `fnv_record_users` | read | Everything that references a record: placed refs, scripts (SCRO), leveled lists, containers, conditions (typed by the game's condition-function table), packages, form lists, any form field; whether the winning version still does |
| `fnv_conflicts` | read | Records several active plugins edit, worst first: plugins in load order, winner, subrecords each override changes, earlier changes the winner loses |
| `fnv_asset_check` | read | Meshes, textures, icons and sounds a plugin names that no enabled mod, the Data folder or any BSA has; textures inside its loose meshes too |
| `fnv_profile_set` | control | Enable or disable MO2 mods and plugins in the dev profile (game closed), with a backup of the three profile files |
| `fnv_record_show` | read | One record decoded (strings, form references with EditorIDs, script header and source, common DATA layouts), every version in load order, and what each override changed |
| `fnv_script_lint` | read | Script source checked offline: structure, block types, variables, unknown commands, argument counts, EditorIDs of the wrong type or in no plugin |
| `fnv_compile_ingame` | read | Compile with the running game's compiler: its errors with line numbers, or the variables, references, statements and bytecode |
| `fnv_script_hot_swap` | act | Replace a running script's code with a new compile; instances keep their variables; undoable |
| `fnv_scripts_compile` | control | Compile the project spec's scripts with the running game into geck-mcp-nv's compiled-script cache, so `geck_plugin_build` emits compiled scripts without GECK |
| `fnv_dialogue_tree` | read | The responses a speaker could give for a topic (GREETING by default): prompt, quest and priority, flags, response text, every condition with its value, the engine's verdict, and the response it would pick now |
| `fnv_dialogue_choose` | act | Talk to an NPC and pick responses by index, prompt text or info id; each step's options, the pick and the answer |
| `fnv_ai_packages` | read | An actor's packages in order: type, flags, schedule against the game clock, location, conditions with values; which one runs, which are eligible or shadowed, and why the others are not |
| `fnv_form_live_diff` | read | A form in memory versus its winning plugin record (name, value, weight, health, damage, script, global value, quest data): what changed at runtime |
| `fnv_trace` | read | Calls to any proven engine function while the game runs: `this` and arguments decoded with the decomp signature (forms named), result, time, caller by name; counts by caller and `this`; filter by `this`, caller or a parameter |
| `fnv_watch` | read | Values sampled over time (expressions, globals, actor values, positions, memory fields by class layout): first, last, min, max and a timeline of changes with frames; stops early on `until` |
| `fnv_screenshot` | read | The current frame as PNG, returned as an image: the engine's own back-buffer capture (`SaveScreenshot`, works with the window covered), else a window capture |
| `fnv_test_save` | act | Named test saves (`fnvmcp_test_*`) in the dev profile: save, load (waits for the game's post-load message), list, delete |
| `fnv_crash_history` | read | Every crash log explained and grouped by signature: count, first/last time, likely culprits, new signatures flagged, crashes during exit cleanup marked |
| `fnv_move` | act | Teleport the player or a ref to a ref, a cell (coc, or JIP `MoveToCell` with a position) or a position; waits for the cell to load; undoable |
| `fnv_spawn` | act | Place a base form near a ref (`PlaceAtMe`) and return the new refs; undo disables and deletes them |
| `fnv_condition_eval` | read | Every condition list of a record (INFO, PACK, PERK and its entry tabs, SPEL/ENCH/ALCH/INGR effects, QUST quest, stage log entries and objective targets, RCPE, IDLE, MESG buttons, CPTH) evaluated against chosen refs, with what fails |
| `fnv_quest_targets` | read | Each objective's targets: ref, cell, position, target conditions, the door a marker points at, the player's target cache, and every reason a marker is not shown |
| `fnv_script_instances` | read | Running quest scripts and the object scripts of every loaded cell (the lists `TES::RunScripts` walks), variables, instance counts per script, and whether scripts run at all |
| `fnv_menu` | read | Interface mode, active menu, menu stack, visible menus by name, loading screen, and an open message box with title, text and buttons |
| `fnv_menu_control` | act | Press a message box button by index or label through the engine's click handler (`GetButtonPressed` sees it), close the open menu, close all menus |
| `fnv_rng` | act | The game's one Mersenne Twister: state, or seed it like `MersenneTwister::Seed` |
| `fnv_time` | act | Read the calendar, jump to an hour, advance hours (day/month/year roll over), change the timescale; `engine_wait` runs the game's own one-hour wait step |
| `fnv_leveled_roll` | read | Roll a leveled list many times with `TESLeveledList::CalculateCurrentForm` at a level: share of each form and count, and of nothing; repeatable with a seed |
| `fnv_detection` | read | Why an NPC sees someone or not: the detection records on its process (lost/unseen/noticed/seen, score, line of sight in the view cone), both directions, light, sneak, distance, settings; for NPC observers the game's `CalculateDetectionLevel` recomputes and the 25 inputs of its detection formula are captured |
| `fnv_reaction` | read | How two actors relate: hostile or not (the engine's `IsHostileTo`, behind `GetShouldAttack`), fight reaction, the faction pair that decides it, disposition, crime anger, teammates, aggression, and the rule branch that decided |
| `fnv_combat` | read | An actor's combat controller: target, procedure, queued procedure, action, goal, weapon, combat style, and the combat group's targets (last known position, attackers) and members |
| `fnv_perk_entries` | read | Record perk entry-point checks (value before, each perk entry that applied, value after), or `evaluate` one value entry point now with a value and its filter forms |
| `fnv_pathing` | read | An actor's path: mover state, the pathing request (start, goal, navmesh, door rules), the solution's points, the path handler's state (waiting, blocked, avoidance failed) and the reasons found |
| `fnv_raycast` | read | A ray through the game's Havok world (`TES::Pick`) between points or refs: hit, hit point, hit object and its reference; `ground_at` gives the terrain height |
| `fnv_cell_grid` | read | Loaded exterior grid, current interior, interior and exterior cell buffers: each cell with its grid coordinates, load state and why it is loaded |
| `fnv_hook_set` | act | Change an engine function live: skip it with a result (float results in st0), replace its result, change one argument, or run a script line on each call; filter, hit limit, read the changed calls, journaled |
| `fnv_patch` | act | Bytes, NOPs, a ret or a jmp/call into the game's code; refuses over companion hooks and foreign patches (clean exe compare) unless forced; journaled with the original bytes |
| `fnv_tracepoint` | read | Record a function's calls that meet conditions, with arguments as objects, result, time, caller and the call stack; groups hits by path |
| `fnv_engine_get` | read | Call a proven getter (Get/Is/Has/Can/...) whose direct callees change nothing, with typed arguments; pointer results described |
| `fnv_globals` | read | Global variables from the data handler's list, filtered; set values (journaled) |
| `fnv_dialogue` | read | The conversation on screen: speaker, state, spoken topic and info, the line and name as shown, the choices in order with topic, info, quest and heard flag |
| `fnv_heap` | read | The game's address space (2 GB unless large address aware, largest free block), memory pools by use, form and process-level actor counts; growth over N seconds |
| `fnv_world_edit` | act | Move, rotate, scale, enable, disable, delete or duplicate a ref with the functions the console commands use; force or release weather; trigger or stop an image-space modifier; undoable |
| `fnv_scene_graph` | read | A loaded ref's node tree: classes (MSVC RTTI or Gamebryo NiRTTI), names, hidden flag, transforms, geometry counts, skinning, shader and texture paths, controllers and sequences, extra data |
| `fnv_ui_tiles` | read | A menu's live tile tree with trait values named by the game's trait table |
| `fnv_vats_odds` | read | VATS hit chance per target and limb from CombatFormulas::GetVATSChanceToHit and the ep 8 perk layer, the percent the menu shows, and the gun formula term by term |
| `fnv_frame_profile` | read | Time per frame by subsystem from timing hooks on the main loop's children, removed afterwards |

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
blocks, comments) or JSON; no YAML library is needed. Numbers with a leading zero (`00138562`) stay text, so
form ids can be written bare. Examples: `examples/fnv-tests/`.

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

## Dialogue, AI packages and runtime changes

Conditions are evaluated the way `TESConditionItem::ContainsTrait` (0x00681600) does it: pick the reference
the function runs on (subject, target, a reference, the combat target or the linked reference), call the
command's eval handler from the running game's own command table (`CommandInfo.eval` at +0x20, cdecl
`(ref, param1, param2, double &)`), compare with the value or global, and combine (an OR item joins the next
one; groups are ANDed). So each condition shows its own value, not only pass or fail. Handler calls are
batched, 250 per frame: Trudy's GREETING (5579 responses of every quest) takes about 10 s.

`fnv_dialogue_tree` asks the engine for each response's verdict with `TESTopicInfo::CheckCondition`
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
`TESScriptableForm`), plus `TESGlobal` and `TESQuest` data, and compares them with `fnv_record_show`'s
decoding of the winning record.

## Agent tools: testing and debugging with no one at the keyboard

The point is a closed loop: an agent changes a mod, runs it, sees what the game did, and finds out why,
without a person clicking. Where the game has a function or a data structure for it, the tools use that.

- **fnv_trace** hooks a proven function (status proven or class proven only) with the companion's observe-only
  watch hook and decodes each call with the decomp signature: `this`, stack and register arguments by calling
  convention (fastcall: integer and pointer arguments in ecx/edx), forms named with RTTI and the plugin
  indexes, the result (float results are in st0, not recorded), the caller named from the engine map. The
  ring keeps 4096 calls; `overwritten` says how many fell out between reads. Example: in two seconds
  `TESQuest::RunScript` ran for every running quest, all from `TES::RunScripts` (proven) on the main thread.
- **fnv_screenshot** calls the engine's `SaveScreenshot(path)` (0x00878860; a decomp name, identified by its
  own log strings, and checked against the unpacked exe's bytes). It copies the back buffer, so it works
  when the window is covered. It needs the device to allow locking the back buffer (`bAllowScreenShot=1`,
  and multisampling off or a driver that allows it); otherwise the tool falls back to a window capture.
- **fnv_menu / fnv_menu_control** read `InterfaceManager` (0x011D8A80, read by `InterfaceManager::Instance`,
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
- **fnv_quest_targets** follows how the game builds markers: the quest must run and be the player's active
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
- **Tracepoints** (`fnv_tracepoint`): the hook record's snapshot area holds up to 24 stack words that point into the
  exe (`HF_STACK`); the bridge keeps those preceded by a call instruction. GetLevel on the gecko: 67 hits, all from
  CalculateDetectionLevel (0x008A0D10). Deeper frames can be stale stack values.
- **fnv_engine_get**: Actor::GetLevel on the player returned 1.
- **fnv_globals**: TimeScale 30 set to 60 (`GetGlobalValue` agreed) and undone.
- **fnv_dialogue**: a conversation with a spawned Sunny Smiles: GREETING, the line on screen, quest
  VFreeformGoodsprings and her choices in order.
- **fnv_heap**: 1856 of 2048 MB of address space used after loading the test save, largest free block 14 MB; 133552
  forms; 18 high-process actors. Windows reserves the region above 2 GB for a non-large-address-aware 32-bit process,
  so the walker stops at the limit the loaded image's header gives.
- **fnv_world_edit**: transform (position, Z angle, scale 2.0), duplicate, disable on a gecko, then undo of all three;
  HVDustStorm forced (seen in a screenshot) and undone (current weather back, override cleared).
- **fnv_scene_graph**: the player's 97 nodes (BSFadeNode, NiNode, BSFaceGenNiNode, NiTriShape, NiTriStrips), skinned
  shapes with vertex and triangle counts, PPLighting texture sets (HandMale.dds, _n, _sk), locomotion sequences, BSX
  flags. Gamebryo classes have no MSVC RTTI: their names come from NiObject::GetRTTI (vtable slot 2).
- **fnv_ui_tiles**: HUDMainMenu with 401 trait names read from the game's own trait map (0x011F32F4).
- **fnv_vats_odds**: 9mm pistol on a gecko at 210 units: head 0.4907 from the engine, and the recomputed terms give
  49.07% too; the torso term is 147% and caps at 95. The menu number uses single precision (Direct3D's x87 control
  word), so 0.95 shows as 95. Not yet compared with an open VATS menu: injected input did not open VATS here.
- **fnv_frame_profile**: 60 fps, 16.4 ms frames, render 13.7 ms (vsync wait included); with iNumHWThreads 12 the AI
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

Dialogue, AI packages and live diffs (2026-11-07, a fresh game started in the Prospector Saloon):
- `fnv_dialogue_tree TrudyRef`: 5579 GREETING responses checked in about 10 s; 10 kept for Trudy (her 7
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
  text, OK); `fnv_menu_control press` pressed OK and the next queued box appeared; on the hardcore prompt
  (`ShowMessage 0011E661`) "No (Recommended)" and "Yes" were pressed by label and `GetButtonPressed`'s byte
  became 0 and 1 (hardcore stayed off: the result belonged to the console). `fnv_trace TESQuest::RunScript`
  decoded every quest and named the caller. `fnv_watch` sampled at 250 ms with frame numbers.
  `fnv_script_instances`: 229 running quest scripts, 25 loaded cells, 68 scripted refs. `fnv_quest_targets`:
  VMQ01 listed every reason its markers are hidden; the active Old World Blues quest's target was shown and in
  the game's cache. `fnv_condition_eval`: the Cowboy perk's skill requirement failed (15 against 45), the
  Super Stimpak's effects split on `IsHardcore` / `HasPerk`. `fnv_leveled_roll Ammo5mm75`: nothing in 23% of
  300 rolls against chance none 25%; the same seed gave the same rolls twice. `fnv_time` advanced 2 hours
  (globals) and 2 hours (`engine_wait`); `fnv_spawn`, `fnv_move` and their undo; `fnv_test_save` save, list
  and load. The engine screenshot worked with the window in the background. A YAML test with every new step
  passed, 17 steps, unattended; a failing one carried the debug bundle; `trace_script` listed the four lines
  of the Hello Quest script that never ran.
- About 40 s after a test advanced the clock and its teardown set it back, the game crashed in low-process
  pathfinding (`LowProcess::ProcessPatrol` > `Pathing::FindPathDistance` > door search > scrap heap), the same
  family as the crash after Trudy was force-enabled. Since then time changes only go forward.
- One fresh start hung before the main menu (main thread waiting in ntdll under
  `LoadedNode::ReadFromStream`, 4 s of CPU, no hooks installed); `fnv_restart` terminated it and the next
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
