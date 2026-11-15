# Engine companion, wire version 2

Contract for the new ops of `native/engine/fnvmcp_engine.c` (Plan 2, waves W1 and W2) and the bridge code that
uses them. Wire version 1 stays valid: every old op keeps its format.

## 1. Rules that do not change

- Request: `<id> TAB <op> SP <arg>... [TAB <op>...]... LF`. Numbers are hex (`parse_u32`), text arguments
  are hex-encoded bytes. Response: `{"id":..,"frame":N,"results":[{...},...]} LF`, one result per op, in order.
  An op that fails emits `{"error":"<code>"}` through `fail()`.
- The companion hard-codes no engine address. The bridge resolves addresses from the engine map and sends them
  (config ops below).
- `ping` is answered by the pipe thread, so it works while the main thread is held or stuck.
- `COMPANION_VERSION` 2, `WIRE_VERSION` 2; `geck_mcp/fnv_link.py` `WIRE = 2`. New code lives in new files
  included by `fnvmcp_engine.c` (one translation unit, `#include "w1_gate.c"` and so on); each new file is
  added to `fnv_link.SOURCES` so the build id changes with it.

## 2. Predicates (W1)

A predicate is a small stack program. `pred set <id> <hex>` stores one (id 0..63, at most 64 ops, stack depth
16). Values are 32-bit; each stack slot remembers whether it is an int or a float.

| Byte | Op | Operands (little-endian) | Effect |
|---|---|---|---|
| `01` | push int | `i32` | |
| `02` | push float | `f32` | |
| `10` | load | `u8 base`, `u32 value`, `u8 n`, `n` x `i32 offset`, `u8 type` | See below |
| `20`-`25` | eq ne lt le gt ge | | Pop b, a; push 1 or 0. Float compare if either is a float, else signed int |
| `30` / `31` / `32` | and or not | | Logical, on "non-zero" |
| `40`-`44` | add sub band shr shl | | Integer (add and sub are float when either is a float) |
| `50` | frame | | Push the companion's frame counter |
| `51` | is main thread | | Push 1 on the game's main thread |
| `60` | expression | `u16 slot` | Evaluate the compiled xNVSE expression in that `compile` cache slot; main thread only, else false |

`load`: `base` 0 = absolute address (`value` is the address), 1 = register (`value` 0..7 = eax ecx edx ebx esp
ebp esi edi of the stop or hook context), 2 = stack argument `value` (0 = first argument), 3 = `this` (ecx of
the context), 4 = the stack slot of argument `value` (so offset 0 reads the argument's own value; in predicate
text `arg6:u32`). Bases 1-4 only exist where a context exists (hooks, breakpoints); elsewhere the predicate is
false. Then: `p = base value`; for each offset except the last, `p = read_u32(p + offset)`; the result is
`read<type>(p + last offset)`. `n` must be at least 1. `type`: 0 u8, 1 i8, 2 u16, 3 i16, 4 u32, 5 i32, 6 f32.
Every read is a safe read (`ReadProcessMemory` on this process): a bad pointer makes the whole predicate false
and raises its error count.

A predicate is true when the program ends with a non-zero value on top. A program that ends with several
values on the stack is also a value list: the sampler records them all.

| Op | Result |
|---|---|
| `pred set <id> <hex>` | `{"ok":true,"id":N,"ops":K}`; errors `bad_program`, `too_long`, `bad_id` |
| `pred clear <id\|ff>` | `{"ok":true,"cleared":K}` (`ff` = all) |
| `pred list` | `{"preds":[{"id","ops","evals","errors"}]}` |
| `pred eval <id>` | `{"value":true\|false,"top":number,"errors":E}`, evaluated now with no context |

Hook holds, breakpoints, `until` and the sampler take a predicate **slot id**, never the program: the bridge stores
the program with `pred set` first. Slots the bridge uses: 32-55 `fnv_debug` break conditions, 56 `fnv_script_debug`,
57 `fnv_flow`, 62 the frame read of `fnv_time_control`/`fnv_wait_for`, 63 the `fnv_wait_for` condition. The rest are
free.

## 3. Frame gate (W1)

The gate lives in the main-loop message (`MSG_LOOP`). While the gate is held the main thread does not return
to the game: it waits for requests (an event the pipe thread sets, no busy loop) and serves them.

