# The game crashed
Summary: Find out why Fallout: New Vegas crashed, reproduce it, fix it and prove the fix.
Words: crash, crashed, ctd, crash to desktop, fault, exception, access violation, freeze, hang, stuck, exits, closes by itself, CrashLogger

Use this when the game closed by itself, showed a crash message, or stopped answering. Status: written from the
tool descriptions in this repository. The tests check that every tool name below exists; they do not play the game.

## Steps

1. **Is it new?** Call `fnv_crash history`. It groups crash logs by signature (exception, crash-site function, next
   frames). `new: true` means no earlier call has seen this signature. Every normal quit under MO2 can leave a
   `CrashLogger.log` too: the call flags crashes during shutdown, so ignore those unless the game really crashed.
2. **Read the crash.** Call `fnv_crash`. It gives the stack in engine function names and DLL offsets, the
   forms involved and the likely culprit. `fnv_crash latest` gives the same log without the interpretation.
3. **What was running?** Call `fnv_crash context` (action `latest`). The engine companion writes a file at the
   fault: the engine calls active on the crashing thread, the console line or request being run and recent events.
   Without it the crash log cannot name the running script. To get it next time, call `fnv_crash context` with
   action `enable` before you reproduce the crash.
4. **What did the engine say just before?** Call `fnv_warnings` with action `status`. When the setting is off, call
   `enable` and restart the game (`fnv_game restart`), then reproduce. Then call `read` with category `MASTERFILE` or
   `SCRIPTS`, or `summary`. A missing master, a wrong record type or a missing asset often comes before the crash.
   Also read `fnv_logs` with sources `nvse` and `crash`.
5. **Check the load order.** Call `fnv_loadorder`: missing masters, wrong order, plugins that are on disk but off
   in the profile. Call `fnv_conflicts` for records that several plugins change, and `fnv_asset_check` for meshes,
   textures and sounds that a plugin names but no archive has.
6. **Does a plugin patch the crashing code?** When the crash is inside engine code, call `fnv_patch_map`. It lists
   code and pointers other plugins changed, by module. A crash site inside a patched range points at that module.
7. **Understand the crash site.** `fnv_symbolize` turns an address into a function and offset. `fnv_func_code` shows
   its decomp source, `fnv_func_find mode=callers` shows who calls it, `fnv_class_layout` shows the fields of the object it
   used. For a null pointer, find which caller passed it.
8. **Reproduce it on demand.** Write a short YAML scenario that does what you did and run it with `fnv_test`.
   With `relaunch_on_crash` (the default) the run reports the crash doctor's diagnosis and starts the game again.
   Use a named test save (`fnv_test_save`) so the start state is the same each time.
9. **A hang is not a crash.** When the game does not answer and writes no crash log, call `fnv_hang_doctor`. It
   samples the call stacks from outside the process and names the functions it is stuck in. When the main thread
   waits (in a load, on a lock), it also samples the threads that use the most CPU, and any thread the crash hold
   parked at a fault, with the file paths found on that thread's stack.
10. **A file on that stack?** When a mesh or texture path shows up (a held loader fault names the file), check it
   offline with `fnv_asset` action `nif` (header, child links, collision, textures) or `dds`, and find the record
   that uses it with `fnv_asset` action `users`: a bad child link is what makes `NiAVObject::AttachParent` fault.

## What the answers mean

- A crash site in a game function and a form id of your plugin in the context: suspect your data (a wrong type, a
  missing script, a bad model path).
- A crash site in a DLL (JIP, a plugin, the companion): suspect that plugin or a version mismatch. Check
  `fnv_loadorder` and `fnv_setup_check`.
- The same signature each time, in the same place: it is deterministic. Remove plugins or changes one at a time.
- A different signature each time, in memory code: suspect memory use. Call `fnv_heap` while you play to watch the
  2 GB address space and the pools.

## Known traps

- The log files of the game are rewritten at each start. Read them before you launch again.
  `fnv_crash history` keeps what it has seen.
- `falloutnv_error.log` is written through a buffer, so the last lines before a crash may be missing.
- A plugin can be installed only while the game is closed (`fnv_install`).
- `fnv_crash` names a likely culprit. It is evidence, not proof. Confirm with a reproduction.

## Checks that prove the fix

1. Run the same scenario again with `fnv_test`. It must finish with no crash.
2. Call `fnv_crash history`. The signature must not have a new entry after the fix.
3. Call `fnv_status`: phase `in_game` after the scenario, not `not_running`.
4. Call `fnv_warnings` with action `clear` before the run and `read` after it. No new warning may name your plugin.
5. Run the tests of the mod (`fnv_test run_all`) to see that nothing else broke.

## Stop at the crash instead of dying

`fnv_debug` with action `break` and location `fault` turns on the crash hold. When the game faults, the faulting
thread waits: `fnv_debug status` shows the source line, `stack` and `locals` read the live process, and `continue`
lets the crash logger write its log as usual. `return` or `skip` try to recover, to test a guess. `dump` writes a
minidump to look at later.

A fault on a loader thread during a load (a corrupt mesh, seen 2026-11-11) leaves the main thread waiting for it, and
the companion answers only on the main thread. `fnv_debug status` then reads the held fault from outside: the
registers of the fault from its context file, the stack above them with function names and source lines, and the
file paths on that stack (`meshes\...nif`). `fnv_asset where` on that path names the mod folder, `fnv_record_find`
with the plugin lists its records. `fnv_game quit` with force ends the game afterwards.

## Find the culprit plugin or object

`fnv_object identify` on a pointer from `locals` or the crash log says which pool block it is in, its class and its
form: a freed or reused block shows a class that does not fit. When a load-order change brought the crash,
`fnv_bisect` with level `mods` or `plugins` and the `fnv_test steps` steps that reproduce it names the one mod or plugin
(the game restarts each round).
