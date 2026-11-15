# PLAN-3 exit gate, round-4 audit: tool surface and contract

Area: fnv_live_tools.py registrations, fnv_live.py envelope / console guard / resolve, fnv_args.py, the engine_control
gate (gated_kind / MUTATING_ACTIONS), fnv_game.py launch/quit/status, fnv_session.py, fnv_crawl.py, fnv_scenario.py,
supervisor.py, server.py (same scope as audit3/contract.md), followed into the modules the read-kind tools call.
Read-only. Offline checks: FakeLive (tests/fakes.py) with engine_control="read", the real engine map
(state/engine_map.json) and clean exe for the project's own Purity checker (fnv_oracle.Purity), and the test_stab_gate
fixture (real server, engine_control="read", pipe stubbed). Scratch files were in a temp folder, now deleted.
Known items (B1, B2, W5..W9 and the plan3 deferred lists) are not re-reported.

Counts: high 1, medium 1, low 4.

Round-3 fixes checked, no regression found: W1 (fnv_snapshot / fnv_diff / fnv_watch are '*' in MUTATING_ACTIONS,
the kind table and the annotations), W2 (resolve() evaluates only fixed-shape expressions; a bare name goes through the
EditorID index and form_expr, never into eval), W3 (fnv_warnings capture start/stop and fnv_window mouse/unpause pass
their action and run as act; capture with no mode is act), W4 (fnv_link.quoted on resolve's Plugin.esp:LOCALID,
formid_live, master lookups). tests/test_stab_gate.py passes (4/4). Annotations match the kind table for all 146 tools.
fnv_args, fnv_game, fnv_session, fnv_crawl, fnv_scenario, supervisor.py and server.py are unchanged since 13312e0.

---

## HIGH

### H1. fnv_engine_get (read, "no full control needed") writes game memory at an address the caller picks, and calls unguarded with any pointer: memory corruption or a game crash under engine_control="read"