| Op | Effect and result |
|---|---|
| `gatecfg <disable_fn> <enable_fn> <timer>` | Addresses of `BSTimer_Disable`, `BSTimer_Enable` (thiscall) and TimeGlobal. Without it the gate still works but the game clock is not paused. `{"ok":true}` |
| `hold on [timeout_ms]` | Close the gate at the end of the current request. Timeout = dead-man time, default `0x1D4C0` (120 s). `{"held":true,"timeout":ms}` |
| `hold off` | Open the gate. `{"held":false}` |
| `step <n>` | Let `n` frames run (n 1..`0x2710`), then hold. The ops after `step` in the same request are served at that stop, in the same response. `{"stepped":n,"frame":F}` |
| `until <pred> <max>` | Run frames until predicate `pred` is true at the start of a frame or `max` frames passed, then hold; the rest of the request is served there. `{"met":bool,"frames":k,"frame":F}` |
| `gate` | `{"held","clockPausedByUs","timeout","msSinceContact","stepLeft","untilPred"}` |

- When the gate closes, call `BSTimer_Disable(timer)` once and remember it; when it opens, call
  `BSTimer_Enable` only if we disabled. The engine's own users of the counter (focus loss) stay balanced.
- `step` and `until` run their frames with the clock enabled and disable it again at the stop.
- Call `DisableProcessWindowsGhosting()` once at load.
- Dead-man switch: while held, with no request and no `ping` for the timeout, open the gate, record an event
  (`events` op, type `0x3E9` "deadman") and go on. Every request and every ping counts as contact.
- Disconnect: when the client's pipe closes (the bridge exited or was killed), the companion does not wait for the
  dead-man time. Stopped threads are resumed with `run` at once; on the next frame the gate opens, held input is
  released and a `step` or `until` still running ends without a reply. An event of type `0x3EC` "disconnect" says
  what was let go. A request the client left behind finishes before the next client's first request is taken.
- If the pipe cannot be created (another game with the companion holds the name), the companion writes a line to
  `%LOCALAPPDATA%\fnvmcp\companion.log` and an event of type `0x3EB` "pipe", once and then at most once a minute,
  and keeps trying every second.
- `ping` gains `"held"`, `"stepLeft"`, `"parked"` (number of parked threads, W2) and `"lastFrameTick"`.
- Holds never nest: `hold on` while held only changes the timeout.
- On `MSG_EXIT` the gate opens.

## 4. Sampler (W1)

| Op | Effect and result |
|---|---|
| `samp set <id> <every_n> <program_hex> <stop_pred\|ff> <ring>` | Every `every_n` frames (in the main-loop message) run the program (same bytecode as predicates) and store every value left on its stack (at most 16) as one sample. Ring of `ring` samples (at most `0x10000`). When `stop_pred` becomes true the sampler stops and records the frame. id 0..15. `{"ok":true}` |
| `samp read <id> <since> [max]` | `{"samples":[{"seq","frame","tick","values":[..]}],"next":S,"stopped":bool,"stopFrame":F\|null,"dropped":D}`; values are numbers (floats as floats) |
| `samp clear <id\|ff>`, `samp list` | |

## 5. Event sinks (W1)

One ring of 65,536 records `{seq, frame, tick, source, ref, 6 words}` with an interlocked sequence.

| Op | Effect |
|---|---|
| `sink add nvse <name_hex>` | Subscribe to an xNVSE event by name through the event manager interface (`SetNativeEventHandler`; see `C:\DecompModGithub\NVSE` for the ABI). One static handler per slot (at most 64 slots) so a record knows its event. `{"ok":true,"slot":N}` |
| `sink add msg <type>` | Record xNVSE messages of that type (also the per-form ones the companion drops today) |
| `sink remove <slot\|ff>` | |
| `sink read <since> <max> [ref]` | `{"records":[{"seq","frame","tick","source","ref","words":[6]}],"next":S,"dropped":D}`; `source` is the event name or `msg:<type>`; optional filter by ref form id |

## 6. Guarded call (W1)

`call` accepts a two-letter kind: `ig`, `fg`, `vg` (as `i`, `f`, `v`, guarded). Before the call the companion
saves a recovery point (esp, ebp, ebx, esi, edi and a resume address) for this thread. A fault on this thread
while the guard is set is caught by the vectored handler, which resumes at the recovery point (edit the
`CONTEXT`, return `EXCEPTION_CONTINUE_EXECUTION`; no `longjmp` out of the handler). The op returns
`{"ok":false,"fault":{"code":"0x..","address":"0x..","access":"read|write|execute","target":"0x.."},
"stateSuspect":true}`. Guards nest per thread. Stack overflow is not caught. The fault still writes no
crash-context file.

## 7. Input injector (W1)

