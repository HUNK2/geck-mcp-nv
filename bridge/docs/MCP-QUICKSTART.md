# GECK MCP — local static-record prototype

> Actor photos (macOS only for now): [PHOTO.md](PHOTO.md). Persistent helper: [M1-FAST-BRIDGE.md](M1-FAST-BRIDGE.md).

This is a local stdio MCP server for the 32-bit Fallout New Vegas GECK. It was developed under CrossOver on macOS and is tested end to end on Windows 11 (`launcher: native`); the Linux (`launcher: wine`) path is untested. It loads configured plugins with exactly FalloutNV.esm as master and edits configured existing STAT model fields. Record creation, cell placement, animation checks, and gameplay checks remain unsupported.

## Runtime diagnostics (29 bridge tools)

The four vanilla tools below remain available. The server now has 44 tools total:
two additional automatic snapshot/compare tools use an optional read-only xNVSE
companion. See [automatic setup and validation limits](AUTOMATIC-RUNTIME.md).

The runtime feature uses native vanilla FNV `bat` + `scof`. The user runs batches and
performs gameplay actions. It never drives the screen, runs gameplay mutations,
attaches to GECK or assumes xNVSE is available. Installed extender files are not
runtime capability evidence. Windows vanilla item-count/delta/error capture passed
on 18 October 2026; valid quest-variable/reference probes have offline coverage only.
Wine/CrossOver runtime behavior is unverified.

Use a regular project configuration for game/state paths. Runtime probes have their
own strict version-1 profile, separate from the project's STAT records. Start from
[`examples/runtime-diagnostics.json`](../examples/runtime-diagnostics.json), a fictional
profile: replace its plugin, six-digit **local** forms and quest variable names with
your own. Do not run the example unchanged. Probe kinds are `quest_variable` (`sqv`,
requires `variable`), `item_count` (player inventory), `disabled` and `dead` (persistent
reference selected with `prid`). Only targeted observations are collected; no broad
inventory dump or arbitrary console code is accepted.

Supply `runtime_mapping` as `{"prefix":"XX","source":"unknown","evidence":""}`
with XX replaced by the session's actual two hex digits. `disk_order` is also
unverified. Only `user_runtime_observation` with a description of fresh in-game
FormID evidence yields `user_attested` identity. This is a user's attestation, not
automatic proof of correct mapping. Observe a known item's full FormID in the
current console and corroborate the expected quest variables/reference. Never
reuse a historical prefix. A successful capture alone cannot establish identity.

CLI/MCP share `Service.runtime_call`, validation, locking, journal and reports:

| MCP tool | CLI subcommand | Inputs beyond `--project` |
| --- | --- | --- |
| `geck_runtime_prepare` | `prepare` | profile object / `--profile JSON`; save_label / `--save-label`; runtime_mapping object / `--runtime-mapping JSON`; expected_plugin_hash / `--expected-plugin-hash`; request_key / `--request-key`; optional preview / `--preview` |
| `geck_runtime_collect` | `collect SESSION` | checkpoint_label / `--checkpoint-label`; CLI-only `--watch-seconds 0..60` |
| `geck_runtime_report` | `report SESSION` | optional baseline_checkpoint / `--baseline-checkpoint`; sanitized / `--sanitized` |
| `geck_runtime_close` | `close SESSION` | request_key / `--request-key`; archive_helpers / `--archive-helpers`; game_exited / `--game-exited` |

From `bridge/` on Windows (replace uppercase placeholders):

```powershell
.venv\Scripts\python.exe runtime_diagnostics.py --project projects\hellowasteland.json prepare --profile PROFILE.json --runtime-mapping MAPPING.json --save-label "Disposable save, before action" --expected-plugin-hash SHA256 --request-key preview-1 --preview
# Review the exact commands and side effects, then repeat without --preview and with a NEW request key.
.venv\Scripts\python.exe runtime_diagnostics.py --project projects\hellowasteland.json collect SESSION --checkpoint-label before --watch-seconds 30
.venv\Scripts\python.exe runtime_diagnostics.py --project projects\hellowasteland.json report SESSION --baseline-checkpoint before
.venv\Scripts\python.exe runtime_diagnostics.py --project projects\hellowasteland.json close SESSION --request-key close-1
```