- Where:
  - geck_mcp/fnv_live_tools.py:1231-1239 (`fnv_engine_get`, kind read, readOnlyHint; not in MUTATING_ACTIONS)
  - geck_mcp/fnv_control.py:398-416 (engine_get: the only side-effect check is the name: GETTER regex :35 on the
    short name, and no *direct, named* callee starting with a SETTER_WORDS word; unnamed callees, deeper callees and
    the function's own stores are not checked)
  - geck_mcp/fnv_control.py:429-438 (a `ptr`-kind parameter given as "0x..." is passed through as a raw pointer; a
    dict {type: ptr, value} is taken as is), :466-468 (`this` = any "0x..." string), :480 (the call op is `i`/`f`/`v`,
    never the guarded `ig`; fnvmcp_engine.c:2045-2052 runs the plain form with no fault guard)
- What happens:
  1. engine_control="read" (the default, config.py:33).
  2. `fnv_engine_get function="0x00476930" this="0x01000000" args=[0, "0x011DEA3C"]`. 0x00476930 is
     NiMatrix3::GetRow, status proven, signature `void __thiscall GetRow(unsigned int, NiPoint3 &)`; its name
     passes GETTER and it has no setter-named callee.
  3. The companion runs `call 476930 v 1000000 0 0 11dea3c`: the function copies 12 bytes (row 0 of the "matrix"
     at this) to 0x011DEA3C (the TES pointer global used by the raycast tools). The caller picks both the source
     (any readable address as `this`) and the destination, so this is a 12-byte memcpy anywhere in the game, under
     read, with no journal and no save backup. The reply is ok, outcome "unchanged", note "nothing was journaled".
  4. `fnv_engine_get function=Actor::GetLevel this=0x00000010` sends `call 87f9f0 i 10 0`: an unguarded thiscall
     with ecx=0x10; the first field read faults on the main thread and the game crashes (the guard exists, `ig`,
     and engine_call uses it for unproven functions, fnv_live.py:1266-1269, but engine_get never asks for it). A
     ref of the wrong class (a base form, a static where an Actor is expected) is not checked either.
- Scale: over the engine map, 3,312 proven functions pass engine_get's name filter. The project's own machine-code
  checker (fnv_oracle.Purity, depth 3, size limit 4000) finds 195 of them storing to non-stack memory in their own
  body and 289 more through a callee; 1,521 are proven pure. Examples besides GetRow: NiMatrix3::GetCol 0x00439F50,
  BGSPrimitive::GetColor 0x00413F90, TESObjectCELL::GetDataAmbientColor / GetDataFogColor / GetDataDirectionalColor
  0x005447B0 / 0x00544A30 / 0x00544890 and TES::GetLandNormal 0x004573F0 (all write through an out-pointer
  argument), Sky::GetSunriseBegin 0x00595EA0 (writes the global 0x011CA9E8), TESForm::GetFormTypeFromFormString
  0x00486890 (writes 0x011C54F0), TESLeveledList::CalculateCurrentForm 0x00487D00 (out pointer, and draws random
  numbers). Any getter with an out parameter needs the caller to invent a pointer, because the tool has no buffer of
  its own, so ordinary use of such a getter writes wherever the agent points.
- Confidence: confirmed offline. FakeLive(engine_control='read'), live._engine = the real engine map,
  ControlTools.engine_get('0x00476930', '0x01000000', [0, '0x011DEA3C']) through live.run with
  gated_kind('fnv_engine_get', None, 'read') == 'read': ok True, outcome unchanged, ops sent
  `[['476930', 'v', '1000000', '0', '0', '11dea3c']]`; the GetLevel case sent `[['87f9f0', 'i', '10', '0']]`.
  Through the real server (test_stab_gate fixture, read), fnv_engine_get reaches the pipe (GAME_NOT_RUNNING after one
  pipe op, not CONTROL_DISABLED). The write itself was not run live (read-only audit). plan3/progress.md S1.5 named
  engine_get a round-3 candidate; round 3 did not cover it.
- Why it matters: it is the same power as fnv_mem_write (act) and a crash, from a read tool that says it needs no
  full control; nothing is journaled, so fnv_undo / fnv_revert_all cannot repair it.
- Fix idea: in engine_get, require `Purity.check(address)['pure']` (the checker exists and needs only the clean exe;
  refuse with MAY_WRITE when it is missing or says no) instead of the name rule; refuse `ptr`-kind arguments and a raw
  "0x" this (or allow `this` only when its RTTI class is the function's class or a subclass); always call with the
  guarded form (`ig`/`fg`/`vg`). If any of that is too strict, move fnv_engine_get to act.

---

## MEDIUM

### M1. fnv_perk_entries (read) installs two engine hooks with action=start and leaves them in until stop; evaluate installs and removes them: hooks under engine_control="read" (D2 / T1 incomplete, as W3 was)

- Where: geck_mcp/fnv_live_tools.py:1134-1147 (`fnv_perk_entries`, kind read, readOnlyHint, no action passed to
  invoke; not in MUTATING_ACTIONS) -> geck_mcp/fnv_engine_ai.py:493-503 (start: `_hook(APPLY_PERK_MODIFIERS ...)` and
  `_hook(APPLY_FUNCTION ...)`, fnv_engine_ai.py:178-195 sends the companion `hook` op), :486-492 (stop unhooks),
  :561-590 (evaluate: start, an engine call of ApplyPerkModifiers, stop).
- What happens: with engine_control="read", `fnv_perk_entries action=start` patches the entry of
  ApplyPerkModifiers 0x005E58F0 and BGSEntryPointFunction::ApplyFunction 0x005E5B40 with recording hooks that stay
  until `action=stop`. The reply says outcome "unchanged". D2 (PLAN-3.md:56): anything that writes game code or
  leaves a hook installed is act; the same shape (fnv_trace start/stop, fnv_warnings capture start/stop, W3) is gated.
- Confidence: confirmed offline. FakeLive(engine_control='read'), EngineAITools over DeepTools, perk_entries('start')
  through live.run with gated_kind('fnv_perk_entries', None, 'read') == 'read': ok True, outcome unchanged, hooks now
  installed `[('0x005e58f0', 'fnvmcp_perk_apm'), ('0x005e5b40', 'fnvmcp_perk_fn')]`, ops `hooks rd hook hooks rd hook`.
  The real server with read reaches the pipe for action=start (not CONTROL_DISABLED).
- Why it matters: a read-only project gets code patches it did not allow; the gate test does not see it because the
  tool has no MUTATING_ACTIONS row.
- Fix idea: `'fnv_perk_entries': {'start', 'stop', 'evaluate'}` in MUTATING_ACTIONS, pass `action=action` in the
  registration, add the rows to tests/test_stab_gate.py (read stays read).

---

## LOW

### L1. Read actions that install a hook for a few seconds, or write engine globals, while fnv_frame_profile (the same pattern) is act

- Where:
  - fnv_detection (read, recompute=True by default): fnv_engine_ai.py:258-262, :269-283: a recording hook on the
    detection formula 0x00642ED0, then Actor::CalculateDetectionLevel 0x008A0D10 (status probable) called with the
    unguarded `call ... i` op (`_call`, :150-153), which skips the guard engine_call gives unproven functions.
  - fnv_anim action=events (read unless play/rebuild, fnv_live_tools.py:2141-2143) -> fnv_inspect.py:430-434
    (record_hooks on Actor::SetAnimAction, :103-127).
  - fnv_audio action=log and action=subtitles without enable (read, fnv_live_tools.py:2166-2171) ->
    fnv_inspect.py:476-478 (hooks on InitSoundForm / InitSoundPath), :652-689 (hook on Interface::ShowSubtitle).
  - fnv_physics action=triggers (read unless set) -> fnv_inspect.py:823-826 (hook on the script event raiser).
  - fnv_form action=bytes (read, fnv_live_tools.py:2000) -> fnv_create.py:898-921 (writes the engine's record buffer
    global 0x011C54CC, calls the form's SaveForm vtable slot, frees the buffer with FormHeap_Free, writes the global
    back).
- What happens: under engine_control="read" each of these patches game code (or writes engine globals) for the
  length of the call and puts it back. fnv_frame_profile does the same (timing hooks for a few seconds, then
  removed) and is '*' in MUTATING_ACTIONS by the S1.5 ruling "every call installs hooks". All of them reach the pipe
  under read in the test_stab_gate fixture (checked: fnv_detection, fnv_anim events, fnv_audio log, fnv_physics
  triggers, fnv_form bytes).
- Why it matters: the contract is applied unevenly. The patches are removed in `finally`, so nothing stays behind in
  normal use; the risk is a crash from the unguarded probable call, or a hook left in when the unhook fails (K6).
- Fix idea: either list them (fnv_anim {'events'}, fnv_audio {'log', 'subtitles'}, fnv_physics {'triggers'},
  fnv_form {'bytes'}, and fnv_detection with recompute) or record a ruling that hooks removed within the call are
  read and move fnv_frame_profile back; at least call CalculateDetectionLevel through the guarded op.

### L2. fnv_perk_entries evaluate ends a perk recording the caller started

- Where: geck_mcp/fnv_engine_ai.py:575-582 (evaluate calls perk_entries('start'), which re-installs both labels and
  resets `_perk_since`, then perk_entries('stop'), which removes every hook with those labels, :486-492).
- What happens: `fnv_perk_entries action=start`, play for a while, `action=evaluate entry_point=9 value=100`, then
  `action=read` answers NOT_RECORDING: the recording and the checks made before the evaluate are gone, and no reply
  said so.
- Fix idea: in evaluate, remember whether the hooks were there and leave them (and `_perk_since`) as found.

### L3. EngineAITools._hook removes any hook with its label first: two calls at once lose each other's records

- Where: geck_mcp/fnv_engine_ai.py:178-184 (a hook with the same label is removed, whoever installed it).
- What happens: two fnv_detection calls in parallel (two clients, or two servers on one game): the second's
  `_hook(DETECTION_FORMULA, 'fnvmcp_detection')` removes the first's hook between its install and its engine call, so
  the first reports `formula: null` "The formula was not reached", which is wrong. The same for a second
  fnv_perk_entries start from another server.
- Fix idea: a per-call label (suffix with the call id), or refuse with BUSY while a hook with the label exists.

### L4. Control-kind file writes under read that B2's table does not list

- Where: fnv_live_tools.py:1327-1334 (`fnv_window action=status persist=true|false`: label 'status' is not in
  MUTATING_ACTIONS, kind control; fnv_world.py:1008-1009 writes bBackground Mouse to the dev profile's Fallout.ini),
  fnv_live_tools.py:1533-1537 (`fnv_warnings action=enable`, kind control) -> fnv_warnings.py:321-326 (writes
  nvse_config.ini in the winning MO2 mod folder, which can be a user's own mod, or in overwrite).
- What happens: both answer under engine_control="read" and edit files outside the state folder. B2's fix rule
  ("writes outside the state folder needs full") covers them, but its table and the planned test rows name only
  fnv_plugin_build, fnv_install, fnv_profile_set, fnv_companion_install, the zig paths and fnv_quit.
- Fix idea: include them when B2 is fixed (rows in test_stab_gate for every control tool, as B2 says).

---

Checked and found sound (no finding): the gate itself (`_run` refuses kind act before any op; gated_kind decides from
MUTATING_ACTIONS; every listed (tool, action) answers CONTROL_DISABLED with no pipe op, every READ_CASES row passes);
conditional kinds at the call sites (fnv_debug READ_ACTIONS, fnv_script_debug, fnv_script_log, fnv_blame, fnv_camera,
fnv_hud, fnv_asset, fnv_form changes, fnv_native, fnv_code, fnv_place, fnv_render, fnv_script_coverage,
fnv_time_control, fnv_test_save, fnv_rng, fnv_time, fnv_globals, fnv_hook_set read/list, fnv_replay list,
fnv_events); fnv_actor_values names (whitelist), fnv_setting_set names (pattern), Start patterns; the read paths that
build eval or console text (resolve, formid_live, dialogue/condition/quest readers, decompile) use resolved form ids
or quoted names; fnv_wait_for leaves the gate as it found it; the shared game scratch users (raycast, ground,
detection, reaction, perks, vats) use separate offsets and raycast sends one batch.
