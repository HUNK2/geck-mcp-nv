# The game is slow
Summary: Find where the frame time goes, down to the function and source line, fix it and measure before and after.
Words: slow, lag, stutter, fps, frame rate, frame time, performance, hitch, profile, cpu

Use this when the frame rate drops, a cell stutters or a script makes the game slow. Status: checked in the game on
2026-11-11 (a 25 ms module in front of TES::RunScripts was found, removed, and measured 34 -> 57 fps).

## Steps

1. **Measure first.** Call `fnv_profile` with action `sample` and a `name` (for example `before`), 3 to 5 seconds,
   standing where it is slow. Read `fps`, then `gameFunctions` and `gameLines`: the innermost game function and
   decomp line of each sample. A wait inside the driver counts for the game function that called it, so an idle
   game at the vsync limit shows `PresentFrame` on top.
2. **Who is it?** `selfFunctions` shows where the thread really was, with `calledFrom`. A plugin DLL appears as
   `name.dll!offset`. `inclusive` shows every function on the stack. Use `threads=all` when the main thread is
   waiting on another thread.
3. **Scripts?** `fnv_script_profile` gives time per script; `fnv_profile action=frame` per frame phase.
4. **Memory churn?** `fnv_memory` action `alloc_trace` counts allocations per call site; `pools` gives committed
   memory and the live NiRefObject count.
5. **Fix it**: a plugin setting, a script, a module (`fnv_native unload`), or an engine function (`fnv_code`).
6. **Measure after.** `fnv_profile sample` with `name` `after`, then `fnv_profile diff` `before` `after`: the frame
   rate and the change per game function. The fix is proven when the hot function's share fell and the frame rate
   rose.

## What the answers mean

- `gameFunctions` high on `PresentFrame`: the game waits for the screen (vsync or the GPU); the CPU has room.
- A plugin DLL (`name.dll!offset`) on top of `selfFunctions`: that plugin's code runs in the frame.
- `fps` from the profile is frames counted by the engine companion over the sampling time.

## Known traps

- At the vsync limit a slowdown that still fits inside the frame does not change the frame rate: read the shares.
- A hook's handler runs before the hooked function's prologue, so its time shows under the caller's line.

## Checks that prove the fix

- `fnv_profile diff before after`: the hot function's share fell and `fps` rose.
- `fnv_health` reports nothing new since the session mark.
