"""Descriptions for tool parameters that the signatures leave undescribed (release audit M1).

The server fills them into each tool's input schema when it lists the tools (BridgeServer.list_tools), so a client
sees what every argument does without reading the tool's prose. A parameter that already has a description in its
signature (Field(description=...)) keeps it and needs no entry here. tests/test_param_docs.py checks that every tool
with six or more parameters has all of them described and that every entry names a real tool and parameter."""

PARAM_DOCS: dict[str, dict[str, str]] = {'fnv_anim': {'action': 'What to do: state (current/queued groups), play (start an animation), groups (list groups '
                        'with KF files), idle_tree (why an idle would or would not be picked), biped (equipment '
                        'slots), face (expression values), events (record animation actions).',
              'group': 'For action=play: name of the animation group to play on the reference. Use this or idle.',
              'idle': 'For action=play: the IDLE form to play on the reference. Use this or group.',
              'rebuild': "For action=biped: re-runs the engine's equipment build for the reference. This changes the "
                         'game; false only reads the 20 equipment slots.',
              'seconds': 'For action=events: how many seconds to record animation actions (equip, attack, '
                         'reload...). Default 3.'},
 'fnv_asset': {'action': 'What to do: reload (re-read a file in the running game), watch (reload changed files in a '
                         'mod folder), file_log (log file opens/failures), queue, where, models, archives, nif/dds '
                         '(offline file checks), users (records naming a file).',
               'path': 'File or folder path. For reload, where, nif, dds, users: the asset file. For watch: the mod '
                       'folder to scan for changes.',
               'mode': 'For action=file_log: start the file-open log, read what it collected, or stop it. Omitted '
                       'means read.',
               'missing': 'For action=file_log: if true, list only files the engine failed to find.',
               'ext': 'For action=file_log: only show files with this extension.',
               'folder': 'For action=file_log: only show files under this folder.',
               'limit': 'Maximum number of rows returned. Default 200.',
               'wait_idle': 'For action=queue: wait until the asset IO queue is idle before returning.',
               'menu': 'For action=reload of a menu .xml path: the menu name (for example HUDMainMenu) to evict and '
                       'rebuild.'},
 'fnv_atlas': {'action': 'What to do: actions (list scripted actions), run (measure actions), runs (stored runs), '
                         'function (fits for one function), action (functions an action runs), import_frida (load a '
                         'Frida trace file).',
               'actions': 'For action=run: names of the scripted actions to measure. Omitted means the default set '
                          'from the action library.',
               'reps': 'For action=run: how many times each measurement is repeated. Default 2.',
               'doses': 'For action=run: how many times the action is done in each segment, for example [1, 2, 4]. '
                        "Omitted uses each action's own default.",
               'name': 'For action=action: the name of the game action whose functions to list.',
               'function': 'For action=function: the function (name or address) whose recorded fits and callers to '
                           'show.',
               'kind': 'For action=action: keep only fits of this kind, per_unit (calls per action) or with_action '
                       '(only while acting).',
               'run': 'For action=action: the run number to read. Omitted means the latest run that measured the '
                      'action.',
               'statuses': 'For action=action: keep only functions with these naming statuses, for example '
                           "['unnamed'].",
               'exclusive': 'For action=action: if true, keep only functions that no other action runs.',
               'include_console_machinery': 'For action=action: if true, also include functions that only the '
                                            'console-command driver runs; normally left out.',
               'path': 'For action=import_frida: path of the fnvsym Frida trace file to load as its own run.',
               'notes': 'For action=run: free-text note stored with the run.',
               'limit': 'For action=action: maximum functions returned. Default 60.'},
 'fnv_audio': {'action': 'What to do: log (sounds created meanwhile), play, playing (sounds held now), stop, music, '
                         'radio, subtitles, hear (would a sound play at a position), voice_check (voice/lip files of '
                         "an NPC's lines).",
               'seconds': 'For action=log and subtitles: how many seconds to watch. Default 3.',
               'sound': 'Sound form (SOUN) name or id. Used by play, stop and hear.',
               'sound_id': 'For action=stop: id of the playing sound instance to stop, as shown by playing.',
               'file': 'For action=music: path of a music track to force.',
               'stop': 'For action=music: if true, fade the music out.',
               'fade_ms': 'For action=music with stop: fade-out time in milliseconds. Default 1000.',
               'enable': 'For action=radio: turn the radio on or off. For action=subtitles: turn subtitles on. '
                         'Omitted leaves the setting alone.',
               'position': 'For action=hear: world position [x, y, z] in game units where the sound would be '
                           'played.'},
 'fnv_bisect': {'level': 'What to turn off to find the culprit: mods (MO2 mods), plugins, or records of one plugin '
                         '(needs plugin).',
                'steps': 'The failing scenario to run each round, in the same format as fnv_test steps.',
                'keep': 'Mods, plugins or record form ids that are never turned off. The companion, xNVSE data, JIP '
                        'and base game files are always kept.',
                'max_rounds': 'Maximum number of halving rounds before stopping. Default 10.',
                'plugin': 'For level=records: the plugin whose records are the suspects. Each round writes a variant '
                          'of it without half of them.'},
 'fnv_blame': {'action': 'What to do: field (watch writes to an address), ref (hook engine functions that touch a '
                         'reference), report (group hits so far by culprit), form (change flags set on a form '
                         'meanwhile).',
               'address': 'For action=field: the address to watch, as 0x... or a reference plus 0x offset.',
               'ref': 'For action=ref and form: the reference to watch.',
               'what': 'For action=ref: comma-separated hook presets to arm, or all. Omitted uses all presets.',
               'length': 'For action=field: number of bytes watched at the address, 1 to 64. Default 4.',
               'access': 'For action=field: break on writes only, or on reads and writes.',
               'scope': 'For action=field: watch the main thread only, or all threads.',
               'seconds': 'How long to watch. For field and form: default 5. For ref: seconds to wait and collect '
                          'hits before answering; omitted returns at once with the hooks armed (run report later).'},
 'fnv_camera': {'action': 'What to do: get (read camera), set (free camera), orbit (around a reference), pov '
                          "(first/third person), restore (back to the player's view).",
                'yaw': 'Camera heading in degrees (0 = north, clockwise). For action=orbit it is relative to the '
                       "reference's heading: 180 in front, 0 behind.",
                'pitch': 'Look-down angle in degrees; positive looks down.',
                'distance': "For action=orbit: distance from the reference's centre in game units. Omit it and use "
                            'fit=true to derive it.',
                'fit': "For action=orbit: if true, choose the distance from the reference's size and the FOV.",
                'fov': 'Field of view in degrees; changes the world FOV when given.',
                'view': 'For action=pov: switch to first or third person.',
                'freeze': 'For action=set: if true (default) the world is stopped while the camera is free.'},
 'fnv_code': {'action': "Step: checkout (copy a function's source), edit (find/replace in the copy), build (compile "
                        'and link), compare (run original and new side by side), swap/unswap (activate or undo), '
                        'list (active swaps), export (patch set and loader).',
              'function': 'Engine function to work on, by name or address. Used by checkout, edit, build, compare, '
                          'swap, unswap.',
              'find': 'For action=edit: text to find inside the function copy.',
              'replace': 'For action=edit: text that replaces the found text.',
              'args': 'For action=compare: list of argument objects to call the original and the new code with.',
              'this': 'For action=compare: the this-pointer or reference for a method call.',
              'returns': 'For action=compare: type of the return value (void, int, float, ptr, form...), used to '
                         'compare the results.',
              'mode': "For action=compare: call (default) runs both with given args; shadow records the game's own "
                      'calls of the original and replays them on the new code.',
              'seconds': "For action=compare with mode=shadow: how long to record the game's calls. Default 5.",
              'nargs': 'For action=compare with mode=shadow: number of arguments the function takes, 0 to 8.'},
 'fnv_coverage': {'action': 'What to do: start (arm line traps), stop (report lines run and never run, save the '
                            'run), diff (compare two saved runs).',
                  'files': 'For action=start: decomp source files to trace, for example tesobjectrefr.cpp.',
                  'functions': 'For action=start: names of engine functions to trace, as an alternative to files.',
                  'name': 'For action=start: name for the saved run (plain name, no paths). Omitted gives a '
                          'coverage- timestamp name.',
                  'a': 'For action=diff: the first saved run to compare.',
                  'b': 'For action=diff: the second saved run; the result lists lines that ran in one but not the '
                       'other.'},
 'fnv_crawl': {'action': 'What to check: cells (visit every cell the plugin touches), dialogue (lines of one NPC), '
                         'quest (set quest stages in order), items (give, equip and use each new item).',
               'plugin': 'For action=cells and items: the plugin whose cells or new items are checked.',
               'max_cells': 'For action=cells: maximum number of cells to visit. Default 20.',
               'settle_frames': 'For action=cells: frames to wait in each cell for it to settle before checking. '
                                'Default 90.',
               'photos': 'For action=cells: take a photo in each cell. Default true.',
               'limit': 'Maximum rows returned. Default 60.',
               'path': 'For action=quest: the quest stages to set in order.'},
 'fnv_debug': {'action': 'What to do: break sets/lists/clears stops; status, stack, locals read the stop; eval and '
                         'set work on the stopped frame; continue, step, until, return, skip resume; '
                         'freeze/thaw/threads act on threads; dump writes a minidump.',
               'op': 'For action=break: set creates a breakpoint (needs location), list shows them, clear removes '
                     'one (break_id). Omitted with no location behaves as list.',
               'location': "For break, until and skip: a function name, 0xADDRESS, file:line, or 'fault' (break "
                           'only) naming where to stop or resume.',
               'break_id': 'For action=break op=clear: the id of the breakpoint to remove.',
               'when': "For break op=set: a predicate such as 'frame >= 100'; the breakpoint only counts or stops "
                       'when it holds.',
               'hits': 'For break op=set: stop from the Nth hit that passes the when predicate (1 stops on the '
                       'first).',
               'thread': 'For break op=set: main stops only on the main thread, any stops on whichever thread hits '
                         'it.',
               'do': 'For break op=set: stop (halt the game at the breakpoint) or log (record the log expressions '
                     'and keep running).',
               'log': 'For break op=set with do=log: expressions whose values are recorded at each hit instead of '
                      'stopping.',
               'depth': 'For action=stack: how many stack frames to return.',
               'with_locals': 'For action=stack: also include the local variables of each frame. Default false.',
               'frame': 'For locals, eval and set: the stack frame number to work in, 0 being the innermost frame.',
               'expression': 'For eval: the expression to evaluate in the stopped frame. For set: the target to '
                             'change, such as eax, a local or this->m_fHealth.',
               'value': 'For set: the number to store in the expression target. For return: the value the function '
                        'returns.',
               'mode': 'For action=step: instruction, into, over, out or line, choosing how far to advance before '
                       'stopping again.',
               'file': 'For action=dump: file name for the minidump, written in the bridge state folder.'},
 'fnv_test': {'action': 'run (default) runs one scenario file or inline yaml, run_all every scenario in a '
                       'folder, steps the steps given inline.',
              'path': 'For action=run: the scenario YAML file to run.',
              'yaml': 'For action=run: the scenario as YAML text, instead of a file.',
              'folder': "For action=run_all: the folder of scenarios. Default: the project's fnv.tests_dir.",
              'steps': 'The scenario steps to run in order, in the same form as a YAML scenario file (run, script, '
                       'wait, expect, set, input, move, step, call, and so on).',
              'name': "Name of this scenario, used in the report. Default 'inline'.",
              'plugins': 'Plugins the scenario needs; the run stops with PLUGIN_NOT_LOADED if one is not loaded in the '
                         'dev profile (install it with fnv_install first).',
              'setup': 'Steps run before the main steps, in the same step format.',
              'teardown': 'Steps run after the main steps, in the same step format.',
              'deterministic': 'When true, fixes the frame step at 60 fps and seeds the random generator so runs are '
                               'repeatable. Default false.',
              'repeat': 'Run the whole scenario this many times and report the steps that differ between runs. Default '
                        '1.',
              'timeout_seconds': 'Time limit for the scenario run, in seconds. Default 240.',
              'relaunch_on_crash': 'When true (default), the game is relaunched if it crashes during the run.'},
 'fnv_func_find': {'query': 'Words of a function name, an address, code text or a regular expression; for callers '
                           'and callees the function, by name or address.',
                   'mode': 'auto, name, code, regex or address search; callers or callees walk the call graph.',
                   'status': 'For the search modes: keep only these proof statuses (proven, class_proven, probable, '
                             'decomp, unnamed).',
                   'limit': 'Maximum number of functions returned.',
                   'offset': 'For the search modes: how many results to skip, for paging.',
                   'depth': 'For callers and callees: how many call levels to walk, 1 to 4.'},
 'fnv_menu': {'action': "state (default) reads the menus; tiles reads one menu's live tile tree.",
              'path': 'For action=tiles: keep only tiles whose path contains this text.',
              'depth': 'For action=tiles: how deep to read the tile tree. Default 8.',
              'max_tiles': 'For action=tiles: maximum number of tiles returned. Default 400.',
              'traits': 'For action=tiles: keep only these trait names, such as _x or _visible.'},
 'fnv_engine_call': {'function': 'The engine function to call: a decomp name or an address.',
                     'this': 'The object for a thiscall: a reference spec or a 0xADDRESS. Leave empty for functions '
                             'without a this pointer.',
                     'args': 'Arguments in order, each an object {type, value} with type one of int, bool, ptr, ref, '
                             'form, float, double or string.',
                     'returns': "How to read the function's return value: void, int, uint, bool, ptr, float, double "
                                'or form. Default int.',
                     'convention': 'Calling convention override: cdecl, stdcall, thiscall or fastcall. When null it '
                                   'is taken from the decomp signature.',
                     'allow_unproven': 'When true, also allows functions not yet proven; the call then runs guarded '
                                       'and a fault returns CALL_FAULTED instead of crashing. Default false.'},
 'fnv_field_watch': {'action': 'find snapshots an object before and after trigger lines and lists what changed; '
                               'watch sets a hardware watchpoint and reports each function that touched the address. '
                               'Default watch.',
                     'ref': 'The object to snapshot (find, defaults to the player) or the base for ref + offset in '
                            'watch. Trigger console lines also run on it.',
                     'offset': "For watch: byte offset added to the ref's address to get the watched address. "
                               'Default 0.',
                     'address': 'For watch: the memory address to watch (instead of ref + offset), for example '
                                '0x01234567.',
                     'length': 'For watch: size in bytes of the watched region, 1, 2 or 4. Default 4.',
                     'access': 'For watch: write catches writes only, readwrite catches reads and writes. Default '
                               'write.',
                     'scope': 'For watch: main watches the main thread only, all watches every thread. Default main.',
                     'trigger': 'Console lines run on the ref while watching (watch) or between the two snapshots '
                                '(find).',
                     'trigger_ref': 'For watch: reference the trigger console lines run on, when different from ref.',
                     'seconds': 'For watch: seconds to keep watching after the trigger lines have run (or with no '
                                'trigger). Default 0, no extra wait.',
                     'size': 'For find: number of bytes of the object to snapshot. Default 4096.'},
 'fnv_flow': {'action': "trace arms a breakpoint on the function's entry and records every statement it executes; "
                        'call runs the function itself with the given args and traces it, returning its eax.',
              'function': 'The engine function to trace or call, by name or address.',
              'when': "For trace: optional predicate over the function's arguments and locals; only runs that "
                      'satisfy it are traced.',
              'depth': '0 traces only the function itself; 1 also traces direct callees that have source lines.',
              'max_steps': 'Cap on the number of statements recorded; leave null for the default limit.',
              'args': 'For action=call: the arguments, as numbers, floats, or form / reference names for pointers.',
              'this': 'For action=call: the object for a thiscall, as a reference name or spec.'},
 'fnv_form': {'action': "create makes an empty or cloned form; reload applies a rebuilt plugin's changed fields to "
                        "live forms; changes lists what this session made; bytes decodes a live form's record bytes; "
                        'delete removes a form made this session.',
              'type': 'For create: the four-letter record type of the new empty form, such as STAT, MISC or ARMO.',
              'name': 'For create: the name to give the new form.',
              'plugin': 'For reload: the plugin name or the path of the rebuilt plugin file to read changed records '
                        'from.',
              'records': 'For reload: optional list of records to restrict the reload to.',
              'since': 'For changes: only report changes after this point in time (a number); leave null for '
                       'everything this session.'},
 'fnv_hook_set': {'action': 'skip bypasses the function and returns value; result runs it and replaces its result; '
                            'arg replaces one parameter; script runs a console line on each call; read shows hit '
                            'info; remove deletes the hook; list shows hooks. Default skip.',
                  'value': 'The value to return (skip), the replacement result (result) or the replacement parameter '
                           '(arg).',
                  'value_type': 'How to interpret value: int, float, bool, ptr, ref or form.',
                  'arg': 'For action=arg: index of the parameter to replace, starting at 0.',
                  'filter': 'Only apply the hook to matching calls: {on: this, caller or param0..7, ref or value}.',
                  'limit': 'Apply the hook to only the first N calls; 0 means no limit. Default 0.',
                  'script': 'A console or script line run on the main thread each time the function is called. Used '
                            'by action=script, and with other actions to also run a line.',
                  'pop_bytes': 'For action=skip: number of stack bytes to pop on return, overriding the amount '
                               'derived from the calling convention.',
                  'allow_unproven': 'When true, also allows functions that are not proven. Default false.'},
 'fnv_input': {'action': 'tap presses a control, key or mouse input briefly; hold keeps it down for duration_ms; '
                         'aim_at turns the player toward target; activate makes the player activate target.',
               'control': 'Named game control to press, such as forward, backward, left, right, attack, activate, '
                          'aim, jump, sneak, run, ready, vats or pipboy.',
               'key': 'DirectInput key code to press instead of a named control.',
               'duration_ms': 'For hold: how long to hold the input, in milliseconds; the companion releases it '
                              'after this time even if the bridge goes away. Default 0.',
               'frames': 'Hold the input for exactly this many game frames and return when they have run; for '
                         'tap/hold, and required with button or mouse.',
               'key_name': 'Key name such as E, Space, Escape, F1, 1 or Up, converted to a key code. Alternative to '
                           'key.',
               'button': 'Mouse button to press: 0 left, 1 right (up to 7). Needs frames.',
               'mouse': 'Mouse movement [dx, dy] applied each frame. Needs frames.'},
 'fnv_look': {'types': 'Only return references of these kinds: actors, doors, containers, activators, items, '
                       'furniture, statics, lights. Null returns all kinds.',
              'limit': 'Maximum number of references returned, largest on screen first. Default 20.',
              'radius': 'Only consider references within this distance of the player, in game units. Default 6000.',
              'fields': 'Keep only these keys for each reference in the result; null returns all fields.',
              'rays': 'Line-of-sight ray budget; references past the budget report line of sight as unknown. Default '
                      '40.',
              'include_hidden': 'When true, also include references that are in range but not currently visible. '
                                'Default false.'},
 'fnv_move': {'cell': 'Name (editor ID) of the cell to move to. The player moves by coc; other refs also need a '
                      'position in that cell.',
              'timeout_seconds': 'How long to wait for the destination cell to load when the player changes cells, '
                                 'in seconds. Default 180.'},
 'fnv_name_oracle': {'action': "run compares a function's results with known script commands and form fields; purity "
                               'only checks it is free of side effects; calibrate runs the method blind on proven '
                               'getters; sweep runs it on every unproven pure getter. Default run.',
                     'function': 'The engine function to test. Needed for run and purity.',
                     'mode': 'For run: call invokes the function on nearby objects, sampled uses recorded probe '
                             'samples, auto picks (calls it when proven side-effect free). Default auto.',
                     'harvest': 'For run and sweep: optional number controlling how many samples are harvested; null '
                                'uses the default.',
                     'radius': 'Distance around the player, in game units, within which objects are gathered. '
                               'Default 8000.',
                     'limit': 'For calibrate and sweep: maximum number of functions to process. Default 400.'},
 'fnv_native': {'action': 'What to do: headers (show module API and engine types), build (compile C to a DLL), load, '
                          'unload, reload, call (run an exported function), log (read module logs), list (modules '
                          'and built DLLs).',
                'name': 'Module name. Used by build, load, call, unload and reload; build writes <name>-<hash>.dll '
                        'and load picks the newest build of that name.',
                'source': 'For action=build or reload: C source text, or the path of a .c file. A one-line argument '
                          'is treated as a path.',
                'functions': 'For action=headers: engine function names to emit as FNV_ADDR_<name> addresses and '
                             'typed FNV_<name> pointers.',
                'classes': 'For action=headers: class names whose padded struct layouts are emitted from the symbol '
                           'database.',
                'export': 'For action=call: name of the exported function in the loaded module to run on the main '
                          'thread.',
                'args': 'For action=call: numeric arguments (integers or floats) passed to the exported function.',
                'since': 'For action=log: return only log lines after this sequence number. Use the next value from '
                         'the previous call; default 0 is everything.'},
 'fnv_overlay': {'action': 'text draws words on screen, marker places a visible object in the world, clear removes '
                           'the texts and markers this tool made.',
                 'text': "For action=text: the words to draw on screen with the engine's debug text layer.",
                 'x': 'For action=text: horizontal screen position of the text. The same place again replaces the '
                      'text there.',
                 'y': 'For action=text: vertical screen position of the text. The same place again replaces the text '
                      'there.',
                 'position': 'For action=marker: [x, y, z] in game units where the marker is placed. Use this or '
                             'ref.',
                 'scale': 'For action=marker: size multiplier of the marker object (1.0 = normal).'},
 'fnv_patch': {'bytes': 'Raw hex bytes to write at the address, for example 90 90. One of bytes, nop, ret, jmp or '
                        'call selects the patch.',
               'nop': 'Write this many NOP (0x90) bytes at the address.',
               'ret': 'Write a ret instruction; the value is the number of stack bytes to pop (0 for a plain ret).',
               'jmp': 'Write a jump to this target, given as a 0x address or an engine function name.',
               'call': 'Write a call to this target, given as a 0x address or an engine function name.',
               'expect': 'Hex bytes that must currently be at the address; the patch is refused if they differ.',
               'force': 'If true, patch even where another module already changed the bytes (compared with the clean '
                        'exe). Does not override companion hooks.'},
 'fnv_perk_entries': {'action': 'start hooks the perk entry-point dispatcher, read lists recorded checks, stop '
                                'removes the hooks, evaluate runs one value through the dispatcher now.',
                      'entry_point': 'Perk entry point number (0 to 73), for example 0 weapon damage, 8 to-hit '
                                     'chance, 9 XP, 17 buy price. For start it limits hooking to that one; evaluate '
                                     'needs it.',
                      'limit': 'For action=read: maximum number of recorded checks returned.',
                      'value': 'For action=evaluate: the starting value passed through the entry point.',
                      'filters': 'For action=evaluate: filter forms for the entry point, for example [weapon, '
                                 'target] for weapon damage.'},
 'fnv_photo': {'action': 'ref (tiled views of a reference), cell (above and four corners), view (current view), '
                         'sheet (ref views or the view), sequence (film strip of frames), compare (view against a '
                         'baseline image).',
               'angles': 'For ref and sheet: view angles; names front, three_quarter, side, back, top, or [yaw, '
                         'pitch] pairs in degrees. Default is a set of named angles.',
               'size': 'Image size in pixels for each picture.',
               'hide_hud': 'If true (default), hide the HUD while taking the picture.',
               'label': 'If true, draw the fnv_look screen rectangles of references on the picture (view action).',
               'frames': 'For action=sequence: number of frames to capture (default 10).',
               'every': 'For action=sequence: capture one image every this many frames.',
               'baseline': 'For action=compare: path of the baseline image to compare the current view against.',
               'distance': 'For ref views: camera distance from the reference in game units. Default is chosen from '
                           'its size.',
               'name': 'Optional file name prefix for the saved PNG files.'},
 'fnv_physics': {'action': 'controller (actor state and fall), body (collision chain and velocity), set (give a body '
                           'a velocity), triggers (record trigger events), projectiles (in flight), query (is a spot '
                           'free), layers (collision layer names).',
                 'velocity': 'For action=set: linear velocity [x, y, z] in game units per second.',
                 'seconds': 'For action=triggers: how long to record trigger events, in seconds.',
                 'position': 'For action=query: [x, y, z] in game units of the spot to test.',
                 'radius': 'For action=query: radius in game units around the position to test for free space.'},
 'fnv_place': {'action': 'at places a base form, array places several, nudge/rotate/scale change a placed ref, snap '
                         'drops it on the surface, check tests floating or sinking, list shows placements, remove '
                         'deletes one or all.',
               'scale': "Scale factor. For action=at it sets the new object's scale; for action=scale it is the new "
                        'scale of ref.',
               'shape': 'For action=array: row, grid or circle layout.',
               'count': 'For action=array: how many objects to place.',
               'spacing': 'For action=array: distance between neighbouring objects in game units.',
               'columns': 'For action=array shape=grid: number of columns per row.',
               'radius': 'For action=array shape=circle: circle radius in game units.',
               'snap': 'If true, drop the placed object onto the surface below it.',
               'local': "For action=nudge: if true, the offset is in the object's own axes instead of world axes.",
               'align': 'For action=snap: if true, also tilt the object to the slope of the surface at its heading.',
               'export': 'For action=at: false makes a test placement (PlaceAtMe near a ref) that fnv_author export '
                         'leaves out. Default true.',
               'distance': 'For action=at with export=false: how far from the near ref to place, in game units.',
               'direction': 'For action=at with export=false: 0 front, 1 back, 2 left, 3 right of the near ref.'},
 'fnv_play': {'action': 'goto walks to a ref or position, look_at turns the view, activate presses activate on a '
                        'ref, talk, loot, equip, use, drop work on a ref, stop releases held inputs.',
              'direct': 'For action=activate: if true, activate without requiring the ref to be under the crosshair.',
              'max_seconds': 'For action=goto: give up walking after this many seconds.',
              'run': 'For action=goto: if true, run instead of walk.'},
 'fnv_probe': {'action': 'plan, install, status, read, remove, clear, config, harvest or harvests; see the tool '
                         'description for what each does.',
               'statuses': 'For plan and install: select functions with these naming statuses. Default is every '
                           'status that is not proven.',
               'functions': 'For plan and install: function names or addresses to select. For read: the functions '
                            'whose samples and callers to return.',
               'start': 'For plan and install: start address (hex) of an address range to select, used with end.',
               'end': 'For plan and install: end address (hex) of the address range to select, used with start.',
               'cls': 'For plan and install: select the functions of this class.',
               'callees_of': 'For plan and install: select the callees of these functions.',
               'limit': 'For plan and install: maximum number of functions selected.',
               'part': 'For plan and install: a slice k/n of the selection, to probe it in several parts.',
               'samples': 'Number of first calls recorded per function with this, arguments and vtables (install and '
                          'config).',
               'exit_samples': 'How many of those samples also record the result on return (install and config).',
               'every': 'One in this many later calls also records its caller (install and config).',
               'dry_run': 'Only valid for action=install: do the planning and checks without patching the game.',
               'top': 'For action=status: how many of the most-called functions to list.',
               'int3': 'For plan and install: also probe functions with no entry jump and no call site, using a '
                       'one-byte trap.',
               'int3_budget': 'For int3 probes: number of calls after which a probe removes itself.',
               'notes': 'For action=harvest: free-text note stored with the harvest.'},
 'fnv_profile': {'action': 'sample records where frame time goes; diff compares two saved profiles.',
                 'seconds': 'For action=sample: how long to sample, in seconds.',
                 'rate': 'For action=sample: samples per second.',
                 'threads': 'For action=sample: sample only the main thread or all threads.',
                 'name': 'For action=sample: name prefix for the saved profile files (default profile). Must be a '
                         'plain file name.',
                 'before': 'For action=diff: the first profile, as a saved file path, a file name, or a name prefix '
                           '(newest match).',
                 'after': 'For action=diff: the second profile to compare, same forms as before.',
                 'top': 'How many functions to list in the report.'},
 'fnv_raycast': {'layer': 'Which collision test to use: line_of_sight (default), path_pick or projectile.',
                 'ground_at': 'Object with x and y in game units; instead of casting a ray, return the terrain '
                              'height at that point.'},
 'fnv_save_inspect': {'types': 'Record types to keep in the changed-form table, as four-letter codes such as QUST, '
                               'ACHR, REFR. Null shows all types.',
                      'forms': 'Form ids to keep (hex, optional 0x prefix, up to 8 digits). Null shows every changed '
                               'form.',
                      'limit': 'Maximum number of changed forms returned per page.',
                      'offset': 'Number of changed forms to skip, for paging through the table.',
                      'screenshot': "If true, also write the save's thumbnail image as a PNG file. Default false."},
 'fnv_script_debug': {'action': 'What to do: break (set, list or clear breakpoints), status, vars (list script '
                                'variables), set (change a variable), eval (run a snippet), step, continue, freeze '
                                'or thaw.',
                      'op': 'For action=break: set adds a breakpoint, list shows them, clear removes them. If '
                            'omitted: set when a script, line, opcode or when is given, else list.',
                      'script': 'For action=break: the quest or script (name or form id) to stop in. Required when a '
                                'when condition names script variables.',
                      'line': 'For action=break: source line number in the script; the game stops before that '
                              'statement runs.',
                      'opcode': 'For action=break: stop on statements with this command opcode (0 to 65535) instead '
                                'of at a line.',
                      'when': 'For action=break: a condition that must hold for the stop, in predicate text or over '
                              "the script's variables. It stops only when true.",
                      'break_id': 'For action=break with op=clear: the number of the one breakpoint to remove. If '
                                  'omitted, all breakpoints are cleared.',
                      'name': 'For action=set: the name of the script variable to change, as action=vars lists it.',
                      'value': 'For action=set: the number to write into the variable. Must be numeric.',
                      'expression': 'For action=eval: a script snippet to run on the stopped reference, such as '
                                    'GetStage QuestName.',
                      'mode': 'For action=step: line runs to the next line, over steps over called functions, out '
                              'runs to the first statement outside the current run. Default line.'},
 'fnv_script_log': {'action': 'Which log: commands (statements of every script), history (world-changing commands '
                              'run on a ref), events (block flags raised), raise (raise a block flag on a ref), '
                              'errors (script errors since the last call).',
                    'mode': 'For action=commands or events: start begins recording, read returns what was recorded '
                            '(default), stop ends it.',
                    'script': 'For action=commands with mode read: keep only statements run by this script.',
                    'ref': 'For history: the reference whose commands to list. For commands read: keep only that '
                           'reference. For raise: the reference to raise the flag on.',
                    'opcode': 'For action=commands with mode read: keep only statements with this command opcode.',
                    'flag': 'For action=raise: the block flag to raise on the reference, such as OnAdd or '
                            'OnTriggerEnter.',
                    'since': 'Record sequence number to read after (the next value an earlier call returned). For '
                             'errors: last (default) or start.',
                    'limit': 'Maximum number of records returned. Default 200 when omitted.'},
 'fnv_session': {'action': 'start readies the game and opens a session, mark records a named point, end reverts the '
                           "session's changes and reports, status shows the session cheaply.",
                 'label': 'For action=mark: the name of the point that fnv_health and the warning log report from.',
                 'deterministic': 'For action=start: if true, run deterministically (fixed frame step, seeded random '
                                  'numbers). Default false.',
                 'quit_game': 'For action=end: if true, quit the game after the report. Default false leaves it '
                              'running.',
                 'timeout_seconds': 'For action=start: seconds to wait for the game to launch, load and start '
                                    'simulating. Default 240.'},
 'fnv_symbols': {'action': "status checks the symbol database, lines gives a function's line table, locals its "
                           "parameters and locals, where maps file and line to code addresses, type gives a type's "
                           'members.',
                 'line': 'For action=where: the source line number in the given file to map to code addresses.',
                 'limit': 'Maximum number of rows returned.',
                 'offset': 'Number of rows to skip, for paging.'},
 'fnv_systems': {'action': 'Which game system to query: formulas (catalog), formula, sweep, progression, needs, '
                           'perk, barter, zone or crime.',
                 'name': 'For formula and sweep: the formula or engine function name. For perk: the PERK. For zone: '
                         'the ECZN. For barter: an optional vendor.',
                 'args': 'For formula and sweep: the argument values to pass. For barter: the list of items to '
                         'price.',
                 'index': 'For action=sweep: the zero-based position in args of the argument to vary over the range.',
                 'start': 'For action=sweep: the first value of the swept argument.',
                 'stop': 'For action=sweep: the last value of the swept argument.',
                 'steps': 'For action=sweep: number of values between start and stop. Default 10.'},
 'fnv_time': {'set_hour': 'Jump the game clock to this hour of day (0 up to but not including 24, fractions '
                          'allowed).',
              'advance_hours': 'Advance the game clock by this many hours. Days, months and years roll over like the '
                               "game's calendar.",
              'timescale': "Set the game's time multiplier (game time per real time). 0 is allowed.",
              'mode': "globals (default) moves the clock directly; engine_wait runs the game's own one-hour wait "
                      'step per hour and needs allow_unproven.',
              'evaluate_packages': 'For mode=globals: if true (default), nearby actors re-evaluate their AI packages '
                                   'after the time change.',
              'allow_unproven': 'If true, allow an engine function that is not yet proven. Needed for '
                                'mode=engine_wait. Default false.',
              'action': 'calendar (default) reads or sets the calendar; pause, resume, force_resume, hold, release, '
                        'step, fixed and speed drive the game clock and the frame gate; status reads them.',
              'frames': 'For action=step: how many frames to advance, 1 to 10000.',
              'fps': 'For action=step: the fixed frame rate to step at. For action=fixed: the fixed frame rate, 0 '
                     'turns the fixed step off.',
              'x': 'For action=speed: the time multiplier, 0.05 to 10.',
              'timeout_seconds': 'For action=hold: seconds without calls after which the gate opens by itself. '
                                 'Default 120.'},
 'fnv_trace': {'action': 'start installs an observe-only hook (default), read returns calls since the last read, '
                         'stop removes the hook, list shows running traces.',
               'filter': 'Keep only calls where this, caller or paramN matches, such as {on: this, ref: MyNPC} or '
                         '{on: param1, value: 5}.',
               'limit': 'Maximum number of calls returned by a read.',
               'include_calls': 'If true (default), a read lists the individual calls along with the counts; if '
                                'false, counts only.',
               'allow_unproven': 'If true, allow tracing any function of the engine map, not only proven ones. A '
                                 'wrong address can still crash the game. Default false.',
               'when': 'For action=start: conditions that must all hold for a call to be recorded, each like {on: '
                       'this|caller|param0..7, ref|value}. Null records every call.',
               'stack': 'For action=start: if true, record the call stack of each hit, so a read groups the hits in '
                        'commonStacks. Default false.'},
 'fnv_ui_do': {'action': 'click a tile, press a button by label, wait hours, sleep, fast_travel to a marker, message '
                         '(press a message box button), close or back the top menu, or type text into the text entry '
                         'menu.',
               'target': 'For action=click: the tile path or name to click (fnv_menu tiles lists them).',
               'label': 'For action=button: the text on the button, such as Take All or Done. For action=type: the '
                        'text to enter.',
               'menu': 'For click, button and close: the menu to act on, by name or number, instead of the one at '
                       'the top.',
               'hours': 'For action=wait or sleep: number of hours to pass, 1 to 24. Default 1 when omitted.',
               'button': 'For action=message: the message box button to press, as an index or its label.'},
 'fnv_wait_for': {'frames': 'Wait until this many frames have passed. Give exactly one condition.',
                  'seconds': 'Wait this many seconds, measured by the clock choice. Give exactly one condition.',
                  'clock': 'For seconds: game (default) counts game seconds scaled by the time multiplier, real '
                           'counts wall-clock seconds.',
                  'predicate': 'Wait until this condition text is true, such as frame >= 500 or [0x011DEA0C]:u8 == '
                               '1.',
                  'loaded': 'If true, wait until the game has finished loading and is simulating. Default false.',
                  'timeout': 'Real seconds to wait before giving up. Default 60.',
                  'max_frames': 'Upper limit on frames run during a predicate wait. Default 3600.'},
 'fnv_warnings': {'action': "status checks the setting and files, enable turns the warning log on in xNVSE's config, "
                            'read shows lines, summary counts by category and plugin, clear sets the mark, capture '
                            "hooks the engine's Warning function.",
                  'mode': 'For action=capture: start, stop or status of the live capture. With no mode, capture '
                          'starts.',
                  'since': 'For read: mark (default) reads from the last mark, start reads the whole files, a number '
                           'reads from that line.',
                  'category': 'For read: keep only this warning category, such as MASTERFILE, SCRIPTS, MODELS, '
                              'TEXTURES, ANIMATION, AI, Audio or other.',
                  'text': 'For read: keep only warnings containing this text.',
                  'plugin': 'For read: keep only warnings about this plugin.',
                  'source': 'For read: keep only the engine log or the havok log.',
                  'limit': 'Maximum number of warning lines returned per page. Default 100.',
                  'offset': 'Number of lines to skip, for paging.',
                  'brief': 'If true, return a shorter result.',
                  'top': 'For action=summary: how many of the most repeated messages to list. Default 10.'},
 'fnv_watch': {'items': 'What to sample: expressions such as player.GetAV Health, or mappings like {eval, ref}, '
                        '{actor_value, ref}, {position: ref}, {mem, path}. With frames, memory-read predicate text, '
                        'at most 16.',
               'seconds': 'How long to sample, in seconds. Default 5. Not used when frames is given.',
               'interval_ms': 'Milliseconds between samples. Default 100. Not used when frames is given.',
               'until': 'An expression that ends sampling early as soon as it is true.',
               'keep_unpaused': 'If true (default), the game is kept running while sampling.',
               'frames': 'If set, sample frame by frame inside the game for this many frames instead of by time.',
               'every': 'With frames: take a sample every this many frames. Default 1.'},
 'fnv_world_edit': {'action': 'Edit to make: move, rotate, scale, transform (any of these), enable, disable, delete, '
                              'duplicate, weather (force or release), imagespace (start a modifier), '
                              'imagespace_stop.',
                    'position': 'For move/transform: [x, y, z] in game units. A null entry keeps that axis '
                                'unchanged.',
                    'rotation': 'For rotate/transform: [x, y, z] angles in degrees. A null entry keeps that axis '
                                'unchanged.',
                    'scale': 'For scale/transform: new scale factor of the reference (1 = normal).',
                    'fade': 'For action=disable: if true the reference fades out instead of vanishing at once.',
                    'weather': 'For action=weather: the weather form to force, or "release" to drop the override.',
                    'override': 'For action=weather: true (default) forces it as an override; false clears the '
                                'override.',
                    'imagespace': 'For imagespace and imagespace_stop: the image-space modifier form to start or '
                                  'stop.',
                    'strength': 'For action=imagespace: strength of the modifier. Default 1.',
                    'distance': 'For action=duplicate: distance in game units for placing the copy. Default 0.',
                    'count': 'For action=duplicate: how many copies to place, 1 to 20.'},
 'geck_plugin_load': {'expected_editor_title': 'The GECK window title you expect, used as a guard so the load only '
                                               'runs against the intended editor.',
                      'expected_plugin_hash': 'SHA-256 hex hash of the plugin file you expect to load, as a guard '
                                              'against changed content.',
                      'request_key': 'Caller-chosen key for this operation. Reuse it only for an identical retry.',
                      'reload': 'If true, force reloading a plugin that is already active and clean. Default false.'},
 'geck_plugin_save': {'expected_models': 'Map of STAT EditorID to the expected model path, checked independently '
                                         'after saving.',
                      'expected_plugin_hash': 'SHA-256 hex hash of the plugin file you expect before saving, as a '
                                              'guard.',
                      'request_key': 'Caller-chosen key for this operation. Reuse it only for an identical retry.',
                      'save_entire_active_plugin': 'Must be true. Confirms you accept saving all pending edits of '
                                                   'the whole active plugin, including manual ones.'},
 'geck_record_set_model': {'editor_id': 'EditorID of the existing STAT record whose model to change.',
                           'model_path': 'New model path, relative to Data\\meshes.',
                           'expected_model': 'The model path the record currently has, checked before the change as '
                                             'a guard.',
                           'expected_plugin_hash': 'SHA-256 hex hash of the plugin file you expect before the '
                                                   'change, as a guard.',
                           'request_key': 'Caller-chosen key for this operation. Reuse it only for an identical '
                                          'retry.'}}
