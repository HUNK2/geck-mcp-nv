# Round 4 fix batch: review (fresh Opus)

Scope: the uncommitted round-4 fixes (LV1, Y1, V1, Q1, Q2, Y2, V2 ruling, V3, V4, V7) in geck_mcp/fnv_create.py,
fnv_control.py, fnv_live_tools.py, fnv_live.py, fnv_data.py, native/engine/fnvmcp_engine.c, w5_mod.c, w5_code.c, and
the new or changed tests. Read-only on the repo apart from this file. No game, no MCP tools.

Offline checks:
- Tests run, all OK: test_r4_contract (13, real-map cases ran with the local clean exe), test_r4_state (19),
  test_p1_place_surface (19), test_args (11), test_s5_review (11), test_stab_gate (4), test_engine_bp (20).
- `_ground_info`, `snap` and `check` were driven with the test's PlaneRays fake on extra scenes: a buried object with
  something under it, a cabinet with clutter on its own shelf, a table with a plate on it, a 70-unit stage, and an
  object sunk 60 units with no own hit.
- fnv_oracle.Purity was run over the real engine map and the clean exe: 3,514 proven getter-named candidates,
  1,523 pure. Capstone access flags on memory operands were checked: none missing except 3 `test`. No indexed stack
  writes were found in pure bodies.
- Scratch was in %TEMP%\r4-review (deleted).

Counts: Critical 0, Important 2, Minor 10.

Tool contract: no new tools or actions. The Literal action lists are unchanged. fnv_perk_entries moves from
read to act in the kind table and its annotation, with action=read still read (the fnv_probe pattern).
test_stab_gate covers the new MUTATING_ACTIONS rows. engine_get stays read.

---

## Important

### I1. snap still moves a sunk object further down, by two routes: the V2 ruling only changed what check reports, and the no-own-hit half of V1 (audit H1) was left as it was

- Where: geck_mcp/fnv_create.py:1072-1077 (own-hit path: a buried object gets `after[0]`, the surface under it),
  1078-1083 (no-own path, `sink=50` for snap and check), 1194-1204 (`snap` acts on `g` with no check of `over`),
  1216-1229 (`check`).
- Route A (V2, own hit). A bottle is wholly buried: the floor top is 60 over its origin, its own collision top is
  at 10, and there is a room under the floor at -400. The planes are (60, floor), (10, own), (-400, cellar).
  1. `_ground_info` collects surfaces [60, -400] with `own_at` = 1, so it returns g = -400 and over = 60.
  2. `fnv_place snap` moves the bottle 400 units down into the cellar and answers `snapped: true, ground: -400`.
  3. `fnv_place check` says "floats 400 units above the surface under it" and also "a surface 60 units over it ... or
     it is buried under it". The first line tells the agent to snap, which does step 2.
  - Same with a 20-unit floor slab laid on terrain at -30 (planes 20, own 10, -30): snap moves the object 30 down,
    under the slab, and it stays buried. Floor slabs over terrain are common in exteriors.
  - Checked offline, both cases.
- Route B (V1, no own hit). The object's collision is not on its origin column, or the ray misses it. It is sunk 60
  units into a slab that sits on terrain: planes (60, slab) and (-1, terrain).
  1. 60 > z + 50, so the slab is not "below". The terrain at -1 is, so `g` = -1.
  2. snap moves the object 1 unit down, where it stays sunk. check reports it as resting.
  - audit4/state.md H1, third bullet, names this case. The fix raised `sink` to 64 for at and array only, so
    the case still reproduces at sink=50 (checked offline), but BUG-LOG lists V1 as fixed.
- Why it matters: snap is the tool for repairing sunk objects. In both routes it reports success and moves the
  object the wrong way.
- On the V2 ruling: I agree that downward rays alone cannot tell "buried under S" from "under a ceiling S" in
  general. But the ruling should then make snap cautious, not only add a line to check.
- Fix idea:
  - In snap: when `over` lies within about 64 of z and the found surface is more than about 10 below z, do not move.
    Return `snapped: false` with "ambiguous: buried under a surface N over it, or under a low ceiling; pass the
    height or move it up with adjust".
  - In check, give the same ambiguous result instead of "floats N".
  - For route B, either use the at/array rule (sink 64), or never let snap move down by more than the distance to
    the nearest surface above z.
  - Add snap tests for both routes.

### I2. Q2 is incomplete: fnv_patch and its journal undo write game code through `wr`, which nothing tracks, so the int3/patch crash Q2 fixed for fnv_code remains for fnv_patch

- Where:
  - geck_mcp/fnv_control.py:312-322: fnv_patch writes with `self.link.write(at, data)`, and its undo is
    `{'op': 'write'}`.
  - geck_mcp/fnv_live.py:1741: `_undo_write` calls `live.link.write`.
  - native/engine/fnvmcp_engine.c:2061: `op_write` runs no isite, probe, hook or code-site check.
  - native/engine/w2_bp.c:125: `isite_add` asks `patched_range`, which knows hooks, probes and fnv_code sites,
    but not fnv_patch bytes.
  - w2_bp.c:139-144: `isite_remove` writes its saved byte back without checking anything.
