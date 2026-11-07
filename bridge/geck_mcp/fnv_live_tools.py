"""MCP registration of the live fnv_* tools: game control and tests (milestone 4), live inspection
(milestone 5) and full control (milestone 6). Kinds: read (changes nothing), act (changes the running game,
journaled, refused unless fnv.engine_control = "full"), control (starts/stops the game or installs files)."""
import time
import uuid
from typing import Annotated, Any, Literal

from pydantic import Field

from . import fnv_link
from .fnv_game import Game
from .fnv_data import DataTools
from .fnv_deep import DeepTools
from .fnv_live import LiveService
from .fnv_social import SocialTools
from .fnv_agent import AgentTools
from .fnv_scenario import ScenarioRunner, diff, load_snapshot, snapshot

FNV_LIVE_TOOLS = {
    # milestone 4: game control, tests, logs
    'fnv_status': ('read', 'game running? phase (main menu, loading, in game), cell, loaded plugins, companion'),
    'fnv_launch': ('control', 'start the game through the MO2 dev profile at the menu, a save or a cell; waits until ready'),
    'fnv_quit': ('control', 'quit the game (console qqq, then window close, then terminate if forced)'),
    'fnv_restart': ('control', 'quit and launch again'),
    'fnv_install': ('control', 'install a built plugin into the dev profile as an MO2 mod and enable it (game closed)'),
    'fnv_companion_install': ('control', 'build the engine companion with Zig and install it into the dev profile'),
    'fnv_logs': ('read', 'new lines of nvse.log, the console log, CrashLogger.log, JIP and MO2 logs'),
    'fnv_test_run': ('control', 'run one YAML scenario test in the game and report each step'),
    'fnv_test_run_all': ('control', 'run every scenario in a folder; summary plus failure details'),
    'fnv_snapshot': ('read', 'capture chosen game state under a name'),
    'fnv_diff': ('read', 'compare two snapshots (or a snapshot with now)'),
    # milestone 5: live inspection
    'fnv_player': ('read', 'player position, cell, health, AP, level, SPECIAL, skills, active effects'),
    'fnv_ref': ('read', 'any reference: base, position, cell, owner, lock, flags, class, extra data'),
    'fnv_refs_near': ('read', 'references around the player within a radius, nearest first'),
    'fnv_actor_ai': ('read', 'actor AI: package, procedure, process level, combat target, detection, state'),
    'fnv_quest': ('read', 'quest: stage, stage list done flags, objectives with text and state, script variables'),
    'fnv_actor_values': ('read', 'actor values with base, permanent and temporary parts and the effects behind them'),
    'fnv_effects': ('read', 'active effects of an actor with source item, effect, magnitude, duration, actor value'),
    'fnv_extra_data': ('read', 'extra data list of a reference, decoded'),
    'fnv_crosshair': ('read', 'what the player is looking at'),
    'fnv_inventory': ('read', 'items of an actor or container with counts and equipped state'),
    'fnv_vars': ('read', 'script variables of a quest or reference, by name'),
    'fnv_script_errors': ('read', 'script errors (script, mod, message) since the last call'),
    'fnv_events': ('read', 'xNVSE events seen by the companion: loads, saves, new game, script errors'),
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
    'fnv_undo': ('act', 'undo the last journaled change(s) or one by id'),
    'fnv_revert_all': ('act', 'undo every undoable change of this game process'),
    # milestone 7: watch hooks and deep tools
    'fnv_script_profile': ('read', 'calls and time per script (quest, object, effect) from watch hooks on Script::Run'),
    'fnv_script_trace': ('read', 'the statements of one script as they run, with branches taken and failures'),
    'fnv_damage_log': ('read', 'recent hits broken down: base, health, limb, armor damage, flags, applied damage'),
    'fnv_crash_context': ('read', 'what was running when the game faulted: hooked engine calls, scripts, request'),
    'fnv_hang_doctor': ('read', 'sample the call stacks of the game threads and name the functions a hang is stuck in'),
    'fnv_patch_map': ('read', 'engine code and pointers other plugins changed, by owner module, with chains'),
    'fnv_hooks': ('read', 'list the watch hooks of the engine companion, or remove them'),
    # milestone 8: data and authoring
    'fnv_loadorder': ('read', 'the dev profile load order: plugins, index, real file, MO2 mod, masters, problems'),
    'fnv_record_find': ('read', 'records in every active plugin by EditorID, name, form id or type'),
    'fnv_record_show': ('read', 'one record decoded, with every plugin that edits it and what each changed'),
    'fnv_script_lint': ('read', 'check script source against the vanilla, xNVSE and JIP LN command tables'),
    'fnv_record_users': ('read', 'what references a record: placed refs, scripts, leveled lists, conditions, dialogue'),
    'fnv_conflicts': ('read', 'records several plugins edit: winner, fields each override changes, changes lost'),
    'fnv_asset_check': ('read', 'meshes, textures and sounds a plugin names that no archive or loose folder has'),
    'fnv_profile_set': ('control', 'enable or disable mods and plugins in the MO2 dev profile (game closed, backed up)'),
    'fnv_compile_ingame': ('read', 'compile script source with the running game: errors, variables, bytecode'),
    'fnv_script_hot_swap': ('act', 'replace a running script with a new compile, keeping its variables (undoable)'),
    # milestone 9: end to end
    'fnv_scripts_compile': ('control', "compile the project spec's scripts in game into geck-mcp-nv's script cache"),
    # P1 completion: dialogue, AI packages, live form diff
    'fnv_dialogue_tree': ('read', 'every response an NPC could give for a topic, each with its conditions evaluated'),
    'fnv_dialogue_choose': ('act', 'start a conversation with an NPC and pick responses, for testing dialogue end to end'),
    'fnv_ai_packages': ('read', "an actor's packages in order with schedule and conditions: which runs now and why"),
    'fnv_form_live_diff': ('read', 'a form in memory now versus its winning plugin record: what changed at runtime'),
    # agent wave: unattended testing and debugging
    'fnv_trace': ('read', 'calls to any proven engine function: decoded arguments, results, callers (watch hook)'),
    'fnv_watch': ('read', 'values sampled over time: expressions, memory fields, actor values, positions'),
    'fnv_screenshot': ('read', "the game's frame as PNG (engine back buffer, else the window), shown to the agent"),
    'fnv_test_save': ('act', 'named test saves kept apart from other saves: save, load, list, delete'),
    'fnv_crash_history': ('read', 'crash logs grouped by signature across runs, new ones flagged'),
    'fnv_move': ('act', 'teleport the player or a ref to a ref, a cell or a position (undoable)'),
    'fnv_spawn': ('act', 'place a form near a ref and return the new refs (undoable)'),
    'fnv_condition_eval': ('read', "every condition list of a record evaluated against chosen refs, with what fails"),
    'fnv_quest_targets': ('read', "where each objective's markers point and why a marker is missing"),
    'fnv_script_instances': ('read', 'running quest and object script instances with variables and counts'),
    'fnv_menu': ('read', 'open menus, the menu stack, loading screens and the message box with its buttons'),
    'fnv_menu_control': ('act', 'press a message box button through the engine, close a menu, close all menus'),
    'fnv_rng': ('act', "the game's random number generator: state, or seed it for repeatable rolls"),
    'fnv_time': ('act', 'read the calendar, jump to an hour, advance hours, change the timescale'),
    'fnv_leveled_roll': ('read', "roll a leveled list many times with the game's own code: chances per form"),
    # engine AI wave (decomp-built)
    'fnv_detection': ('read', 'why an NPC sees (or does not see) someone: detection records and the formula inputs'),
    'fnv_reaction': ('read', 'how two actors relate: hostile or not, fight reaction, faction pair, disposition, the rule'),
    'fnv_combat': ('read', "an actor's combat controller: target, procedure, action, weapon, group targets, style"),
    'fnv_perk_entries': ('read', 'record perk entry-point checks: value before, perk entries applied, value after'),
    'fnv_pathing': ('read', "an actor's path: mover state, request start/goal on the navmesh, nodes, why it fails"),
    'fnv_raycast': ('read', "cast a ray through the game's physics world: hit object, reference, point; ground height"),
    'fnv_cell_grid': ('read', 'loaded exterior grid, current interior, cell buffers, each cell with why it is loaded'),
    # phase 2: behaviour control and engine calls
    'fnv_hook_set': ('act', 'change an engine function: skip it with a result, replace its result, change an argument, run a script'),
    'fnv_patch': ('act', "write bytes, NOPs, ret or jmp/call into the game's code, journaled, checked against the clean exe"),
    'fnv_tracepoint': ('read', 'record calls of a function when conditions hold: arguments as objects, caller, call stack'),
    'fnv_engine_get': ('read', 'call a proven read-only engine getter with typed arguments (no full control needed)'),
    'fnv_globals': ('read', 'global variables from the data handler, filtered; set values (journaled)'),
    'fnv_dialogue': ('read', 'the conversation now: speaker, spoken topic and line, the choices shown and their state'),
    'fnv_heap': ('read', "memory: the 2 GB address space, the game's pools, form and actor counts, growth over time"),
    'fnv_world_edit': ('act', 'move, rotate, scale, enable, disable, delete or duplicate a ref; weather; image space'),
    'fnv_scene_graph': ('read', "a ref's 3D node tree: transforms, geometry, shaders, texture paths, controllers"),
    'fnv_ui_tiles': ('read', "a menu's live UI tile tree with trait values named by the game's trait table"),
    'fnv_vats_odds': ('read', 'VATS hit chance per target and limb from the game formula, term by term, with perks'),
    'fnv_frame_profile': ('read', 'time per frame by subsystem (scripts, AI wait, render, audio, cells...) from timing hooks'),
    # milestone 10: setup
    'fnv_setup_check': ('read', 'is this machine ready for the fnv tools? each prerequisite with its state and the fix'),
}