Preparation hashes the **profile's installed plugin** in `Data`, checks the supplied
SHA-256 before/after, and returns unique filenames, commands, hashes (in the manifest)
and exact `bat` instructions. New batches use short `gd_<8 hex digits>` stems for
manual entry; the manifest retains the full session ID. It writes exclusive-create CRLF ASCII helpers and never
overwrites existing files. `--preview` writes no helpers or manifest; the operation
itself is journaled. Reusing a request key with identical arguments replays the original
result; different arguments conflict. After interruption inspect `geck_operation_get`
and the local manifest before retrying with a new key. Partial preparation is not runnable.

Run `start` **once**, collect the baseline, close the console and perform the deliberate
action, then run `sample` once and collect a new explicit label. Collect each checkpoint
before running another batch; multiple unlabelled cycles are ambiguous and require a
new session. Never rerun start on an existing log: SCOF can truncate it. Every new save,
load, plugin build or runtime load order requires a **new session and baseline**.
Vanilla cannot automatically detect all save changes. Batches are flattened (no nested
`bat`), redirect console logging, and change the selected reference, ending on player
`00000014`. Do not overlap another console logger. Sequential queries are not atomic;
same-frame readback cannot verify a gameplay mutation.

Collection returns promptly: `LOG_MISSING`, `LOG_LOCKED` and `NO_FRESH_SAMPLE` mean
`awaiting_output`. It retries locked reads three times, preserves incomplete lines,
binds responses to the exact ordered echoed commands/forms, and retains source line
numbers/raw lines. Unrelated commands break an unfinished sample; command echoes are
never values. Missing values remain missing. An unexpected form cannot satisfy a probe.
`LOG_REPLACED` invalidates continuity (including same-size rewrites); `LOG_TOO_LARGE`
limits a session to 16 MiB; `AMBIGUOUS_CHECKPOINT` refuses to invent action labels.
Console errors remain attached to their cycle; unrelated errors are separately visible.

`state_dir/runtime/SESSION/` holds `manifest.json` (identity, save label, timestamps,
mapping evidence, exact owned paths and file hashes), `console.log`, `report.json` and
`report.md`. Reports can be replayed without FNV running; an absent/changed installed
plugin lowers current identity confidence. The newest checkpoint is compared to the
first by default or a named baseline. A report refers to its labelled checkpoint;
replaying it is not a fresh capture. Capture quality (`complete`, `partial`, `errored`)
and assertion results (`pass`, `fail`, `inconclusive`) are separate. No rules means
inconclusive. Unverified identity, missing/errored observations and missing baselines
cannot pass assertions. A `pass` describes only the declared diagnostic rules.

Rules are versioned data, never Python/eval: `equals` requires `value`; `range` uses
inclusive `minimum`/`maximum`; `count_limit` requires nonnegative `maximum`;
`mutually_exclusive` permits at most one nonzero field; `unchanged` requires a separate
baseline checkpoint with equal values. All rule fields name configured probe keys.
Reports show before/after/delta and each rule's result. CLI exits: 0 completed operation
or passing assertions, 1 operation/capture error, 2 awaiting/partial/inconclusive, 3
assertion failure. Runtime journal outcomes are `runtime_prepared`, `runtime_collected` and
`runtime_closed`; awaiting/preview/report operations use `unchanged`. These are local
diagnostic outcomes and do not create editor recovery barriers. An MCP envelope
`ok=true` means the operation ran; inspect capture
and assertions for its diagnostic result.

Closing stops **bridge collection only**. It does not send the invalid `scof ""`, stop
the game logger or claim logger shutdown. Normal FNV exit is the verified SCOF shutdown.
After normal exit, optional archival requires `game_exited=true`; it moves only
manifest-owned, unchanged helper batches into the session archive, preserving modified
helpers, unrelated files, raw logs and evidence. Nothing is deleted.

