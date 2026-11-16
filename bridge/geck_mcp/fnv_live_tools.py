"""MCP registration of the live fnv_* tools: game control and tests (milestone 4), live inspection
(milestone 5) and full control (milestone 6). Kinds: read (changes nothing), act (changes the running game,
journaled, refused unless fnv.engine_control = "full"), control (starts/stops the game or installs files; the
control calls in FULL_ONLY_ACTIONS put code or files into the game and need "full" too)."""
import time
import uuid
from typing import Annotated, Any, Literal

from pydantic import Field

from . import fnv_link
from .fnv_args import hex_int
from .fnv_game import Game
from .fnv_data import DataTools
from .fnv_deep import DeepTools
from .fnv_live import LiveService
from .fnv_social import SocialTools
from .fnv_agent import AgentTools
from .fnv_scenario import ScenarioRunner, diff, load_snapshot, snapshot
from .fnv_warnings import for_live as warnings_for_live
from .fnv_sampling import FrameSampler, SinkTools
from .fnv_native import NativeTools
from .fnv_code import CodeTools
from .fnv_plugin_build import PluginTools
from .fnv_perf import MemoryTools, ObjectTools, ProfileTools, ThreadTools
from .fnv_coverage import CoverageTools
from .fnv_inspect import AnimTools, AudioTools, PhysicsTools, SystemsTools
from .fnv_crawl import CrawlTools
from .fnv_suite import BisectTools, SelfTest
from .safe_paths import inside, read_roots

# Tool -> the actions that change the running game (PLAN-3 decision D2, bug T1): anything that writes game memory or
# code, changes behaviour, or leaves something installed (a hook, a probe, a sampler, a subscription, a held gate)
# runs as kind 'act', which needs fnv.engine_control = "full". '*' is every call of the tool. invoke() derives the kind
# from this table, so a tool cannot pass a weaker kind by mistake; a listed tool that passes no action is treated as
# changing the game.
MUTATING_ACTIONS = {
    'fnv_probe': {'install', 'remove', 'clear', 'config', 'harvest'},
    'fnv_name_oracle': {'run', 'calibrate', 'sweep'},
    'fnv_name_test': {'test', 'battery'},
    'fnv_field_watch': '*',
    'fnv_atlas': {'run', 'import_frida'},
    'fnv_hooks': {'remove'},
    'fnv_trace': {'start', 'stop'},
    'fnv_script_profile': '*',                      # read installs the hook as well (it starts the window)
    'fnv_script_trace': '*',
    'fnv_damage_log': '*',
    'fnv_crash': {'capture_on', 'capture_off'},
    'fnv_memory': {'alloc_trace', 'refcount'},       # refcount sets hardware write watches on every thread
    'fnv_profile': {'frame'},                       # timing hooks on the main loop for a few seconds
    'fnv_events': {'subscribe', 'unsubscribe'},
    # caller expressions and console lines can change the game, and the sampler closes message boxes (W1)
    'fnv_watch': '*',
    'fnv_snapshot': '*',                          # diff captures again when after is left out
    'fnv_warnings': {'capture'},                  # start/stop hooks the engine's Warning function (W3)
    'fnv_window': {'release_mouse', 'capture_mouse', 'unpause'},     # game memory and the pause menu (W3)
    'fnv_systems': {'formula', 'sweep', 'barter'},
    'fnv_leveled_roll': {'seed'},
    'fnv_test': '*',
    'fnv_session': {'start', 'end'},
    'fnv_crawl': '*',
    'fnv_bisect': '*',
    'fnv_replay': {'record', 'stop', 'play'},
    # start hooks ApplyPerkModifiers and ApplyFunction until stop; evaluate hooks, calls the dispatcher, unhooks (round 4 M1)
    'fnv_perk_entries': {'start', 'stop', 'evaluate'},
    # calendar_set: set_hour, advance_hours or timescale given (calendar alone reads it)
    'fnv_time': {'calendar_set', 'pause', 'resume', 'force_resume', 'hold', 'release', 'step', 'fixed', 'speed'},
    'fnv_undo': '*',
    'fnv_ui_do': '*',
    'fnv_place': {'at', 'array', 'nudge', 'rotate', 'scale', 'snap', 'remove'},
}


def gated_kind(name, action, kind):
    """The kind a call runs as: 'act' when MUTATING_ACTIONS lists the tool's action (or the whole tool), else kind."""
    rule = MUTATING_ACTIONS.get(name)
    if rule is None:
        return kind
    if rule == '*' or action is None or action in rule:
        return 'act'
    return kind


# The one rule for what engine_control = "read" allows (release plan Task 1.2, audit B2): anything that puts
# agent-chosen code or files into the game or its mod profile needs "full", whatever the call's kind. Same shape as
# MUTATING_ACTIONS ('*' = every call).
FULL_ONLY_ACTIONS = {
    'fnv_plugin_build': {'build', 'install'},   # agent C becomes a DLL in the game
    'fnv_install': '*',                         # a plugin file of the caller's choice into the dev profile
    'fnv_profile_set': {'apply'},               # which mods and plugins load (dry_run stays a read)
}

# Every control tool that neither table covers in full, and why it may run under "read".
READ_LEVEL_CONTROL = {
    'fnv_game': 'launch starts the dev game through the MO2 dev profile and puts nothing new into it; quit stops '
                'it, under read only the game fnv_game launch started (Game.owns); restart is quit and launch',
    'fnv_companion_install': "installs the product's own engine companion, not agent code",
    'fnv_scripts_compile': "compiles the project spec's scripts into the bridge's script cache",
    'fnv_name_export': 'writes the runtime evidence the bridge recorded into the fnvsym traces folder',
    'fnv_author': 'export, check, scaffold and package write only into the bridge state folder',
    'fnv_plugin_build': 'scaffold and log only: a starting source in the state folder, or the plugin log',
    'fnv_profile_set': 'dry_run only: shows the changes without writing',
    'fnv_window': 'status only; the actions that write game memory are in MUTATING_ACTIONS',
    'fnv_warnings': 'reading the captured warnings; capture is in MUTATING_ACTIONS',
    'fnv_session': 'status only; start and end are in MUTATING_ACTIONS',
    'fnv_replay': 'list only; record, stop and play are in MUTATING_ACTIONS',
}


def needs_full(name, action):
    """True when FULL_ONLY_ACTIONS lists the tool's action (or the whole tool); a listed tool with no action counts."""
    rule = FULL_ONLY_ACTIONS.get(name)
    if rule is None:
        return False
    return rule == '*' or action is None or action in rule


FNV_LIVE_TOOLS = {
    # milestone 4: game control, tests, logs
    'fnv_status': ('read', 'game running? phase (main menu, loading, in game), cell, loaded plugins, companion'),
    'fnv_game': ('control', 'start the game through the MO2 dev profile at the menu, a save or a cell and wait until ready (launch); quit it (console qqq, window close, terminate if forced); restart'),
    'fnv_install': ('control', 'install a built plugin into the dev profile as an MO2 mod and enable it (game closed)'),
    'fnv_companion_install': ('control', 'build the engine companion with Zig and install it into the dev profile'),
    'fnv_logs': ('read', 'new lines of nvse.log, the console log, CrashLogger.log, JIP and MO2 logs, and the engine warning logs'),
    'fnv_test': ('control', 'run one YAML scenario test in the game and report each step (run, default); every scenario in a folder (run_all); test steps given inline, no file (steps): wait for frames, a predicate or an event, press inputs for exact frames, save values with as, repeat the run to show differences'),
    'fnv_snapshot': ('act', 'capture chosen game state under a name; diff two snapshots (or a snapshot with now)'),
    # milestone 5: live inspection
    'fnv_player': ('read', 'player position, cell, health, AP, level, SPECIAL, skills, active effects'),
    'fnv_ref': ('read', 'any reference: base, position, cell, owner, lock, flags, class, extra data'),
    'fnv_refs_near': ('read', 'references around the player within a radius, nearest first'),
    'fnv_actor_ai': ('read', 'actor AI: package, procedure, process level, combat target, detection, state'),
    'fnv_quest': ('read', "quest: stage, stage list done flags, objectives with text and state, script variables (state); where each objective's markers point and why a marker is missing (targets)"),
    'fnv_actor_values': ('read', 'actor values with base, permanent and temporary parts and the effects behind them'),
    'fnv_effects': ('read', 'active effects of an actor with source item, effect, magnitude, duration, actor value'),
    'fnv_extra_data': ('read', 'extra data list of a reference, decoded'),
    'fnv_crosshair': ('read', 'what the player is looking at'),
    'fnv_inventory': ('read', 'items of an actor or container with counts and equipped state'),
    'fnv_script_errors': ('read', 'script errors (script, mod, message) since the last call'),
    'fnv_events': ('act', 'xNVSE events seen by the companion: loads, saves, new game, script errors; subscribe to game events (onactivate, onhit, ondeath...) recorded in the game'),
    'fnv_formid_live': ('read', "a plugin's load-order prefix in the running game"),
    'fnv_mem_read': ('read', 'memory at an address or Class field path, decoded with the decomp layouts'),
    # milestones 5-6: act
    'fnv_console': ('act', 'run a console line and return what it printed'),
    'fnv_run_script': ('act', 'evaluate an xNVSE expression or run a script snippet; returns its result'),
    'fnv_mem_write': ('act', 'write memory at an address or field path, journaled with the old bytes'),
    'fnv_engine_call': ('act', 'call an engine function by proven name or address with typed arguments'),
    'fnv_form_edit': ('act', 'change a field of a form in memory (journaled)'),
    'fnv_setting_set': ('act', 'change a game setting or numeric INI setting live (journaled)'),
    'fnv_actor_control': ('act', 'make an actor act now: combat, packages, idles, move, kill, resurrect, items, AVs'),
    'fnv_input': ('act', 'bounded player input: tap/hold a control or key, aim at a ref, activate a ref'),
    'fnv_script_set': ('act', 'set a quest stage, objective state, script variable or global (journaled)'),
    'fnv_journal': ('read', 'every change made through the act tools, with old values'),
    'fnv_undo': ('act', 'undo the last journaled change(s), one by id, or every undoable change of this game process (all)'),
    # milestone 7: watch hooks and deep tools
    'fnv_script_profile': ('act', 'calls and time per script (quest, object, effect) from watch hooks on Script::Run'),
    'fnv_script_trace': ('act', 'the statements of one script as they run, with branches taken and failures'),
    'fnv_damage_log': ('act', 'recent hits broken down: base, health, limb, armor damage, flags, applied damage'),
    'fnv_crash': ('act', 'the crash doctor: newest (or given) crash log explained in decomp names, DLL offsets, forms and likely culprit (explain, default); the log parsed (latest); every crash grouped by signature, new ones flagged (history); what was running at the fault from the companion (context, capture_on, capture_off, capture_status)'),
    'fnv_patch_map': ('read', 'engine code and pointers other plugins changed, by owner module, with chains'),
    'fnv_hooks': ('act', 'list the watch hooks of the engine companion, or remove them'),
    # milestone 8: data and authoring
    'fnv_loadorder': ('read', 'the dev profile load order: plugins, index, real file, MO2 mod, masters, problems'),
    'fnv_record': ('read', 'plugin records: one decoded, with every plugin that edits it and what each changed (show); records in every active plugin by EditorID, name, form id or type (find); what references a record: placed refs, scripts, leveled lists, conditions, dialogue (users)'),
    'fnv_script_lint': ('read', 'check script source against the vanilla, xNVSE and JIP LN command tables'),
    'fnv_conflicts': ('read', 'records several plugins edit: winner, fields each override changes, changes lost'),
    'fnv_asset_check': ('read', 'meshes, textures and sounds a plugin names that no archive or loose folder has'),
    'fnv_profile_set': ('control', 'enable or disable mods and plugins in the MO2 dev profile (game closed, backed up)'),
    'fnv_compile_ingame': ('read', 'compile script source with the running game: errors, variables, bytecode'),
    'fnv_script_hot_swap': ('act', 'replace a running script with a new compile, keeping its variables (undoable)'),
    # milestone 9: end to end
    'fnv_scripts_compile': ('control', "compile the project spec's scripts in game into geck-mcp-nv's script cache"),
    # P1 completion: dialogue, AI packages, live form diff
    'fnv_dialogue_choose': ('act', 'start a conversation with an NPC and pick responses, for testing dialogue end to end'),
    'fnv_ai_packages': ('read', "an actor's packages in order with schedule and conditions: which runs now and why"),
    'fnv_form_live_diff': ('read', 'a form in memory now versus its winning plugin record: what changed at runtime'),
    # agent wave: unattended testing and debugging
    'fnv_trace': ('act', 'calls to any proven engine function: decoded arguments, results, callers (watch hook); stack=true or several when conditions also record the call stack of each hit'),
    'fnv_watch': ('act', 'values sampled over time: expressions, memory fields, actor values, positions; frames=N samples memory every frame inside the game'),
    'fnv_screenshot': ('read', "the game's frame as PNG (engine back buffer, else the window), shown to the agent"),
    'fnv_test_save': ('act', 'named test saves kept apart from other saves: save, load, list, delete'),
    'fnv_move': ('act', 'teleport the player or a ref to a ref, a cell or a position (undoable)'),
    'fnv_condition_eval': ('read', "every condition list of a record evaluated against chosen refs, with what fails"),
    'fnv_script_instances': ('read', 'running quest and object script instances with variables and counts'),
    'fnv_menu': ('read', "open menus, the menu stack, loading screens and the message box with its buttons (state); a menu's live UI tile tree with trait values named by the game's trait table (tiles)"),
    'fnv_rng': ('act', "the game's random number generator: state, or seed it for repeatable rolls"),
    'fnv_time': ('act', 'the calendar: read it, jump to an hour, advance hours, change the timescale; the game clock and frames: pause and resume the clock (only its own pause), hold and release the frame gate, step N frames at a fixed rate, set a fixed frame step, set the time multiplier, status'),
    'fnv_leveled_roll': ('act', "roll a leveled list many times with the game's own code: chances per form"),
    # engine AI wave (decomp-built)
    'fnv_detection': ('read', 'why an NPC sees (or does not see) someone: detection records and the formula inputs'),
    'fnv_reaction': ('read', 'how two actors relate: hostile or not, fight reaction, faction pair, disposition, the rule'),
    'fnv_combat': ('read', "an actor's combat controller: target, procedure, action, weapon, group targets, style"),
    'fnv_perk_entries': ('act', 'record perk entry-point checks: value before, perk entries applied, value after'),
    'fnv_pathing': ('read', "an actor's path: mover state, request start/goal on the navmesh, nodes, why it fails"),
    'fnv_raycast': ('read', "cast a ray through the game's physics world: hit object, reference, point; ground height"),
    'fnv_cell_grid': ('read', 'loaded exterior grid, current interior, cell buffers, each cell with why it is loaded'),
    # phase 2: behaviour control and engine calls
    'fnv_hook_set': ('act', 'change an engine function: skip it with a result, replace its result, change an argument, run a script'),
    'fnv_patch': ('act', "write bytes, NOPs, ret or jmp/call into the game's code, journaled, checked against the clean exe"),
    'fnv_engine_get': ('read', 'call a proven getter whose machine code writes no memory, guarded, with typed arguments (no full control needed)'),
    'fnv_globals': ('act', 'global variables from the data handler, filtered; set values (journaled)'),
    'fnv_dialogue': ('read', 'the conversation now: speaker, spoken topic and line, the choices shown and their state (now); every response an NPC could give for a topic, each with its conditions evaluated (tree)'),
    'fnv_world_edit': ('act', 'move, rotate, scale, enable, disable, delete or duplicate a ref; weather; image space'),
    'fnv_scene_graph': ('read', "a ref's 3D node tree: transforms, geometry, shaders, texture paths, controllers"),
    'fnv_vats_odds': ('read', 'VATS hit chance per target and limb from the game formula, term by term, with perks'),
    'fnv_window': ('control', 'free or recapture the mouse (DirectInput background mode), close the pause menu, persist it'),
    # naming from the running game (NAMING-PLAN N1-N3)
    'fnv_probe': ('act', 'count calls of thousands of functions at once (entry jumps, vtable slots, call sites): plan, install, read, remove'),
    'fnv_atlas': ('act', 'run scripted game actions with doses against the probe set: which functions run per action, and how often'),
    'fnv_name_dossier': ('read', "one function's full case for naming: static facts, fnvsym claims, runtime callers (virtual slots resolved), this/result profile, actions, suggested tests"),
    'fnv_name_oracle': ('act', 'does a function compute a value the game already knows (script commands, form headers) on the same objects; blind calibration'),
    'fnv_name_test': ('act', 'experiments: skip or force a function during an action, with a control; blind battery re-identification'),
    'fnv_name_export': ('control', 'write the runtime evidence as fnvsym-trace/2 into fnvsym/traces for the runtime2 evidence pass, with the calibration of each method'),
    'fnv_field_watch': ('act', 'hardware watchpoints: which functions write or read one field of a live object, with the values; find the field an action changes'),
    # Plan 2 wave 0
    'fnv_warnings': ('control', "the engine's own warnings (missing assets, wrong form types, script problems) from xNVSE's error log: status, enable (changes xNVSE's config file), read, summary, clear"),
    'fnv_wait_for': ('read', 'block inside the game until N frames, N game or real seconds, a predicate, or the game is loaded and simulating; one request, with a timeout'),
    'fnv_session': ('control', 'a test session: start (check setup, launch or wait until loaded, turn on capture, open a journal scope), mark (a named point for fnv_health), end (revert the scope, one report, optional quit), status'),
    'fnv_health': ('read', 'what is abnormal since the last session mark: script errors, engine warnings by category, crash files, frame time and object count against the session start; each item names the next tool'),
    'fnv_guide': ('read', 'which tool for which problem: guides with tested procedures, search of tools, saved engine research and facts'),
    # Plan 2 wave 2: source-level debugger
    'fnv_debug': ('act', 'source-level debugger: breaks (function, address, file:line, fault) with conditions and logs; status, stack, locals, eval, set, continue, step, until, return, skip, freeze/thaw, threads, dump'),
    'fnv_flow': ('act', 'trace a function: one breakpoint, then a trace of the function range that maps each executed source line to its order, hit count and the locals that changed; or call it ourselves with given arguments under the same trace; depth 1 also traces the direct callees'),
    'fnv_blame': ('act', 'who changed a field or a reference: a hardware write watch on an address path, or hooks on the engine functions that touch a reference; report groups the hits by culprit (script line or engine function)'),
    'fnv_script_debug': ('act', 'script debugger: breaks on a script statement (script, line or opcode, with a when over its variables); status, vars, set, eval, step line, continue, freeze/thaw of the script runner'),
    'fnv_script_log': ('act', 'script log: the statement ring (commands, history of a reference), block events from the flag setters (events), raise a block flag, and the script errors'),
    # Plan 2 wave 3: eyes
    'fnv_camera': ('act', "the camera: get the view, set a free camera at a position and direction or looking at a ref, orbit a ref (fit to its size), switch first/third person, restore the player's view"),
    'fnv_look': ('read', 'the screen as data: references in view, largest first, with screen rectangles, distance, line of sight and flags, plus the crosshair, prompt, corner message, subtitle and open menus'),
    'fnv_photo': ('act', 'pictures: a reference from set angles in one sheet (world paused, view restored), the cell from above and the corners, the view with labels, a frame-exact film strip, a compare against an earlier image'),
    'fnv_hud': ('act', 'the HUD: mode, visible parts, crosshair ref and prompt, the corner message and the queue, subtitle; post shows a corner message'),
    'fnv_visibility': ('read', 'why a reference does not draw: no 3D, disabled, deleted, culled, zero scale, first person, other cell, outside the view or behind something'),
    # Plan 2 wave 3: hands
    'fnv_play': ('act', 'play as a player: goto a ref or position on foot (route around walls, through doors), look_at, activate with the reason when refused, talk, item actions, stop'),
    'fnv_ui_do': ('act', 'menus by meaning: click a tile or a button by its label (HandleClick, as a mouse click), wait N hours through the wait menu, fast travel to a marker (refusal reason), message box buttons, close, close all'),
    # Plan 2 wave 4: creation loop
    'fnv_asset': ('act', 'assets in the running game: reload a model or texture from disk (no restart), watch a mod folder, the log of every file the engine opened or missed, where a path comes from, load queue, model cache, archives'),
    'fnv_form': ('act', 'forms at run time: create (a new form or a clone), reload the changed records of a rebuilt plugin into the live forms, what this session changed'),
    'fnv_native': ('act', 'C modules inside the game: generate engine headers, build with Zig, load (hooks installed), call exports, read their log, reload, unload'),
    'fnv_code': ('act', "swap an engine function for one compiled from edited decomp source: checkout, edit, build (with the game's own compiler), compare side by side, swap, unswap, export"),
    'fnv_plugin_build': ('control', 'ship a native module as its own xNVSE plugin (no companion needed): scaffold, build with hook prologues planned, install into the dev profile, read its log'),
    'fnv_profile': ('act', 'where the time goes: sample thread stacks from outside the game, self and inclusive time per function and source line, callers, a flame-graph file (sample); diff two profiles; time per frame by subsystem from timing hooks (frame)'),
    'fnv_threads': ('read', 'every game thread with its start routine and current function, and the known locks with their owner (list); sample the call stacks of the game threads and name the functions a hang is stuck in (hang)'),
    'fnv_memory': ('act', 'memory: the 2 GB address space with growth over time, form and actor counts (address_space); pools with exact numbers and the live NiRefObject count (pools); allocation counts by call site (alloc_trace); who changes an object\'s reference count (refcount)'),
    'fnv_object': ('read', 'what a pointer is (pool block, class, form); every live object of a class; every place in memory that holds a pointer'),
    'fnv_coverage': ('act', 'which decomp source lines ran during a scenario: one-shot traps at every line start, then lines run and never run per file and function; diff two runs'),
    'fnv_anim': ('act', 'animation of an actor: current and queued groups with their sequences, every group it has, the idle the engine would pick and why others fail, equipment slots, face values, animation actions over time; play a group or an idle'),
    'fnv_audio': ('act', 'sounds playing now and every sound created for a while (form or file, and who asked); play and stop; music channels and why; radio stations; subtitles shown; would a sound be heard; voice and lip files of an NPC\'s lines'),
    'fnv_physics': ('act', 'character controller of an actor (state, fall time and distance), the Havok body of a reference with its velocity (and set it), trigger enter and leave events, projectiles in flight, is a spot free, collision layers'),
    'fnv_systems': ('act', 'call game formulas (catalog or any function) and sweep an argument; level and XP progression; hardcore needs; can a perk be taken and why not; barter prices; encounter zone levels; karma'),
    'fnv_replay': ('control', 'record the input sent through the bridge frame by frame from a save, and play it again frame-exact under the frame gate'),
    'fnv_render': ('act', 'render toggles that still work in retail (wireframe, sky, grass, trees, LOD, menus, borders, trigger volumes, collision, test light...), image-space modifiers, shader reload, the scene lights'),
    'fnv_overlay': ('act', 'text drawn on screen and visible markers placed in the world, for a person watching; clear removes them'),
    'fnv_script_coverage': ('act', 'script lines a play-through or a test folder ran: per script the share of lines run, lines never run, blocks never entered'),
    'fnv_script_decompile': ('read', 'source text of a compiled script that ships without it (also quest stage, topic info, package and terminal scripts), by xNVSE\'s decompiler'),
    'fnv_crawl': ('control', 'unattended checks of a plugin: every cell it adds or edits (errors, warnings, missing files, refs without 3D, dead load doors, frame rate, photo) and every line an NPC can say (conditions, voice and lip files)'),
    'fnv_bisect': ('control', 'find the one MO2 mod or plugin that makes a failing scenario fail, by halves (the game restarts each round)'),
    'fnv_selftest': ('read', 'check what every tool family relies on against the running game: ok, degraded or broken, with the fact that failed'),
    'fnv_place': ('act', 'level work in the game: place a form at a position or near a ref, rows, grids and circles, nudge, rotate, scale, snap to the surface, check floating or sinking, list and remove what this session placed; export=false places test refs left out of fnv_author export'),
    'fnv_author': ('control', "turn the session's live work into a plugin spec part, check a plugin (missing files, conflicts, engine warnings), scaffold a starting project, package a release folder and archive"),
    # milestone 10: setup
    'fnv_setup_check': ('read', 'is this machine ready for the fnv tools? each prerequisite with its state and the fix'),
}


