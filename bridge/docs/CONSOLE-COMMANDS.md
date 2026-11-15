# Console companion wire v2 - v0.4.0 feedback beta

The command companion uses public xNVSE APIs on the game loop, with no keyboard
injection. It is separate from read-only telemetry. Tools: console_readiness,
console_setup, console_execute, console_query, console_status, console_release.
Bridge discovery now contains 44 tools.

## Explicit setup and readiness

`geck_console_readiness(expected_plugin_hash=HASH)` installs nothing. It reports
`not_installed`, `restart_needed`, `waiting_for_loaded_save`, `waiting_for_loading`,
`busy` or `ready`, exact paths/backups and command restrictions before dispatch.
`started` is a decimal STRING: preserve it exactly; never convert it to a JS Number.
The uint64 wire field remains exact. A v1 DLL is refused before queuing v2 work.

One explicit upgrade/build/install/restart is required for wire v2. Thereafter,
commands can name any already loaded plugin without another per-plugin DLL.
The anchor configured ESP and hash remain the companion's installation identity.

Use `geck_console_setup` with a unique request_key for each intended setup action:
- mode=preflight: expected_plugin_hash; previews requirements/files/restrictions.
- mode=build: also zig=ABSOLUTE_LOCAL_ZIG_PATH (checked Zig 0.14.1, no download).
  Returns build_receipt path and source/build/DLL fingerprints.
- mode=install: build_receipt=RETURNED_OWNED_BUILD_JSON. FNV must be normally
  closed, prior operations reconciled, modified/foreign DLLs refused, backups
  retained. Restart through xNVSE and load a save; require ready=true.

CLI parity: console_commands.py --project PROFILE preflight/build/install/readiness,
execute/query/status/release. Use --help for exact options. Setup does not change
MCP registration or install xNVSE itself; diagnostic xNVSE is not a mod dependency.

## Cross-plugin forms

Pass `binding` with an explicit plugin name, six-digit local_form and exact file
sha256. For example the user-requested probe operation is:

```json
{
  "command": "player.AddItemHealthPercent {form} 1 0.66",
  "intent": "Add one explicitly requested probe item at 66 percent condition",
  "binding": {"plugin":"MyProbe.esp", "local_form":"000800", "sha256":"ACTUAL_SHA256"},
  "target": "FULL_FRESH_READINESS_TARGET_OBJECT",
  "expected_plugin_hash": "ANCHOR_PLUGIN_SHA256",
  "request_key": "unique-intended-add-1",
  "verify": {"kind":"item_count", "local_form":"000800", "equals":1}
}
```

Target/hash placeholders are explanatory. Use real returned values. The example
expected count 1 is correct ONLY if a fresh baseline count was 0. Query before
adding; verify expected baseline+amount on a later loop. An acknowledgment alone
is not inventory proof, and inventory count alone is not condition proof.

Native code validates file hash/identity, IsModLoaded, live GetModIndex and
IsFormValid. It resolves {form} immediately before dispatch. A raw eight-digit
argument with the explicitly bound local suffix is rebound from its named plugin;
it never trusts a historical 0C. Other unbound prefixes remain refused.
`{prefix}` remains the configured anchor's live prefix.

A named file modified after FNV process startup is refused: disk bytes alone
cannot prove that an already running game loaded that new version. Restart after
plugin changes. File hash proofs are cached with file ID/size/write-time checks;
large masters are supported up to 2 GiB (first hash can take time). Native query
expressions are bounded/cached: at most 64 distinct expressions per game process.

## Typed numeric queries and verification

`geck_console_query(kind, intent, target, expected_plugin_hash, request_key, binding?, slot=5)`
uses the same durable claim/status workflow but issues no console line:
- plugin_index: named plugin binding; local_form may be omitted/000000.
- item_count: player.GetItemCount of the named bound item (including vanilla).
- equipped: whether that named item is equipped by the player.
- weapon_health_percent: current equipped weapon condition percentage.
- equipped_health: current equipped object's ABSOLUTE health in slot 0..19.

Results record exact subjectPlugin/subjectPrefix/subjectHash and native process/
build/context provenance. Empty/missing/failed expression output never becomes 0.
With a binding, condition queries also verify that the requested slot holds that
exact base form; otherwise they fail. Without a binding, condition reads describe
the currently equipped object; they do not select an arbitrary
instance from a stack of unequipped inventory items.