Reports stay local by default. `report --sanitized` / `sanitized=true` explicitly writes
`report-sanitized.json` using an allowlist that omits raw lines, mapping evidence, labels,
paths and session IDs. It still contains diagnostic field/rule names and numeric changes;
review those before sharing. Never commit raw player logs, saves or private profiles.

## Helper transport

By default the server keeps one persistent helper (`Probe.Mcp.exe serve`) and falls back to one Wine launch per call if the handshake fails. Set `"transport": "oneshot"` in the project JSON or `GECK_MCP_TRANSPORT=oneshot` to force the legacy path. `geck_status` reports the active mode under `bridgeTransport`. See [M1-FAST-BRIDGE.md](M1-FAST-BRIDGE.md).

## Start the server

From this directory:

```sh
.venv/bin/python run_mcp.py --project projects/mcp-fixture.json
```

The server communicates over stdin/stdout; it is intended to be launched by an MCP client. Diagnostic output belongs on stderr. No network listener is opened. The server does not load a plugin or edit GECK at startup; it creates/opens its local operation journal.

A generic stdio client configuration uses:

```json
{
  "mcpServers": {
    "geck": {
      "command": "/path/to/bridge/.venv/bin/python",
      "args": [
        "/path/to/bridge/run_mcp.py",
        "--project",
        "/path/to/bridge/projects/hellowasteland.json"
      ]
    }
  }
}
```

On Windows use `.venv\Scripts\python.exe` and Windows paths.

### Code changes reload by themselves

`run_mcp.py` is a small supervisor (`geck_mcp/supervisor.py`) that runs the server as a child process. When a file
under `geck_mcp/` or the project JSON changes, it waits until no call is running, starts a new server with the new
code, swaps it in and tells the client the tool list changed: `notifications/tools/list_changed` for clients that
used the `initialize` handshake (the `initialize` result declares `tools.listChanged`), a change event on each
`subscriptions/listen` stream for 2026-07-28 clients. A client that honours the notice lists the tools again, so
new tools, arguments and descriptions arrive without a reconnect (the MCP SDK client does; for the Claude app see
FNV-LIVE-TOOLS.md, "Live checks of the leftovers"). The code itself is new on the next call in any client. Other
behaviour:

- A change that does not start (a syntax error) leaves the old server serving; the traceback is in the MCP log
  (stderr), and the reload is tried again on the next save.
- A server process that exits during a call (a crash, a kill) fails only the calls it had running, with an error
  that says so; the next call starts a new one. Killing the child to free the game's companion pipe still works.
- A reload loses what the server held in memory (the same as any restart): a debugger stop, an operation being
  polled. Reloads happen only between calls.
- `--no-reload` (or `GECK_MCP_RELOAD=0`) serves in one process, as before. Changing the client's arguments, or
  `run_mcp.py` / `supervisor.py` themselves, still needs the client to restart the server.

### Session argument (0.2.0)

Tools that act on the running GECK take an optional `editor_session` (the `sessionId` from `geck_status`). Leave it out and the bridge uses the GECK that is running now; the expected title, plugin hash and model arguments still guard every edit. `session_id` is still accepted as an old alias, but the Claude desktop device proxy (Cowork) drops arguments named `session_id`, so use `editor_session` or nothing there.

## Project configuration

Copy a project JSON and set the launcher, plugin, game root, wine/bottle (not needed for `native`), helper and configured STAT identities. Relative paths resolve against the JSON file. Use a shared `state_dir` for all profiles targeting the same bottle so they share recovery barriers as well as the bottle lock. Do not change the project ID or state directory to bypass an unresolved operation.

The `projects/example-*.json` files show one profile per launcher (`crossover`, `wine`, `native`). Copy one to `projects/<project_id>.json` and edit the paths.

Dependencies are pinned in `requirements.lock.txt`; the current environment uses Python 3.12 and MCP SDK 2.2.0. On another installation, create a Python 3.12 virtual environment and install that lock file. Update absolute installation paths in the profiles.

## Normal workflow

