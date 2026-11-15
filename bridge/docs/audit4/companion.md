# Round-4 audit: companion (C) and the code that talks to it

Scope read (same as round 3): native/engine/fnvmcp_engine.c (hooks, K3 reuse, probes, serve/pipe, K2), w1_gate.c,
w1_input.c, w1_samp.c, w2_stop.c, w2_bp.c, w5_mod.c, w5_code.c, w6_cov.c; geck_mcp/fnv_link.py, fnv_debugger.py
(breaks, hook hold, stops, bp ids), fnv_flow.py, fnv_control.py, fnv_deep.py (hook plumbing, prologue decoder);
fnv_probe.py install/remove and fnv_live.py `_undo_probe_remove` (to follow the probe chain); fnv_code.py swap/unswap
(to follow `code patch`). Diffs read: `git show bea0b44 8aea7cd 20f7d7f 1025e81 a0a74d0` (K1, K1 review I1/I5, K2+K3,
K4, K3 review). Read-only: nothing was run, no scratch files.

Counts: high 0, medium 1, low 2.

Round-3 fixes checked:
- K1: every isite/coverage write path now checks hook and armed-probe bytes, and op_hook checks isites, probes and
  other hooks. Sound for live hooks and ARMED probes. Gap: see M1 (a probe that is no longer ARMED but whose jump is
  live again).
- K2: last_contact is set in serve_request and by pings only while no request was dropped. No regression found (a
  dropped request needs the main thread outside main_loop and gate_park; long served requests set pending=2 and are
  never flagged).
- K3: reuse needs stolen 0, removed_at plus 60 s, inflight 0, no native handler running, no stop of the hook or with
  eip in its stub. Every failure path after hook_reuse calls hook_retire. The ring and table are cleared. Python
  readers look hooks up again by label on each call (deep.hooks(), filtered on stolen), so a reused ring is never read
  as the old hook's. Module-unload hooks never get removed_at: already deferred (progress.md line 341).
- K4: a correct check, but the snapshot is kept on the shared instance (L2).

One deferred item is worse than its recorded severity: "K1: the input-poll patch, fnv_code writes and probe
call-site fields are not in patched_range" (progress.md line 323, minor). For fnv_code it is a crash, so medium:
1. `fnv_debug break F thread=any` puts an int3 at F (isite orig 0x55).
2. `fnv_code swap F` sends `code patch F E9..`. w5_code.c:51-63 checks only coverage, so the 0xCC goes into the
   swap's saved `old`.
3. `fnv_debug break clear` runs isite_remove (w2_bp.c:139-144). It writes 0x55 over the jump's first byte with no
   check, and the next call of F crashes.
- Other orders crash the same way: a trap break or coverage line at F+1..F+4 after a swap, or unswap writing back an
  `old` that holds 0xCC.

---

## HIGH

None.

---

## MEDIUM

### M1. After a hook chained on an entry probe is removed, the probe's jump is live again but the companion no longer tracks it; the next probe set then sends calls of that function into another function's trampoline

- Where:
  - `native/engine/fnvmcp_engine.c:1120-1128` `probe_blocks_hook` allows a hook exactly at an ARMED entry probe's
    target (the K1 review I1 chain). The hook's `original` is then the probe's `E9 rel32` into the probe stub.
  - `:1628-1632` `probe_disarm`: the target no longer holds the probe's jump (the hook's jump is there), so the
    probe becomes PROBE_CHANGED and its bytes are left as they are.
  - `:931-952` `op_unhook` writes `h->original` back: the probe's jump to the old stub is live again.
  - `:1106-1116` `probe_range` and `probe_patch_size` count only PROBE_ARMED probes, and `:1694-1695` probe init
    checks only `probe_any_armed()` (ARMED) and `hook_on_probe_code()` (live hooks). Nothing covers the restored
    jump any more.
  - `:1151-1200` `fnvmcp_probe_enter`: the old stub pushes its index i and returns `probes[i].tramp` from the
    *current* global `probes` array. Probe init reuses that array, or allocates a zeroed one (`:1698-1717`).
  - Python lets the chain happen, since a probe's E9 passes the "chainable" test:
    - `fnv_control.py:87` and `fnv_deep.py:285` (`live[0] != 0xE9`).
    - `fnv_flow.hook_at` and `fnv_debugger._hook_hold` do not compare against the clean image.
  - `fnv_probe.install` (`fnv_probe.py:388-398`) skips functions with a live hook. Its `_live_check` (`:436-446`)
    leaves F out of the new set (live != clean) but does not touch F's leftover jump.
  - `fnv_live._undo_probe_remove` (`fnv_live.py:1726-1727`) says the old stub code is "unreachable". After this
    sequence it is reachable.