def register(server, project, annotations, result_type):
    import json
    from functools import partial

    import anyio
    from mcp.types import CallToolResult, TextContent

    live = LiveService(project)
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
    read, act, control = annotations

    def service_call(name, fn, kind):
        def call():
            return live.run(name, fn, kind)
        return call

    async def invoke(name, fn, kind='read'):
        with anyio.CancelScope(shield=kind != 'read'):
            result = await anyio.to_thread.run_sync(partial(live.run, name, fn, kind), abandon_on_cancel=kind == 'read')
        return CallToolResult(content=[TextContent(type='text', text=json.dumps(result, default=str))],
                              structured_content=json.loads(json.dumps(result, default=str)), is_error=not result.get('ok', True))

    Ref = Annotated[str, Field(min_length=1, max_length=200, description=(
        'player | crosshair | form id (0x00000014, 000A3E7C) | Plugin.esp:LOCALID | EditorID'))]
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
    async def fnv_launch(start: Start = 'menu',
                         timeout_seconds: Annotated[int, Field(ge=20, le=600)] = 240) -> result_type:
        """Start Fallout: New Vegas through the MO2 dev profile (never the user's own profile or saves) and wait
        until it is ready: at the main menu, in a save (loaded by the companion, no clicks), or in a cell via
        coc. Refuses when the game already runs. Returns a timeline of the startup."""
        return await invoke('fnv_launch', lambda: live.game.launch(start, timeout_seconds), 'control')

    @server.tool(annotations=control)
    async def fnv_quit(force: bool = True, timeout_seconds: Annotated[int, Field(ge=5, le=120)] = 20) -> result_type:
        """Quit the game: the console's qqq first, then closing its window, then (force=true) terminating it."""
        return await invoke('fnv_quit', lambda: live.game.quit(timeout_seconds, force), 'control')

    @server.tool(annotations=control)
    async def fnv_restart(start: Start = 'menu',
                          timeout_seconds: Annotated[int, Field(ge=20, le=600)] = 240) -> result_type:
        """Quit (if running) and launch again; use after installing plugins or DLLs."""
        return await invoke('fnv_restart', lambda: live.game.restart(start, timeout_seconds), 'control')

    @server.tool(annotations=control)
    async def fnv_install(plugin_path: Annotated[str, Field(min_length=5, max_length=1000)],
                          mod_name: Annotated[str, Field(max_length=80)] | None = None) -> result_type:
        """Install a built .esp/.esm into the dev profile as its own MO2 mod (mods/<name>/), enable the mod and the
        plugin. The game must be closed. A previous copy is backed up."""
        return await invoke('fnv_install', lambda: live.game.install_plugin(plugin_path, mod_name), 'control')

    @server.tool(annotations=control)
    async def fnv_companion_install() -> result_type:
        """Build the fnvmcp engine companion (C, Zig 0.14.1 from fnv.zig or GECK_MCP_ZIG) and install it into the dev
        profile as the MO2 mod "fnvmcp engine companion". The game must be closed; it loads at the next launch."""
        def action():
            if live.game.process():
                raise ValueError('Quit the game before installing the companion (fnv_quit)')
            receipt = fnv_link.build(project.state_dir, fnv_link.find_zig(project))
            return {'build': receipt, 'install': fnv_link.install(project.fnv.mo2_root, project.fnv.mo2_profile, receipt,
                                                                  project.state_dir)}
        return await invoke('fnv_companion_install', action, 'control')

    @server.tool(annotations=read)
    async def fnv_logs(sources: list[Literal['nvse', 'console', 'crash', 'jip', 'mo2']] | None = None,
                       since: Literal['last', 'start'] = 'last',
                       max_lines: Annotated[int, Field(ge=1, le=5000)] = 200) -> result_type:
        """New lines of the game's logs since the last call (since='last') or the whole file ('start'): nvse.log,
        the companion's console log (everything the console printed, script errors included), CrashLogger.log,
        JIP LN's log and MO2's log."""
        return await invoke('fnv_logs', lambda: live.game.logs(sources, 0 if since == 'start' else 'last', max_lines))

    @server.tool(annotations=control)
    async def fnv_test_run(path: Annotated[str, Field(max_length=1000)] | None = None,
                           yaml: Annotated[str, Field(max_length=100000)] | None = None,
                           relaunch_on_crash: bool = True) -> result_type:
        """Run one scenario test (a YAML file, or inline YAML) in the game. Launches the game when needed, applies
        `start` (menu, continue, a save, a test save or a cell), runs setup and steps (run, script, wait, expect,
        set, actor, input, move, mem_write, call, snapshot, and the agent steps spawn, save, load, menu, screenshot,
        watch, trace, time, rng), then teardown (revert_all: this test's changes, quit). Agent checks: called (a
        traced engine function ran), condition, message_box, marker, menu_open. guard: {message_boxes: keep} leaves
        message boxes for the test; trace_script reports which statements of a script ran. Returns every step's
        result with actual values, failures with reasons, new script errors, a debug bundle on failure (menus,
        message box, console tail, screenshot), and on a crash the crash doctor's diagnosis plus a relaunch."""
        def action():
            if not path and not yaml:
                raise ValueError('Give path or yaml')
            return ScenarioRunner(live).run(path, yaml, relaunch_on_crash)
        return await invoke('fnv_test_run', action, 'control')

    @server.tool(annotations=control)
    async def fnv_test_run_all(folder: Annotated[str, Field(max_length=1000)] | None = None) -> result_type:
        """Run every *.yaml / *.yml / *.json scenario in a folder (default: the project's fnv.tests_dir)."""
        return await invoke('fnv_test_run_all', lambda: ScenarioRunner(live).run_all(folder), 'control')

    @server.tool(annotations=read)
    async def fnv_snapshot(name: Annotated[str, Field(min_length=1, max_length=80)],
                           items: list[Any] | None = None) -> result_type:
        """Capture chosen state under a name: 'player', or items like {eval: expr}, {item_count: form, ref: player},
        {actor_value: Health, ref: ...}, {stage: quest}, {global: name}, {variable: name, of: quest}."""
        return await invoke('fnv_snapshot', lambda: snapshot(live, items, name))

    @server.tool(annotations=read)
    async def fnv_diff(before: Annotated[str, Field(min_length=1, max_length=80)],
                       after: Annotated[str, Field(max_length=80)] | None = None) -> result_type:
        """Compare two snapshots by name; without `after`, capture the same items again now and compare."""
        def action():
            a = load_snapshot(live, before)
            if after:
                b = load_snapshot(live, after)
            else:
                items = []
                for key in a['values']:
                    if key.startswith('player.'):
                        if 'player' not in items:
                            items.append('player')
                    else:
                        items.append(json.loads(key))
                b = snapshot(live, items, before + '-now')
            return diff(a, b)
        return await invoke('fnv_diff', action)

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
    async def fnv_quest(quest: Ref) -> result_type:
        """A quest: running, current stage, completed, every stage with its done flag, objectives with text and
        displayed/completed state, and its script variables by name."""
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
    async def fnv_vars(ref: Ref) -> result_type:
        """Script variables of a quest or a scripted reference, by name, with type and current value."""
        return await invoke('fnv_vars', lambda: live.variables(ref))

    @server.tool(annotations=read)
    async def fnv_script_errors(since: Literal['last', 'start'] = 'last') -> result_type:
        """Script errors printed since the last call (or since the game started): script form id and name, mod,
        and the error message."""
        return await invoke('fnv_script_errors', lambda: live.script_errors(0 if since == 'start' else 'last'))

    @server.tool(annotations=read)
    async def fnv_events(since: Annotated[int, Field(ge=0)] = 0) -> result_type:
        """Events the engine companion recorded after sequence number `since`: game loads, saves, new game, exit
        to menu, script errors (with message)."""
        return await invoke('fnv_events', lambda: live.events(since))

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
        Only proven functions unless allow_unproven=true. A wrong call can crash the game."""
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
                        target: Ref | None = None) -> result_type:
        """Bounded player input. tap/hold a control (forward, backward, left, right, attack, activate, aim, jump,
        sneak, run, ready, vats, pipboy...) or a DirectInput key code; a hold is released by the companion after
        duration_ms even if the bridge goes away. aim_at turns the player toward a ref; activate makes the player
        activate a ref (doors, containers, talking to an NPC)."""
        return await invoke('fnv_input', lambda: live.input(action, control, key, duration_ms, target), 'act')

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
                       journal_id: Annotated[str, Field(max_length=20)] | None = None) -> result_type:
        """Undo the last `count` undoable changes, or the one with journal_id, in reverse order."""
        return await invoke('fnv_undo', lambda: live.undo(count, journal_id), 'act')

    @server.tool(annotations=act)
    async def fnv_revert_all() -> result_type:
        """Undo every undoable change made in the current game process; lists those that cannot be undone."""
        return await invoke('fnv_revert_all', live.revert_all, 'act')

    # ------------------------------------------------------------------ milestone 7
    Action = Literal['start', 'read', 'stop']

    @server.tool(annotations=read)
    async def fnv_script_profile(action: Action = 'read', seconds: Annotated[float, Field(ge=0.2, le=60)] = 3.0,
                                 top: Annotated[int, Field(ge=1, le=200)] = 25) -> result_type:
        """Which scripts run and how long they take. Installs watch hooks on Script::Run and
        Script::RunScriptEffectStart/Finish/Update (proven), which only time each call. action=start clears the
        counters and returns; read gives the top scripts by total time since start (on first use it starts,
        waits `seconds`, then reads): calls per frame, ms per frame, average and worst call, owning quest. stop
        removes the hooks."""
        return await invoke('fnv_script_profile', lambda: deep.script_profile(action, seconds, top))

    @server.tool(annotations=read)
    async def fnv_script_trace(script: Annotated[str, Field(min_length=1, max_length=200, description=(
            'script EditorID or form id, or a quest / reference / base form whose script to trace'))],
                               action: Action = 'read', source: Annotated[str, Field(max_length=200000)] | None = None,
                               limit: Annotated[int, Field(ge=1, le=4096)] = 200) -> result_type:
        """Line-by-line trace of one script as the game runs it (watch hook on ScriptRunner::EvaluateLine,
        proven, filtered to that script). Each executed statement: frame, statement number, kind (set, if,
        command name...), the reference it ran on, whether it succeeded, time. Pass the script's source text to
        map statement numbers to source lines and list the lines that did not run (branches not taken). Calls
        after the first read return only new statements. stop removes the hook."""
        return await invoke('fnv_script_trace', lambda: deep.script_trace(script, action, source, limit))

    @server.tool(annotations=read)
    async def fnv_damage_log(action: Action = 'read', limit: Annotated[int, Field(ge=1, le=1000)] = 50) -> result_type:
        """Each recent hit broken down: attacker, target, weapon, body part, health / limb / fatigue / armor
        damage, weapon base damage, multiplier, flags (critical, sneak attack, fatal, cripple, dismember), and the
        health damage the engine applied. A watch hook on
        Actor::DamageHealthAndFatigue (proven), which also copies the hit data its caller, the actor hit handler, passed on. start clears, stop removes the hook."""
        return await invoke('fnv_damage_log', lambda: deep.damage_log(action, limit))

    @server.tool(annotations=read)
    async def fnv_crash_context(action: Literal['latest', 'enable', 'disable', 'status'] = 'latest',
                                limit: Annotated[int, Field(ge=1, le=20)] = 3) -> result_type:
        """What the crash log cannot know. The engine companion's exception handler writes a context file at a
        fault: the hooked engine calls active on the faulting thread (with this/argument form ids), the request
        and console line being run, recent events. enable adds watch hooks on the script entry points so the
        file names the running script and its reference; latest returns the newest files, named with the engine
        map. fnv_crash_explain attaches the matching file automatically."""
        return await invoke('fnv_crash_context', lambda: deep.crash_context(action, limit))

    @server.tool(annotations=read)
    async def fnv_hang_doctor(samples: Annotated[int, Field(ge=1, le=20)] = 5,
                              interval_ms: Annotated[int, Field(ge=50, le=5000)] = 250,
                              all_threads: bool = False) -> result_type:
        """When the game freezes: read the main thread's call stack (or every thread's) several times from
        outside the process, name each frame with the engine map, and say where it is stuck. Works without the
        companion answering; threads are suspended only while their registers are read."""
        return await invoke('fnv_hang_doctor', lambda: deep.hang_doctor(samples, interval_ms, all_threads))

    @server.tool(annotations=read)
    async def fnv_patch_map(include_bytes: bool = False, limit: Annotated[int, Field(ge=1, le=5000)] = 400) -> result_type:
        """Which engine code and pointers other plugins (xNVSE, JIP LN, Stewie's, ours...) changed in memory:
        the running game's code compared with your unpacked FalloutNV.exe (fnv.clean_exe). Each patch: address,
        engine function, kind (jump, call, pointer, bytes), owner module, and the jump chain when several
        modules hook the same place; functions patched by more than one owner; hooked imports and vtable
        slots."""
        return await invoke('fnv_patch_map', lambda: deep.patch_map(include_bytes, limit))

    @server.tool(annotations=read)
    async def fnv_hooks(action: Literal['list', 'remove'] = 'list',
                        hook_id: Annotated[str, Field(max_length=10)] | None = None) -> result_type:
        """The engine companion's watch hooks (function, calls seen, records). remove takes a hook id or 'all'."""
        return await invoke('fnv_hooks', lambda: deep.hooks_tool(action, hook_id))

    # ------------------------------------------------------------------ milestone 8
    @server.tool(annotations=read)
    async def fnv_loadorder() -> result_type:
        """The dev profile's plugins in load order: index prefix, the real file behind MO2's virtual folder (the
        MO2 mod or the game Data folder), masters, and problems (missing masters, missing files). Uses the running
        game's list when the game runs."""
        return await invoke('fnv_loadorder', data.loadorder)

    @server.tool(annotations=read)
    async def fnv_record_find(query: Annotated[str, Field(max_length=200)] = '',
                              record_type: Annotated[str, Field(pattern=r'^[A-Za-z_]{4}$')] | None = None,
                              plugin: Annotated[str, Field(max_length=120)] | None = None,
                              limit: Annotated[int, Field(ge=1, le=200)] = 25,
                              offset: Annotated[int, Field(ge=0)] = 0) -> result_type:
        """Find records in every active plugin of the load order (masters, DLC, mods) by EditorID or name
        substring, load-order form id (0x00104C6C), or Plugin.esp:LOCALID; filter by type (WEAP, NPC_, QUST...)
        or plugin. Each result names the plugin that defines it, the winning plugin and the override count. The
        first call indexes the plugins (seconds; cached until a file changes)."""
        return await invoke('fnv_record_find', lambda: data.record_find(query, record_type, plugin, limit, offset))

    @server.tool(annotations=read)
    async def fnv_record_show(record: Annotated[str, Field(min_length=1, max_length=200, description=(
            'EditorID, load-order form id, or Plugin.esp:LOCALID'))], all_versions: bool = True,
                              max_bytes: Annotated[int, Field(ge=16, le=4096)] = 256) -> result_type:
        """One record decoded from the plugins: the winning version's subrecords (strings, form references with
        their EditorIDs, script header, source and bytecode size, common DATA layouts), every plugin that edits it
        in load order, and which subrecords each override changed."""
        return await invoke('fnv_record_show', lambda: data.record_show(record, all_versions, max_bytes))

    RecordRef = Annotated[str, Field(min_length=1, max_length=200, description=(
        'EditorID, load-order form id, or Plugin.esp:LOCALID'))]

    @server.tool(annotations=read)
    async def fnv_record_users(record: RecordRef, types: list[Annotated[str, Field(pattern=r'^[A-Za-z_]{4}$')]] | None = None,
                               limit: Annotated[int, Field(ge=1, le=500)] = 100,
                               offset: Annotated[int, Field(ge=0)] = 0) -> result_type:
        """Everything in the load order that references a record: placed references of it, scripts (SCRO), leveled
        lists, containers and inventories, conditions (dialogue, packages, perks), packages, form lists, enable
        parents, owners, door links and any other field holding its form id. Each user says how it uses the record,
        which plugins' versions reference it and whether the winning version still does. The first call indexes the
        load order's references (seconds; cached until a plugin changes)."""
        return await invoke('fnv_record_users', lambda: records.record_users(record, types, limit, offset))

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
        return await invoke('fnv_profile_set', lambda: records.profile_set(mods, plugins, live.game, dry_run), 'control')

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
    @server.tool(annotations=read)
    async def fnv_dialogue_tree(speaker: Ref, topic: Annotated[str, Field(min_length=1, max_length=200, description=(
            'topic EditorID or form id (DIAL); GREETING by default'))] = 'GREETING',
                                scope: Literal['speaker', 'available', 'all'] = 'speaker',
                                include_text: bool = True,
                                limit: Annotated[int, Field(ge=1, le=300)] = 60) -> result_type:
        """All responses the speaker could give for a topic, from every quest that adds to it: each response's
        prompt, quest and priority, flags (say once, goodbye, random...), speech challenge, response text, every
        condition with its current value and pass/fail, and the engine's own verdict (TESTopicInfo::CheckCondition,
        proven, run with the speaker and the player). wouldSay is the response the engine would pick now. scope:
        speaker (default) keeps the responses written for this speaker (their GetIsID / GetIsVoiceType / GetInFaction
        conditions pass) and any available now; available keeps only those; all lists every response of the topic."""
        return await invoke('fnv_dialogue_tree', lambda: social.dialogue_tree(speaker, topic, include_text, limit, scope))

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
    @server.tool(annotations=read)
    async def fnv_trace(function: Annotated[str, Field(max_length=200, description=(
            'proven engine function name (Actor::DamageHealthAndFatigue) or address'))] | None = None,
                        action: Literal['start', 'read', 'stop', 'list'] = 'start',
                        filter: dict[str, Any] | None = None,
                        limit: Annotated[int, Field(ge=1, le=500)] = 50,
                        include_calls: bool = True) -> result_type:
        """Watch any proven engine function while the game runs: start installs an observe-only hook, read returns
        the calls since the last read: `this` and every argument decoded with the function's decomp signature (forms
        and refs named), the result, the time, and the caller named from the engine map, plus counts by caller and by
        `this`. filter keeps only calls where this / caller / paramN equals a ref ({on: this, ref: MyNPC}) or a value
        ({on: param1, value: 5}). Answers "does the engine run X for my object, from where, with what". stop removes
        the hook; list shows running traces."""
        return await invoke('fnv_trace', lambda: agent.trace(function, action, filter, limit, include_calls))

    @server.tool(annotations=read)
    async def fnv_watch(items: list[Any],
                        seconds: Annotated[float, Field(gt=0, le=120)] = 5.0,
                        interval_ms: Annotated[int, Field(ge=0, le=10000)] = 100,
                        until: Annotated[str, Field(max_length=500)] | None = None,
                        keep_unpaused: bool = True) -> result_type:
        """Sample values over time and return how each changed: items are expressions ("player.GetAV Health",
        "MyQuest.counter", "GetStage MyQuest") or mappings {eval, ref}, {actor_value, ref}, {variable, of},
        {position: ref}, {mem: ref or 0xADDR, path: Class.field} (decomp layouts). Stops early when `until` (an
        expression) is true. Each item gives first, last, min, max, number of changes and a timeline of changes
        with time and frame; paused=true when the frame counter did not move."""
        return await invoke('fnv_watch', lambda: agent.watch(items, seconds, interval_ms, until, keep_unpaused))

    @server.tool(annotations=read)
    async def fnv_screenshot(name: Annotated[str, Field(max_length=60)] | None = None,
                             max_width: Annotated[int, Field(ge=320, le=3840)] = 1280,
                             method: Literal['auto', 'engine', 'window'] = 'auto') -> result_type:
        """The game's current frame, returned as an image you can look at and saved as PNG. auto asks the engine
        to write its back buffer (SaveScreenshot, works while the window is covered; needs iMultiSample=0 and
        bAllowScreenShot=1 in the dev profile), else captures the game window."""
        import base64
        from mcp.types import ImageContent
        with anyio.CancelScope(shield=True):
            result = await anyio.to_thread.run_sync(partial(live.run, 'fnv_screenshot',
                                                            lambda: agent.screenshot(name, max_width, method), 'read'))
        content = [TextContent(type='text', text=json.dumps(result, default=str))]
        path = (result.get('data') or {}).get('path') if result.get('ok') else None
        if path:
            with open(path, 'rb') as f:
                content.append(ImageContent(type='image', mimeType='image/png',
                                            data=base64.b64encode(f.read()).decode('ascii')))
        return CallToolResult(content=content, structured_content=json.loads(json.dumps(result, default=str)),
                              is_error=not result.get('ok', True))

    @server.tool(annotations=act)
    async def fnv_test_save(action: Literal['save', 'load', 'list', 'delete'] = 'list',
                            name: Annotated[str, Field(pattern=r'^[A-Za-z0-9_\-]{1,60}$')] | None = None,
                            timeout_seconds: Annotated[int, Field(ge=20, le=600)] = 180) -> result_type:
        """Named test saves in the dev profile, kept apart from other saves by the prefix fnvmcp_test_: save the
        current game, load one (waits until gameplay), list or delete them. Tests start from one with
        start: {test_save: name}."""
        kind = 'read' if action == 'list' else 'act'
        return await invoke('fnv_test_save', lambda: agent.test_save(action, name, timeout_seconds), kind)

    @server.tool(annotations=read)
    async def fnv_crash_history(limit: Annotated[int, Field(ge=1, le=200)] = 40,
                                include_shutdown: bool = True) -> result_type:
        """Every crash log in the crash logger folders, explained with the engine map and grouped by signature
        (exception, crash-site function, next frames): count, first and last time, the likely culprits, and new=true
        for signatures no earlier call has seen. Tells a new crash from the same one again, and flags crashes during
        exit cleanup (seen on every quit), which are usually not your mod."""
        return await invoke('fnv_crash_history', lambda: agent.crash_history(limit, include_shutdown))

    @server.tool(annotations=act)
    async def fnv_move(ref: Ref = 'player', to: Ref | None = None,
                       cell: Annotated[str, Field(pattern=r'^[A-Za-z0-9_]{1,120}$')] | None = None,
                       position: dict[str, float] | None = None, rotation: dict[str, float] | None = None,
                       timeout_seconds: Annotated[int, Field(ge=10, le=600)] = 180) -> result_type:
        """Teleport the player or any ref: to another ref (MoveTo), to a cell (the player by coc; other refs need a
        position in it, PositionCell), or to a position {x, y, z} in its current cell; rotation {z} in degrees.
        Waits for the new cell to load when the player changes cells. Journaled: fnv_undo puts it back."""
        return await invoke('fnv_move', lambda: agent.move(ref, to, cell, position, rotation, timeout_seconds), 'act')

    @server.tool(annotations=act)
    async def fnv_spawn(form: Ref, near: Ref = 'player', count: Annotated[int, Field(ge=1, le=20)] = 1,
                        distance: Annotated[int, Field(ge=0, le=10000)] = 0,
                        direction: Annotated[int, Field(ge=0, le=3)] = 0) -> result_type:
        """Place a base form (NPC, creature, item, activator...) near a ref with PlaceAtMe and return the new refs
        with their positions. distance/direction as PlaceAtMe (direction 0 front, 1 back, 2 left, 3 right).
        Journaled: fnv_undo / fnv_revert_all disable and delete them."""
        return await invoke('fnv_spawn', lambda: agent.spawn(form, near, count, distance, direction), 'act')

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
    async def fnv_quest_targets(quest: Ref) -> result_type:
        """Where each quest objective's markers point and why a marker is missing: per target the ref, its cell,
        position and distance, the target conditions (run on the target ref) with pass/fail, the door the marker
        points at for targets in other cells, whether the game has it in the player's target cache, and every reason
        it is not shown (quest not running or not the active quest, objective not displayed or completed, ref
        missing, deleted or disabled, conditions failing)."""
        return await invoke('fnv_quest_targets', lambda: agent.quest_targets(quest))

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
    async def fnv_menu() -> result_type:
        """The game's menus now: interface mode, the active menu, the menu stack, every visible menu by name, whether
        a loading screen is up, and an open message box with its title, text and buttons (labels and values), read
        from the interface manager and the message menu's tiles."""
        return await invoke('fnv_menu', lambda: agent.menus())

    @server.tool(annotations=act)
    async def fnv_menu_control(action: Literal['press', 'close', 'close_all'],
                               button: Annotated[str, Field(max_length=200)] | None = None,
                               menu: Annotated[str, Field(max_length=60)] | None = None) -> result_type:
        """Drive menus without a person: press a message box button by index ("0") or label ("Yes") through the
        engine's own click handler, so the script's GetButtonPressed sees it; close the active (or a named) menu;
        or close all menus."""
        return await invoke('fnv_menu_control', lambda: agent.menu_control(action, button, menu), 'act')

    @server.tool(annotations=act)
    async def fnv_rng(seed: Annotated[int, Field(ge=0, le=0xFFFFFFFF)] | None = None) -> result_type:
        """The game's random number generator (one Mersenne Twister for leveled lists, VATS, combat, AI, lockpicks,
        GetRandomPercent): without seed its state; with seed it is seeded like MersenneTwister::Seed, so rolls in the
        same request repeat exactly (later frames keep drawing numbers)."""
        kind = 'read' if seed is None else 'act'
        return await invoke('fnv_rng', lambda: agent.rng(seed), kind)

    @server.tool(annotations=act)
    async def fnv_time(set_hour: Annotated[float, Field(ge=0, lt=24)] | None = None,
                       advance_hours: Annotated[float, Field(gt=0, le=720)] | None = None,
                       timescale: Annotated[float, Field(ge=0, le=10000)] | None = None,
                       mode: Literal['globals', 'engine_wait'] = 'globals',
                       evaluate_packages: bool = True, allow_unproven: bool = False) -> result_type:
        """Control game time for tests: jump to an hour, advance hours (day, month and year roll over like the game's
        calendar), or change the timescale; with no arguments it reads the calendar. mode=globals moves the clock and
        re-evaluates nearby actors' packages; mode=engine_wait runs the game's own one-hour wait step per hour
        (process lists catch up, healing, hardcore needs), an unnamed function, so it needs allow_unproven."""
        if set_hour is None and advance_hours is None and timescale is None:
            return await invoke('fnv_time', lambda: {'calendar': agent.calendar(), 'changed': False})
        return await invoke('fnv_time', lambda: agent.time_control(set_hour, advance_hours, timescale, mode,
                                                                   evaluate_packages, allow_unproven), 'act')

    @server.tool(annotations=read)
    async def fnv_leveled_roll(form: Ref, level: Annotated[int, Field(ge=1, le=255)] | None = None,
                               rolls: Annotated[int, Field(ge=1, le=5000)] = 200,
                               seed: Annotated[int, Field(ge=0, le=0xFFFFFFFF)] | None = None) -> result_type:
        """Roll a leveled list (items, NPCs, creatures) many times with the game's own code
        (TESLeveledList::CalculateCurrentForm, proven) at a level (default the player's): how often each form and
        count comes out and how often nothing, next to the list's entries, chance none and flags. With seed the
        rolls repeat exactly. Changes nothing in the world (it only draws random numbers)."""
        return await invoke('fnv_leveled_roll', lambda: agent.leveled_roll(form, level, rolls, seed))

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

    @server.tool(annotations=read)
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
                                                                                actor, filters))

    @server.tool(annotations=read)
    async def fnv_pathing(actor: Ref) -> result_type:
        """The path an actor is following and why it fails: mover state (building, moving, failed), the pathing
        request (start and goal with their navmesh triangle, door rules), the solution's path points, the path
        handler's state (waiting, blocked, avoidance failed) and the reasons found."""
        return await invoke('fnv_pathing', lambda: engine_ai.pathing(actor))

    @server.tool(annotations=read)
    async def fnv_raycast(start: dict[str, float] | None = None, end: dict[str, float] | None = None,
                          from_ref: Ref | None = None, to_ref: Ref | None = None,
                          layer: Literal['line_of_sight', 'path_pick', 'projectile'] = 'line_of_sight',
                          ground_at: dict[str, float] | None = None) -> result_type:
        """Cast a ray through the game's Havok world (the engine's own TES::Pick): from start to end positions, or
        from one ref to another (eye height); returns whether it hits, the hit object and its reference, and the
        hit point. ground_at {x, y} instead returns the terrain height there."""
        if ground_at:
            return await invoke('fnv_raycast', lambda: engine_ai.ground_height(ground_at.get('x', 0.0),
                                                                                ground_at.get('y', 0.0)))
        return await invoke('fnv_raycast', lambda: engine_ai.raycast(start, end, from_ref, to_ref, layer))

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
    async def fnv_tracepoint(function: Annotated[str, Field(min_length=1, max_length=300)],
                             action: Literal['start', 'read', 'stop', 'list'] = 'start',
                             when: list[Filter] | None = None,
                             limit: Annotated[int, Field(ge=1, le=200)] = 30,
                             stack: bool = True, allow_unproven: bool = False) -> result_type:
        """A breakpoint that never stops the game: record the calls of an engine function that meet conditions
        (when: [{on: this|caller|param0..7, ref|value}], all must hold), each with its arguments decoded as objects,
        the result, the time it took, the caller and the call stack (functions it came through). Read while it runs;
        commonStacks groups the hits by the path that led to them."""
        return await invoke('fnv_tracepoint', lambda: control_tools.tracepoint(function, action, when, limit, allow_unproven,
                                                                               stack))

    @server.tool(annotations=read)
    async def fnv_engine_get(function: Annotated[str, Field(min_length=1, max_length=300, description=(
            'A proven getter: Actor::GetLevel, TESObjectREFR::GetDistanceFromReference, ...'))],
                             this: Ref | None = None, args: list[Any] | None = None) -> result_type:
        """Call a proven read-only engine getter (Get/Is/Has/Can/Count/Find... names whose direct callees change
        nothing) with typed arguments: plain values are typed from the decomp signature, or give {type, value} with
        type int, bool, float, double, ptr, ref or form. Pointer results are described (form, class). For anything
        else use fnv_engine_call."""
        return await invoke('fnv_engine_get', lambda: control_tools.engine_get(function, this, args))

    # ------------------------------------------------------------------ phase 2: world, UI, memory
    @server.tool(annotations=read)
    async def fnv_globals(query: Annotated[str, Field(max_length=120)] | None = None,
                          set: dict[Annotated[str, Field(min_length=1, max_length=120)], float] | None = None,
                          limit: Annotated[int, Field(ge=1, le=2000)] = 200) -> result_type:
        """Global variables as the running game holds them (TESDataHandler's list): editor id, form id, type and
        value, filtered by a name substring. set {"GameHour": 12} writes values (shorts and longs are whole
        numbers); each write is journaled and undoable."""
        return await invoke('fnv_globals', lambda: world.globals(query, set, limit), 'act' if set else 'read')

    @server.tool(annotations=read)
    async def fnv_dialogue() -> result_type:
        """The conversation on screen now: speaker, dialogue state, the topic being spoken and its info, the NPC
        line and name as shown, and the player's choices in order (prompt, topic, info, quest, already heard). Says
        why other topics are not listed; fnv_dialogue_tree evaluates a topic's conditions."""
        return await invoke('fnv_dialogue', world.dialogue)

    @server.tool(annotations=read)
    async def fnv_heap(seconds: Annotated[float, Field(ge=0, le=120)] = 0) -> result_type:
        """Memory of the running game: its address space (committed, reserved, free, largest free block, the 2 GB
        limit of the non-large-address-aware exe), the game's memory pools by use, counts of forms and of high /
        middle / low process actors. seconds > 0 samples twice and reports the growth (leak finding)."""
        return await invoke('fnv_heap', lambda: world.heap(seconds))

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
    async def fnv_ui_tiles(menu: Annotated[str, Field(min_length=1, max_length=40, description=(
            'hud, dialog, inventory, pipboy menus by name, a menu id 1001..1084, or root'))] = 'hud',
                           path: Annotated[str, Field(max_length=200)] | None = None,
                           depth: Annotated[int, Field(ge=0, le=30)] = 8,
                           max_tiles: Annotated[int, Field(ge=1, le=3000)] = 400,
                           traits: list[Annotated[str, Field(max_length=60)]] | None = None) -> result_type:
        """A menu's live UI tile tree: each tile's path, class (TileRect, TileImage, TileText, ...) and trait
        values (_x, _y, _visible, _string, user traits...) named with the game's own trait table. path filters by
        a substring of the tile path; traits keeps only those trait names. For debugging UI mods."""
        return await invoke('fnv_ui_tiles', lambda: world.ui_tiles(menu, path, depth, max_tiles, traits))

    @server.tool(annotations=read)
    async def fnv_vats_odds(target: Ref | None = None, limb: Annotated[int, Field(ge=-1, le=14)] | None = None,
                            include_terms: bool = True) -> result_type:
        """VATS hit chance for every target and limb VATS offers now (or a given target), computed with the game's
        CombatFormulas::GetVATSChanceToHit and the perk layer, with the percent the menu shows next to what it
        stored. terms explains the gun formula: spread cone at the distance, target area, visibility, limb to-hit
        and the settings involved. Without VATS open, pass target (and limb) to compute as if fully visible."""
        return await invoke('fnv_vats_odds', lambda: world.vats_odds(target, limb, include_terms))

    @server.tool(annotations=read)
    async def fnv_frame_profile(seconds: Annotated[float, Field(ge=0.5, le=30)] = 3.0) -> result_type:
        """Where frame time goes: timing hooks on the main loop and its children (player update, input, IO, actor
        scripts, quest/cell scripts, process lists, cell streaming, culling, scene update, render, audio, AI wait,
        post-render, delayed actions) for a few seconds, then removed. ms per frame, share of the frame and the
        worst call per subsystem, with fps."""
        return await invoke('fnv_frame_profile', lambda: world.frame_profile(seconds))

    @server.tool(annotations=read)
    async def fnv_setup_check() -> result_type:
        """Is this machine ready for the fnv_* tools? Checks the game build, xNVSE, the MO2 dev instance and its NVSE
        executable, JIP LN NVSE, the crash logger, bAlwaysActive, Zig, the engine companion (installed, up to date),
        the engine map, the optional unpacked exe and full control; each with what to do when it is missing."""
        from .fnv_setup import setup_check
        return await invoke('fnv_setup_check', lambda: setup_check(project))

    return live