| Op | Effect |
|---|---|
| `inpcfg <poll_fn> <stolen> <osinput_ptr> <force_fn>` | Install the injector: a hook that runs after the engine's input poll `InputPoller::Controls_UpdateValues` (entry address, `stolen` = bytes to relocate, as `hook` takes them), the address that holds the OSInputGlobals pointer, and `OSInputGlobals::Controls_ForceUserAction` (thiscall, one int argument: the control) |
| `inp key <dik> <frames>` | Hold a key: after each poll set `globals + 0x18F8 + dik` to `0x80` |
| `inp btn <i> <frames>` | Mouse button `i` (0..7): `globals + 0x1B30 + i` = `0x80` |
| `inp mouse <dx> <dy> <frames>` | Add signed deltas to `globals + 0x1B24` and `+ 0x1B28` each frame |
| `inp ctl <control> <frames>` | Call `force_fn(globals, control)` after each poll (controls 0..12) |
| `inp clear` | Drop every job |
| `inp` | `{"jobs":[{"kind","code","framesLeft"}],"installed":bool}` |

Never write the previous-state arrays. Each job ends after its frames, on `inp clear`, on any fault in the
injector and when the dead-man switch fires. At most 32 jobs.

## 8. Stops (W2)

A stop is a thread parked by the companion: by a hook hold, a trap breakpoint, a single step or a fault. At most
16 stops at once. Each has a sequence number (`seq`, never reused while the process lives).

- **Main thread:** the parked code runs the same serve loop as the gate (`gate_park`), so every op works while it
  is stopped; it returns when its stop is resumed. While stopped the gate counts as held (clock paused the same way).
- **Other threads:** park on an event without serving. `"main":false` in the stop record tells the bridge that
  engine calls on the main thread still work but this thread is frozen.
- Holds and breakpoints are ignored while a request is being served (`serving_request`), so a served engine call
  cannot stop inside itself, and ignored on the pipe thread.
- The dead-man switch resumes every stop with `run` (and opens the gate). `MSG_EXIT` and a disconnect do the same.
- `ping` reports `"parked"`.

| Op | Effect and result |
|---|---|
| `stops` | `{"stops":[{"seq","kind","id","thread","main","eip","frame","regs","context","exception"}]}`. `kind`: `hook`, `bp`, `step`, `fault`. `regs`: for hook stops, the address of the saved register block (layout `eflags edi esi ebp esp ebx edx ecx eax slot ret args...`, as the hooks save it today). `context` / `exception`: for bp, step and fault stops, the addresses of the live `CONTEXT` and `EXCEPTION_RECORD`. Addresses are hex strings. Writes to them (with `wr`) take effect on resume |
| `resume <seq> run` | Continue |
| `resume <seq> next <hook_id>` | Hook stops: continue and stop at the next passing call of that hook (any thread unless the hook is main-only) |
| `resume <seq> skip <result>` | Hook stops: return from the hooked function at once with `eax = result`, popping its arguments as the existing `skip` action does |
| `resume <seq> step` | bp, step and fault stops: execute one instruction (trap flag), then stop again as kind `step` |
| `resume <seq> to <addr>` | Continue and stop at `addr` once (a temporary breakpoint), on this thread |
| `resume <seq> ret <eax> <pop>` | Return from the current function now: `eax` = value, `esp` += 4 + `pop`, `eip` = the return address read at `esp` (only valid at a function's first instruction or after the frame is torn down; the bridge decides) |
| `resume <seq> eip <addr>` | Continue at another address |
| `resume ff run` | Resume every stop |

Results: `{"ok":true,"seq":S}`; errors `no_stop`, `bad_mode` (a mode the stop kind does not support).

### 8.1 Hook hold

`hookact <hook_id> hold <pred|ff>`: a new behaviour action for the existing hooks. When a hooked call passes the
hook's filter and the predicate (with the hook's register block as context: registers, `this` = ecx, arguments on
the stack), the thread parks inside the hook (kind `hook`, `id` = hook id). On resume it continues into the
original function (or skips it). Follow how the existing `hookact` actions (`skip`, scripts) are stored and run.

### 8.2 Trap breakpoints