- What happens (order A):
  1. `fnv_patch address=F nop=5` writes 5 NOPs. They are journaled and not tracked by the companion.
  2. `fnv_debug break F+2` is allowed (`patched_range` is false) and saves orig = 0x90.
  3. `fnv_undo` of the patch runs `wr F <old 5 bytes>` over the 0xCC. The site still counts as live.
  4. `fnv_debug break clear` writes 0x90 at F+2, inside the restored instruction. The next call of F crashes.
- Order B: the break is set first, then `fnv_patch` over it. The clean-exe compare turns this into
  PATCHED_ELSEWHERE, unless `force=true` is passed or no clean exe is set (`_clean_bytes` gives None). Then the
  patch's "old" holds the 0xCC. Its undo puts the 0xCC back after the break is cleared, and executing that
  untracked int3 crashes.
- The same applies when an fnv_patch undo writes into a hook placed after the patch: op_hook does not know the
  patch, and the undo cuts the hook's jump.
- Why it matters: the BUG-LOG row says "Live code patches are tracked and count as patched bytes; a patch over an
  int3, hook or probe is refused". That is true only for fnv_code. fnv_patch is the general tool for writing game
  code.
- Fix idea:
  - Send fnv_patch's write, and the `write` undo of `kind: 'patch'` entries, through `code patch` instead of `wr`.
    Then they are tracked as code sites, refused `in_use` over int3s, hooks and probes, and written frozen.
  - Or make `op_write` refuse a range in the exe's .text that `isite_in`, `hook_range` or `probe_range` covers, and
    note a code site for .text writes.
  - Add the order-A test next to test_no_int3_inside_a_code_patch_until_it_is_put_back.

---

## Minor

### M1. The overhead note fires for every object under or carrying something within 64 units, so check reports issues in normal scenes
- Where: fnv_create.py:1229 (the note), measured from the object's origin z, with `over` = the nearest surface hit
  before the object's own hit.
- What happens: a 40-unit table at the floor with a plate on it (planes 42 plate, 40 own, 0 floor) gets "a surface
  42 units over it: a low ceiling or shelf, or it is buried under it" (checked offline). The same happens to a box
  under a desk or a crate under a shelf. The `issues` list is no longer empty for correctly placed objects.
- Fix idea: measure the gap from the object's own top (the own hit's height) and note only a surface within a few
  units of it. Or put the note in a separate `notes` key rather than `issues`.

### M2. Own-hit path: a surface between two of the object's own hits is taken as the ground (pre-existing, but the new docstring says otherwise)
- Where: fnv_create.py:1068-1076. `own_at` is the first own hit; `after[0]` is the first non-own surface after it.
- What happens: a cabinet has own collision at 180, 100 and 50, a bottle on its middle shelf at 110, and the floor
  at 0. `after` is [110, 0], so snap moves the cabinet up 110 and check says "below the surface under it by 110"
  (checked offline). HEAD did the same.
- Fix idea: keep the index of the last own hit and take the first surface after it.

### M3. V1's 64-unit sink leaves a band: a point 64 to 200 units under a raised surface with something under it still falls through
- Where: fnv_create.py:1078-1083 (at and array, sink 64).
- What happens: a stage or loading dock 70 units over the player's feet, with floor or terrain under it. `at snap`
  at a point on it returns 0 (checked offline); HEAD returned 70. The LV1 ceiling case is 125 over the point, so
  the two overlap and no single sink separates them.
- Fix idea: test the band. For `near`, use the anchor's own ground height as z (a point next to the player is at
  the player's surface), so the sink matters less.

### M4. Y1 left raw `this` and `ptr` arguments as they were (part of the audit's fix idea): pure getters still run on any memory under read
- Where: fnv_control.py:487-496 (`this` = any "0x..."; `ptr` args are passed through).
- What happens: faults are now caught. But a pure list walker such as a Count* or Find* getter, given a pointer
  into a cyclic structure, loops forever on the main thread, and the guard does not catch a hang. A wrong-class ref
  quietly returns garbage as `result`.
- Fix idea: for thiscall, check the RTTI class of a raw `this` against the function's class or its subclasses, or
  allow only refs and forms for `this`. Keep raw pointers for fnv_engine_call.

### M5. One Purity instance is shared across concurrent read calls: the "recursion" placeholder poisons the cache
- Where: fnv_control.py:466-474 (`self._purity`), fnv_oracle.py:196-203 (cache placeholder
  `{'pure': False, 'why': 'recursion'}`).
- What happens: two fnv_engine_get calls run on worker threads at the same time. On first use they reach a common
  callee. Thread B reads A's placeholder and caches its own getter as impure ("callee X: recursion"). That getter
  then answers MAY_WRITE until the server restarts. The capstone handle is also used by both threads at once.
- Fix idea: a lock around `checker.check` in `_purity_of`.