# One shape for a position (release audit M1): [x, y, z] in game units. Where a null is allowed it keeps that axis.
Xyz = Annotated[list[float | None], Field(min_length=3, max_length=3,
                                          description='[x, y, z]; null keeps that axis. The old {x, y, z} object works '
                                                      'too.')]
XyzFull = Annotated[list[float], Field(min_length=3, max_length=3, description='[x, y, z] in game units')]


def xyz_dict(value):
    """[x, y, z] (null keeps an axis) or the old {x, y, z} object, as the {axis: value} the tools use."""
    if value is None:
        return None
    if isinstance(value, dict):
        bad = set(value) - set('xyz')
        if bad:
            raise ValueError('A position or rotation has the axes x, y and z only (got %s)' % ', '.join(sorted(bad)))
        return {axis: float(v) for axis, v in value.items() if v is not None}
    return {axis: float(v) for axis, v in zip('xyz', value) if v is not None}


def register(server, project, annotations, result_type, maintain=True, offline=None):
    import json
    from functools import partial

    import anyio
    from mcp.types import CallToolResult, TextContent

    live = LiveService(project)
    # journal compaction and state pruning, off the start path (tasklist and file walks take a moment)
    # (tests pass maintain=False: it prunes the real %LOCALAPPDATA%\\fnvmcp crash folder; S2 review I4)
    if maintain:
        import threading
        threading.Thread(target=live.maintain, name='fnv-state-maintenance', daemon=True).start()
    deep = DeepTools(live)
    live.deep = deep
    data = DataTools(project, live)
    social = SocialTools(live, data)
    agent = AgentTools(live, deep, social, data)
    live.agent = agent
    from .fnv_engine_ai import EngineAITools
    engine_ai = EngineAITools(agent)
    live.engine_ai = engine_ai
    from .fnv_records import RecordsTools
    records = RecordsTools(data)
    from .fnv_control import ControlTools
    control_tools = ControlTools(agent)
    live.control_tools = control_tools
    from .fnv_world import WorldTools
    world = WorldTools(agent)
    live.world = world
    from .fnv_probe import ProbeTools
    probe_tools = ProbeTools(live)
    live.probe_tools = probe_tools
    from .fnv_atlas import AtlasTools
    atlas = AtlasTools(live, probe_tools, world)
    from .fnv_naming import NamingTools, fnvsym_root
    naming = NamingTools(live, probe_tools, atlas)
    from .fnv_oracle import OracleTools
    oracle = OracleTools(live, naming, agent)
    from .fnv_experiment import ExperimentTools
    experiments = ExperimentTools(live, control_tools, naming)
    from .fnv_watch import FieldWatchTools
    field_watch = FieldWatchTools(live, naming)
    from .fnv_inject import shared_time
    time_control, wait_for = shared_time(live)
    from .fnv_session import HealthTools, SessionTools
    session_tools = SessionTools(live)
    health_tools = HealthTools(live)
    from .fnv_debugger import Debugger, READ_ACTIONS
    debugger = Debugger(live)
    from .fnv_flow import FlowTools
    from .fnv_blame import BlameTools
    from .fnv_scriptdbg import EvalHook, ScriptDebugger, ScriptLog
    flow_tools = FlowTools(live, debugger=debugger)
    statement_hook = EvalHook(live)
    script_debug = ScriptDebugger(live, eval_hook=statement_hook, dbg=debugger, data=data)
    from .fnv_scriptcov import ScriptCoverage
    script_coverage = ScriptCoverage(live, statement_hook, script_debug,
                                     runner=lambda: __import__('geck_mcp.fnv_scenario', fromlist=['ScenarioRunner'])
                                     .ScenarioRunner(live), data=data)
    blame_tools = BlameTools(live, debugger=debugger, line_of=script_debug._line_of, statement_hook=statement_hook)
    script_log = ScriptLog(live, eval_hook=statement_hook, debugger=debugger)
    from .fnv_eyes import EyesTools
    eyes = EyesTools(live, agent=agent, engine_ai=engine_ai)
    live.eyes = eyes
    from .fnv_hands import HandsTools
    hands = HandsTools(live, agent=agent, eyes=eyes)
    live.hands = hands
    from .fnv_create import AssetTools, AuthorTools, FormTools, PlaceTools
    asset_tools = AssetTools(live, records=records)
    form_tools = FormTools(live, data=data, assets=asset_tools)
    place_tools = PlaceTools(live, hands=hands)
    from .fnv_render import OverlayTools, RenderTools
    render_tools = RenderTools(live)
    from .fnv_replay import ReplayTools
    replay_tools = ReplayTools(live, agent=agent)
    overlay_tools = OverlayTools(live, place=place_tools)
    author_tools = AuthorTools(live, data=data, records=records, place=place_tools, forms=form_tools)
    native_tools = NativeTools(live)
    flow_tools.native = native_tools
    code_tools = CodeTools(live)
    plugin_tools = PluginTools(live)
    profile_tools = ProfileTools(live)
    thread_tools = ThreadTools(live)
    memory_tools = MemoryTools(live, deep)
    memory_tools.blame = blame_tools
    object_tools = ObjectTools(live)
    coverage_tools = CoverageTools(live)
    anim_tools = AnimTools(live)
    audio_tools = AudioTools(live, deep)
    physics_tools = PhysicsTools(live)
    systems_tools = SystemsTools(live)
    crawl_tools = CrawlTools(live, agent=agent, data=data, records=records, social=social, assets=asset_tools,
                             eyes=eyes, warnings=warnings_for_live)
    audio_tools.crawl = crawl_tools
    physics_tools.engine_ai = engine_ai
    systems_tools.engine_ai = engine_ai

    def _scenario_runner():
        from .fnv_scenario import ScenarioRunner
        return ScenarioRunner(live)
    bisect_tools = BisectTools(live, records, _scenario_runner)
    self_test = SelfTest(live, {'memory': memory_tools, 'systems': systems_tools, 'eyes': eyes,
                                'profile': profile_tools})
    frame_sampler = FrameSampler(live)
    sinks = SinkTools(live)
    live.eyes, live.hands = eyes, hands      # the scenario runner's photo / play / ui steps
    live.social = social                     # its dialogue step
    read, act, control = annotations
    if offline is None:                      # the offline fnv_* service (fnv_crash explain and latest read through it)
        from .fnv_tools import FnvService
        offline = FnvService(project)

    async def offline_invoke(fn, *args):
        """An offline FnvService call (it has its own envelope) on a worker thread."""
        result = await anyio.to_thread.run_sync(partial(fn, *args))
        return CallToolResult(content=[TextContent(type='text', text=json.dumps(result, default=str))],
                              structured_content=json.loads(json.dumps(result, default=str)),
                              is_error=not result.get('ok', True))

    def service_call(name, fn, kind):
        def call():
            return live.run(name, fn, kind)
        return call

    async def invoke(name, fn, kind='read', action=None, shield=None, attach=None):
        """Run fn in live.run's envelope on a worker thread. shield: never abandon the call on cancel (default: act
        calls only). attach(): more content items for the reply (an image), built after the call."""
        kind = gated_kind(name, action, kind)      # MUTATING_ACTIONS decides, not the call site (T1)
        full = needs_full(name, action)            # FULL_ONLY_ACTIONS: code or files into the game (B2)
        shield = kind != 'read' if shield is None else shield
        with anyio.CancelScope(shield=shield):
            result = await anyio.to_thread.run_sync(partial(live.run, name, fn, kind, full),
                                                    abandon_on_cancel=kind == 'read' and not shield)
        content = [TextContent(type='text', text=json.dumps(result, default=str))] + (list(attach()) if attach else [])
        return CallToolResult(content=content, structured_content=json.loads(json.dumps(result, default=str)),
                              is_error=not result.get('ok', True))

    Ref = Annotated[str, Field(min_length=1, max_length=200, description=(
        'player, crosshair, form id (0x14), Plugin.esp:LOCALID or EditorID'))]
    Start = Annotated[str, Field(pattern=r'^(menu|continue|save:[A-Za-z0-9_. \-]{1,120}|coc:[A-Za-z0-9_]{1,120})$',
                                 description='menu | continue (newest dev save) | save:<name> | coc:<cell EditorID>')]

    # ------------------------------------------------------------------ milestone 4
    @server.tool(annotations=read)
    async def fnv_status() -> result_type:
        """Is the game running, and in which phase (not_running, starting, main_menu, loading, in_game, in_menu,
        paused_menu)? Gives the cell, the loaded plugins with their load-order index, the engine companion's
        ping (frame counter, ms since the last frame) and whether a crash log was written since launch."""
        return await invoke('fnv_status', lambda: live.game.status())

    @server.tool(annotations=control)
    async def fnv_game(action: Literal['launch', 'quit', 'restart'],
                       start: Start = 'menu',
                       force: bool = True,
                       timeout_seconds: Annotated[int, Field(ge=5, le=600)] | None = None) -> result_type:
        """The game process. launch: start Fallout: New Vegas through the MO2 profile named in fnv.mo2_profile (and its
        saves) and wait until it is ready: at the main menu, in a save (loaded by the companion, no clicks), or in a
        cell via coc; refuses when the game already runs; returns a timeline of the startup. quit: the console's qqq
        first, then closing its window, then (force=true) terminating it. restart: quit (if running) and launch
        again; use after installing plugins or DLLs. timeout_seconds: launch and restart 20 to 600 (default 240),
        quit 5 to 120 (default 20). fnv_session start also launches, with a setup check and a journal scope."""
        def run():
            if action == 'quit':
                timeout = 20 if timeout_seconds is None else timeout_seconds
                if timeout > 120:
                    raise ValueError('quit takes timeout_seconds 5 to 120')
                return live.game.quit(timeout, force)
            timeout = 240 if timeout_seconds is None else timeout_seconds
            if timeout < 20:
                raise ValueError('%s takes timeout_seconds 20 to 600' % action)
            return (live.game.launch if action == 'launch' else live.game.restart)(start, timeout)
        return await invoke('fnv_game', run, 'control', action=action)

    @server.tool(annotations=control)
    async def fnv_install(plugin_path: Annotated[str, Field(min_length=5, max_length=1000)],
                          mod_name: Annotated[str, Field(max_length=80)] | None = None) -> result_type:
        """Install a built .esp/.esm into the dev profile as its own MO2 mod (mods/<name>/), enable the mod and the
        plugin. The game must be closed. A previous copy is backed up."""
        return await invoke('fnv_install', lambda: live.game.install_plugin(plugin_path, mod_name), 'control')

    @server.tool(annotations=control)
    async def fnv_companion_install() -> result_type:
        """Build the fnvmcp engine companion (C, Zig 0.14.1 from fnv.zig or FNV_MCP_ZIG) and install it into the dev
        profile as the MO2 mod "fnvmcp engine companion". The game must be closed; it loads at the next launch."""
        def action():
            if live.game.process():
                raise ValueError('Quit the game before installing the companion (fnv_game quit)')
            receipt = fnv_link.build(project.state_dir, fnv_link.find_zig(project))
            return {'build': receipt, 'install': fnv_link.install(project.fnv.mo2_root, project.fnv.mo2_profile, receipt,
                                                                  project.state_dir)}
        return await invoke('fnv_companion_install', action, 'control')

    @server.tool(annotations=read)
    async def fnv_logs(sources: list[Literal['nvse', 'console', 'crash', 'jip', 'mo2', 'error', 'havok']] | None = None,
                       since: Literal['last', 'start'] = 'last',
                       max_lines: Annotated[int, Field(ge=1, le=5000)] = 200) -> result_type:
        """New lines of the game's logs since the last call (since='last') or the whole file ('start'): nvse.log,
        the companion's console log (everything the console printed, script errors included), CrashLogger.log,
        JIP LN's log and MO2's log; also error and havok, the engine warning logs xNVSE writes when its config has
        EnableGameErrorLog=1 (fnv_warnings reads them with filters)."""
        return await invoke('fnv_logs', lambda: live.game.logs(sources, 0 if since == 'start' else 'last', max_lines))

    @server.tool(annotations=control)
    async def fnv_test(action: Literal['run', 'run_all', 'steps'] = 'run',
                       path: Annotated[str, Field(max_length=1000)] | None = None,
                       yaml: Annotated[str, Field(max_length=100000)] | None = None,
                       folder: Annotated[str, Field(max_length=1000)] | None = None,
                       steps: Annotated[list[Any], Field(min_length=1, max_length=200)] | None = None,
                       name: Annotated[str, Field(max_length=80)] = 'inline',
                       start: Start | None = None,
                       plugins: Annotated[list[str], Field(max_length=20)] | None = None,
                       setup: Annotated[list[Any], Field(max_length=200)] | None = None,
                       teardown: Annotated[list[Any], Field(max_length=200)] | None = None,
                       deterministic: bool = False,
                       repeat: Annotated[int, Field(ge=1, le=20)] = 1,
                       timeout_seconds: Annotated[int, Field(ge=20, le=900)] = 240,
                       relaunch_on_crash: bool = True) -> result_type:
        """Scenario tests in the game. run (default): one scenario test (a YAML file at path, or inline yaml).
        Launches the game when needed, applies `start` (menu, continue, a save, a test save or a cell), runs setup and
        steps (run, script, wait, expect, set, actor, input, move, mem_write, call, snapshot, and the agent steps
        spawn, save, load, menu, screenshot, watch, trace, time, rng), then teardown (revert_all: this test's changes,
        quit). Agent checks: called (a traced engine function ran), condition, message_box, marker, menu_open. guard:
        {message_boxes: keep} leaves message boxes for the test; trace_script reports which statements of a script
        ran. Returns every step's result with actual values, failures with reasons, new script errors, a debug bundle
        on failure (menus, message box, console tail, screenshot), and on a crash the crash doctor's diagnosis plus a
        relaunch. run_all: every *.yaml / *.yml / *.json scenario in a folder (default: the project's fnv.tests_dir).
        steps: test steps given inline, the same steps a scenario file has, with one report. New in this form: step:
        {frames: N, fps: F} advances exact frames; wait: {for: loaded | event name | predicate, timeout: s}; input:
        {control|key|button|mouse: .., frames: N} presses for exact frames; any step may carry as: name, and later
        steps use $name. deterministic: true fixes the frame step at 60 fps and seeds the random generator. repeat: N
        runs the scenario N times and reports the steps that differ. photo {action, ref, angles}, play {action:
        goto|talk|activate|look_at|..., ref} and ui {action, label, ...} use the eyes and hands; break and flow stop
        the main thread and are not scenario steps. start is menu | continue | save:<name> | coc:<cell>."""
        def inline():
            if action == 'run_all':
                return ScenarioRunner(live).run_all(folder)
            if action == 'run':
                if not path and not yaml:
                    raise ValueError('run needs path or yaml')
                return ScenarioRunner(live).run(path, yaml, relaunch_on_crash)
            if not steps:
                raise ValueError('steps needs steps (a list of scenario steps)')
            data = {'name': name, 'steps': steps, 'deterministic': deterministic, 'repeat': repeat,
                    'timeout': timeout_seconds}
            if start:
                data['start'] = start
            if plugins:
                data['plugins'] = plugins
            if setup:
                data['setup'] = setup
            if teardown:
                data['teardown'] = teardown
            return ScenarioRunner(live).run(data=data, relaunch=relaunch_on_crash)
        return await invoke('fnv_test', inline, 'control', action=action)

    @server.tool(annotations=act)
    async def fnv_snapshot(action: Literal['capture', 'diff'] = 'capture',
                           name: Annotated[str, Field(min_length=1, max_length=80)] | None = None,
                           items: list[Any] | None = None,
                           before: Annotated[str, Field(min_length=1, max_length=80)] | None = None,
                           after: Annotated[str, Field(max_length=80)] | None = None) -> result_type:
        """Game state by name. capture (default): capture chosen state under name: 'player', or items like {eval:
        expr}, {item_count: form, ref: player}, {actor_value: Health, ref: ...}, {stage: quest}, {global: name},
        {variable: name, of: quest}. diff: compare two snapshots by name (before, after); without after, capture the
        same items again now and compare."""
        def compare():
            if action == 'capture':
                if not name:
                    raise ValueError('capture needs name')
                return snapshot(live, items, name)
            if not before:
                raise ValueError('diff needs before (a snapshot name)')
            a = load_snapshot(live, before)
            if after:
                b = load_snapshot(live, after)
            else:
                again = []
                for key in a['values']:
                    if key.startswith('player.'):
                        if 'player' not in again:
                            again.append('player')
                    else:
                        again.append(json.loads(key))
                b = snapshot(live, again, before + '-now')
            return diff(a, b)
        return await invoke('fnv_snapshot', compare, action=action)

    # ------------------------------------------------------------------ milestone 5: reads
    @server.tool(annotations=read)
    async def fnv_player() -> result_type:
        """The player now: position, heading, cell, health, action points, level, XP, karma, rads, SPECIAL,
        skills and active effects."""
        return await invoke('fnv_player', live.player)

    @server.tool(annotations=read)
    async def fnv_ref(ref: Ref) -> result_type:
        """Any reference in the running game: base form and name, position, rotation, scale, cell, disabled,
        owner, lock, form flags, runtime class (RTTI), health/dead for actors, and its extra data."""
        return await invoke('fnv_ref', lambda: live.ref(ref))

    @server.tool(annotations=read)
    async def fnv_refs_near(radius: Annotated[float, Field(gt=0, le=100000)] = 2000.0,
                            form_type: Annotated[int, Field(ge=0, le=255)] = 0,
                            base: Annotated[str, Field(max_length=10)] | None = None,
                            limit: Annotated[int, Field(ge=1, le=300)] = 50) -> result_type:
        """References around the player within `radius` game units (xNVSE GetRefs, the player's cell and loaded
        neighbours), nearest first; filter by form type code (42 NPC, 43 creature...) or base form id."""
        return await invoke('fnv_refs_near', lambda: live.refs_near(radius, form_type, limit, base))

    @server.tool(annotations=read)
    async def fnv_actor_ai(ref: Ref) -> result_type:
        """What an actor's AI is doing: current package (form, type, procedure), process level, combat target,
        detection both ways, dead/unconscious/sitting/sleeping/knocked state, teammate, AI values."""
        return await invoke('fnv_actor_ai', lambda: live.actor_ai(ref))

    @server.tool(annotations=read)
    async def fnv_quest(quest: Ref, action: Literal['state', 'targets'] = 'state') -> result_type:
        """A quest. state (default): running, current stage, completed, every stage with its done flag, objectives
        with text and displayed/completed state, and its script variables by name. targets: where each quest
        objective's markers point and why a marker is missing: per target the ref, its cell, position and distance,
        the target conditions (run on the target ref) with pass/fail, the door the marker points at for targets in
        other cells, whether the game has it in the player's target cache, and every reason it is not shown (quest
        not running or not the active quest, objective not displayed or completed, ref missing, deleted or disabled,
        conditions failing)."""
        if action == 'targets':
            return await invoke('fnv_quest', lambda: agent.quest_targets(quest))
        return await invoke('fnv_quest', lambda: live.quest(quest))

    @server.tool(annotations=read)
    async def fnv_actor_values(ref: Ref, names: list[str] | None = None) -> result_type:
        """Actor values (all 77, or the names given): current, base, permanent, the permanent and temporary
        modifiers, and the active effects that change each one."""
        return await invoke('fnv_actor_values', lambda: live.actor_values(ref, names))

    @server.tool(annotations=read)
    async def fnv_effects(ref: Ref) -> result_type:
        """Active effects on an actor: the spell/ingestible/ability, the base effect, the source item (armor,
        weapon), magnitude, duration, elapsed time, the actor value affected, and active/applied flags."""
        return await invoke('fnv_effects', lambda: live.effects(ref))

    @server.tool(annotations=read)
    async def fnv_extra_data(ref: Ref) -> result_type:
        """A reference's extra data list decoded: type names plus ownership, count, health, lock (level, key),
        teleport, enable parent, linked ref, leveled origin, encounter zone and more."""
        return await invoke('fnv_extra_data', lambda: live.extra_data(ref))

    @server.tool(annotations=read)
    async def fnv_crosshair() -> result_type:
        """The reference under the player's crosshair (form id, base, name, distance, class), or null."""
        return await invoke('fnv_crosshair', live.crosshair)

    @server.tool(annotations=read)
    async def fnv_inventory(ref: Ref = 'player', limit: Annotated[int, Field(ge=1, le=1000)] = 300) -> result_type:
        """Items carried by an actor or held by a container: form id, name, count, equipped."""
        return await invoke('fnv_inventory', lambda: live.inventory(ref, limit))

    @server.tool(annotations=read)
    async def fnv_script_errors(since: Literal['last', 'start'] = 'last') -> result_type:
        """Script errors printed since the last call (or since the game started): script form id and name, mod,
        and the error message."""
        return await invoke('fnv_script_errors', lambda: live.script_errors(0 if since == 'start' else 'last'))

    @server.tool(annotations=act)
    async def fnv_events(since: Annotated[int, Field(ge=0)] = 0,
                         subscribe: list[Annotated[str, Field(max_length=40)]] | None = None,
                         unsubscribe: list[Annotated[str, Field(max_length=40)]] | None = None,
                         sink_since: Annotated[int, Field(ge=0)] | None = None,
                         ref: Annotated[str, Field(max_length=10)] | None = None) -> result_type:
        """Events the engine companion recorded after sequence number `since`: game loads, saves, new game, exit
        to menu, script errors (with message). subscribe=[onactivate, onhit, ondeath, onadd, onload, ...] also
        records those xNVSE events in the game (frame, the reference, the event's first argument words);
        sink_since=N returns them after N (ref filters by form id); unsubscribe=[names] or ["all"]."""
        def action():
            out = {}
            if unsubscribe:
                out['unsubscribed'] = sinks.unsubscribe(None if 'all' in [u.lower() for u in unsubscribe] else unsubscribe)
            if subscribe:
                out['subscribed'] = sinks.subscribe(subscribe)
            if sink_since is not None or subscribe:
                out['sinks'] = sinks.read(sink_since or 0, ref)
            if not out:
                return live.events(since)
            out['events'] = live.events(since)
            return out
        return await invoke('fnv_events', action,
                            action='subscribe' if subscribe else 'unsubscribe' if unsubscribe else 'read')

    @server.tool(annotations=read)
    async def fnv_formid_live(plugin: Annotated[str, Field(min_length=5, max_length=100)]) -> result_type:
        """Whether a plugin is loaded in the running game and its load-order prefix (form ids = prefix + local id)."""
        return await invoke('fnv_formid_live', lambda: live.formid_live(plugin))

    @server.tool(annotations=read)
    async def fnv_mem_read(target: Annotated[str, Field(min_length=1, max_length=200, description=(
                               '0xADDRESS, or a reference/form spec (player, form id, EditorID)'))],
                           path: Annotated[str, Field(max_length=300)] | None = None,
                           as_type: Literal['u8', 'i8', 'u16', 'i16', 'u32', 'i32', 'f32', 'f64', 'ptr', 'cstr'] | None = None,
                           class_name: Annotated[str, Field(max_length=200)] | None = None,
                           length: Annotated[int, Field(ge=1, le=4096)] | None = None) -> result_type:
        """Read engine memory. With a field path (m_pBaseProcess.m_eProcessType) the fields are found in the
        decomp's class layouts, starting from the object's runtime class (RTTI), following pointers. Structs come
        back as hex plus decoded simple fields."""
        return await invoke('fnv_mem_read', lambda: live.mem_read(target, path, as_type, length, class_name))

    # ------------------------------------------------------------------ milestones 5-6: act
    @server.tool(annotations=act)
    async def fnv_console(line: Annotated[str, Field(min_length=1, max_length=500)], ref: Ref | None = None) -> result_type:
        """Run one console line (optionally on a reference, like ref.command) and return everything it printed.
        Journaled; console changes cannot be undone automatically."""
        return await invoke('fnv_console', lambda: live.run_console(line, ref), 'act')

    @server.tool(annotations=act)
    async def fnv_run_script(source: Annotated[str, Field(min_length=1, max_length=20000)], ref: Ref | None = None) -> result_type:
        """Evaluate one xNVSE expression (e.g. "player.GetAV Health * 2") or run a script snippet (several lines,
        wrapped in Begin Function {} ... End when needed) with the running game's compiler; returns the result."""
        return await invoke('fnv_run_script', lambda: live.run_script(source, ref), 'act')

    @server.tool(annotations=act)
    async def fnv_mem_write(target: Annotated[str, Field(min_length=1, max_length=200)], value: Any,
                            path: Annotated[str, Field(max_length=300)] | None = None,
                            as_type: Literal['u8', 'i8', 'u16', 'i16', 'u32', 'i32', 'f32', 'f64', 'ptr', 'bool', 'bytes'] | None = None,
                            class_name: Annotated[str, Field(max_length=200)] | None = None) -> result_type:
        """Write engine memory at an address or field path, typed by the class layout (or as_type). The old
        bytes are journaled; fnv_undo restores them. Saves are backed up before the first change."""
        return await invoke('fnv_mem_write', lambda: live.mem_write(target, value, path, as_type, class_name), 'act')

    @server.tool(annotations=act)
    async def fnv_engine_call(function: Annotated[str, Field(min_length=1, max_length=300)],
                              this: Annotated[str, Field(max_length=200)] | None = None,
                              args: list[dict[str, Any]] | None = None,
                              returns: Literal['void', 'int', 'uint', 'bool', 'ptr', 'float', 'double', 'form'] = 'int',
                              convention: Literal['cdecl', 'stdcall', 'thiscall', 'fastcall'] | None = None,
                              allow_unproven: bool = False) -> result_type:
        """Call an engine function by name or address. The calling convention comes from the decomp signature.
        args: [{type: int|bool|ptr|ref|form|float|double|string, value}]; `this` is a ref spec or 0xADDRESS.
        Only proven functions unless allow_unproven=true; unproven calls run guarded (a fault returns CALL_FAULTED instead of crashing the game). A wrong proven call can still crash it."""
        return await invoke('fnv_engine_call', lambda: live.engine_call(function, this, args, returns, convention,
                                                                        allow_unproven), 'act')

    @server.tool(annotations=act)
    async def fnv_form_edit(form: Ref, field: Annotated[str, Field(min_length=1, max_length=300)], value: Any,
                            as_type: Literal['u8', 'i8', 'u16', 'i16', 'u32', 'i32', 'f32', 'f64', 'ptr', 'bool'] | None = None) -> result_type:
        """Change a field of a form in memory (weapon damage, NPC data, package data...) by field path in the
        form's class layout. Journaled and undoable; the plugin file is not changed."""
        return await invoke('fnv_form_edit', lambda: live.form_edit(form, field, value, as_type), 'act')

    @server.tool(annotations=act)
    async def fnv_setting_set(name: Annotated[str, Field(min_length=2, max_length=120)],
                              value: Annotated[str | float, Field()]) -> result_type:
        """Change a game setting (fCombatDistance, sSomething) or numeric INI setting (bAlwaysActive:General) in
        the running game. The old value is journaled; the engine map's default and address are included."""
        return await invoke('fnv_setting_set', lambda: live.setting_set(name, value), 'act')

    @server.tool(annotations=act)
    async def fnv_actor_control(ref: Ref, action: Annotated[str, Field(min_length=3, max_length=40)],
                                target: Annotated[str, Field(max_length=200)] | None = None,
                                value: Any = None) -> result_type:
        """Make an actor do something now: evaluate_package, start_combat (target), stop_combat, add_package /
        remove_package, play_idle (target idle), move_to (target), kill, resurrect, enable, disable, equip /
        unequip / add_item / remove_item (target item, value count), cast (target spell), look_at, say_to,
        set_av / force_av (target actor value name, value; read back, says whether it took), set_position (value
        [x, y, z]). Journaled; undoable ones say so."""
        return await invoke('fnv_actor_control', lambda: live.actor_control(ref, action, target, value), 'act')

    @server.tool(annotations=act)
    async def fnv_input(action: Literal['tap', 'hold', 'aim_at', 'activate'],
                        control: Annotated[str, Field(max_length=20)] | None = None,
                        key: Annotated[int, Field(ge=0, le=300)] | None = None,
                        duration_ms: Annotated[int, Field(ge=0, le=10000)] = 0,
                        target: Ref | None = None,
                        frames: Annotated[int, Field(ge=1, le=10000)] | None = None,
                        key_name: Annotated[str, Field(max_length=20)] | None = None,
                        button: Annotated[int, Field(ge=0, le=7)] | None = None,
                        mouse: Annotated[list[int], Field(min_length=2, max_length=2)] | None = None) -> result_type:
        """Bounded player input. tap/hold a control (forward, backward, left, right, attack, activate, aim, jump,
        sneak, run, ready, vats, pipboy...), a DirectInput key code or key_name (E, Space, Escape, F1, 1, Up...),
        a mouse button (0 left, 1 right) or a mouse move [dx, dy] per frame (button and mouse need frames); a hold is
        released by the companion after
        duration_ms even if the bridge goes away. aim_at turns the player toward a ref; activate makes the player
        activate a ref (doors, containers, talking to an NPC). frames=N (with tap or hold) makes the input frame-exact:
        it is held for exactly N frames through the input injector, and the call returns when they have run; use it
        with fnv_time step to press a key on a known frame. Controls 0 to 12 go through the engine's own force
        call; other controls by their bound key or mouse button."""
        def press():
            code = key
            if key_name is not None:          # inside the envelope: an unknown name is INVALID_ARGUMENT (T2)
                from .fnv_hands import scan_code
                code = scan_code(key_name)
            return live.input(action, control, code, duration_ms, target, frames=frames, button=button, mouse=mouse)
        return await invoke('fnv_input', press, 'act')

    @server.tool(annotations=act)
    async def fnv_script_set(target: Annotated[str, Field(min_length=1, max_length=200)],
                             kind: Literal['stage', 'objective_completed', 'objective_displayed', 'variable', 'global'],
                             value: Any, name: Annotated[str, Field(max_length=128)] | None = None,
                             index: Annotated[int, Field(ge=0, le=100000)] | None = None) -> result_type:
        """Set a quest stage, an objective's completed/displayed state (index), a script variable (name; target is
        the quest or reference EditorID) or a global (target = EditorID). The old value is journaled."""
        return await invoke('fnv_script_set', lambda: live.script_set(target, kind, value, name, index), 'act')

    @server.tool(annotations=read)
    async def fnv_journal(limit: Annotated[int, Field(ge=1, le=1000)] = 100, include_undone: bool = True) -> result_type:
        """Every change made through the act tools (memory writes, engine calls, settings, console lines, actor
        actions, input), with old and new values and whether it can be or was undone."""
        return await invoke('fnv_journal', lambda: live.journal_list(limit, include_undone))

    @server.tool(annotations=act)
    async def fnv_undo(count: Annotated[int, Field(ge=1, le=100)] = 1,
                       journal_id: Annotated[str, Field(max_length=20)] | None = None,
                       all: bool = False) -> result_type:
        """Undo the last `count` undoable changes, or the one with journal_id, in reverse order. all=true: undo every
        undoable change made in the current game process and list those that cannot be undone (fnv_journal shows
        them all)."""
        def run():
            if not all:
                return live.undo(count, journal_id)
            if count != 1 or journal_id is not None:
                raise ValueError('all=true undoes everything; leave out count and journal_id')
            return live.revert_all()
        return await invoke('fnv_undo', run, 'act')

    # ------------------------------------------------------------------ milestone 7
    Action = Literal['start', 'read', 'stop']

    @server.tool(annotations=act)
    async def fnv_script_profile(action: Action = 'read', seconds: Annotated[float, Field(ge=0.2, le=60)] = 3.0,
                                 top: Annotated[int, Field(ge=1, le=200)] = 25) -> result_type:
        """Which scripts run and how long they take. Installs watch hooks on Script::Run and
        Script::RunScriptEffectStart/Finish/Update (proven), which only time each call. action=start clears the
        counters and returns; read gives the top scripts by total time since start (on first use it starts,
        waits `seconds`, then reads): calls per frame, ms per frame, average and worst call, owning quest. stop
        removes the hooks."""
        return await invoke('fnv_script_profile', lambda: deep.script_profile(action, seconds, top), action=action)

    @server.tool(annotations=act)
    async def fnv_script_trace(script: Annotated[str, Field(min_length=1, max_length=200, description=(
            'script EditorID or form id, or a quest / reference / base form whose script to trace'))],
                               action: Action = 'read', source: Annotated[str, Field(max_length=200000)] | None = None,
                               limit: Annotated[int, Field(ge=1, le=4096)] = 200) -> result_type:
        """Line-by-line trace of one script as the game runs it (watch hook on ScriptRunner::EvaluateLine,
        proven, filtered to that script). Each executed statement: frame, statement number, kind (set, if,
        command name...), the reference it ran on, whether it succeeded, time. Pass the script's source text to
        map statement numbers to source lines and list the lines that did not run (branches not taken). Calls
        after the first read return only new statements. stop removes the hook."""
        return await invoke('fnv_script_trace', lambda: deep.script_trace(script, action, source, limit),
                            action=action)

    @server.tool(annotations=act)
    async def fnv_damage_log(action: Action = 'read', limit: Annotated[int, Field(ge=1, le=1000)] = 50) -> result_type:
        """Each recent hit broken down: attacker, target, weapon, body part, health / limb / fatigue / armor
        damage, weapon base damage, multiplier, flags (critical, sneak attack, fatal, cripple, dismember), and the
        health damage the engine applied. A watch hook on
        Actor::DamageHealthAndFatigue (proven), which also copies the hit data its caller, the actor hit handler, passed on. start clears, stop removes the hook."""
        return await invoke('fnv_damage_log', lambda: deep.damage_log(action, limit), action=action)

    @server.tool(annotations=read)
    async def fnv_patch_map(include_bytes: bool = False, limit: Annotated[int, Field(ge=1, le=5000)] = 400) -> result_type:
        """Which engine code and pointers other plugins (xNVSE, JIP LN, Stewie's, ours...) changed in memory:
        the running game's code compared with your unpacked FalloutNV.exe (fnv.clean_exe). Each patch: address,
        engine function, kind (jump, call, pointer, bytes), owner module, and the jump chain when several
        modules hook the same place; functions patched by more than one owner; hooked imports and vtable
        slots."""
        return await invoke('fnv_patch_map', lambda: deep.patch_map(include_bytes, limit))

    @server.tool(annotations=act)
    async def fnv_hooks(action: Literal['list', 'remove'] = 'list',
                        hook_id: Annotated[str, Field(max_length=10)] | None = None) -> result_type:
        """The engine companion's watch hooks (function, calls seen, records). remove takes a hook id or 'all'."""
        return await invoke('fnv_hooks', lambda: deep.hooks_tool(action, hook_id), action=action)

    # ------------------------------------------------------------------ milestone 8
    @server.tool(annotations=read)
    async def fnv_loadorder() -> result_type:
        """The dev profile's plugins in load order: index prefix, the real file behind MO2's virtual folder (the
        MO2 mod or the game Data folder), masters, and problems (missing masters, missing files). Uses the running
        game's list when the game runs."""
        return await invoke('fnv_loadorder', data.loadorder)

    RecordRef = Annotated[str, Field(min_length=1, max_length=200, description=(
        'EditorID, load-order form id, or Plugin.esp:LOCALID'))]

    @server.tool(annotations=read)
    async def fnv_record(action: Literal['show', 'find', 'users'] = 'show',
                         record: RecordRef | None = None,
                         query: Annotated[str, Field(max_length=200)] = '',
                         record_type: Annotated[str, Field(pattern=r'^[A-Za-z_]{4}$')] | None = None,
                         plugin: Annotated[str, Field(max_length=120)] | None = None,
                         types: list[Annotated[str, Field(pattern=r'^[A-Za-z_]{4}$')]] | None = None,
                         all_versions: bool = True,
                         max_bytes: Annotated[int, Field(ge=16, le=4096)] = 256,
                         limit: Annotated[int, Field(ge=1, le=500)] | None = None,
                         offset: Annotated[int, Field(ge=0)] = 0) -> result_type:
        """Records of the plugins in the load order (masters, DLC, mods). show (default) record: one record decoded:
        the winning version's subrecords (strings, form references with their EditorIDs, script header, source and
        bytecode size, common DATA layouts), every plugin that edits it in load order, and which subrecords each
        override changed. find query: records in every active plugin by EditorID or name substring, load-order form
        id (0x00104C6C), or Plugin.esp:LOCALID; filter by record_type (WEAP, NPC_, QUST...) or plugin; each result
        names the plugin that defines it, the winning plugin and the override count; limit 1 to 200, default 25.
        users record: everything that references it: placed references of it, scripts (SCRO), leveled lists,
        containers and inventories, conditions (dialogue, packages, perks), packages, form lists, enable parents,
        owners, door links and any other field holding its form id; each user says how it uses the record, which
        plugins' versions reference it and whether the winning version still does; filter by types; limit default
        100. The first call indexes the load order (seconds; cached until a plugin changes)."""
        def run():
            if action == 'find':
                if limit is not None and limit > 200:
                    raise ValueError('find takes limit 1 to 200')
                return data.record_find(query, record_type, plugin, 25 if limit is None else limit, offset)
            if not record:
                raise ValueError('%s needs record (EditorID, load-order form id, or Plugin.esp:LOCALID)' % action)
            if action == 'users':
                return records.record_users(record, types, 100 if limit is None else limit, offset)
            return data.record_show(record, all_versions, max_bytes)
        return await invoke('fnv_record', run)

    @server.tool(annotations=read)
    async def fnv_conflicts(plugin: Annotated[str, Field(max_length=120)] | None = None,
                            record_type: Annotated[str, Field(pattern=r'^[A-Za-z_]{4}$')] | None = None,
                            include_identical: bool = False, limit: Annotated[int, Field(ge=1, le=200)] = 50,
                            offset: Annotated[int, Field(ge=0)] = 0) -> result_type:
        """Records that more than one active plugin edits, worst first: the plugins in load order, the winner, the
        subrecords each override changes and the changes an earlier plugin makes that the winner loses (the usual
        reason a mod seems not to work). plugin limits it to records that plugin touches; identical overrides are
        skipped unless asked for."""
        return await invoke('fnv_conflicts', lambda: records.conflicts(plugin, record_type, limit, offset, include_identical))

    @server.tool(annotations=read)
    async def fnv_asset_check(plugin: Annotated[str, Field(min_length=1, max_length=1000, description=(
            'A plugin name in the load order or a plugin file path'))],
                              include_nif_textures: bool = True,
                              limit: Annotated[int, Field(ge=1, le=1000)] = 200) -> result_type:
        """Asset files a plugin names (meshes, textures, icons, sounds, music) that exist in none of the enabled
        MO2 mods, the game Data folder or any BSA archive; also textures named inside the plugin's loose meshes that
        are missing. Each missing file lists the records using it."""
        return await invoke('fnv_asset_check', lambda: records.asset_check(plugin, include_nif_textures, limit))

    @server.tool(annotations=control)
    async def fnv_profile_set(mods: dict[Annotated[str, Field(min_length=1, max_length=200)], bool] | None = None,
                              plugins: dict[Annotated[str, Field(min_length=1, max_length=200)], bool] | None = None,
                              dry_run: bool = False) -> result_type:
        """Enable or disable MO2 mods ({"Mod Name": true}) and plugins ({"Mod.esp": false}) in the dev profile.
        Refuses while the game runs. Backs up modlist.txt, plugins.txt and loadorder.txt first and reports plugins
        whose masters are no longer active. dry_run shows the changes without writing."""
        return await invoke('fnv_profile_set', lambda: records.profile_set(mods, plugins, live.game, dry_run),
                            'control', action='dry_run' if dry_run else 'apply')

    @server.tool(annotations=read)
    async def fnv_script_lint(source: Annotated[str, Field(min_length=1, max_length=200000)],
                              check_forms: bool = True) -> result_type:
        """Check script source before compiling, offline: scn line, begin/end and if/endif structure, block
        types, declared variables, unknown commands (vanilla, xNVSE and JIP LN tables from the engine map), argument
        counts, and EditorIDs no active plugin defines or of the wrong record type. Each problem has a line."""
        return await invoke('fnv_script_lint', lambda: data.script_lint(source, check_forms=check_forms))

    @server.tool(annotations=read)
    async def fnv_compile_ingame(source: Annotated[str, Field(min_length=1, max_length=200000)],
                                 include_bytecode: bool = True) -> result_type:
        """Compile script source with the running game's own compiler (nothing runs): the compiler's errors, or
        the variables with their indexes, the referenced forms, the statement count and the bytecode. EditorIDs the
        game does not keep are replaced by their form ids first (listed)."""
        return await invoke('fnv_compile_ingame', lambda: data.compile_ingame(source, include_bytecode))

    @server.tool(annotations=act)
    async def fnv_script_hot_swap(script: Annotated[str, Field(min_length=1, max_length=200, description=(
            'script EditorID or form id, or a quest / reference whose script to replace'))],
                                  source: Annotated[str, Field(min_length=1, max_length=200000)],
                                  force: bool = False) -> result_type:
        """Replace a running script's code with a new compile, without restarting: quests, objects and effects
        using it run the new code from their next run and keep their variable values. Refuses when existing
        variables would move or disappear (instances keep values by index) unless force. Journaled; fnv_undo
        swaps the old code back."""
        return await invoke('fnv_script_hot_swap', lambda: data.hot_swap(script, source, force), 'act')

    @server.tool(annotations=control)
    async def fnv_scripts_compile(only: list[Annotated[str, Field(max_length=128)]] | None = None) -> result_type:
        """Compile the project spec's scripts with the running game's compiler and store them in geck-mcp-nv's
        compiled-script cache, so geck_plugin_build emits compiled scripts without GECK. The last build must be
        installed and loaded (its forms must exist). Each entry is checked with the builder's verification and
        records an in-game provenance. Scripts with Quest.variable references cannot be compiled in game."""
        return await invoke('fnv_scripts_compile', lambda: data.compile_spec_scripts(only), 'control')

    # ------------------------------------------------------------------ P1 completion

    @server.tool(annotations=act)
    async def fnv_dialogue_choose(speaker: Ref | None = None,
                                  choices: list[Annotated[str, Field(max_length=200)]] | None = None,
                                  start: bool = True, close: bool = False,
                                  timeout_seconds: Annotated[int, Field(ge=3, le=180)] = 30) -> result_type:
        """Test dialogue end to end: the player talks to the speaker (start), then each choice is picked in order
        once the speaker has finished talking. A choice is an option index (0 = first shown), a prompt substring, or
        an info form id. The pick goes through DialogMenu::HandleSelection, the call a click makes. Returns each
        step's options, the pick, the speaker's answer (response text) and the menu state. With no choices it
        returns the options shown. close ends the conversation afterwards."""
        return await invoke('fnv_dialogue_choose',
                            lambda: social.dialogue_choose(speaker, choices, start, timeout_seconds, close), 'act')

    @server.tool(annotations=read)
    async def fnv_ai_packages(actor: Ref) -> result_type:
        """Every AI package on the actor's list, in order: type, flags, schedule (and whether it is in schedule now,
        against the game clock), location, each condition with its current value, and whether it runs, is eligible,
        is shadowed by an earlier one, or why not. Compared with the package the engine is running now."""
        return await invoke('fnv_ai_packages', lambda: social.ai_packages(actor))

    @server.tool(annotations=read)
    async def fnv_form_live_diff(form: Ref) -> result_type:
        """A form as it is in memory now versus its winning plugin record: name, value, weight, health, damage,
        script (from the decomp's component layouts), global values and quest data. Lists the fields that differ,
        meaning scripts, console commands or the engine changed them at runtime."""
        return await invoke('fnv_form_live_diff', lambda: social.form_live_diff(form))

    # ------------------------------------------------------------------ agent wave

    @server.tool(annotations=act)
    async def fnv_trace(function: Annotated[str, Field(max_length=300, description=(
            'proven engine function name (Actor::DamageHealthAndFatigue) or address'))] | None = None,
                        action: Literal['start', 'read', 'stop', 'list'] = 'start',
                        when: list[dict[str, Any]] | None = None,
                        filter: dict[str, Any] | None = None,
                        limit: Annotated[int, Field(ge=1, le=500)] = 50,
                        include_calls: bool = True,
                        stack: bool = False,
                        allow_unproven: bool = False) -> result_type:
        """Watch any proven engine function while the game runs (allow_unproven=true: any function of the engine map;
        the hook only observes, but a wrong address can still crash the game): start installs an observe-only hook,
        read returns the calls since the last read: `this` and every argument decoded with the function's decomp
        signature (forms and refs named), the result, the time, and the caller named from the engine map, plus counts
        by caller and by `this`. when keeps only calls where every condition holds: [{on: this|caller|param0..7, ref
        or value}], for example [{on: this, ref: MyNPC}] (filter, one condition as an object, is the old spelling).
        stack=true also records the call stack of each hit (commonStacks groups the hits by the path that led to
        them), a breakpoint that never stops the game; several when conditions record that way too. Answers "does the
        engine run X for my object, from where, with what". read and stop find the trace by function; stop removes
        the hook; list shows running traces."""
        conditions = when if when is not None else ([filter] if filter is not None else None)

        def run():
            if filter is not None and when is not None:
                raise ValueError('when and filter both given; filter is the old spelling of one when condition')
            if action == 'list':
                out = agent.trace(None, 'list')
                out['traces'] += [dict(t, stack=True) for t in control_tools.tracepoint(None, 'list')['tracepoints']]
                return out
            if action == 'start':
                recorded = stack or len(conditions or ()) > 1
            else:
                recorded = bool(function) and control_tools.has_tracepoint(function, allow_unproven)
            if recorded:
                if not function:
                    raise ValueError('function is required')
                return control_tools.tracepoint(function, action, conditions, min(limit, 200), allow_unproven,
                                                True if action != 'start' else stack)
            return agent.trace(function, action, conditions[0] if conditions else None, limit, include_calls,
                               allow_unproven)
        return await invoke('fnv_trace', run, action=action)

    @server.tool(annotations=act)
    async def fnv_watch(items: list[Any],
                        seconds: Annotated[float, Field(gt=0, le=120)] = 5.0,
                        interval_ms: Annotated[int, Field(ge=0, le=10000)] = 100,
                        until: Annotated[str, Field(max_length=500)] | None = None,
                        keep_unpaused: bool = True,
                        frames: Annotated[int, Field(ge=1, le=60000)] | None = None,
                        every: Annotated[int, Field(ge=1, le=600)] = 1) -> result_type:
        """Sample values over time and return how each changed: items are expressions ("player.GetAV Health",
        "MyQuest.counter", "GetStage MyQuest") or mappings {eval, ref}, {actor_value, ref}, {variable, of},
        {position: ref}, {mem: ref or 0xADDR, path: Class.field} (decomp layouts). Stops early when `until` (an
        expression) is true. Each item gives first, last, min, max, number of changes and a timeline of changes
        with time and frame; paused=true when the frame counter did not move. frames=N (with every=k): exact, frame by
        frame, inside the game (the companion sampler): items are then memory reads in predicate text
        ("[[0x011DEA3C]+0x30]:f32") or player.x, player.y, player.z, player.yaw, frame; at most 16."""
        if frames:
            return await invoke('fnv_watch', lambda: frame_sampler.sample(items, frames, every), action='frames')
        return await invoke('fnv_watch', lambda: agent.watch(items, seconds, interval_ms, until, keep_unpaused),
                            action='sample')

    @server.tool(annotations=read)
    async def fnv_screenshot(name: Annotated[str, Field(max_length=60)] | None = None,
                             max_width: Annotated[int, Field(ge=320, le=3840)] = 1280,
                             method: Literal['auto', 'engine', 'window'] = 'auto') -> result_type:
        """The game's current frame, returned as an image you can look at and saved as PNG. auto asks the engine
        to write its back buffer (SaveScreenshot, works while the window is covered; needs iMultiSample=0 and
        bAllowScreenShot=1 in the dev profile), else captures the game window."""
        import base64
        from mcp.types import ImageContent
        image = []

        def shot():
            data = agent.screenshot(name, max_width, method)
            path = (data or {}).get('path')
            if path:                          # read inside the envelope: a missing file is an error reply (T2)
                with open(path, 'rb') as f:
                    image.append(base64.b64encode(f.read()).decode('ascii'))
            return data
        return await invoke('fnv_screenshot', shot, 'read', shield=True,
                            attach=lambda: [ImageContent(type='image', mimeType='image/png', data=b) for b in image])

    @server.tool(annotations=act)
    async def fnv_test_save(action: Literal['save', 'load', 'list', 'delete'] = 'list',
                            name: Annotated[str, Field(pattern=r'^[A-Za-z0-9_\-]{1,60}$')] | None = None,
                            timeout_seconds: Annotated[int, Field(ge=20, le=600)] = 180) -> result_type:
        """Named test saves in the dev profile, kept apart from other saves by the prefix fnvmcp_test_: save the
        current game, load one (waits until gameplay), list or delete them. Tests start from one with
        start: {test_save: name}."""
        kind = 'read' if action == 'list' else 'act'
        return await invoke('fnv_test_save', lambda: agent.test_save(action, name, timeout_seconds), kind)

    @server.tool(annotations=act)
    async def fnv_crash(action: Literal['explain', 'latest', 'history', 'context', 'capture_on', 'capture_off',
                                        'capture_status'] = 'explain',
                        path: Annotated[str, Field(max_length=1000)] | None = None,
                        max_frames: Annotated[int, Field(ge=5, le=400)] = 40,
                        limit: Annotated[int, Field(ge=1, le=200)] | None = None,
                        include_shutdown: bool = True) -> result_type:
        """The crash doctor. explain (default): the newest (or given) crash log, each stack frame as a decomp function
        with proof status or as plugin DLL + offset, the forms involved, and the likely culprit with reasons; the
        matching companion context file is attached. latest: the newest crash log (Yvile's Crash Logger
        CrashLogger.log or its Crash Logs folder; Cobb's CobbCrashLogger.log) parsed: exception, registers, call
        stack, plugins, modules; optional path. history: every crash log grouped by signature (exception, crash-site
        function, next frames): count, first and last time, the likely culprits, new=true for signatures no earlier
        call has seen, and crashes during exit cleanup flagged (usually not your mod); limit default 40. context:
        what the crash log cannot know, the companion's context files (the hooked engine calls active on the faulting
        thread with this/argument form ids, the request and console line being run, recent events); limit 1 to 20,
        default 3. capture_on adds watch hooks on the script entry points so the file names the running script and
        its reference; capture_off removes them; capture_status tells whether they are on."""
        if action == 'explain':
            return await offline_invoke(offline.crash_explain, path, max_frames)
        if action == 'latest':
            return await offline_invoke(offline.crash_latest, path, max_frames)

        def run():
            if action == 'history':
                return agent.crash_history(40 if limit is None else limit, include_shutdown)
            if action == 'context' and limit is not None and limit > 20:
                raise ValueError('context takes limit 1 to 20')
            mode = {'context': 'latest', 'capture_on': 'enable', 'capture_off': 'disable', 'capture_status': 'status'}
            return deep.crash_context(mode[action], 3 if limit is None else limit)
        return await invoke('fnv_crash', run, action=action)

    @server.tool(annotations=act)
    async def fnv_move(ref: Ref = 'player', to: Ref | None = None,
                       cell: Annotated[str, Field(pattern=r'^[A-Za-z0-9_]{1,120}$')] | None = None,
                       position: Xyz | dict[str, float] | None = None,
                       rotation: Xyz | dict[str, float] | None = None,
                       timeout_seconds: Annotated[int, Field(ge=10, le=600)] = 180) -> result_type:
        """Teleport the player or any ref: to another ref (MoveTo), to a cell (the player by coc; other refs need a
        position in it, PositionCell), or to a position [x, y, z] in its current cell (null keeps an axis);
        rotation [x, y, z] in degrees, for example [null, null, 90]. Waits for the new cell to load when the player
        changes cells. Journaled: fnv_undo puts it back."""
        return await invoke('fnv_move', lambda: agent.move(ref, to, cell, xyz_dict(position), xyz_dict(rotation),
                                                           timeout_seconds), 'act')

    @server.tool(annotations=read)
    async def fnv_condition_eval(form: Ref, subject: Ref | None = 'player', target: Ref | None = None,
                                 tab_subjects: list[Annotated[str, Field(max_length=200)]] | None = None,
                                 only_failed: bool = False) -> result_type:
        """Evaluate every condition list of a record against chosen refs and show which condition fails: dialogue
        responses (INFO), packages, perk requirements and each perk entry's tabs (tab 0 = perk owner, other tabs'
        subjects from tab_subjects), effects of spells / enchantments / ingestibles / ingredients, quest conditions,
        stage log entries and objective targets (run on the target ref), recipes, idles, message box buttons and
        camera paths. Each condition shows its function, run-on, current value, comparison and pass/fail, combined
        with the game's OR/AND rule."""
        return await invoke('fnv_condition_eval', lambda: agent.condition_eval(form, subject, target, tab_subjects,
                                                                               only_failed))

    @server.tool(annotations=read)
    async def fnv_script_instances(of: Ref | None = None,
                                   script: Annotated[str, Field(max_length=200)] | None = None,
                                   include_variables: bool = True,
                                   limit: Annotated[int, Field(ge=1, le=300)] = 60) -> result_type:
        """Running script instances: with `of`, the quest's or ref's script instance and variables (plus an actor's
        effect scripts); without, every running quest script (the quests TES::RunScripts runs each frame) and the
        object scripts on refs in the loaded cells, with instance counts per script (many instances of one script
        point at spawned refs that are never deleted). scriptsRunning=false means the game runs no scripts at all.
        script filters by script form id or quest name."""
        return await invoke('fnv_script_instances', lambda: agent.script_instances(of, script, include_variables, limit))

    @server.tool(annotations=read)
    async def fnv_menu(action: Literal['state', 'tiles'] = 'state',
                       menu: Annotated[str, Field(min_length=1, max_length=40, description=(
                           'tiles: hud, dialog, inventory, pipboy menus by name, a menu id 1001..1084, or root'))] = 'hud',
                       path: Annotated[str, Field(max_length=200)] | None = None,
                       depth: Annotated[int, Field(ge=0, le=30)] = 8,
                       max_tiles: Annotated[int, Field(ge=1, le=3000)] = 400,
                       traits: list[Annotated[str, Field(max_length=60)]] | None = None) -> result_type:
        """The game's menus. state (default): interface mode, the active menu, the menu stack, every visible menu by
        name, whether a loading screen is up, and an open message box with its title, text and buttons (labels and
        values), read from the interface manager and the message menu's tiles. tiles: a menu's live UI tile tree:
        each tile's path, class (TileRect, TileImage, TileText, ...) and trait values (_x, _y, _visible, _string,
        user traits...) named with the game's own trait table; path filters by a substring of the tile path; traits
        keeps only those trait names. For debugging UI mods. fnv_ui_do acts on menus."""
        if action == 'tiles':
            return await invoke('fnv_menu', lambda: world.ui_tiles(menu, path, depth, max_tiles, traits))
        return await invoke('fnv_menu', lambda: agent.menus())

    @server.tool(annotations=act)
    async def fnv_rng(seed: Annotated[int, Field(ge=0, le=0xFFFFFFFF)] | None = None) -> result_type:
        """The game's random number generator (one Mersenne Twister for leveled lists, VATS, combat, AI, lockpicks,
        GetRandomPercent): without seed its state; with seed it is seeded like MersenneTwister::Seed, so rolls in the
        same request repeat exactly (later frames keep drawing numbers)."""
        kind = 'read' if seed is None else 'act'
        return await invoke('fnv_rng', lambda: agent.rng(seed), kind)

    @server.tool(annotations=act)
    async def fnv_time(action: Literal['calendar', 'pause', 'resume', 'force_resume', 'hold', 'release', 'step',
                                       'fixed', 'speed', 'status'] = 'calendar',
                       set_hour: Annotated[float, Field(ge=0, lt=24)] | None = None,
                       advance_hours: Annotated[float, Field(gt=0, le=720)] | None = None,
                       timescale: Annotated[float, Field(ge=0, le=10000)] | None = None,
                       mode: Literal['globals', 'engine_wait'] = 'globals',
                       evaluate_packages: bool = True, allow_unproven: bool = False,
                       frames: Annotated[int, Field(ge=1, le=10000)] | None = None,
                       fps: Annotated[int, Field(ge=0, le=1000)] | None = None,
                       x: Annotated[float, Field(ge=0.05, le=10)] | None = None,
                       timeout_seconds: Annotated[int, Field(ge=1, le=600)] | None = None) -> result_type:
        """Game time for tests. calendar (default): with no other argument it reads the calendar; set_hour jumps to an
        hour, advance_hours advances (day, month and year roll over like the game's calendar), timescale changes the
        timescale. mode=globals moves the clock and re-evaluates nearby actors' packages; mode=engine_wait runs the
        game's own one-hour wait step per hour (process lists catch up, healing, hardcore needs), an unnamed
        function, so it needs allow_unproven. The game clock and the frame gate: pause stops the game clock (the
        world keeps rendering, so the camera and photo tools see a still scene); resume starts it again, only if this
        tool paused it; force_resume starts it again whoever paused it (after a server restart lost the pause record).
        hold closes the frame gate, so nothing runs between frames (requests are still served; the gate reopens by
        itself after timeout_seconds with no calls, default 120). release opens the gate. step frames=N [fps=F]:
        advance exactly N frames and stop, optionally at a fixed frame step of F fps. fixed fps=F (0 turns it off):
        every frame takes 1000/F ms of game time. speed x=F (0.05 to 10): the time multiplier. status: the gate,
        clock counter, fixed step, multiplier, frame and real frame rate. Changing actions need fnv.engine_control
        full."""
        setting = action == 'calendar' and not (set_hour is None and advance_hours is None and timescale is None)

        def run():
            if action != 'calendar':
                return time_control.run(action, frames=frames, fps=fps, x=x, timeout_seconds=timeout_seconds)
            if not setting:
                return {'calendar': agent.calendar(), 'changed': False}
            return agent.time_control(set_hour, advance_hours, timescale, mode, evaluate_packages, allow_unproven)
        return await invoke('fnv_time', run, action='calendar_set' if setting else action)

    @server.tool(annotations=act)
    async def fnv_leveled_roll(form: Ref, level: Annotated[int, Field(ge=1, le=255)] | None = None,
                               rolls: Annotated[int, Field(ge=1, le=5000)] = 200,
                               seed: Annotated[int, Field(ge=0, le=0xFFFFFFFF)] | None = None) -> result_type:
        """Roll a leveled list (items, NPCs, creatures) many times with the game's own code
        (TESLeveledList::CalculateCurrentForm, proven) at a level (default the player's): how often each form and
        count comes out and how often nothing, next to the list's entries, chance none and flags. With seed the
        rolls repeat exactly. Changes nothing in the world (it only draws random numbers)."""
        return await invoke('fnv_leveled_roll', lambda: agent.leveled_roll(form, level, rolls, seed),
                            action='seed' if seed is not None else 'roll')

    # ------------------------------------------------------------------ engine AI wave
    @server.tool(annotations=read)
    async def fnv_detection(observer: Ref, target: Ref = 'player', recompute: bool = True) -> result_type:
        """Why does this NPC see (or not see) the target? The detection records the game keeps on the observer's
        process (lost/unseen/noticed/seen, score, line of sight in the view cone), the reverse direction, light,
        sneak, distance and the detection settings. With recompute (NPC observers) the game's own
        CalculateDetectionLevel runs now and the 25 inputs it gives the detection formula are captured: perception,
        sneak, line of sight, view cone, distance, light, invisibility, movement, levels, armor penalty."""
        return await invoke('fnv_detection', lambda: engine_ai.detection(observer, target, recompute))

    @server.tool(annotations=read)
    async def fnv_reaction(actor: Ref, other: Ref = 'player') -> result_type:
        """How two actors relate and why: hostile or not (the engine's IsHostileTo, the check behind
        GetShouldAttack), the faction fight reaction (neutral/enemy/ally/friend), the faction pair that decides
        it, disposition, crime anger, teammates, aggression and the rule branch that decided hostility."""
        return await invoke('fnv_reaction', lambda: engine_ai.reaction(actor, other))

    @server.tool(annotations=read)
    async def fnv_combat(actor: Ref) -> result_type:
        """An actor's combat state from its combat controller: target, current procedure (attack ranged/melee,
        cover, search, flee...), current action, weapon, combat style, and the combat group's targets (last known
        position, attackers) and members."""
        return await invoke('fnv_combat', lambda: engine_ai.combat(actor))

    @server.tool(annotations=act)
    async def fnv_perk_entries(action: Literal['start', 'read', 'stop', 'evaluate'] = 'read',
                               entry_point: Annotated[int, Field(ge=0, le=73)] | None = None,
                               limit: Annotated[int, Field(ge=1, le=500)] = 50,
                               value: float | None = None, actor: Ref = 'player',
                               filters: list[Ref] | None = None) -> result_type:
        """Which perks changed a calculation and by how much. start hooks the game's perk entry-point dispatcher
        (optionally one entry point, e.g. 0 weapon damage, 8 to-hit chance, 9 XP, 17 buy price); read lists each
        check where a perk applied: the entry point, the actor and filter forms, the value before, each perk entry
        (perk, rank, set/add/multiply, value) and the value after; stop removes the hooks. evaluate runs one value
        entry point now with your value (and its filter forms, e.g. [weapon, target] for weapon damage) through the
        game's own dispatcher and returns the result and the perks that changed it."""
        return await invoke('fnv_perk_entries', lambda: engine_ai.perk_entries(action, entry_point, limit, value,
                                                                                actor, filters), action=action)

    @server.tool(annotations=read)
    async def fnv_pathing(actor: Ref) -> result_type:
        """The path an actor is following and why it fails: mover state (building, moving, failed), the pathing
        request (start and goal with their navmesh triangle, door rules), the solution's path points, the path
        handler's state (waiting, blocked, avoidance failed) and the reasons found."""
        return await invoke('fnv_pathing', lambda: engine_ai.pathing(actor))

    @server.tool(annotations=read)
    async def fnv_raycast(start: XyzFull | dict[str, float] | None = None,
                          end: XyzFull | dict[str, float] | None = None,
                          from_ref: Ref | None = None, to_ref: Ref | None = None,
                          layer: Literal['line_of_sight', 'path_pick', 'projectile'] = 'line_of_sight',
                          ground_at: dict[str, float] | None = None) -> result_type:
        """Cast a ray through the game's Havok world (the engine's own TES::Pick): from start to end [x, y, z], or
        from one ref to another (eye height); returns whether it hits, the hit object and its reference, and the
        hit point. ground_at {x, y} instead returns the terrain height there."""
        if ground_at:
            return await invoke('fnv_raycast', lambda: engine_ai.ground_height(ground_at.get('x', 0.0),
                                                                                ground_at.get('y', 0.0)))
        return await invoke('fnv_raycast', lambda: engine_ai.raycast(xyz_dict(start), xyz_dict(end), from_ref, to_ref,
                                                                     layer))

    @server.tool(annotations=read)
    async def fnv_cell_grid() -> result_type:
        """Which cells are loaded and why: the exterior grid around the player (uGridsToLoad), the current
        interior, the interior and exterior cell buffers, each cell with its grid coordinates and load state."""
        return await invoke('fnv_cell_grid', lambda: engine_ai.cell_grid())

    # ------------------------------------------------------------------ phase 2: control
    Filter = dict[str, Any]

    @server.tool(annotations=act)
    async def fnv_hook_set(function: Annotated[str, Field(min_length=1, max_length=300, description=(
            'Engine function: proven name (Actor::GetLevel) or address'))],
                           action: Literal['skip', 'result', 'arg', 'script', 'read', 'remove', 'list'] = 'skip',
                           value: Any = None,
                           value_type: Literal['int', 'float', 'bool', 'ptr', 'ref', 'form'] | None = None,
                           arg: Annotated[int, Field(ge=0, le=31)] | None = None,
                           filter: Filter | None = None,
                           limit: Annotated[int, Field(ge=0, le=1000000)] = 0,
                           script: Annotated[str, Field(max_length=254)] | None = None,
                           pop_bytes: Annotated[int, Field(ge=0, le=1024)] | None = None,
                           allow_unproven: bool = False) -> result_type:
        """Change what an engine function does, live: skip = do not run it and return value (pops its stack
        arguments per its calling convention; float results go in st0); result = run it and replace the integer or
        pointer result; arg = replace parameter `arg` with value before it runs; script = run a console/script line
        on the main thread each time it is called. Works with any action: filter {on: this|caller|param0..7, ref|value}
        (only matching calls), limit (only the first N calls), script (also run a line). read = the calls it
        changed so far; remove = take it out. Journaled: fnv_undo removes it. Proven functions only unless
        allow_unproven."""
        if action in ('read', 'list'):
            return await invoke('fnv_hook_set', lambda: control_tools.hook_set(function, action, allow_unproven=allow_unproven))
        return await invoke('fnv_hook_set', lambda: control_tools.hook_set(function, action, value, value_type, arg, filter,
                                                                           limit, script, pop_bytes, allow_unproven), 'act')

    @server.tool(annotations=act)
    async def fnv_patch(address: Annotated[str, Field(min_length=1, max_length=300, description=(
            '0x address or a function name (patches its first bytes)'))],
                        bytes: Annotated[str, Field(max_length=200)] | None = None,
                        nop: Annotated[int, Field(ge=1, le=64)] | None = None,
                        ret: Annotated[int, Field(ge=0, le=1024)] | None = None,
                        jmp: Annotated[str, Field(max_length=300)] | None = None,
                        call: Annotated[str, Field(max_length=300)] | None = None,
                        expect: Annotated[str, Field(max_length=200)] | None = None,
                        force: bool = False) -> result_type:
        """Patch the running game's code at an address: raw bytes (hex), N NOPs, a ret (with the bytes to pop), or a
        jmp/call to an address or function. Refuses where a companion hook sits, and where another module already
        changed the bytes (compared with the clean exe) unless force. expect = the bytes that must be there now.
        Journaled with the original bytes: fnv_undo restores them."""
        return await invoke('fnv_patch', lambda: control_tools.patch(address, bytes, nop, ret, jmp, call, expect, force), 'act')

    @server.tool(annotations=read)
    async def fnv_engine_get(function: Annotated[str, Field(min_length=1, max_length=300, description=(
            'A proven getter: Actor::GetLevel, TESObjectREFR::GetDistanceFromReference, ...'))],
                             this: Ref | None = None, args: list[Any] | None = None) -> result_type:
        """Call a proven read-only engine getter (Get/Is/Has/Can/Count/Find... names) with typed arguments: plain
        values are typed from the decomp signature, or give {type, value} with type int, bool, float, double, ptr,
        ref or form. Runs only when the clean exe's machine code shows the getter and its callees write no memory
        but their own stack (out-pointer getters such as NiMatrix3::GetRow are refused, MAY_WRITE), and always as
        a guarded call (a bad this faults back as CALL_FAULTED). Pointer results are described (form, class). For
        anything else use fnv_engine_call (needs fnv.engine_control = "full")."""
        return await invoke('fnv_engine_get', lambda: control_tools.engine_get(function, this, args))

    # ------------------------------------------------------------------ phase 2: world, UI, memory
    @server.tool(annotations=act)
    async def fnv_globals(query: Annotated[str, Field(max_length=120)] | None = None,
                          set: dict[Annotated[str, Field(min_length=1, max_length=120)], float] | None = None,
                          limit: Annotated[int, Field(ge=1, le=2000)] = 200) -> result_type:
        """Global variables as the running game holds them (TESDataHandler's list): editor id, form id, type and
        value, filtered by a name substring. set {"GameHour": 12} writes values (shorts and longs are whole
        numbers); each write is journaled and undoable."""
        return await invoke('fnv_globals', lambda: world.globals(query, set, limit), 'act' if set else 'read')

    @server.tool(annotations=read)
    async def fnv_dialogue(action: Literal['now', 'tree'] = 'now',
                           speaker: Ref | None = None,
                           topic: Annotated[str, Field(min_length=1, max_length=200, description=(
                               'tree: topic EditorID or form id (DIAL); GREETING by default'))] = 'GREETING',
                           scope: Literal['speaker', 'available', 'all'] = 'speaker',
                           include_text: bool = True,
                           limit: Annotated[int, Field(ge=1, le=300)] = 60) -> result_type:
        """Dialogue. now (default): the conversation on screen now: speaker, dialogue state, the topic being spoken
        and its info, the NPC line and name as shown, and the player's choices in order (prompt, topic, info, quest,
        already heard); says why other topics are not listed. tree speaker: all responses the speaker could give for
        a topic, from every quest that adds to it: each response's prompt, quest and priority, flags (say once,
        goodbye, random...), speech challenge, response text, every condition with its current value and pass/fail,
        and the engine's own verdict (TESTopicInfo::CheckCondition, proven, run with the speaker and the player).
        wouldSay is the response the engine would pick now. scope: speaker (default) keeps the responses written for
        this speaker (their GetIsID / GetIsVoiceType / GetInFaction conditions pass) and any available now;
        available keeps only those; all lists every response of the topic. fnv_dialogue_choose picks responses."""
        def run():
            if action == 'now':
                return world.dialogue()
            if not speaker:
                raise ValueError('tree needs speaker')
            return social.dialogue_tree(speaker, topic, include_text, limit, scope)
        return await invoke('fnv_dialogue', run)

    @server.tool(annotations=act)
    async def fnv_world_edit(action: Literal['move', 'rotate', 'scale', 'transform', 'enable', 'disable', 'delete',
                                             'duplicate', 'weather', 'imagespace', 'imagespace_stop'],
                             ref: Ref | None = None,
                             position: list[float | None] | None = None,
                             rotation: list[float | None] | None = None,
                             scale: Annotated[float, Field(gt=0, le=10)] | None = None,
                             fade: bool = False,
                             weather: Annotated[str, Field(max_length=200)] | None = None,
                             override: bool = True,
                             imagespace: Annotated[str, Field(max_length=200)] | None = None,
                             strength: float = 1.0,
                             distance: Annotated[int, Field(ge=0, le=5000)] = 0,
                             count: Annotated[int, Field(ge=1, le=20)] = 1) -> result_type:
        """Edit the live world with the functions the console commands use: move (position [x, y, z], null keeps an
        axis), rotate (degrees), scale, transform (any of them), enable, disable (fade), delete (MarkForDelete),
        duplicate (PlaceAtMe of the same base, distance, count), weather (force a weather form, or "release"),
        imagespace / imagespace_stop (an image-space modifier at a strength). Journaled; fnv_undo restores
        position, angle, scale, enable state, weather and removes duplicates."""
        return await invoke('fnv_world_edit', lambda: world.world_edit(action, ref, position, rotation, scale, fade, weather,
                                                                        override, imagespace, strength, distance, count), 'act')

    @server.tool(annotations=read)
    async def fnv_scene_graph(ref: Ref = 'player', depth: Annotated[int, Field(ge=0, le=30)] = 6,
                              max_nodes: Annotated[int, Field(ge=1, le=2000)] = 300, first_person: bool = False,
                              name_filter: Annotated[str, Field(max_length=120)] | None = None) -> result_type:
        """The 3D node tree of a loaded reference: each node's class, name, hidden flag, local and world position
        and scale, children; geometry with vertex and triangle counts, skinning, the shader and its texture paths;
        animation controllers and active sequences; extra data (BSX flags, strings). Lists every texture in use and the
        model path. first_person=true reads the player's first-person skeleton."""
        return await invoke('fnv_scene_graph', lambda: world.scene_graph(ref, depth, max_nodes, first_person, name_filter))

    @server.tool(annotations=read)
    async def fnv_vats_odds(target: Ref | None = None, limb: Annotated[int, Field(ge=-1, le=14)] | None = None,
                            include_terms: bool = True) -> result_type:
        """VATS hit chance for every target and limb VATS offers now (or a given target), computed with the game's
        CombatFormulas::GetVATSChanceToHit and the perk layer, with the percent the menu shows next to what it
        stored. terms explains the gun formula: spread cone at the distance, target area, visibility, limb to-hit
        and the settings involved. Without VATS open, pass target (and limb) to compute as if fully visible."""
        return await invoke('fnv_vats_odds', lambda: world.vats_odds(target, limb, include_terms))

    @server.tool(annotations=control)
    async def fnv_window(action: Literal['status', 'release_mouse', 'capture_mouse', 'unpause'] = 'status',
                         persist: bool | None = None) -> result_type:
        """The game window and the user's mouse: release_mouse switches the game's DirectInput mouse to non-exclusive
        background mode on its own window, so the cursor is free while the game keeps running; capture_mouse puts
        exclusive mode back; unpause closes the pause menu (it opens when the window loses focus). persist=true writes
        bBackground Mouse=1 to the dev profile's Fallout.ini so every launch leaves the mouse free (false undoes it).
        Works without the game for persist."""
        return await invoke('fnv_window', lambda: world.window(action, persist), 'control', action=action)

    # ------------------------------------------------------------------ naming from the running game
    @server.tool(annotations=act)
    async def fnv_probe(action: Literal['plan', 'install', 'status', 'read', 'remove', 'clear', 'config', 'harvest',
                                        'harvests'] = 'status',
                        statuses: list[str] | None = None, functions: list[str] | None = None,
                        start: str | None = None, end: str | None = None, cls: str | None = None,
                        callees_of: list[str] | None = None, limit: int | None = None, part: str | None = None,
                        samples: int = 8, exit_samples: int = 4, every: int = 64, dry_run: bool = False,
                        top: int = 25, int3: bool = False, int3_budget: int | None = None,
                        notes: str | None = None) -> result_type:
        """Probes count the calls of thousands of engine functions at once, so you can see which unnamed functions run.
        plan: (no game needed) choose functions (statuses, default every not-proven status; or functions / start..end /
        cls / callees_of / part 'k/n') and say how each can be probed: an entry jump, its vtable slots and direct call
        sites (functions too small for a jump), or why not. install: plan, check the live bytes against your clean exe,
        build the stubs and patch them all at once with the game's other threads suspended (samples: first calls
        recorded with this/args/vtables, exit_samples of those with the result, every: one in N later calls adds its
        caller). status: counts, most-called functions. read: decoded samples and callers (with the call kind: direct,
        virtual slot, pointer) for functions. remove: put every original byte back (also fnv_undo). clear: zero the
        counters. config: change samples / exit_samples / every live. int3: also probe functions with no entry jump and
        no call site with a one-byte trap (costs microseconds a call; a probe past int3_budget calls removes itself).
        harvest: copy every count, sample and caller table into state/atlas.sqlite (fnv_name_dossier reads them);
        harvests: list them."""
        def action_fn():
            if dry_run and action != 'install':     # it would act for real (clear, remove, config; S6 review C I4)
                raise ValueError('dry_run: only action=install has a dry run; %s would change the probes' % action)
            selection = {k: v for k, v in (('statuses', statuses), ('addresses', functions if action in ('plan', 'install') else None),
                                           ('start', hex_int('start', start) if start else None),
                                           ('end', hex_int('end', end) if end else None),
                                           ('cls', cls), ('callees_of', callees_of), ('limit', limit), ('part', part))
                         if v is not None}
            if action == 'plan':
                return probe_tools.plan(selection, int3)[0]
            if action == 'install':
                return probe_tools.install(selection, samples, exit_samples, every, dry_run, int3, int3_budget)
            if action == 'harvest':
                return naming.harvest(notes)
            if action == 'harvests':
                return {'harvests': naming.harvests()}
            if action == 'status':
                return probe_tools.status(top)
            if action == 'read':
                if not functions:
                    raise ValueError('read needs functions')
                return {'functions': probe_tools.read(functions)}
            if action == 'remove':
                return probe_tools.remove()
            if action == 'clear':
                return probe_tools.clear()
            return probe_tools.config(samples, exit_samples, every)
        return await invoke('fnv_probe', action_fn, 'read', action=action)

    def _public(out):
        if isinstance(out, dict):
            return {k: _public(v) for k, v in out.items() if not str(k).startswith('_')}
        if isinstance(out, list):
            return [_public(v) for v in out]
        return out

    @server.tool(annotations=read)
    async def fnv_name_dossier(function: Annotated[str, Field(min_length=1, max_length=200)],
                               harvest: int | None = None, max_items: int = 12) -> result_type:
        """Everything known about one function, for naming it: engine-map status, size and signature, static callers
        and callees with proven names, the vtable slots it fills (and whether the body is folded across unrelated
        classes), strings and settings its decompiled body uses, fnvsym's claims, names.csv and verified.csv rows; and from
        harvested probe sets (fnv_probe harvest): calls, atlas fits per action, callers with the call resolved (direct, the
        vtable class and slot of a virtual call - also through a register -, or a callback), `this` classes and their
        common base, form types, argument classes, the result's shape (constant, bool, enum, int/float range, pointer
        class), threads and the top caller chains. Ends with suggested next tests (oracle, experiment, family packet,
        layout squeeze)."""
        return await invoke('fnv_name_dossier', lambda: fnvsym_root() and naming.dossier(function, harvest, max_items))

    @server.tool(annotations=act)
    async def fnv_name_oracle(action: Literal['run', 'purity', 'calibrate', 'sweep'] = 'run',
                              function: str | None = None, mode: Literal['auto', 'call', 'sampled'] = 'auto',
                              harvest: int | None = None, radius: float = 8000.0, limit: int = 400) -> result_type:
        """Does the function return a value the game already knows, on the same objects? run: its results - from probe
        samples, or by calling it on the references, base forms, quests, cells, worldspaces and packages around the
        player when its machine code is proven free of side effects (purity) - are compared with script commands
        (GetLevel, GetScale, GetAV x, GetRace, GetStage, GetCellFlag n, ...) and form header fields. A match needs >= 20
        objects, no mismatch, a chance of matching by a random pairing <= 1e-6, and no shuffled pairing matching (PROOF
        4.2). purity: the side-effect check alone. calibrate: the method run blind on proven getters and scored after
        (state/oracle_calibration_*.json). sweep: run it on every not-proven pure getter whose this class is known."""
        def action_fn():
            if action == 'calibrate':
                return oracle.calibrate(limit, radius)
            if action == 'sweep':
                return oracle.sweep(limit, radius, harvest=harvest)
            if not function:
                raise ValueError('%s needs function' % action)
            if action == 'purity':
                engine = live.engine()
                return dict(oracle.purity().check(engine.resolve(function)), function=engine.summary(engine.resolve(function)))
            return _public(oracle.run(function, mode, harvest, radius))
        return await invoke('fnv_name_oracle', lambda: fnvsym_root() and action_fn(), 'read', action=action)

    @server.tool(annotations=act)
    async def fnv_name_test(action: Literal['test', 'battery', 'panels'] = 'test', hypothesis: dict | None = None,
                            functions: list[str] | None = None, panels: list[str] | None = None,
                            reps: int = 3) -> result_type:
        """Experiments on one function (PROOF 4.2): test runs a hypothesis {function, intervention {action skip|result|arg,
        value, arg}, panel (a battery panel name) or target/setup/trigger/observe/expect, predict blocked|normal, reps >= 3}:
        each repetition does the action without the change (control) and with it; confirmed when the control shows the
        normal effect and the change the predicted one every time. battery: skip each function during each panel (set
        scale, disable, enable, set angle, destroyed, ghost, set/damage actor values on an NPC, a creature and the player,
        XP, items) - the panels it blocks identify it, blind; scored against names afterwards. panels: the battery."""
        def action_fn():
            from .fnv_experiment import PANELS
            if action == 'panels':
                return {'panels': PANELS}
            if action == 'battery':
                return experiments.blind_battery(functions, panels, reps)
            if not hypothesis:
                raise ValueError('test needs hypothesis')
            return experiments.test(hypothesis)
        return await invoke('fnv_name_test', lambda: fnvsym_root() and action_fn(), 'read', action=action)

    @server.tool(annotations=control)     # writes a file (X1)
    async def fnv_name_export(path: Annotated[str, Field(max_length=500)] | None = None) -> result_type:
        """Write everything the naming tools recorded (harvested probe sets, atlas fits, resolved callers, this classes,
        result shapes, oracle matches, experiment verdicts, field watches) as fnvsym-trace/2 into fnvsym/traces, with the
        measured accuracy of each method (PROOF 4.1). fnvsym's `python -m namer.runtime2` turns it into evidence.
        With nothing recorded yet it writes no file (changed: false, exported: 0). path: a .json file under
        fnvsym/traces or <state>/traces; an existing file only when it is a trace (else BAD_PATH)."""
        return await invoke('fnv_name_export', lambda: fnvsym_root() and naming.export(path))

    @server.tool(annotations=act)
    async def fnv_field_watch(action: Literal['find', 'watch'] = 'watch', ref: str | None = None,
                              offset: int = 0, address: str | None = None, length: int = 4,
                              access: Literal['write', 'readwrite'] = 'write', scope: Literal['main', 'all'] = 'main',
                              trigger: list[str] | None = None, trigger_ref: str | None = None, seconds: float = 0.0,
                              size: int = 4096) -> result_type:
        """Which functions write (or read) one field, from the CPU's debug registers. find: snapshot an object (ref,
        size bytes, and one level into the objects its pointer fields point at) before and after console lines (trigger,
        run on the ref) and list what changed, named from the class layouts. watch: a hardware watchpoint on address (or
        ref + offset), length 1/2/4, on the main thread or every thread, while the trigger runs (or for seconds): each
        function that touched it, the instruction, the values and its callers. Cleared afterwards (also fnv_undo)."""
        def action_fn():
            if action == 'find':
                return field_watch.find(ref or 'player', trigger, size)
            return field_watch.watch(address, ref, offset, length, access, scope, trigger, seconds, trigger_ref)
        return await invoke('fnv_field_watch', action_fn, 'control')

    @server.tool(annotations=act)
    async def fnv_atlas(action: Literal['actions', 'run', 'runs', 'function', 'action', 'import_frida'] = 'actions',
                        actions: list[str] | None = None, reps: int = 2, doses: list[int] | None = None,
                        name: str | None = None, function: str | None = None, kind: str | None = None,
                        run: int | None = None, statuses: list[str] | None = None, exclusive: bool = False,
                        include_console_machinery: bool = False, path: str | None = None, notes: str | None = None,
                        limit: int = 60) -> result_type:
        """Which functions run for which game action, measured with the probe set (install it with fnv_probe first).
        actions: the scripted action library. run: for each action, idle segments and segments doing it 1, 2 and 4
        times (reps repetitions), counters read before and after each; a fit per function gives calls per action
        (per_unit) or 'only while acting' (with_action). Results go to state/atlas.sqlite. runs: the runs stored.
        function: every fit and caller recorded for one function. action: the functions an action runs (kind,
        statuses e.g. ['unnamed'], exclusive: no other action runs them; console machinery left out unless asked).
        import_frida: load fnvsym's Frida trace file (path) as a run of its own."""
        def action_fn():
            if action == 'actions':
                return atlas.list_actions()
            if action == 'run':
                return atlas.run(actions, reps, doses, notes)
            if action == 'runs':
                return atlas.runs()
            if action == 'function':
                if not function:
                    raise ValueError('function needs function')
                return atlas.function(function)
            if action == 'action':
                if not name:
                    raise ValueError('action needs name')
                return atlas.action(name, kind, run, statuses, exclusive, include_console_machinery, limit)
            if not path:
                raise ValueError('import_frida needs path')
            return atlas.import_frida(str(inside(path, read_roots(live.project), 'path')))     # H5
        return await invoke('fnv_atlas', action_fn, 'read', action=action)

    @server.tool(annotations=control)
    async def fnv_warnings(action: Literal['status', 'enable', 'read', 'summary', 'clear', 'capture'] = 'status',
                           mode: Literal['start', 'stop', 'status'] | None = None,
                           since: int | Literal['mark', 'start'] = 'mark',
                           category: Annotated[str, Field(max_length=40)] | None = None,
                           text: Annotated[str, Field(max_length=200)] | None = None,
                           plugin: Annotated[str, Field(max_length=100)] | None = None,
                           source: Literal['engine', 'havok'] | None = None,
                           limit: Annotated[int, Field(ge=1, le=500)] = 100, offset: Annotated[int, Field(ge=0)] = 0,
                           brief: bool = False, top: Annotated[int, Field(ge=1, le=100)] = 10) -> result_type:
        """The engine's own warnings, such as "Form (id) in file X has wrong type", missing files and script
        problems. They exist only when xNVSE's config has [Logging] EnableGameErrorLog=1; xNVSE then writes
        falloutnv_error.log (engine) and falloutnv_havok.log (Havok) in the game folder. status: is the setting on,
        the files, what to do next. enable: set it in xNVSE's config (a file in the MO2 overwrite folder or mod that
        the game reads; other lines stay); it takes effect at the next game start. read: lines since the mark
        (since=start: whole files; a number: from that line) with filters category (MASTERFILE, SCRIPTS, MODELS,
        TEXTURES, ANIMATION, AI, Audio, other), text, plugin, source; paged. summary: counts per category and
        plugin, and the most repeated messages with ids and numbers masked. clear: remember the end of the files
        as the mark (the game keeps the files open, so they are not emptied). The running game keeps both files
        locked (no reads): capture mode=start|stop|status hooks the engine's Warning function itself, and read and
        summary then show the warnings live, with the calling function (fnv_session start turns it on)."""
        return await invoke('fnv_warnings', lambda: warnings_for_live(live).run(
            action, mode=mode, since=since, category=category, text=text, plugin=plugin, source=source, limit=limit,
            offset=offset, brief=brief, top=top), 'control' if action in ('enable', 'capture') else 'read',
            # capture with no mode starts the capture: only mode=status is a read (S7 review I1)
            action='capture_status' if action == 'capture' and mode == 'status' else action)

    @server.tool(annotations=read)
    async def fnv_wait_for(frames: Annotated[int, Field(ge=1, le=10000)] | None = None,
                           seconds: Annotated[float, Field(gt=0, le=600)] | None = None,
                           clock: Literal['game', 'real'] = 'game',
                           predicate: Annotated[str, Field(min_length=1, max_length=2000)] | None = None,
                           loaded: bool = False,
                           timeout: Annotated[int, Field(ge=1, le=600)] = 60,
                           max_frames: Annotated[int, Field(ge=1, le=10000)] = 3600) -> result_type:
        """Block inside the game until one condition holds, in one request: frames=N (N frames pass), seconds=S with
        clock game (game seconds, scaled by the time multiplier) or real (wall seconds at the current frame rate),
        predicate=TEXT (a compiled condition, for example "frame >= 500" or "[0x011DEA0C]:u8 == 1"), or loaded (the
        game has finished loading and is simulating: loading flag and menu mode clear, the frame delta above 0, two
        frames run, and the player has a parent cell). Exactly one condition. The wait runs frames even while the gate
        is held or open; if the gate was open before, it is opened again at the stop. timeout is in real seconds;
        max_frames caps a predicate wait."""
        return await invoke('fnv_wait_for', lambda: wait_for.run(
            frames=frames, seconds=seconds, clock=clock, predicate=predicate, loaded=loaded,
            timeout_seconds=timeout, max_frames=max_frames), 'read')

    @server.tool(annotations=control)
    async def fnv_session(action: Literal['start', 'mark', 'end', 'status'],
                          start: Start = 'continue',
                          label: Annotated[str, Field(max_length=60)] | None = None,
                          deterministic: bool = False,
                          quit_game: bool = False,
                          timeout_seconds: Annotated[int, Field(ge=20, le=600)] = 240) -> result_type:
        """One call to be ready and one to finish. start: check the setup, launch the game when it is not running
        (at start: menu, continue, save:<name> or coc:<cell>), wait until it is loaded and simulating, turn on capture
        (script errors, engine warnings, crash context), optionally a deterministic run, and open a journal scope.
        mark: a named point (label) that fnv_health and the warning log report from. end: revert this session's
        journal changes, return one report, and quit the game when quit_game is true. status: the session, cheaply.
        Starting the game and changing the warning setting need the game closed or a restart to take effect."""
        return await invoke('fnv_session', lambda: session_tools.run(action, start=start, label=label,
                                                                     timeout_seconds=timeout_seconds,
                                                                     deterministic=deterministic,
                                                                     quit_game=quit_game), 'control', action=action)

    @server.tool(annotations=read)
    async def fnv_health() -> result_type:
        """What is abnormal since the last session mark (fnv_session mark, or the session start): script errors,
        engine warnings by category, new crash files, frame time against the session start, and the live object count
        (NiRefObject) against the session start. Returns ok true, or a short list where each item names the tool to
        call next. Needs an open session (fnv_session start)."""
        return await invoke('fnv_health', lambda: health_tools.check(), 'read')

    @server.tool(annotations=read)
    async def fnv_guide(action: Literal['list', 'topic', 'find', 'engine'] = 'list',
                        words: Annotated[str, Field(max_length=300)] | None = None,
                        limit: Annotated[int, Field(ge=1, le=100)] = 20, offset: Annotated[int, Field(ge=0)] = 0,
                        brief: bool = False) -> result_type:
        """Which tool to use for a problem. list: the guides (for example "the game crashed", "a script does not
        run"). topic: one guide, by name or by words that describe the problem: tools in order, what each answer
        means, known traps, checks that prove the fix. find: search the tool names and descriptions and the guides
        ("which tool for X"). engine: search the saved engine research notes (fnv.research_dir or FNV_RESEARCH_DIR)
        and the recorded engine facts for how the engine does something. Needs no running game."""
        def answer():
            from .fnv_guide import GuideTools
            research = getattr(project.fnv, 'research_dir', None) if project.fnv else None
            return GuideTools(str(research) if research else None,
                              toolsets=project.active_toolsets()).run(action, words, limit, offset, brief)
        return await invoke('fnv_guide', answer)

    @server.tool(annotations=read)
    async def fnv_setup_check() -> result_type:
        """Is this machine ready for the fnv_* tools? Checks the game build, xNVSE, the MO2 dev instance and its NVSE
        executable, JIP LN NVSE, the crash logger, bAlwaysActive, Zig, the engine companion (installed, up to date),
        the engine map, the optional unpacked exe and full control; each with what to do when it is missing."""
        from .fnv_setup import setup_check
        return await invoke('fnv_setup_check', lambda: setup_check(project))

    @server.tool(annotations=act)
    async def fnv_debug(action: Literal['break', 'status', 'stack', 'locals', 'eval', 'set', 'continue', 'step', 'until',
                                        'return', 'skip', 'freeze', 'thaw', 'threads', 'dump'],
                        op: Literal['set', 'list', 'clear'] | None = None,
                        location: Annotated[str, Field(max_length=200)] | None = None,
                        break_id: Annotated[int, Field(ge=0, le=100000)] | None = None,
                        when: Annotated[str, Field(max_length=500)] | None = None,
                        hits: Annotated[int, Field(ge=1, le=100000)] | None = None,
                        thread: Literal['main', 'any'] | None = None,
                        do: Literal['stop', 'log'] | None = None,
                        log: list[Annotated[str, Field(max_length=500)]] | None = None,
                        depth: Annotated[int, Field(ge=1, le=200)] | None = None,
                        with_locals: bool = False,
                        frame: Annotated[int, Field(ge=0, le=200)] | None = None,
                        expression: Annotated[str, Field(max_length=300)] | None = None,
                        value: Annotated[str, Field(max_length=100)] | None = None,
                        mode: Literal['instruction', 'into', 'over', 'out', 'line'] | None = None,
                        file: Annotated[str, Field(max_length=200)] | None = None) -> result_type:
        """Source-level debugger for the running game. break op=set location=<function | 0xADDR |
        file:line | fault> sets a stop (when = a predicate such as "frame >= 100"; do=log with log=[expression] records
        values instead; hits=N stops from the Nth hit that passes when). break op=list / op=clear break_id. status: stopped or running, the place, the source
        lines and why. stack and locals (frame n) read the stop; eval and set work on a stop's frame. continue, step
        (mode), until location, return value, skip location, freeze / thaw the threads, threads, dump file (a
        minidump in the bridge state folder). Needs fnv.engine_control = full for actions that change the game."""
        is_read = action in READ_ACTIONS or (action == 'break' and (op == 'list' or (op is None and not location)))
        args = {'op': op, 'location': location, 'break_id': break_id, 'when': when, 'hits': hits, 'thread': thread,
                'do': do, 'log': log, 'depth': depth, 'with_locals': with_locals, 'frame': frame,
                'expression': expression, 'value': value, 'mode': mode, 'file': file}
        def run():
            try:
                return debugger.run(action, **args)
            except fnv_link.LinkError as e:
                # A fault on a loader thread during a load leaves the main thread waiting for it, and the
                # companion answers only on the main thread. Read the held fault from outside instead.
                if action not in ('status', 'stack') or e.code not in ('COMPANION_UNREACHABLE', 'COMPANION_TIMEOUT'):
                    raise
                doctor = deep.hang_doctor(samples=1)
                if not doctor.get('heldFaults'):
                    raise
                return {'verdict': doctor['verdict'], 'state': 'stopped (held fault, read from outside)',
                        'reason': 'The companion cannot answer: its main thread waits for the held thread. These are '
                                  'the registers of the fault from its context file and the stack above them; fnv_game quit '
                                  'force=true ends the game.', 'heldFaults': doctor['heldFaults'],
                        'mainThread': next((t for t in doctor['threads'] if t['main']), None)}
        return await invoke('fnv_debug', run, 'read' if is_read else 'act')

    @server.tool(annotations=act)
    async def fnv_flow(action: Literal['trace', 'call'],
                       function: Annotated[str, Field(min_length=1, max_length=200)],
                       when: Annotated[str, Field(max_length=300)] | None = None,
                       depth: Annotated[int, Field(ge=0, le=1)] = 0,
                       max_steps: Annotated[int, Field(ge=1, le=200000)] | None = None,
                       args: list[Any] | None = None,
                       this: Annotated[str, Field(max_length=200)] | None = None) -> result_type:
        """Trace one function run: arm a breakpoint on its entry (when = an optional predicate over its arguments and
        locals), then record every statement the function executes in its range. The result lists the source lines
        in order with hit counts and the locals that changed on each. depth 1 also traces the direct callees that
        have source lines (each line names its function). call function args [this]: run the function ourselves
        with these arguments (numbers, floats, or form / reference names for pointers; this = the object for a
        thiscall) under the same trace, and return its eax: for decision functions, "what would it say for this"."""
        return await invoke('fnv_flow', lambda: flow_tools.run(action, function=function, when=when, depth=depth,
                                                                max_steps=max_steps, args=args, this=this), 'act')

    @server.tool(annotations=act)
    async def fnv_blame(action: Literal['field', 'ref', 'report', 'form'],
                        address: Annotated[str, Field(max_length=120)] | None = None,
                        ref: Annotated[str, Field(max_length=200)] | None = None,
                        what: Annotated[str, Field(max_length=200)] | None = None,
                        length: Annotated[int, Field(ge=1, le=64)] = 4,
                        access: Literal['write', 'readwrite'] = 'write',
                        scope: Literal['main', 'all'] = 'main',
                        seconds: Annotated[float, Field(gt=0, le=600)] | None = None) -> result_type:
        """Who changed it: field arms a hardware write watch on an address (0x... or ref + 0x offset) and records each
        writer with its thread, stack and running script line. ref hooks the engine functions that touch a reference
        (what = presets, comma-separated, or all). report groups the hits so far by culprit: the script line, else the
        engine function. form ref seconds: the change flags the game sets on a form meanwhile (named by the
        engine), with the function that set each."""
        is_read = action == 'report'
        args = {'address': address, 'ref': ref, 'what': what, 'length': length, 'access': access, 'scope': scope,
                'seconds': seconds}
        return await invoke('fnv_blame', lambda: blame_tools.run(action, **args), 'read' if is_read else 'act')

    @server.tool(annotations=act)
    async def fnv_script_debug(action: Literal['break', 'status', 'vars', 'set', 'eval', 'step', 'continue', 'freeze',
                                               'thaw'],
                               op: Literal['set', 'list', 'clear'] | None = None,
                               script: Annotated[str, Field(max_length=200)] | None = None,
                               line: Annotated[int, Field(ge=1, le=100000)] | None = None,
                               opcode: Annotated[int, Field(ge=0, le=65535)] | None = None,
                               when: Annotated[str, Field(max_length=300)] | None = None,
                               break_id: Annotated[int, Field(ge=1, le=100000)] | None = None,
                               name: Annotated[str, Field(max_length=100)] | None = None,
                               value: Annotated[str, Field(max_length=100)] | None = None,
                               expression: Annotated[str, Field(max_length=300)] | None = None,
                               mode: Literal['line', 'over', 'out'] | None = None) -> result_type:
        """Script debugger: break op=set script=<quest or script> line=<n> (or opcode, or when over the script's
        variables) stops the script runner before that statement; status shows the script, line and reference; vars
        lists the script variables and set changes one by number; eval runs a snippet on the stopped reference; step
        line (or over: a called function runs in its own run, so both step over it) runs to the next line of the same
        run, step out to the first statement outside it (the caller, else the next script); continue resumes; freeze and thaw turn the process-scripts byte off and on.
        Needs fnv.engine_control = full for the actions that change the game."""
        is_read = action in ('status', 'vars') or (action == 'break' and op == 'list')
        args = {'op': op, 'script': script, 'line': line, 'opcode': opcode, 'when': when, 'break_id': break_id,
                'name': name, 'value': value, 'expression': expression, 'mode': mode}
        return await invoke('fnv_script_debug', lambda: script_debug.run(action, **args),
                            'read' if is_read else 'act')

    @server.tool(annotations=control)
    async def fnv_replay(action: Literal['record', 'stop', 'play', 'list'],
                         name: Annotated[str, Field(max_length=60)] | None = None,
                         seed: Annotated[int, Field(ge=0, le=0xFFFFFFFF)] | None = None,
                         load: bool = True) -> result_type:
        """Record input frame by frame and play it again. record name [seed]: save the game as the start, seed the
        random number generator, and keep every input job sent through the bridge (fnv_input, fnv_play, fnv_test steps)
        with its frame. stop: write the replay. play name [load]: load the start save, seed again, and step the frame
        gate to each job's frame; the game is left held at the end (fnv_time release). list: saved
        replays."""
        return await invoke('fnv_replay', lambda: replay_tools.run(action, name=name, seed=seed, load=load),
                            'read' if action == 'list' else 'control', action=action)

    @server.tool(annotations=act)
    async def fnv_render(action: Literal['toggle', 'imagespace', 'shaders', 'lights'],
                         name: Annotated[str, Field(max_length=80)] | None = None,
                         arg: Annotated[str, Field(max_length=40)] | None = None,
                         remove: bool = False,
                         ref: Ref | None = None) -> result_type:
        """Render controls that still work in retail. toggle name=<wireframe, sky, grass, trees, leaves, lod, menus,
        borders, primitives (trigger and activator volumes drawn), collision, test_light, motion_blur,
        eof_imagespace, debug_text> [arg]: the game's own console toggle, with its flag before and after; run it
        again to undo. imagespace name=<IMAD form> [remove]: apply or remove an image-space modifier. shaders:
        RefreshShaders. lights [ref]: the scene lights with their positions, nearest to ref first."""
        return await invoke('fnv_render', lambda: render_tools.run(action, name=name, arg=arg, remove=remove, ref=ref),
                            'read' if action == 'lights' else 'act')

    @server.tool(annotations=act)
    async def fnv_overlay(action: Literal['text', 'marker', 'clear'],
                          text: Annotated[str, Field(max_length=250)] | None = None,
                          x: float | None = None, y: float | None = None,
                          position: list[float] | None = None,
                          ref: Ref | None = None,
                          base: Ref | None = None,
                          scale: Annotated[float, Field(gt=0, le=20)] = 1.0) -> result_type:
        """Show things in the game for a person watching. text text [x y]: words drawn on screen by the engine's
        debug text layer (the same place again replaces them). marker position=[x,y,z] | ref [base scale]: a
        visible object placed there (default a small glowing sphere). clear: remove the texts and markers this
        tool made."""
        return await invoke('fnv_overlay', lambda: overlay_tools.run(action, text=text, x=x, y=y, position=position,
                                                                     ref=ref, base=base, scale=scale), 'act')

    @server.tool(annotations=act)
    async def fnv_script_coverage(action: Literal['start', 'stop', 'run', 'report'],
                                  plugin: Annotated[str, Field(max_length=200)] | None = None,
                                  folder: Annotated[str, Field(max_length=1000)] | None = None) -> result_type:
        """Script lines a play-through or a test folder ran. start [plugin]: record every statement scripts run
        (the EvaluateLine hook, read in the background); stop: per script of the plugin (or every script seen), the
        share of lines run, each line never run with its text, and the begin blocks never entered. run folder
        [plugin]: start, run every scenario of the folder, stop. report: the numbers so far."""
        return await invoke('fnv_script_coverage', lambda: script_coverage.dispatch(action, plugin=plugin,
                                                                                    folder=folder),
                            'read' if action == 'report' else 'act')

    @server.tool(annotations=read)
    async def fnv_script_decompile(form: Ref) -> result_type:
        """Source text of a compiled script that ships without it (a SCPT, or the scripts of a quest's stages, a
        topic info, a package or a terminal), from xNVSE's own decompiler (its DecompileScript command)."""
        return await invoke('fnv_script_decompile', lambda: script_coverage.decompile(form))

    @server.tool(annotations=act)
    async def fnv_script_log(action: Literal['commands', 'history', 'events', 'raise', 'errors'],
                             mode: Literal['start', 'read', 'stop'] | None = None,
                             script: Annotated[str, Field(max_length=200)] | None = None,
                             ref: Annotated[str, Field(max_length=200)] | None = None,
                             opcode: Annotated[int, Field(ge=0, le=65535)] | None = None,
                             flag: Annotated[str, Field(max_length=40)] | None = None,
                             since: Annotated[int, Field(ge=0)] | Literal['last', 'start'] | None = None,
                             limit: Annotated[int, Field(ge=1, le=2000)] | None = None) -> result_type:
        """Script log: commands (mode start / read / stop) records each statement of every script in a ring, with
        script, reference and command names, optionally filtered; history ref lists the world-changing commands run
        on a reference; events (mode start / read / stop) records the block flags raised by the flag setters; raise ref
        flag=<OnAdd, OnTriggerEnter, ...> raises a block flag on a reference; errors lists the script errors since the
        last call."""
        is_read = action == 'errors' or action == 'history' or (action in ('commands', 'events') and mode in (None,
                                                                                                             'read'))
        args = {'mode': mode, 'script': script, 'ref': ref, 'opcode': opcode, 'flag': flag, 'since': since,
                'limit': limit}
        return await invoke('fnv_script_log', lambda: script_log.run(action, **args),
                            'read' if is_read else 'act')

    Point = Annotated[list[float], Field(min_length=3, max_length=3, description='[x, y, z] in game units')]

    @server.tool(annotations=act)
    async def fnv_camera(action: Literal['get', 'set', 'orbit', 'pov', 'restore'] = 'get',
                         position: Point | None = None,
                         yaw: Annotated[float, Field(ge=-360, le=360)] | None = None,
                         pitch: Annotated[float, Field(ge=-89.9, le=89.9)] | None = None,
                         look_at: Ref | None = None, ref: Ref | None = None,
                         distance: Annotated[float, Field(gt=0, le=20000)] | None = None,
                         fit: bool = False, fov: Annotated[float, Field(ge=10, le=150)] | None = None,
                         view: Literal['first', 'third'] | None = None, freeze: bool = True) -> result_type:
        """The camera. get: position, yaw (0 = north, clockwise), pitch (positive looks down), FOV, first/third
        person, fly mode. set: a free camera at position (default: where it is) with yaw/pitch or look_at a ref; fov
        changes the world FOV; freeze (default true) stops the world while the camera is free. orbit ref: around a
        reference looking at its centre; yaw is relative to the reference's heading (180 = in front of it, 0 =
        behind), pitch is the look-down angle, distance or fit=true (from its size and the FOV). pov view=first|third.
        restore: back to the player's own view. Cells load around the player, not the camera."""
        is_read = action == 'get'
        args = {'position': position, 'yaw': yaw, 'pitch': pitch, 'look_at': look_at, 'ref': ref,
                'distance': distance, 'fit': fit, 'fov': fov, 'view': view, 'freeze': freeze}
        return await invoke('fnv_camera', lambda: eyes.camera_tool(action, **args), 'read' if is_read else 'act')

    @server.tool(annotations=read)
    async def fnv_look(types: list[Literal['actors', 'doors', 'containers', 'activators', 'items', 'furniture', 'statics',
                                           'lights']] | None = None,
                       limit: Annotated[int, Field(ge=1, le=200)] = 20,
                       radius: Annotated[float, Field(gt=0, le=40000)] = 6000.0,
                       fields: list[Annotated[str, Field(max_length=40)]] | None = None,
                       rays: Annotated[int, Field(ge=0, le=200)] = 40,
                       include_hidden: bool = False) -> result_type:
        """What is on screen, as data: references in view, largest on screen first, each with form id, name, type,
        distance, the screen rectangle in pixels [x0, y0, x1, y1], screen share, line of sight from the camera (true,
        false or unknown when the ray budget ran out) and flags; plus the crosshair reference, its prompt, the corner
        message, the subtitle and the open menus. types filters; fields keeps only those keys per reference."""
        return await invoke('fnv_look', lambda: eyes.look(types, limit, radius, fields, rays, include_hidden))

    @server.tool(annotations=act)
    async def fnv_photo(action: Literal['ref', 'cell', 'view', 'sheet', 'sequence', 'compare'] = 'view',
                        ref: Ref | None = None,
                        angles: list[Any] | None = None,
                        size: Annotated[int, Field(ge=320, le=3840)] = 960,
                        hide_hud: bool = True, label: bool = False,
                        frames: Annotated[int, Field(ge=1, le=60)] | None = None,
                        every: Annotated[int, Field(ge=1, le=600)] = 1,
                        baseline: Annotated[str, Field(max_length=1000)] | None = None,
                        distance: Annotated[float, Field(gt=0, le=20000)] | None = None,
                        name: Annotated[str, Field(max_length=60)] | None = None) -> result_type:
        """Pictures of the running game (PNG paths). ref: a reference from angles (front, three_quarter, side, back,
        top, or [yaw, pitch] pairs) tiled into one sheet, the world paused and the player's view restored after.
        cell: from above and from the four corners. view: the current view, hide_hud, label=true draws the fnv_look
        rectangles. sheet: ref views or the view. sequence: frames images, every k frames, with the frame gate (a
        film strip). compare: the view against a baseline image: changed percent and a difference image."""
        return await invoke('fnv_photo', lambda: eyes.photo(action, ref=ref, angles=angles, size=size,
                                                            hide_hud=hide_hud, label=label, frames=frames,
                                                            every=every, baseline=baseline, name=name,
                                                            distance=distance), 'act')

    @server.tool(annotations=act)
    async def fnv_hud(action: Literal['read', 'post'] = 'read',
                      text: Annotated[str, Field(max_length=250)] | None = None,
                      seconds: Annotated[float, Field(gt=0, le=30)] = 3.0) -> result_type:
        """The HUD: mode (normal, dialogue, VATS, container...), which parts are visible, the crosshair reference
        and its activation prompt, the corner message on screen and the queued ones, the subtitle line. post: show a
        corner message (text, seconds) so a person watching sees what the agent does (needs full control)."""
        return await invoke('fnv_hud', lambda: eyes.hud(action, text, seconds), 'read' if action == 'read' else 'act')

    @server.tool(annotations=read)
    async def fnv_visibility(ref: Ref) -> result_type:
        """Why a reference does not draw: checks in order (no 3D, deleted, disabled, culled node, zero scale, empty
        bound, first person, other cell, outside the view, behind something) and stops at the first cause, listing
        the rest as also. Says what it could not check (fades, LOD, portals, shader alpha)."""
        return await invoke('fnv_visibility', lambda: eyes.visibility(ref))

    @server.tool(annotations=act)
    async def fnv_play(action: Literal['goto', 'look_at', 'activate', 'talk', 'loot', 'equip', 'use', 'drop', 'stop'],
                       ref: Ref | None = None, position: Point | None = None, direct: bool = False,
                       max_seconds: Annotated[float, Field(gt=0, le=600)] = 90.0, run: bool = False) -> result_type:
        """Play as a player would, through the player's own input. goto ref|position: walk there on foot (a route
        around walls found with rays, doors on the way are opened by activation; stops on arrival, when blocked, when a
        menu opens or after max_seconds) and report the route, doors used and why it stopped. look_at ref: turn the
        view onto it and say whether the crosshair picks it. activate ref: look at it, check the crosshair, press
        activate for one frame and report what happened (menu opened, dialogue, door state, HUD message) or why not
        (menu open, controls disabled, locked, not in the crosshair...); direct=true activates without the crosshair.
        talk ref: goto + activate + dialogue opened? loot ref (a container: take all), equip/use/drop ref (an item
        form) run the engine's commands on the player. stop releases every held input."""
        return await invoke('fnv_play', lambda: hands.play(action, ref=ref, position=position, direct=direct,
                                                           max_seconds=max_seconds, run=run), 'act')

    @server.tool(annotations=act)
    async def fnv_ui_do(action: Literal['click', 'button', 'wait', 'sleep', 'fast_travel', 'message', 'close', 'back',
                                        'type', 'close_all'],
                        target: Annotated[str, Field(max_length=200)] | None = None,
                        label: Annotated[str, Field(max_length=120)] | None = None,
                        menu: Annotated[str, Field(max_length=40)] | None = None,
                        hours: Annotated[int, Field(ge=1, le=24)] | None = None,
                        marker: Ref | None = None,
                        button: Annotated[str, Field(max_length=120)] | None = None) -> result_type:
        """Menus by meaning, on the menu at the top. click target=<tile path or name> (fnv_menu tiles lists them) or
        button label=<text on it> (Take All, Repair, Accept, Done...): the menu's own click handler with the tile's id,
        as a mouse click does; returns the menus opened or closed and the HUD message. wait hours=N: press the wait
        control (a refusal comes back as the reason), then pass the hours with the engine's own hour of waiting.
        fast_travel marker=<map marker ref>: the engine's CanFastTravel (its refusal message) then FastTravel.
        message button=<index or label>: a message box button. close/back: the top menu. type label=<text>: enter
        text in the text entry menu (the character name), as its Done button does. close_all: every menu.
        fnv_menu reads the menus and their tiles."""
        if action == 'close_all':
            return await invoke('fnv_ui_do', lambda: agent.menu_control('close_all'), 'act', action=action)
        return await invoke('fnv_ui_do', lambda: hands.ui_do(action, target=target, label=label, menu=menu,
                                                             hours=hours, marker=marker, button=button), 'act',
                            action=action)

    @server.tool(annotations=act)
    async def fnv_asset(action: Literal['reload', 'watch', 'file_log', 'queue', 'where', 'models', 'archives', 'nif',
                                        'dds', 'users'],
                        path: Annotated[str, Field(max_length=500)] | None = None,
                        mode: Literal['start', 'read', 'stop'] | None = None,
                        missing: bool = False,
                        ext: Annotated[str, Field(max_length=10)] | None = None,
                        folder: Annotated[str, Field(max_length=300)] | None = None,
                        limit: Annotated[int, Field(ge=1, le=5000)] = 200,
                        wait_idle: bool = False,
                        menu: Annotated[str, Field(max_length=60)] | None = None,
                        ref: Ref | None = None) -> result_type:
        """Assets in the running game. reload path: a model (.nif) or texture (.dds) is read from disk again by the
        loaded references that use it (no restart); a menu .xml with menu=<name> (HUDMainMenu...) is evicted and the
        menu rebuilt; a .kf is dropped from the cache (ref=<actor> rebuilds an actor holding it); .ini runs
        RefreshINI, a shader package RefreshShaders. nif path / dds path: offline checks before a file goes in the
        game (NIF header, child links, collision, skin, texture paths; DDS size, format, mip chain). users path: the
        records of the load order that name the file (model, icon, texture set). watch path=<mod
        folder>: reload what changed since the last watch call. file_log mode=start|read|stop: every file the engine
        opened or failed to find (missing=true for the failures; ext, folder filter), with thread and caller; start
        it before entering a cell. where path: which source wins (loose in a mod, Data, an archive). queue: the IO
        queue (wait_idle). models: the model cache (DumpModelMap). archives: the archives and loose roots in order."""
        is_read = action in ('where', 'queue', 'models', 'archives', 'nif', 'dds', 'users') or (
            action == 'file_log' and mode in (None, 'read'))
        args = {'path': path, 'mode': mode, 'missing': missing, 'ext': ext, 'folder': folder, 'limit': limit,
                'wait_idle': wait_idle, 'menu': menu, 'ref': ref}
        return await invoke('fnv_asset', lambda: asset_tools.run(action, **args), 'read' if is_read else 'act')

    @server.tool(annotations=act)
    async def fnv_form(action: Literal['create', 'reload', 'changes', 'bytes', 'delete'],
                       type: Annotated[str, Field(pattern=r'^[A-Za-z_]{4}$')] | None = None,
                       clone: Ref | None = None,
                       name: Annotated[str, Field(max_length=100)] | None = None,
                       plugin: Annotated[str, Field(max_length=200)] | None = None,
                       records: list[Annotated[str, Field(max_length=128)]] | None = None,
                       since: float | None = None,
                       form: Ref | None = None) -> result_type:
        """Forms at run time. create type=<STAT, MISC, ARMO, ...> makes an empty form, or clone=<base form> a copy
        (name sets its name); ready for fnv_form_edit and fnv_place. reload plugin=<name, or the rebuilt file's path: the
        running game locks its own plugin files> after rebuilding it: the
        changed fields of its records (name, models, value, weight, health, armour rating and threshold) go into the
        live forms and the references using a changed model rebuild their 3D; new records and other types are listed
        as needing a restart. changes: what this session created, edited and placed. bytes form: the live form as
        record bytes from the engine's own writer (SaveForm), decoded field by field (runtime form ids). delete
        form: remove a form this session created (flagged deleted and temporary)."""
        is_read = action in ('changes', 'bytes')
        args = {'type': type, 'clone': clone, 'name': name, 'plugin': plugin, 'records': records, 'since': since,
                'form': form}
        return await invoke('fnv_form', lambda: form_tools.run(action, **args), 'read' if is_read else 'act')

    @server.tool(annotations=act)
    async def fnv_native(action: Literal['headers', 'build', 'load', 'unload', 'reload', 'call', 'log', 'list'],
                         name: Annotated[str, Field(max_length=300)] | None = None,
                         source: Annotated[str, Field(max_length=200000)] | None = None,
                         functions: list[Annotated[str, Field(max_length=200)]] | None = None,
                         classes: list[Annotated[str, Field(max_length=200)]] | None = None,
                         export: Annotated[str, Field(max_length=100)] | None = None,
                         args: list[int | float] | None = None,
                         since: Annotated[int, Field(ge=0)] | None = None) -> result_type:
        """C code running inside the game. headers functions=[names] classes=[names]: the module API
        (fnvmcp_module.h) and fnv_engine.h with FNV_ADDR_<name>, typed pointers FNV_<name> and padded structs from
        the symbol database. build source=<C text or .c path> name=N: zig cc to a DLL (compiler errors returned).
        load name=N: loaded on the main thread, fnvmcp_module_init(api) runs, the hooks it asked for (api->hook) are
        installed; handlers get the registers and stack arguments and may change them or skip the call (FNV_SKIP).
        call name export args: an exported function on the main thread (guarded). log: what modules logged
        (api->log). reload name [source]: unload, rebuild, load. unload: hooks removed, writes put back, DLL freed.
        list: loaded modules and built DLLs."""
        is_read = action in ('log', 'list')
        return await invoke('fnv_native', lambda: native_tools.run(action, name=name, source=source,
                                                                 functions=functions, classes=classes,
                                                                 export=export, args=args, since=since),
                            'read' if is_read else 'act')

    @server.tool(annotations=act)
    async def fnv_code(action: Literal['checkout', 'edit', 'build', 'swap', 'unswap', 'compare', 'list', 'export'],
                       function: Annotated[str, Field(max_length=200)] | None = None,
                       find: Annotated[str, Field(max_length=20000)] | None = None,
                       replace: Annotated[str, Field(max_length=20000)] | None = None,
                       args: list[dict[str, Any]] | None = None,
                       this: Annotated[str, Field(max_length=200)] | None = None,
                       returns: Literal['void', 'int', 'uint', 'bool', 'ptr', 'float', 'double', 'form'] | None = None,
                       mode: Literal['call', 'shadow'] = 'call',
                       seconds: Annotated[float, Field(gt=0, le=120)] = 5.0,
                       nargs: Annotated[int, Field(ge=0, le=8)] | None = None) -> result_type:
        """Change an engine function from its decompiled source and run the change in the game (local use only).
        checkout function: a copy of its decomp unit in the bridge state (the decomp checkout is never edited) and
        the function's lines. edit find replace: change the copy inside the function (new code may call the
        original as fnv_orig_<8 hex digits of its address>). build: the unit's own compiler and flags (Visual C++
        2008), then the function is linked against the game's addresses (names it cannot bind are listed). compare
        args [this returns]: the original and the new code called side by side; mode=shadow seconds [nargs]: the
        game's own calls of the original are recorded and replayed on the new code, mismatches listed (pure
        functions). swap: the entry jumps to the new code (functions under 5 bytes: their call sites and vtable
        slots); unswap puts it back. list: active swaps with source diffs. export: a local patch set and a loader
        module that applies it at start. Refused: exception handling inside the function, new writable statics."""
        is_read = action in ('list', 'checkout')
        return await invoke('fnv_code', lambda: code_tools.run(action, function=function, find=find, replace=replace,
                                                             args=args, this=this, returns=returns, mode=mode,
                                                             seconds=seconds, nargs=nargs),
                            'read' if is_read else 'act')

    @server.tool(annotations=control)
    async def fnv_plugin_build(action: Literal['scaffold', 'build', 'install', 'log'],
                               name: Annotated[str, Field(min_length=1, max_length=60)],
                               source: Annotated[str, Field(max_length=200000)] | None = None,
                               hooks: list[Annotated[str, Field(max_length=200)]] | None = None) -> result_type:
        """A native module (the fnv_native API) as a normal xNVSE plugin that needs no engine companion.
        scaffold: a starting source. build [source] hooks=[functions the module hooks]: the runtime, the module
        and the hook prologues (planned from the running game, or from the local game image) compiled with Zig.
        install: as its own MO2 mod in the dev profile (game closed); fnv_game restart loads it. log: its log file.
        Local use only if the source holds decompiled game code (fnv_code output, or functions copied from the
        decomp): such a DLL should not be shared. Your own code calling the engine is yours to share."""
        return await invoke('fnv_plugin_build', lambda: plugin_tools.run(action, name=name, source=source,
                                                                         hooks=hooks),
                            'read' if action == 'log' else 'control', action=action)

    @server.tool(annotations=act)
    async def fnv_profile(action: Literal['sample', 'diff', 'frame'] = 'sample',
                          seconds: Annotated[float, Field(gt=0, le=120)] | None = None,
                          rate: Annotated[int, Field(ge=1, le=1000)] = 200,
                          threads: Literal['main', 'all'] = 'main',
                          name: Annotated[str, Field(max_length=60)] | None = None,
                          before: Annotated[str, Field(max_length=300)] | None = None,
                          after: Annotated[str, Field(max_length=300)] | None = None,
                          top: Annotated[int, Field(ge=1, le=200)] = 25) -> result_type:
        """Where the frame time goes. sample (default): suspend the thread(s) rate times a second for seconds
        (default 5) from outside the game (nothing patched), read the call chain, and report self time per function
        and per decomp source line, inclusive time, the callers of the hottest functions, the frame rate meanwhile,
        and a folded-stack file for flame graphs (saved under state/fnv/profiles). diff before after: two saved
        profiles side by side (inclusive percent per function, frame rate). frame: timing hooks on the main loop and
        its children (player update, input, IO, actor scripts, quest/cell scripts, process lists, cell streaming,
        culling, scene update, render, audio, AI wait, post-render, delayed actions) for seconds (0.5 to 30, default
        3), then removed: ms per frame, share of the frame and the worst call per subsystem, with fps; it installs
        hooks, so it needs fnv.engine_control full."""
        def run():
            if action == 'frame':
                if seconds is not None and not 0.5 <= seconds <= 30:
                    raise ValueError('frame takes seconds 0.5 to 30')
                return world.frame_profile(3.0 if seconds is None else seconds)
            return profile_tools.run(action, seconds=5.0 if seconds is None else seconds, rate=rate, threads=threads,
                                     name=name, before=before, after=after, top=top)
        return await invoke('fnv_profile', run, 'read', action=action)

    @server.tool(annotations=read)
    async def fnv_threads(action: Literal['list', 'hang'] = 'list',
                          samples: Annotated[int, Field(ge=1, le=20)] = 5,
                          interval_ms: Annotated[int, Field(ge=50, le=5000)] = 250,
                          all_threads: bool = False) -> result_type:
        """The game's threads. list (default): every thread of the game, its start routine (named), what it runs now
        (one outside sample), and the known spin locks (process lists, garbage collector, global, script delayed
        actions) with their owner. hang: when the game freezes, read the main thread's call stack (or every thread's
        with all_threads) samples times, interval_ms apart, from outside the process, name each frame with the
        engine map, and say where it is stuck. Works without the companion answering; threads are suspended only
        while their registers are read."""
        if action == 'hang':
            return await invoke('fnv_threads', lambda: deep.hang_doctor(samples, interval_ms, all_threads))
        return await invoke('fnv_threads', lambda: thread_tools.list(), 'read')

    @server.tool(annotations=act)
    async def fnv_memory(action: Literal['address_space', 'pools', 'alloc_trace', 'refcount'] = 'pools',
                         seconds: Annotated[float, Field(ge=0, le=120)] | None = None,
                         top: Annotated[int, Field(ge=1, le=200)] = 20,
                         pointer: Annotated[str, Field(max_length=20)] | None = None) -> result_type:
        """Memory. address_space: the game's address space (committed, reserved, free, largest free block, the 2 GB
        limit of the non-large-address-aware exe), the game's memory pools by use, counts of forms and of high /
        middle / low process actors; seconds > 0 samples twice and reports the growth (leak finding; default 0, up
        to 120). pools (default): every pool with its block size, committed bytes and peak, and the live NiRefObject
        count. alloc_trace seconds: allocation counts by call site (a counting hook on the allocator, removed after),
        with the function and source line of each caller. refcount pointer seconds: who increments and decrements an
        NiRefObject's reference count meanwhile (a write watchpoint on +4, any thread). alloc_trace and refcount
        take seconds above 0 up to 60, default 3."""
        def run():
            if action == 'address_space':
                return world.heap(0 if seconds is None else seconds)
            if seconds is not None and not 0 < seconds <= 60:
                raise ValueError('%s takes seconds above 0 up to 60' % action)
            return memory_tools.run(action, seconds=3.0 if seconds is None else seconds, top=top, pointer=pointer)
        return await invoke('fnv_memory', run, 'read', action=action)

    @server.tool(annotations=read)
    async def fnv_object(action: Literal['identify', 'find', 'referrers'] = 'identify',
                         pointer: Annotated[str, Field(max_length=20)] | None = None,
                         class_name: Annotated[str, Field(max_length=200)] | None = None,
                         limit: Annotated[int, Field(ge=1, le=5000)] = 200) -> result_type:
        """What memory is. identify pointer: the pool and block it is in, the class (RTTI) and the form when it
        is one. find class_name: live objects of that class (pool blocks starting with its vtable). referrers
        pointer: every place in the game's writable memory that holds this address, the first ones identified."""
        return await invoke('fnv_object', lambda: object_tools.run(action, pointer=pointer, class_name=class_name,
                                                                   limit=limit), 'read')

    @server.tool(annotations=act)
    async def fnv_coverage(action: Literal['start', 'stop', 'diff'],
                           files: list[Annotated[str, Field(max_length=300)]] | None = None,
                           functions: list[Annotated[str, Field(max_length=200)]] | None = None,
                           name: Annotated[str, Field(max_length=60)] | None = None,
                           a: Annotated[str, Field(max_length=300)] | None = None,
                           b: Annotated[str, Field(max_length=300)] | None = None) -> result_type:
        """Line coverage of engine code. start files=[decomp paths, e.g. tesobjectrefr.cpp] or
        functions=[names]: a one-shot trap at every source line start of those functions (verified functions only),
        each removing itself on its first hit. stop: lines run and never run per file, functions entered; saved
        under state/fnv/coverage. diff a b: lines that ran in one saved run and not the other."""
        return await invoke('fnv_coverage', lambda: coverage_tools.run(action, files=files, functions=functions,
                                                                       name=name, a=a, b=b), 'act')

    @server.tool(annotations=act)
    async def fnv_anim(action: Literal['state', 'play', 'groups', 'idle_tree', 'biped', 'face', 'events'], ref: Ref,
                       group: Annotated[str, Field(max_length=80)] | None = None,
                       idle: Annotated[str, Field(max_length=80)] | None = None,
                       target: Ref | None = None, rebuild: bool = False,
                       seconds: Annotated[float, Field(gt=0, le=60)] = 3.0) -> result_type:
        """Animation. state ref: per body section the current and queued group (type name, weapon, move, power
        armour) and the sequence playing. play ref group=<name> or idle=<IDLE form>. groups ref: every group the
        actor has a sequence for, with its KF file. idle_tree ref [target]: the idle the engine would pick now and
        the walk to it, each node passing or failing, with the failing conditions. biped ref [rebuild]: the 20
        equipment slots (item, model, node); rebuild re-runs the engine's equipment build. face ref: expression,
        modifier and phoneme values. events ref seconds: animation actions started (equip, attack, reload...)."""
        return await invoke('fnv_anim', lambda: anim_tools.run(action, ref=ref, group=group, idle=idle, target=target,
                                                               rebuild=rebuild, seconds=seconds),
                            'act' if action == 'play' or rebuild else 'read')

    @server.tool(annotations=act)
    async def fnv_audio(action: Literal['log', 'play', 'playing', 'stop', 'music', 'radio', 'subtitles', 'hear',
                                        'voice_check'],
                        seconds: Annotated[float, Field(gt=0, le=60)] = 3.0,
                        sound: Annotated[str, Field(max_length=80)] | None = None,
                        ref: Ref | None = None,
                        sound_id: Annotated[int, Field(ge=0)] | None = None,
                        file: Annotated[str, Field(max_length=260)] | None = None,
                        stop: bool = False,
                        fade_ms: Annotated[int, Field(ge=0, le=20000)] = 1000,
                        enable: bool | None = None,
                        position: list[float] | None = None) -> result_type:
        """Sound. log seconds: every sound the engine creates meanwhile, with its sound form or file and the
        function that asked (footsteps, weapons, scripts...). play sound [ref]: a SOUN form, 2D or from a
        reference. playing: every sound the audio manager holds (file, form, 2D/3D, loop, volume, position, state).
        stop sound|sound_id. music: the two music channels, what plays and its media type, the location controller
        and media set behind it; file=<path> forces a track, stop=true fades music out. radio [enable]: stations,
        signal, the current one; enable turns the radio on or off. subtitles seconds [enable]: subtitle lines shown
        (text, sound id). hear sound position=[x,y,z]: whether the engine would play it there (its distance test).
        voice_check ref: for each line of an NPC the voice and lip files the engine asks for, and whether they
        exist (same as fnv_crawl dialogue)."""
        acts = ('play', 'stop') + (('music',) if file or stop else ()) + (('radio',) if enable is not None else ()) \
            + (('subtitles',) if enable else ())
        return await invoke('fnv_audio', lambda: audio_tools.run(action, seconds=seconds, sound=sound, ref=ref,
                                                                 sound_id=sound_id, file=file, stop=stop,
                                                                 fade_ms=fade_ms, enable=enable, position=position),
                            'act' if action in acts else 'read')

    @server.tool(annotations=act)
    async def fnv_physics(action: Literal['controller', 'body', 'set', 'triggers', 'projectiles', 'query', 'layers'],
                          ref: Ref | None = None,
                          velocity: list[float] | None = None,
                          seconds: Annotated[float, Field(gt=0, le=60)] = 5.0,
                          position: list[float] | None = None,
                          radius: Annotated[float, Field(gt=0, le=4096)] = 64.0) -> result_type:
        """Physics. controller ref: the actor's character controller, state (on ground, in air, swimming...), fall
        time and fall distance (the engine's GetFallDistance). body ref: 3D root to collision object to Havok body,
        each object's class, and the body's velocity. set ref velocity=[x,y,z]: give a body a linear velocity (game
        units per second). triggers seconds [ref]: trigger enter, inside and leave events as scripts get them, with
        frames. projectiles: what is in flight (weapon, shooter, position, distance travelled). query position
        radius: is the spot free (rays in six directions). layers: the collision layer names."""
        return await invoke('fnv_physics', lambda: physics_tools.run(action, ref=ref, velocity=velocity,
                                                                     seconds=seconds, position=position,
                                                                     radius=radius),
                            'act' if action == 'set' else 'read')

    @server.tool(annotations=act)
    async def fnv_systems(action: Literal['formulas', 'formula', 'sweep', 'progression', 'needs', 'perk', 'barter',
                                          'zone', 'crime'],
                          name: Annotated[str, Field(max_length=200)] | None = None,
                          args: list[Any] | None = None,
                          index: Annotated[int, Field(ge=0, le=15)] | None = None,
                          start: float | None = None, stop: float | None = None,
                          steps: Annotated[int, Field(ge=1, le=100)] = 10) -> result_type:
        """Game systems. formulas: the catalog (skill value, XP for a level, walk and run speed, item
        value by condition, force-lock chance). formula name args: call it (or any engine function by name) with
        those inputs, guarded. sweep name args index start stop [steps]: one argument over a range. progression:
        level, XP, the XP the next level needs. needs: hardcore values and radiation. perk name=<PERK>: can the player
        take it now, and the requirement or condition that fails. barter args=[items] [name=vendor]: buy and sell
        price of each item as the barter menu computes them (skill multipliers, perks on the buy price). zone
        name=<ECZN>: the level the zone uses now, its minimum and maximum. crime: karma (New Vegas keeps no
        bounty)."""
        return await invoke('fnv_systems', lambda: systems_tools.run(action, name=name, args=args, index=index,
                                                                     start=start, stop=stop, steps=steps), 'read', action=action)

    @server.tool(annotations=control)
    async def fnv_crawl(action: Literal['cells', 'dialogue', 'quest', 'items'],
                        plugin: Annotated[str, Field(max_length=200)] | None = None,
                        npc: Ref | None = None,
                        max_cells: Annotated[int, Field(ge=1, le=500)] = 20,
                        settle_frames: Annotated[int, Field(ge=1, le=3000)] = 90,
                        photos: bool = True,
                        limit: Annotated[int, Field(ge=1, le=500)] = 60,
                        quest: Ref | None = None,
                        path: list[Annotated[int, Field(ge=0, le=65535)]] | None = None) -> result_type:
        """Unattended checks of a whole plugin. cells plugin: every cell the plugin adds or edits is visited and
        settled, then checked for script errors, engine warnings, files not found, references with no 3D, load doors
        that lead nowhere and the frame rate, with a photo; one row per cell, worst first (moves the player;
        fnv_undo moves it back). dialogue npc: every line written for the NPC with its topic and quest, whether the
        engine allows it now and which condition fails, and the voice and lip files the engine will ask for, found
        or not. quest quest path=[stages]: set each stage in order and check the stage reached, the objectives
        shown, their targets and script errors. items plugin: each new item given to the player, equipped or used,
        its model checked, script errors meanwhile, then taken back."""
        return await invoke('fnv_crawl', lambda: crawl_tools.run(action, plugin=plugin, npc=npc, max_cells=max_cells,
                                                                 settle_frames=settle_frames, photos=photos,
                                                                 limit=limit, quest=quest, path=path), 'control')

    @server.tool(annotations=control)
    async def fnv_bisect(level: Literal['mods', 'plugins', 'records'],
                         steps: Annotated[list[Any], Field(min_length=1, max_length=200)],
                         start: Start = 'continue',
                         keep: list[Annotated[str, Field(max_length=200)]] | None = None,
                         max_rounds: Annotated[int, Field(ge=1, le=16)] = 10,
                         plugin: Annotated[str, Field(max_length=200)] | None = None) -> result_type:
        """Find the one MO2 mod, plugin, or record of a plugin that makes a scenario fail. steps: the failing
        scenario as for fnv_test steps. Checks that it fails with everything on and passes with every candidate off, then
        turns off half of the suspects each round (the game is quit and the runner relaunches it), and names the
        culprit. level records plugin=<name>: the suspects are the plugin's records; each round writes a variant of
        the plugin file without the other half (the original is put back at the end). keep: mods, plugins or record
        form ids never turned off (the companion, xNVSE data and JIP are always kept; base game files too). The
        profile is put back at the end."""
        return await invoke('fnv_bisect', lambda: bisect_tools.bisect(level, steps, start, keep, max_rounds, plugin),
                            'control')

    @server.tool(annotations=read)
    async def fnv_selftest() -> result_type:
        """Check what the tool families rely on, against the running game: the companion and its wire,
        the engine map and symbol database, the singletons the tools read, the prologues hooks plan, the camera, and a
        cheap call into each family (memory, systems, eyes, profile, modules, code placement, coverage). Each row is
        ok, degraded or broken with the fact that failed. Use after a game, xNVSE, plugin or decomp update."""
        return await invoke('fnv_selftest', lambda: self_test.run(), 'read')

    @server.tool(annotations=act)
    async def fnv_place(action: Literal['at', 'array', 'nudge', 'rotate', 'scale', 'snap', 'check', 'list', 'remove'],
                        base: Ref | None = None, ref: Ref | None = None,
                        position: Point | None = None, near: Ref | None = None,
                        offset: Point | None = None, rotation: Point | None = None,
                        scale: Annotated[float, Field(gt=0, le=10)] | None = None,
                        shape: Literal['row', 'grid', 'circle'] | None = None,
                        count: Annotated[int, Field(ge=1, le=100)] | None = None,
                        spacing: Annotated[float, Field(gt=0, le=5000)] | None = None,
                        columns: Annotated[int, Field(ge=1, le=50)] | None = None,
                        radius: Annotated[float, Field(gt=0, le=10000)] | None = None,
                        snap: bool = False, local: bool = False, align: bool = False,
                        export: bool = True,
                        distance: Annotated[int, Field(ge=0, le=10000)] | None = None,
                        direction: Annotated[int, Field(ge=0, le=3)] | None = None) -> result_type:
        """Place things in the running game; every placement is journaled for fnv_author export. at base: at position,
        or near a ref (+ offset), or 150 units in front of the player; rotation in degrees, scale, snap=true drops it
        on the surface below. array base: shape row, grid (columns) or circle (radius), count, spacing, from position
        or the player. nudge ref offset=[dx,dy,dz] (local=true in its own axes), rotate ref rotation=[dx,dy,dz]
        degrees added, scale ref scale. snap ref [align]: onto the surface below (align=true also tilts it to the slope at its heading). check ref: floating or sinking. list: what
        this session placed. remove: one placed ref, or all of them. at with export=false: a test placement left out
        of fnv_author export (an enemy for a test): base near a ref (default the player) with PlaceAtMe, count
        copies, distance and direction (0 front, 1 back, 2 left, 3 right); fnv_undo disables and deletes them.
        fnv_world_edit moves, rotates, scales and deletes any ref, not only this session's placements."""
        is_read = action in ('list', 'check')
        args = {'base': base, 'ref': ref, 'position': position, 'near': near, 'offset': offset, 'rotation': rotation,
                'scale': scale, 'shape': shape, 'count': count, 'spacing': spacing, 'columns': columns,
                'radius': radius, 'snap': snap, 'local': local, 'align': align}

        def run():
            if export:
                if action == 'at' and (count is not None or distance is not None or direction is not None):
                    raise ValueError('count, distance and direction are for at with export=false (a test placement)')
                return place_tools.run(action, **args)
            if action != 'at':
                raise ValueError('export=false is for action=at only')
            extra = [k for k, v in (('position', position), ('offset', offset), ('rotation', rotation),
                                    ('scale', scale), ('snap', snap)) if v not in (None, False)]
            if extra:
                raise ValueError('at with export=false places near a ref with distance and direction; it does not '
                                 'use %s' % ', '.join(extra))
            if not base:
                raise ValueError('at needs base')
            return agent.spawn(base, near or 'player', count or 1, distance or 0, direction or 0)
        return await invoke('fnv_place', run, 'read' if is_read else 'act', action=action)

    @server.tool(annotations=control)
    async def fnv_author(action: Literal['export', 'check', 'scaffold', 'package'],
                         plugin: Annotated[str, Field(max_length=200)] | None = None,
                         kind: Literal['item', 'interior', 'quest', 'terminal', 'perk', 'companion'] | None = None,
                         name: Annotated[str, Field(max_length=60)] | None = None,
                         cell: Annotated[str, Field(max_length=128)] | None = None) -> result_type:
        """Authoring. export: the session's created forms and placed references as a plugin spec part (records by
        EditorID, objects of a cell) in the bridge state folder, to merge into the project spec. check plugin: missing
        files, conflicts with other plugins and the engine warning summary in one report. scaffold kind name: a
        starting project (spec, a smoke test, readme). package plugin: a release folder and zip with the plugin, its
        loose files and a readme, plus the missing-file check."""
        args = {'plugin': plugin, 'kind': kind, 'name': name, 'cell': cell}
        return await invoke('fnv_author', lambda: author_tools.run(action, **args), 'control')

    return live