1. Obtain `geck_status` and a disk `geck_record_read`. Call `geck_plugin_load` with the exact editor title, disk plugin hash, and fresh request key (session optional). The editor must be clean. Poll `geck_plugin_load_status` using the returned operation ID until `loaded_verified` or `loaded_unverified`; `loading` means only dispatch succeeded. `loaded_unverified` means GECK shows the plugin but the cell check could not run (for example a hidden Cell View): call `geck_show_windows`, then `geck_cell_verify`. Use `reload: true` only when intentionally reloading an already active plugin; a reload is only reported done after GECK was seen reloading (or 45 s passed), because the title names the plugin before and after.
2. Call `geck_status`, then `geck_project_attach` (optionally with the returned `sessionId`).
3. Call `geck_record_read` with `source: disk` for the persisted record and plugin hash, then `source: editor` with the session for the current model field.
4. Call `geck_record_set_model` with the expected old model, expected plugin hash, session and a fresh request key. A successful edit is committed in GECK memory, not yet saved.
5. Open/close the identified preview. Preview existence is not visual verification.
6. Call `geck_plugin_save` with expected models, expected disk hash, session, another request key, and `save_entire_active_plugin: true`. This saves all pending edits in the active plugin.
7. Call `geck_plugin_validate` to independently verify supported disk fields.

Repeated identical mutation requests using the same request key return the original receipt. That receipt describes historical execution, not current editor state. A changed request using the same key is rejected. If a request was interrupted, the replay is an error with `outcome_unknown`; it is never dispatched again automatically.

## Diagnostics and recovery

Use `geck_inspect_windows` for a bounded read-only inventory, including modal text. It is available while the recovery barrier is active. Use `geck_operation_get` or `geck://operations/{operation_id}` for persisted step results and failures (`journalState`, `recoveryAck`).

`geck_status` reports `windowsVisible` (Object Window, Cell View, Render Window) and `renderWindowReachable`. On native Windows GECK sometimes leaves these three windows hidden (after a load, or when it was started without user input). `geck_plugin_load_status` and `geck_cell_verify` show them again automatically; `geck_show_windows` does it on request (ShowWindow without activation, no clicks; refused while GECK is busy or a modal dialog is open).

An unknown operation outcome blocks further editor work. Inspect the editor, disk and operation evidence, resolve any dialogs deliberately, then call `geck_recovery_acknowledge` with the current plugin hash and an explanatory review note. Acknowledgment does not roll back or certify the original operation.

A barrier whose GECK session is gone (GECK was closed, crashed or killed and started again) is released automatically by the next editor call: its journal state becomes `stale` and the result lists it under `releasedStaleOperations`. From a shell:

```sh
python recover.py --project projects/my_mod.json --list            # unresolved operations, their session, the current session
python recover.py --project projects/my_mod.json --release-stale   # release barriers from earlier GECK sessions now
python recover.py --project projects/my_mod.json --ack <id> --note "what you checked"   # add --offline if GECK is not running
```

A load whose Data dialog step fails before OK (for example a checkbox that does not toggle) cancels the dialog and reports `failed_before_change`, so it leaves no barrier.

The lock coordinates cooperating bridge callers; it cannot prevent manual edits. Existing record dialogs block automated workflows. The bridge checks session, plugin, configured record identity and expected values, but live identity is currently established by the disk FormID plus the exact EditorID in a scoped Static dialog. Full live provenance/load-order verification remains future work.

## Tests

```sh
PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m unittest discover -s . -p test_esp_check.py -v
```

A live end-to-end smoke test against a disposable fixture plugin exists in the author's development tree but is not shipped in v0.1 (it depends on private test assets).

## Loading and recovery

The MCP helper reads GECK's actual list image indices for selected files and verifies the Active File column before dispatch. This avoids both false checkbox readbacks and toggling an already active file off. Only the rebuilt `Probe.Mcp.exe` is covered; the retained legacy `Probe.exe` is not updated. Rebuild with `.venv/bin/python build_helper.py` after changing C# source.