- What happens:
  1. `fnv_probe install` arms entry probes on thousands of functions, F among them. F = `E9 <probe stub i>`.
  2. Any hook on F: `fnv_hook_set F ...`, `fnv_tracepoint start F`, `fnv_debug break F` (hook hold), a watch hook,
     and so on. `decode_prologue` moves just the E9 (`fnv_deep.py:146-147`), and the companion allows it because
     the target equals the probe's target. F = `E9 <hook stub>`, and the hook's trampoline jumps to the probe stub.
  3. `fnv_probe remove`, or `fnv_undo` of the probe entry. The U1 order guard gives the probe entry no target, so
     the out-of-order undo is allowed. probe_disarm finds the hook's jump at F, sets F's probe to PROBE_CHANGED and
     leaves F alone. The other probes are restored.
  4. The hook is removed (`fnv_hook_set remove`, `fnv_debug break clear`, fnv_hooks remove). op_unhook checks that
     its own jump is there (it is) and writes back `original`. F = `E9 <old probe stub i>` again. The companion now
     lists no hook and no armed probe there.
  5. `fnv_probe install` (a new set; the naming workflow runs sets one after another):
     - probe init is allowed: nothing is ARMED, and no live hook points into probe code.
     - The new set's `probes[i]` is some other function G. With an array large enough to reuse, the old `probe_counts`
       is also zeroed. With a new array, `probes[i]` is G or zero.
  6. The next call of F runs the old stub:
     - `lock xadd` on old `probe_counts[i]` (now G's counter when the array is reused). With the defaults
       samples=8 and every=64 (`fnv_probe.py:361`), the first calls take the sample path.
     - The sample path is `push i; jmp probe_common`, so `fnvmcp_probe_enter` returns `probes[i].tramp`: G's
       relocated prologue and a jump into G's body, with F's caller, stack and registers, or address 0 when
       `probes[i]` is zeroed.
     - The game crashes or silently corrupts state.
  7. Also, between steps 4 and 5, F's first bytes hold a jump the companion does not own. An int3 break at F+1..F+4
     (isite_add `patched_range` says no) or a coverage line there (cov_write_all) corrupts the jump displacement.
     That is the K1 crash again.
  - The same happens with call-site probes (mode 1), if a hook's stolen bytes hold a call whose rel32 a site
    patched. That path is already deferred as minor ("probe call-site fields are not in patched_range").
- Why it matters:
  - Each step is a normal call of fnv_probe and a hook or debug tool. The crash comes on a later, unrelated probe
    install, and nothing points back at the chained hook. `_quarantine_from_last_session` would blame F or G.
  - Even without step 5, the game stays patched after "remove" (F keeps counting into a dead probe set), which the
    tools describe as restored.
  - Medium, not high: it needs the probe-first, hook-second, remove-probe-before-hook order on the same function.
    `fnv_revert_all` (newest first) takes it apart in the safe order.
- Fix idea:
  - In op_unhook (and w5_mod `hook_remove`), when `h->original` is a jump into a probe stub (the test that
    `hook_on_probe_code` already does), write the probe's saved `original` bytes instead. Or set that probe back to
    ARMED, so a later disarm restores it.
  - Or make probe disarm, when it meets a chaining hook at the target, rewrite that hook's saved `original` to the
    probe's clean bytes.
  - And have probe init refuse (or repair) while any CHANGED entry probe's jump is still at its target.

---

## LOW

### L1. fnv_debug treats a removed hook as "already hooked": setting a function break again after clearing it falls back to a trap break

- Where:
  - `geck_mcp/fnv_debugger.py:624-628` `_hook_hold` raises ALREADY_HOOKED for any listed hook at the address. It
    does not filter on `stolen`, unlike `fnv_flow.hook_at:46`, `fnv_deep.hooks()` and `fnv_probe.py:388`.
  - The companion's `hooks` op (`fnvmcp_engine.c:1048-1055`) lists removed hooks (stolen 0). They stay in
    `hooks[]` until the table of 32 is compacted (`:829-839`) or the hook is reused (K3, only on a later install,
    60 s or more after the unhook).
- What happens:
  1. `fnv_debug break F` installs a hook hold, label `dbg 1 F`. `fnv_debug break clear 1` unhooks it, and it stays
     listed with stolen 0.
  2. `fnv_debug break F` again: `_hook_hold` sees the removed hook and raises ALREADY_HOOKED. `_break_set` catches it
     (`:557-560`) and takes the trap path. The record says `hookRefused: The function already carries a hook
     (dbg 1 F)`.
  3. With thread=main this takes one of the 4 debug registers. When they are in use (watchpoints, other breaks) it
     becomes int3, which parks every thread (K5) instead of the hook hold's `main` predicate.
- Fix idea: skip hooks with `stolen` 0 in `_hook_hold`, as the other callers do.

### L2. The K4 snapshot of already-parked stops is kept on the shared FlowTools object, so two traces at once can make one skip its own hit

- Where:
  - `geck_mcp/fnv_flow.py:181` sets `self._seen_stops` inside `dbg.lock`.
  - `:202` reads `getattr(self, '_seen_stops', ())` later in `_trace_armed`, after the lock is released and after
    `_callee_ranges` / `_write_call`.
  - `fnv_live_tools.py:299` makes one FlowTools for the server, and tool calls run on parallel worker threads
    (`fnv_debugger.py:168-170`).
- What happens (suspected, narrow window):
  1. Trace A takes its snapshot, arms bp 3 and leaves the lock. Its function is hit at once, and the stop parks with
     seq 40.
  2. Trace B (another fnv_flow trace, or call) takes the lock and sets `self._seen_stops`, which now includes seq 40.
  3. A reaches `_await_bp` and reads B's snapshot. It skips seq 40 as "parked before this arm" and, after 20 s,
     reports NOT_CALLED and clears bp 3 (already spent).
  4. A's stop stays parked, normally on the main thread. No tool is waiting on it, and the game stays stopped until
     the dead-man switch or fnv_debug continue.
- Fix idea: keep the snapshot in a local variable and pass it through to `_trace_armed` and `_await_bp`.

---

## Checked and found sound (not findings)
- K1:
  - `isite_in` and `hook_range` bounds are half-open and match the hook's stolen length.
  - `to` and `bp set` return `in_use` for -3, and a trace return site inside a patch ends the trace as returned
    instead of writing.
  - Probe arm compares each target with its clean bytes, so it never patches over a hook, isite or coverage byte.
- K3:
  - Shadow frames count in-flight calls, including frames popped when an exception unwound them. Probe frames have
    `hook` NULL.
  - The crash-context walker reads only live frames, and no hook is reused while a frame refers to it.
  - A reused hook keeps its `entry`, so the `push h` stub still names the same struct.
  - `bad_relocation`, `busy` and `unreadable` after reuse all retire the hook.
- K2:
  - `stop_serve_wait` and `stop_wait_worker` use `max(last_contact, parked)`.
  - gate_park can only meet `request_dropped == 0`: a held gate always serves, and clearing the flag needs a served
    request.
- The prologue decoder: prefixes, ModRM/SIB displacement lengths, the imm8/imm16/imm32 groups, the F6/F7 immediates,
  rel32 relocation for E8/E9/0F 8x, and the refusals (short jumps, ret, FF /4 /5, the 0x67 prefix, lock) are correct
  for the prologues they accept.
- fnv_link: `quoted()` refuses a double quote and line breaks. `batch` refuses tabs and newlines in ops. `_exchange`
  never resends after a send.