Command verification supports quest_running, quest_variable, item_count and
equipped with numeric equals. Named verification local_form must match binding.
Verification runs on a later loop. GetModIndex of the anchor retains its legacy
typed readback. Arbitrary console output text is not captured.

## Explicit single teleport

`geck_console_execute(command="coc CellEditorID", binding=CELL_BINDING, authorize_teleport=true, intent=..., ... )`
authorizes exactly one teleport. Only one simple cell EditorID is accepted.
The binding names the destination CELL's originating plugin, six-digit local
FormID and exact installed hash. The bridge verifies the CELL type, file FormID
including its master index, and exact EditorID before queueing. Bind an override
through its originating plugin. Missing/mismatched bindings fail before queueing
or context changes. This supersedes the original unbound coc workflow.

The old runtime context/baselines are invalidated; native context and epoch rotate
before the console call. No other command is admitted while it is in progress.
The companion waits for a loaded game, MenuMode 1007 off, exact destination
`(player.GetParentCell) == (BuildRef (GetModIndex "PLUGIN") LOCAL_DECIMAL)` = 1
and later stable game loops. Only then does it report a verified destination.
After status completes, request fresh console_readiness and use its NEW target.

Timeout or an uncertain engine return never triggers a retry. Inspect the original
operation; do not replay a teleport even if the old target is now rejected. A
failed destination readback is not proof that no movement happened.

Failed expression compilation reports `failedExpression` and `compileFailure` in
the native receipt, including the expression cache limit. This avoids bare CELL
EditorID lookup in the runtime expression compiler. Compile failure remains a
before-dispatch failure; destination verification is never bypassed.

## Durable outcomes and restrictions

Retain the ORIGINAL execute/query operationId and poll geck_console_status.
- pending: queued or awaiting a later game loop; no inferred execution.
- acknowledged: Console API accepted a line; effect may remain unverified.
- verified: typed query or explicit later effect/destination proof passed.
- failed: rejected before dispatch or typed query unavailable.
- outcome_unknown: retain claim and evidence, inspect; never automatically retry.

Single printable ASCII line, 512 characters maximum. Load/quit/restart/cow/batch,
multiline/semicolon chaining and arbitrary keyboard/mouse/gameplay automation are
unsupported. `coc` needs its explicit flag. Setup/readiness expose these restrictions.
Client cancellation/restart and request-key replay never resubmit claimed work.
Unknown release requires normal FNV exit and a review note; owned evidence is
archived and historical outcome remains unknown. No game is force-killed.

## Acceptance evidence

24 October wire-v2 real xNVSE test, through fresh MCP: explicit MCP setup built/
installed the disposable companion, then readiness correctly waited for a loaded
save. Anchor prefix 0D and separately bound probe prefix 0E were resolved live.
Typed count 0, AddItemHealthPercent with later count=1, EquipItem with later
equipped=1, bound weapon condition=66 and absolute health about99, then removal
count=0 all verified. One authorized coc reached the disposable cell; later
GetInCell/loading proof passed and fresh readiness had a new context/epoch.
Reusing its request key returned only the prior receipt; the old target was
refused and no new request appeared. started remained an exact decimal string.
After normal exit, activation was restored byte-for-byte and only disposable
files were archived. Existing workshop companion was preserved.

Offline coverage includes native hash/mapping/form/query rejection, timeout and
context-change paths, typed queries, teleport success/mismatch, duplicate claims,
JSON/RPC precision and setup restrictions. This is a local feedback beta, not publication.

References: [BuildRef safety](https://geckwiki.com/index.php/BuildRef),
[condition readback units](https://geckwiki.com/index.php/GetEquippedCurrentHealth).
No engine addresses or game memory patches are used.

25 October correction acceptance: exact originating CELL binding replaced bare
runtime GetInCell compilation. The owner confirmed a single teleport to the
verified interior GSDocMitchellHouse, on the floor and moving normally. Exact
cell/context/duplicate/stale checks passed. An earlier exterior Caesar CELL
spawned inside a rock: exact-cell identity is not a collision-safety guarantee.
Always check CELL classification and review the intended destination first.