Pending loads block other editor workflows while status and inspection remain available. After an interrupted load, inspect current editor and disk state before recovery acknowledgment. A load can be acknowledged in a clean empty restarted editor; subsequent polling of an acknowledged load is rejected. Acknowledgment never certifies success.

GECK can take several minutes to load the master in this bottle. Transient control timeouts during loading are not evidence of a missing plugin or a corrupt mesh. Recheck status after loading; do not repeat mutations on timeout.

On native Windows the Data list checkboxes are toggled with posted double-clicks; the real mouse cursor is not used, so you can keep working while the bridge loads a plugin (just don't click inside GECK). Each toggle is read back and retried up to 3 times.


## Resumable compilation (0.4.0 candidate)

1. Build/install/load exact bytes through the bridge. CLI: `build_plugin.py --project
   PROFILE`, guarded `install_plugin.py`, then `live_load.py --project PROFILE`.
2. Call `geck_compile_start(request_key=UNIQUE_KEY)` and retain its operationId.
3. Call `geck_compile_resume(operation_id=ID)` until verified. For blocked/uncertain
   outcomes, read `geck_compile_status` / `geck_operation_get` and its next action.
   CLI: `compile_scripts.py --project PROFILE --operation-id ID`.

`ok` means a bounded service call completed, not compilation. Pending is incomplete;
blocked requires review; outcome_unknown requires reconciliation after dispatch;
failed marks a released operation. Only verified sets compiled=true after saved
SCPT, cache, rebuild, record/override roundtrip and install checks. Load verification
is independent: a Cell View failure cannot erase saved-script proof. Per-script
saved hashes survive restart after revalidation; provisional UI handles do not.

Do not start a second compile after timeout. Unknown/unreadable dialogs remain.
A recognized save-current-script prompt requires native owner/script/source/button
checks. Review before recovery acknowledgment: releasing compilation requires clean
editor/current disk hash and prevents resumption of that operation. Use
`geck_show_windows` for root/pane/owned-modal restoration; read-only status does
not take focus. `geck_editor_close` requests normal matching clean-session close;
restart explicitly afterward. No automatic force-kill.

## Adapter readiness and explicit setup (0.4.0 candidate)

Call `geck_runtime_readiness(profile=PROFILE, expected_plugin_hash=HASH,
adapter="xnvse")` before automatic collection. It separates configured, installed,
live, fresh, correct process/build and verified mapping. Automatic support is one
quest's unique quest_variable probes; mixed profiles list unsupported probes and
constraint errors, with the manual geck_runtime_prepare route. Vanilla supports
quest variables, item counts, disabled and dead; the user executes batches.

`geck_runtime_setup_preflight(profile=PROFILE, zig_path=OPTIONAL_PATH)` previews
prerequisites/version, exact DLL/telemetry/receipt/backup paths, build identity and
restart/verification steps. It never builds/downloads/installs. CLI preflight:
`runtime_companion.py --project PROFILE preflight --profile RUNTIME_JSON`.
Explicit setup remains build with --profile/--zig, then install with a checked
build receipt/plugin hash and FNV normally closed. Start through xNVSE and require
fresh readiness/snapshot; installed files are insufficient.

Use `geck_runtime_save_context(label=LABEL)` when the save context explicitly
changes: older samples/mappings/baselines are rejected until fresh observation.
Process/load epoch/plugin/companion/profile changes also invalidate mapping.
Automatic detection of every save-context change is not implemented. Current
prefix is shown with live process/build/sequence evidence or unverified; historical
prefixes and file-local IDs are never runtime proof. Console execution is available through the separate opt-in console companion below;
keyboard injection and general autonomous gameplay remain unsupported.

## Owner-requested console execution (23 October)

The v0.4.0 candidate now includes separate opt-in `geck_console_readiness`,
`geck_console_execute`, `geck_console_status` and `geck_console_release` tools.
Read-only telemetry is unchanged. See [console setup, commands and outcome semantics](CONSOLE-COMMANDS.md).
Real FNV acceptance is pending; this candidate is not release-ready.
