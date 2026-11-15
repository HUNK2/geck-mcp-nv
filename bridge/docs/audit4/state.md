# PLAN-3 exit gate, round 4: shared state, journal and undo

Auditor scope: geck_mcp/fnv_state.py, geck_mcp/fnv_journal.py, the undo executor and op handlers in geck_mcp/fnv_live.py, GameIdentity, and the callers that mark or confirm entries (fnv_time, fnv_eyes, fnv_create, fnv_control, fnv_agent, fnv_probe, fnv_data). The same scope as round 3. Focus: the round-3 fixes U1..U3 and their batch reviews (13312e0..a0a74d0), and the uncommitted P1 change to PlaceTools._ground in fnv_create.py. I reviewed the second version of P1 (first surface at or below z + sink; with `ignore`, the first surface after the object's own collision), not the earlier "nearest hit" version. Read-only. The findings were checked offline with the test's PlaneRays fake, the old HEAD `_ground` loaded from `git show`, and FakeLive journals. Scratch folder `%TEMP%\audit4-state` (deleted). No game, no MCP tools, no pipe.

Counts: high 1, medium 3, low 3.

---

## HIGH

### H1. P1 (uncommitted): a point a few units under a raised surface now falls through to whatever lies under it. fnv_place at/array snap put objects inside steps, sidewalks and platforms.
- Where: geck_mcp/fnv_create.py:1072-1074 (the path with no own hit: `below = [h for h in surfaces if h <= z + sink]`, `return below[0] if below else surfaces[-1]`), called with the default `sink=4.0` from `at` (1089) and `array` (1122). It is also reached from snap and check (1190, 1191, 1212, `sink=50.0`) when the object's own collision is not on the ray.
- What happens (each case checked offline: the new code against the HEAD code on the same planes):
  1. The player stands on a road at z=0. `fnv_place at base=X snap=true` takes the point 150 units ahead at the player's feet height. That point lies on a 12-unit sidewalk or curb, and the terrain carries on under the static, as it does everywhere in an exterior.
  2. The ray from z+200 hits the sidewalk top at 12. That is above z+4, so the loop goes on (`top = 11`) and hits the terrain under the slab at about -1. That is at or below z, so the loop stops.
  3. `below` is [-1], so the object is placed at -1, inside the sidewalk. The old code returned 12.
  - Stairs: the player is at the foot and the point is on a tread 40 units up, over the floor under the stairs. The new code returns 0 and the old one 40. Porches, stages, raised building floors over terrain, and `array` rows that cross a step all behave the same way. Each point that lands more than 4 units under a raised surface is put under it.
  - Snap or check with no own hit (an object whose collision is not on its origin column, or whose hit maps to another ref): an object sunk more than 50 units into a floor slab that sits on terrain gets the terrain under the slab. Snap moves it further down, and check reports it as resting or floating.
- Why it matters: a wrong placement in normal use. The docstring says the reach lifts "a point or an object sunk into a floor or a hill" out, but that only holds when nothing is under the surface. The case where something is under it is the usual one in a built exterior. The object is journaled, so undo can remove it, but it lands hidden inside geometry and the result reports success.
- Fix idea: treat a surface above the point as overhead only when it is well above it. Use a `sink` of about an object's height (64 to 100) for at and array too. The P1 repro still works, because its ceiling is 125 or more over the point. Or keep the old "first hit from above" rule, and drop that hit only when there is open space between the hit and z. Add tests for a 12-unit step and a 40-unit stair over a floor.

---

## MEDIUM

### M1. P1 (uncommitted): snap and check on an object buried under a surface report "no surface found", or move it down to the floor below.
- Where: geck_mcp/fnv_create.py:1057-1071 (the own-collision path: surfaces hit before the object's own hit are skipped; `after = surfaces[own_at:]`; `return None` when nothing follows).
- What happens:
  1. A small static, for example a bottle 10 units tall, was placed at the player's feet on a rising slope or under a raised floor. The surface is 60 units over its origin, so the object is fully buried at its origin column. Its own collision is under that surface.
  2. `fnv_place snap` or `check`: the ray from z+250 hits the terrain or floor at 60 (not own, so it goes in `surfaces`). It then hits the object's own collision at 10 (`own_at = 1`), and then nothing.
  3. `after` is empty and `surfaces` is not, so `_ground` returns None. Snap answers "No surface found under it on the pick layers." Check reports "no surface found under it" instead of "below the surface under it by 60 units".
  4. If anything is under it (a cellar, the room below, a rock buried under the terrain), `after[0]` is that surface. Checked offline: with a surface at -400 the result is -400, so snap moves the buried object 400 units further down. The old code returned 60 in both cases.
  - The same happens when an object is only partly sunk but its collision at the origin column is under the surface: an irregular rock, or a chair whose seat is sunk while its back pokes out.
- Why it matters: snap and check exist to find and fix sunk objects. With this rule the worst-sunk ones are reported as having no surface, or are pushed deeper.
- Fix idea: when surfaces come before the own hit, the nearest one before it (`surfaces[own_at - 1]`) is the surface the object is buried under. Return it when it is within `reach` above z, and use `after` only when nothing was hit before the object.

### M2. U1 incomplete: order targets are keyed by the undo line's verb and first word, so undos that restore one value through different lines never meet.
- Where: geck_mcp/fnv_live.py:164-223 (`_order_targets`, `_line_target`: `key = words[1:2]`), used by `_later_open` at 225-231. The lines come from fnv_world.py:384-392 (world_edit: `SetScale <old>`, `Enable`, `Disable`), fnv_agent.py:800-805 (`fnv_move`: `MoveToCell <cell> x y z`), fnv_live.py:1398-1446 (actor_control: `ref_enabled`, item_count by EditorID or by form id, SetAV or ForceAV).
- What happens (`_later_open` checked offline with FakeLive; it returns [] in every case below):
  1. A ref R is enabled. `fnv_world_edit disable R` (E1, undo line `Enable`), then `fnv_world_edit enable R` (E2, undo line `Disable`). The targets are `('ref', R, 'enable')` and `('ref', R, 'disable')`, so they do not meet. `fnv_undo journal_id=E1` runs Enable (it is already enabled) and marks E1 undone. `fnv_revert_all` then runs E2's Disable, so R ends disabled. It was enabled.
  2. World_edit scale 1 to 2 (undo `SetScale 1.000000`), then 2 to 3 (undo `SetScale 2.000000`). For SetScale, `words[1:2]` is the old value, so the targets are `('ref', R, 'setscale', '1.000000')` and `('ref', R, 'setscale', '2.000000')`. They never meet. Undo of the first by id, then revert, leaves scale 2.
  3. `actor_control disable` (`ref_enabled` target `('ref', R, 'enabled')`) against a world_edit `Disable`/`Enable` line. `fnv_move` (`movetocell`) against `actor_control set_position` or world_edit move (`setpos x/y/z`). `item_count` with `Stimpak` against `00015169`. SetAV Health against ForceAV Health on the same actor. Each pair restores the same value and gets no order check.
- Why it matters: this is the U1 outcome (the final value is wrong after `fnv_undo journal_id` and then a revert), for enable state, scale, position and items. The scale and enable/disable cases need only one tool used twice on one ref.
- Fix idea: key targets by the value, not the line. Map Enable, Disable and `ref_enabled` to `('ref', R, 'enabled')`, and SetScale to `('ref', R, 'scale')`. Make MoveToCell meet setpos x/y/z, and SetAV/ForceAV meet on the AV name. Normalise items to the form id (resolve the EditorID at journal time). A value-less key is safest for any verb not in an argument table.

### M3. N10 gap: `fnv_script_hot_swap` still journals after the swap. A lost reply or a half-failed batch leaves the new code running with no entry.
- Where: geck_mcp/fnv_data.py:733-740 (`swapped = swap_script_code(...)`, then `live.journal.add(...)`), with fnv_data.py:875-889 (one `link.batch`; `SWAP_FAILED` raises after the writes that succeeded).
- What happens:
  1. `fnv_script_hot_swap` sends the 8 swap writes in one batch. The game applies them in that frame.
  2. The reply is lost (COMPANION_TIMEOUT or COMPANION_DISCONNECTED, for example a frame stall or a load), or some writes fail and `SWAP_FAILED` is raised.
  3. The exception leaves `hot_swap` before `journal.add`. There is no entry, so `fnv_undo`, `fnv_revert_all` and `fnv_session end` cannot put the old code back, and with SWAP_FAILED the two scripts are half exchanged.
- Why it matters: the game is left changed with nothing in the journal, in the same failure case round 3 used for U3 (M2). Before U3 this was a reasoned exception, because swap_script was not repeatable. Since U3 review I3 the entry carries both scripts' saved spans, so the undo is a plain restore and intent-first is now safe.
- Fix idea: add the entry as `state: 'pending'` with `spans` before `swap_script_code`, and confirm after. Mark `swap_script` repeatable when `spans` is present, or route a pending spans entry past the "outcome unknown" refusal.

---

## LOW

### L1. A pause undo still ignores `resume()`'s verdict, and since U2 the pause flag comes from a best-effort file read.
- Where: geck_mcp/fnv_live.py:1861-1862 (`control.resume()`, return value dropped), geck_mcp/fnv_time.py:155-158 (U2: `paused_by_us = restored = saved` whenever the file differs), geck_mcp/fnv_pred.py:390-399 and 421-426 (`ProcState.load` uses `read_json`, which returns {} on any OSError or bad JSON; `save` swallows OSError and StateError).
- What happens: server A pauses, but its `_remember` save fails (lock timeout under contention, or an AV holding the file). Or A's later `load` reads the file while another process's `atomic_write` is in its in-place fallback (U6). A's next `require_game` sees `saved=False` and clears its own `paused_by_us`. `fnv_revert_all` then runs the pause undo: `resume()` answers "not paused by this tool" (changed=False) and the op returns None. The entry is marked undone while the clock stays paused. Before U2, A's in-memory flag would have resumed it.
- Fix idea: the round-3 M1 idea that was not taken. When `resume()` returns changed=False, read the clock counter. If it is non-zero, return `{'undone': False, ...}`, or call Enable once for this entry. Do not let a failed read (as opposed to a file that says False) clear the flag.

### L2. The U1 order check makes revert_all quadratic in the journal size.
- Where: geck_mcp/fnv_live.py:225-231. `_later_open` calls `journal.entries(process=..., undoable=True)` (a copy of every open entry) for each undo, inside the per-entry journal lock.
- What happens: measured offline with FakeLive and a no-op link: `revert_all` of 3000 memory entries takes 22.4 s, against 12.2 s with `_later_open` stubbed out. A long level-building session (arrays of 100 placements) reaches thousands of entries. Other servers' `Journal.add` waits on the same lock, which is held per entry, so they slow down but do not time out.
- Fix idea: compute the open later entries' targets once per `_apply_undos` call. Pass that set down, and drop each entry's targets as it is undone.

### L3. tests/test_p1_place_surface.py's module docstring describes the earlier rule.
- Where: tests/test_p1_place_surface.py:1-6 ("The surface is now the hit nearest the point").
- What happens: the code now takes the first surface at or below z+sink, or the first one after the object's own hit. The docstring and the comment on the "nearest" rule mislead the next reader of the test.
- Fix idea: reword it to the current rule.

---

## Checked and found sound (no finding)
- U1 for what it covers: overlapping memory ranges (write and writes), SetPos/SetAngle axes, SetAV per AV, settings, objectives with their index, `player.` console lines against the player's ref (the companion's `%08X` form id equals `PLAYER_FORM_ID`), the weather verbs, fly and HUD. A delete (MarkForDelete) has no target and may run first. `fnv_undo` with a count and revert_all go newest first and mark each entry undone before the next, so they are never refused by their own batch. No pending entry has a non-repeatable op (every `'state': 'pending'` site uses write, writes, console_ref, unhook, probe_remove, ref_enabled, actor_flag or item_count), so a stuck pending entry cannot block older ones forever.
- U2: `require_game` re-reads the shared flag on every call. A flag set by another server is taken as `restored`, and the counter is checked before Enable. A flag cleared by another server ends this server's own. `_close('pause')` inside an undo runs under the journal lock, which is reentrant.
- U3 and I3: the spans undo writes both scripts' saved fields in one batch, so it is safe to repeat and repairs a half swap. A later open swap of the same target is still refused. `data` is checked before the old toggle path.
- The journal (fnv_journal.py, unchanged since round 3): seq under the lock, the fold, the compaction event order, and the identity stamp. Nothing new beyond U4..U7.
- P1 against the cases its tests cover: a shelf under a low ceiling, a mid-air point (125 units under the ceiling, 131 over the floor) drops to the floor, the object's own collision is passed through, and a ceiling over an object is skipped. The existing tests pass (test_p1_place_surface 16/16, test_fnv_live, test_bugfix_live2, test_s7_round3 40/40).
