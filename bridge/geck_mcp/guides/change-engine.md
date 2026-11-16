# Change the engine
Summary: Run your own C inside the game, or change an engine function from its decompiled source, and test it live.
Words: engine, native, module, dll, c code, hook, detour, patch, swap, decomp, change a function, plugin dll, nvse plugin

Use this when a script cannot do it and the change belongs in engine code. Status: checked in the game on 2026-11-11
(an activation hook loaded, reloaded and unloaded; CalculateSkillValue swapped 28 -> 38 and restored).

## Steps

1. **Hook, do not rewrite, when you can.** `fnv_native` action `headers` with `functions` (and `classes`) writes
   `fnvmcp_module.h` and the engine header (addresses, typed pointers, structs). Write a module that calls
   `api->hook(FNV_ADDR_<name>, handler, user, pop)`; the handler sees registers and stack arguments and may change
   them or return `FNV_SKIP`.
2. **Build and load.** `fnv_native build` (compiler errors come back as they are), `load` (init runs on the main
   thread, hooks are installed), then play and read `fnv_native log`. `reload` with new source swaps it in;
   `unload` removes the hooks and puts back what the module wrote.
3. **Change a function inside.** `fnv_code checkout` the function, `edit` (exact text inside it), `build` (the game's
   own compiler; names that cannot be bound are listed), `compare` with arguments (old and new side by side),
   `swap`, test, `unswap`. Functions with exception handling or new writable statics are refused.
4. **Prove it.** Call the function (`fnv_engine_call` or `fnv_systems formula`), run the scenario (`fnv_test steps`), and
   check `fnv_health`. For speed changes, `fnv_profile` before and after.
5. **Ship it.** A module becomes a normal xNVSE plugin with `fnv_plugin_build` (`build` with `hooks` listing the
   functions it hooks, `install`, then `fnv_game restart`, `log`). A `fnv_code` swap is recompiled game code: local use
   only; ship the change as a module hook instead.

## What the answers mean

- `hooksFailed` after a load: the bridge could not plan that function's prologue or it is hooked already.
- `unresolved` after a code build: names the recipe does not bind to one address; the change cannot be placed.
- `compare` gives the original and the new result for the same inputs before anything is swapped.

## Known traps

- Generated structs come from the decomp's best definition of a class; some are placeholders (TESForm). Check offsets
  against `fnv_class` or `fnv_mem_read` before trusting one.
- Callers that inlined a swapped function keep the old body (rare: most game logic is /Od).

## Checks that prove the fix

- The changed behaviour shows in the game (a call, a scenario step, a log line) and `fnv_health` is clean.
- After `unload` or `unswap`, `fnv_mem_read` of the function's entry shows its original bytes again.
