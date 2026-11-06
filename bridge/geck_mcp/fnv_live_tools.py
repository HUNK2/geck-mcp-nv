"""MCP registration of the live fnv_* tools: game control and tests (milestone 4), live inspection
(milestone 5) and full control (milestone 6). Kinds: read (changes nothing), act (changes the running game,
journaled, refused unless fnv.engine_control = "full"), control (starts/stops the game or installs files)."""
import time
import uuid
from typing import Annotated, Any, Literal

from pydantic import Field

from . import fnv_link
from .fnv_game import Game
from .fnv_deep import DeepTools
from .fnv_live import LiveService
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
}


def register(server, project, annotations, result_type):
    import json
    from functools import partial

    import anyio
    from mcp.types import CallToolResult, TextContent

    live = LiveService(project)
    deep = DeepTools(live)
    live.deep = deep
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
        `start`, runs setup and steps (run, script, wait, expect, set, actor, input, move, mem_write, call,
        snapshot), then teardown (revert_all, quit). Returns every step's result with actual values, failures with
        reasons, new script errors, a screenshot on failure, and on a crash the crash doctor's diagnosis plus a
        relaunch."""
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

    return live