| Op | Effect |
|---|---|
| `bp set <id> <addr> hw\|int3 <pred\|ff> <flags> [log_hex]` | id 0..63. `hw`: a debug-register execute breakpoint (DR0-3, at most 4 including the existing `dbg` watchpoints; main thread by default). `int3`: one byte `CC`, written with other threads suspended as probes are, journaled (the old byte is restored on clear). `flags` bits: 1 = log (do not stop: run the program `log_hex`, the same bytecode as predicates, with the trap context and append its values to the breakpoint's log ring of 4,096 entries, then continue), 2 = all threads (best effort for threads that exist now), 4 = once (clear after the first hit). `{"ok":true,"id":N,"kind":"hw\|int3"}` |
| `bp after <id> <n>` | The breakpoint acts (stops or logs) only from the n-th hit that passes its predicate, and on every later one; 0 = every hit. Resets the passing count; `bp list` shows `passed` and `after`. On a free id the count is kept for the next `bp set` of that id (send it first: a breakpoint set first can stop on the very next hit). `{"ok":true,"id":N,"after":n}` |
| `bp clear <id\|ff>`, `bp list` | `bp list`: `{"bps":[{"id","addr","kind","hits","pred","flags"}]}` |
| `bp log <id> <since> [max]` | `{"entries":[{"seq","frame","thread","eip","values":[..]}],"next":S,"dropped":D}` |

- A hit that passes its predicate parks the thread in `on_trap` with the live `CONTEXT` (kind `bp`, `id` = bp id).
- On resume from a hardware hit set EFLAGS bit 16 (resume flag). From an int3 hit: `eip` back to the breakpoint,
  restore the old byte, set the trap flag, and write `CC` again on the following single-step exception (so the
  breakpoint stays armed). Tell a single step (DR6 bit 14) from a watchpoint (DR6 bits 0-3).
- `resume step` sets the trap flag; the next single-step exception becomes a stop of kind `step` (`id` = the bp id
  it came from, or 255).
- Existing int3 probes and `dbg` watchpoints keep working; breakpoints must not take their exceptions.
- `dbg read <since>` hits are `[seq, slot, eip, thread, value, ret, frame_ret, frame, mark_hook, mark_seq]`. The last two (added 2026-11-11) are the id and sequence number of the newest hook record the main thread wrote before a main-thread hit (0 on other threads): with the `ScriptRunner::EvaluateLine` hook armed, the statement that made the write (`fnv_blame field`).

### 8.3 Crash hold

`faulthold on|off`. In `on_exception`, after the crash-context file is written as today: if `faulthold` is on, the
fault is not inside a guarded call, the game is not exiting, it is not a stack overflow, and it is a real fault
(access violation, illegal instruction, divide by zero, privileged instruction), park the faulting thread as kind
`fault`. `resume run` then returns `EXCEPTION_CONTINUE_SEARCH`, so the crash logger and the game go on as without the
hold; `eip`, `ret` and edits to the `CONTEXT` return `EXCEPTION_CONTINUE_EXECUTION` (an attempt to recover).
A fault on the main thread parks with the serve loop like any main-thread stop.

### 8.4 Trace (for `fnv_flow`)

`resume <seq> trace <max> <lo> <hi> [log_hex]`: from a bp or step stop, single-step this thread. Every step whose
`eip` is inside `[lo, hi)` appends `{eip, values}` to the stop's trace ring (values from the program `log_hex`, run
with the trap context, so it can read locals at `ebp` offsets). When a `call` leaves the range, put a temporary
breakpoint on its return address and run until it returns (callees are not traced). The trace ends when `eip`
leaves the range through a return (esp above the esp at the start), or after `max` steps (at most 200,000); the
thread then parks again as kind `step`. `trace read <seq> <since> [max]` returns
`{"entries":[{"n","eip","values":[..]}],"next":S,"done":bool,"reason":"returned|max|fault"}`.

## W5: native modules and code placement

| Op | Result |
|---|---|
| `mod load <path_hex>` | LoadLibrary on the main thread, then `fnvmcp_module_init(api)`. `{"ok":true,"module":{id,name,base,size,hooks:[{request,target,pop,handler,installed,hook}]},"exports":[{name,address}]}`; errors `load_failed` (with `winError`), `no_init_export`, `init_failed` |
| `mod unload <id>` | Removes the module's hooks, waits up to 2 s for running handlers (`busy` otherwise), runs `fnvmcp_module_exit`, puts its writes back, FreeLibrary. `{"ok":true,"hooksRemoved","hooksStuck","writesRestored"}` |
| `mod list` / `mod log <since> [max]` | Modules; log lines `{seq,module,frame,text}` (ring of 1,024) |
| `hooknative <hook> <mod> <request>` | Attaches a module's hook request to a hook installed with `hook` (flags 0): the handler runs at each call; `FNV_SKIP` returns through a `ret <pop>` stub with the frame's eax |
| `code alloc <size>` / `code free <addr>` | Executable memory for swapped functions |
| `code patch <addr> <hex>` | Writes bytes with the other threads suspended; `{"ok":true,"length","old":"<hex>"}` |

## W6: line coverage

| Op | Result |
|---|---|
| `cov add <hex>` | Packed u32 addresses (hex) into a sorted table of up to 262,144; addresses already holding 0xCC, a breakpoint site or a probe are skipped. `{"ok":true,"added","skipped","total"}` |
| `cov arm` / `cov disarm` | Write 0xCC at every address not hit yet / put back every byte not hit yet (other threads suspended) |
| `cov read <since>` | `{"ok":true,"total","hits","next","addresses":"<packed hex>"}` in hit order (up to 8,000 per call) |
| `cov reset` | Empty the table (refused while armed) |

A coverage trap restores its byte on the first hit, re-runs the instruction and is handled before the breakpoint and
probe handlers; a trap already in flight after the byte was restored is still recognised.