### M6. Q2 code-site table: silent overflow, phantom sites, and leaks
- Where: native/engine/w5_code.c:10 and :38-53.
- What happens:
  - A short-function swap redirects every call site. With more than 1,024 sites in total, the rest are not
    tracked, silently.
  - A patch that writes the same bytes as the old ones creates a live site that only an identical patch ends.
  - With two overlapping patches of different address or length undone out of order, the second site's "old"
    never comes back, so hooks and int3s there answer `in_use` until the game restarts. The code-patch check
    itself does not refuse an overlap with another live code site (w5_code.c:100-103).
- Fix idea: fail the patch, or report `untracked`, when the table is full. Skip sites where old equals new. Either
  refuse a code patch that overlaps a live code site at a different address or length, or end overlapped sites
  when their bytes are rewritten.

### M7. fnv_code reports Q2's new refusal badly
- Where: geck_mcp/fnv_code.py:471 and :483 (`_patch_sites` skips a site whose patch is not ok), :532 (unswap
  advice).
- What happens:
  - A swap whose call site or vtable patch is refused `in_use` (an int3 or hook there) leaves that site out
    silently, and the swap reports success with fewer sites.
  - unswap's WRITE_FAILED says "a thread may have been inside the bytes: busy" even when the error is `in_use`
    (clear the break or hook first).
- Fix idea: list refused sites in the swap result. Word the unswap advice from the error code.

### M8. Q1 restore covers bytes the hook never owned
- Where: native/engine/fnvmcp_engine.c:1163-1180 (`hook_restore_bytes`, `length = max(h->stolen, p->stolen)`).
- What happens: the probe is CHANGED, so `probe_range` is 0 for bytes [target + 5, target + p->stolen). Those bytes
  are the probe's NOP padding, and `hook_range` covers only the first 5. An int3 or a raw-address hook placed there
  is overwritten by the unhook. It is contrived (no original instruction starts inside that range), but it is the
  pattern K1 closed elsewhere.
- Fix idea: before writing past `h->stolen`, refuse (`in_use`) if `isite_in`, another hook or a code site covers
  [target + h->stolen, target + p->stolen).

### M9. The Y1 rule is not applied to sibling read paths
- Where: geck_mcp/fnv_inspect.py:1113-1129 (`fnv_systems zone`, GetCalcedZoneLevel 0x00526190: Purity says
  "beyond the depth limit") and :1002-1007 (`progression`, the xp_for_level formula: impure through Setting::Int's
  NULL fallback store).
- What happens: both still call engine functions under read through `live.engine_call`, which has no purity or
  kind gate. The writes found are benign (a static fallback), so this is a consistency gap. The BUG-LOG's
  "same gap in fnv_systems barter (found by the fixer)" reads as if the sweep were complete.
- Fix idea: note it under Y3, or run engine_call from read kinds only when Purity says pure.

### M10. Docs and setup for the new rules
- Where: docs/FNV-LIVE-TOOLS.md:129-140 (the engine_control list), :788 (fnv_systems row), and the setup docs.
- What happens:
  - The "act covers ..." list does not name fnv_perk_entries start, stop and evaluate, or fnv_systems barter.
  - engine_get now needs both fnv.clean_exe and capstone. Capstone is only in the optional `naming` extra
    (pyproject.toml:12), so on a default install every engine_get call answers CAPSTONE_MISSING or
    CLEAN_EXE_MISSING. Neither the docs nor fnv_setup_check says so.
  - Actor::GetLevel, the stab-gate and real-map example, is pure only at exactly depth 3: at depth 2 it is "callee
    0x00461580 beyond the depth limit". An engine-map change can flip the test's example.
- Fix idea: add the rows. Mention the two requirements in the fnv_engine_get docstring and the setup check. Pick a
  shallower example getter for the tests, or pin it.

---

## Rulings

- V2 (a buried object cannot be told from one under a ceiling): accepted as a detection limit for vertical rays,
  not as grounds to let snap act. See I1.
- Y1, purity required under full too: agree. engine_get is a read tool by contract (readOnlyHint, no journal), and
  full already has fnv_engine_call. If engine_get's rules depended on the mode, its annotation would be wrong.
- Y1, about 2,000 undecidable getters now need fnv_engine_call: agree for safety, and the refusal message is
  honest. The scan shows cheap later wins:
  - Setting::Int (0x0043D4D0) stores to a static only when `this` is NULL; it blocks 12 getters directly and more
    through callers.
  - An unknown CRT helper at 0x00EC43FB blocks 54.
  - movsx (Y7) blocks 48.
  - BaseExtraList::GetExtraData blocks 79. Its write is real (a lock field), so it is rightly refused.
  An allowlist of audited benign stores could recover many of these after the freeze.
- Y1, clean exe required: agree (no proof means no call). Capstone is required as well (M10).
- V3 (keys by restored state) and V4 (intent before the hot swap, `repeatable_if` on spans): sound.
  - V3: the enable, scale, position and cell, actor value and form-id cases were checked against every
    console_ref and console undo producer. Verbs not listed fall back to verb-only keys. That over-orders but is
    safe.
  - V4: a retry after a lost reply is ordered correctly by `_undo_swap_script`'s later-swap check.
- Q1: correct for the armed and the disarmed or changed cases, including probe stolen > hook stolen. Only M8 is left.
- Y2: the rows, kinds, annotation and the `action=` pass-through are right. Read stays read.
