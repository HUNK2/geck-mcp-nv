# Changelog

## fnv-mcp 0.5.0b1 (0.5.0 beta 1) - unreleased

The first version of the fork. The entries below this one are geck-mcp-nv's own history, by 2Shay.

- **The fnv_* tools** (165): engine knowledge from the fnv_decomp decompilation (functions, classes, settings,
  commands, a symbol database with source lines and named locals), a crash doctor, game control and scenario
  tests, live inspection, full control of the running game through the engine companion (time control, a
  debugger, hooks, C modules, decomp functions recompiled and swapped in, xNVSE plugin export), profiling,
  coverage and whole-plugin checks. `fnv_guide` has step-by-step guides.
- **Safety:** two levels, `fnv.engine_control` `read` (the default) and `full`; anything that changes the game or
  puts code or files into it or its mod profile needs `full`. Paths from tool arguments stay inside the project's
  folders; package names are plain names; `fnv_quit` closes only the game `fnv_launch` started; tools never take a
  compiler path.
- **No private data needed:** the watch hooks find their functions by address, so an engine map built without
  the fnvsym proof table is enough; the `fnv_name_*` tools are an optional extra that needs `FNVSYM_ROOT`.
- **A project file for the game alone** (`projects/example-fnv.json`): the GECK settings are optional and the
  `geck_*` tools say `NOT_CONFIGURED` without them. An invalid project file stops the server with one line.
- **One setup guide** (`bridge/docs/FNV-MCP-SETUP.md`) with a settings table; `fnv_setup_check` covers the fnv
  block, the symbol database, the proof folder, capstone, the helper and a large-address-aware exe.
- **Windows only.** The CrossOver and Wine launchers, the photo studio and `geck_actor_photo` are removed.
- **Legacy toolset:** the `geck_runtime_*` and `geck_console_*` tools (15) are off by default (`legacy_tools` or
  `--legacy`); the `fnv_*` tools do these jobs.
- **Removed:** the second server under `windows/`, the sprite tools and the PyFFI extra, tracked binaries, the
  upstream working notes, and three examples that needed game files the repository cannot ship.
- **Name and version:** the server announces itself as `fnv-mcp` 0.5.0b1 (the Python package stays `geck_mcp`).
  Environment variables have one prefix, `FNV_MCP_` (`FNV_MCP_ZIG`, `FNV_MCP_PIPE`, `FNV_MCP_RELOAD`,
  `FNV_MCP_SUPERVISOR_LOG`, `FNV_MCP_TRANSPORT`); the old names keep working.
- **Long calls and large answers:** a long tool call sends an MCP progress notification every 5 seconds; an
  answer over 40,000 characters is cut at a row boundary with `truncated: true` and names its paging argument; the
  hot-reload supervisor is opt-in (`--reload`).
- **The companion lets go:** when the bridge disconnects, the frame gate, held input and stopped threads are
  released at once instead of after two minutes.
- **Clearer tool listings:** every parameter of the tools with six or more parameters has a description;
  positions and rotations are `[x, y, z]` everywhere (`fnv_move` and `fnv_raycast` still take the old object);
  tools have display titles; every failure carries an error code; the server's first advice is `fnv_status`, and
  `GECK_NOT_RUNNING` says how to start the GECK.
- **Hardening:** `fnv_run_script` refuses a quit anywhere in the script; the bridge checks that the companion
  pipe belongs to the game (`PIPE_NOT_GAME`); decompression is capped at 256 MiB; `fnv_func_find` regex searches
  refuse nested repeats and stop after 20 seconds; Windows tools are run from the system folder; a cached engine
  build is reused only when its digests match.
- **Documents:** the scenario file format, an uninstall section in the setup guide, `SECURITY.md`,
  `CONTRIBUTING.md` and third-party notices; `fnv_plugin_build`, like `fnv_code`, says a DLL built from decompiled
  code is for local use; `fnv_guide engine` searches the shipped guides when there is no research folder.

## 0.4.0 beta 1 - 25 October 2026

25 October teleport feedback correction: coc now requires an originating CELL
binding matching its EditorID on disk. Native destination proof compares the
player's exact parent cell with a live-index BuildRef, avoiding bare runtime
EditorID expression compilation. Receipts expose failed expressions/reasons;
missing/wrong bindings fail before queue/context changes. Existing no-replay,
loading and fresh-target gates remain. Real interior teleport passed with owner
confirmation of usable landing/normal movement; exterior COC landing safety is
not guaranteed. Larger GECK and controlled Shadow gates remain open.

Console feedback upgrade, 24 October: wire-v2 decimal-string process identity;
explicit named-plugin/local-form/hash binding resolved at dispatch; opt-in single
coc with context rotation and later destination/loading proof; typed item count,
equipped and condition queries; explicit MCP setup and readiness states. Tool
count is 44. One companion upgrade/restart required, no per-target-plugin DLL.
Existing no-replay/stale-target guards retained. Real v2 acceptance passed: separate
plugin item count 0/1/0, equipped condition 66%, coc destination and fresh context.

Owner scope addition, 23 October: separate opt-in xNVSE console actuator, four
CLI/MCP readiness/execute/status/release tools (initially 42; now 44 with v2), process/build/
context-bound requests, durable no-replay claims, typed GetModIndex readback and
optional later quest effect verification. Telemetry stays read-only. Console load/
quit/batch commands and arbitrary console-text capture remain unsupported;
explicit coc is supported by the v2 workflow above;
real FNV console smoke acceptance passed (lookup and later-loop quest start/stop).
Broader GECK/Shadow gates remain pending. Earlier console-actuator exclusion is superseded.

- Save reconciliation no longer waits for a script title asterisk to disappear.
  Checkpoint close rechecks EDID/full-source hash and plugin mutation; hidden
  retained editor HWNDs count as closed. Normal Exit preserves prompts and
  saved source/bytecode proof remains mandatory. Offline verified; live retest pending.
- Beta feedback: narrow PlayerRef validation, compression-aware semantic roundtrip,
  exact canonical-field review that retains saved bytes, transient-safe resumable
  load client, persistent cross-profile load ownership and bounded context checks.
  NPC_ uses the existing generic template/field writer without a process-local adapter.
  Status identifies startup Python source and actual helper binary independently.
- Shared CLI/MCP compile start/status/resume, dispatch journal and per-editor
  reservations across clients/profiles; UI progress never means compiled.
- Full RichEdit source/hash checks, guarded recognized prompts and verified
  close/save boundaries. Atomic cache updates require saved source, bytecode/header,
  reference and semantic-variable proof/provenance. Legacy unverifiable caches recompile.
- Loaded/installed/built identities, stale-save refusal and conservative final
  record/override roundtrip checks. Unsupported override loss blocks installation.
- Root/pane/modal restoration, normal clean-session close, native API/thread/window
  evidence and bounded native list search.
- Adapter readiness, unsupported-probe/manual routing, explicit setup preflight
  and save-context baseline invalidation. No console actuator.
- Explicit local working-tree candidate packaging embeds releaseReady=false.
  Live gates remain required; this candidate is not a published release.


## 0.3.0: runtime diagnostics (2026-10-19)

- Add optional read-only xNVSE companion with generated single-quest profiles,
  guarded install/build binding, runtime-resolved load index, process/module/load
  identity and strict fresh-sample checks. Add shared CLI and two automatic MCP
  tools (31 total). No console typing, screen control or arbitrary command endpoint.
- Native fake-host tests exercise the generated 32-bit DLL and failure/lifecycle
  cases; Windows CI builds with hash-verified Zig. The precursor passed live
  diagnostics/fix regression; the generalized adapter passed main-server MCP/CLI
  in-game acceptance on Windows/xNVSE 6.4.9 on 19 October.

- Add shared runtime diagnostics core, CLI and four MCP tools (29 total): prepare,
  collect, report and close. Vanilla batches are user-run; no screen control or
  assumed extender capability. Versioned profiles support quest variables, targeted
  inventory counts and persistent-reference disabled/dead flags with per-session mapping.
- Record explicit save/checkpoint labels, plugin/file hashes, mapping evidence and raw
  line provenance. Detect missing/partial/locked/rewritten logs; echoes never become
  observations. Compare checkpoints and evaluate declarative assertions separately
  from capture quality and user-attested identity.
- Add separate runtime locks/journal namespace, idempotent prepare/close, local
  JSON/Markdown reports, explicit sanitized export and ownership-safe helper archival.
  Closing collection does not stop SCOF. Windows vanilla item-count/delta/error acceptance passed; quest-variable and reference probes have offline coverage only.

## 0.2.1: shared bridge and final live acceptance (2026-10-06)

- Final native Windows 11 / vanilla GECK acceptance completed on 6 October: direct MCP load/reload/cell/render,
  stdio MCP preview and guarded save, actual script compilation through the CLI, and successful load/render after
  a clean GECK restart. Salvatore and game plugin activation list were unchanged. See docs/TESTING.md for evidence
  and the covered-window capture retry.

- Bridge 0.2.1: port the workshop sprite tool's `--collision-static` option and collision motion readback so
  the workshop can depend on this implementation without losing its unmovable defeated sprite. Fixed collision
  uses static layer, motion system 7, fixed quality, zero mass/inertia and clears the dynamic BSX flag.
  Existing dynamic collision behavior and deterministic hash-seed handling are preserved.

- Document file-local versus runtime FormIDs for game console commands. The Salvatore main-quest lookup failure
  was resolved on the unchanged bridge-built plugin by using runtime index `0A` instead of file-local `01`;
  `sqv 0A00080F` and `startquest 0A000818` were verified in game by 2Shay. No plugin writer change was required.

## 0.2.0: Windows fixes from the Salvatore build (2026-10-05)
### bridge/
- **MCP tools work through the Claude desktop device proxy (Cowork).** The proxy drops tool arguments named
  `session_id` (confirmed: a direct stdio client delivers it, the proxy forwarded `{}`), so every tool that required
  it failed with `session_id Field required`. The argument is now `editor_session` and optional on every tool; when it
  is left out the running GECK session is used, and the expected title / plugin hash / model guards still apply.
  `session_id` stays accepted as an alias.
- **Hidden GECK windows.** On native Windows GECK can leave Object Window, Cell View and Render Window hidden (after a
  load, or when GECK starts without user input), which failed load verification with `Cell View selector matched 0
  windows`. The helper has a new `windows.show` operation (ShowWindow without activation, no clicks; refused while GECK
  is busy or modal). `geck_plugin_load_status` and `geck_cell_verify` use it, and the new **`geck_show_windows`** tool
  exposes it. `geck_status` reports `windowsVisible` and `renderWindowReachable`.
- **`loaded_unverified`.** When the plugin is clearly loaded (title, responsive editor) but the check itself cannot run,
  the load ends `loaded_unverified` instead of failing and leaving a barrier.
- **Stale recovery barriers are released.** An uncertain operation from an earlier GECK session (GECK closed, crashed or
  killed, then restarted) no longer blocks editor work: the next editor call marks it `stale` and reports it under
  `releasedStaleOperations`. New CLI `recover.py --list / --release-stale / --ack ID --note ... [--offline]`.
- **Loads no longer use the mouse.** 0.1.1 parked the real cursor over each Data list row for up to 2 s, so a load
  failed when the user moved the mouse or was away. Posted double-clicks toggle the rows without the cursor (tested
  with the cursor elsewhere and while the mouse was in use); each toggle is read back and retried up to 3 times.
- **Failed loads clean up.** If the Data dialog step fails before OK (e.g. a checkbox that does not toggle), the dialog
  is cancelled and the load is `failed_before_change` (no barrier). `data.state` and `data.cancel` no longer need a
  dummy argument in the helper.
- **Reload completion.** Reloading the plugin GECK already shows was reported done before GECK even started reloading
  (the title names the plugin before and after). A reload is now done only after the reload was seen, or after 45 s.
- **Templates and indexes follow the profile.** Record templates and master indexes are read from the profile's
  `state_dir` first, then from `bridge/state` (job 038 failed with `no template dump` although the profile had it).
  `build_plugin.py` gains `--project`; its default output is `<state>/../build`, the folder the MCP tools use.
  `cell_verify.py` reads the receipt from that folder too.
- **`compile_scripts.py`** loads with `live_load.py --if-needed`: no reload when this GECK session already loaded the same
  plugin bytes; a load whose plugin is active but whose cell check failed still counts.
- **`geck_render_capture` on Windows** (was macOS only): PrintWindow in GECK's own DPI context (the image is the size GECK
  renders and reports), retried with a repaint request when it returns only the grey background; then a DPI-aware
  screen copy if no other window covers the Render Window. A flat (blank/black) result fails with `CAPTURE_BLANK`.
- `ALCH` (aid items) added to the generic record types.
- **Sprite tools.** `sprite_flipbook.py --collision-material organic` (a name or a number) replaces the Havok material
  of the borrowed vanilla collision, which sets the sound when the sprite is knocked about (ported from the Salvatore
  workshop). `sprite_flipbook.py` and `tools/creature_skeleton.py` re-run themselves with `PYTHONHASHSEED=0`, so the
  same input gives the same NIF/KF bytes (PyFFI writes string tables in set order; two hash seeds gave two different
  meshes). `creature_skeleton.py` finds the game Data folder from `--data`, `--project`, `$FNV_DATA` or the usual Steam
  folder (was a hard-coded macOS path). PyFFI 2.2.3 and setuptools 84.0.0 (PyFFI imports distutils, removed in
  Python 3.12) are in `requirements.lock.txt`, so the sprite tests run in CI.
- Unit tests pass on Windows (fixtures no longer assume POSIX/CrossOver paths; Wine launcher tests skip on Windows);
  CI runs them on windows-latest too.

## 0.1.1: first Windows-tested release (2026-10-03)
### bridge/
- Fixed on native Windows: plugin load failed with `MODAL_BLOCKED` because GECK's Data dialog stayed hidden; the helper
  now shows it.
- Fixed on native Windows: Data list checkboxes did not toggle (and a sent click could stall GECK until the mouse moved);
  the helper now posts the double-click with the cursor parked over the row. Wine/CrossOver behaviour is unchanged.
- First Windows 11 end-to-end run (status, build, install, live load, cell verify): see `docs/TESTING.md`.
- The Nexus download is source only (no `.exe`): build `Probe.Mcp.exe` with `build_helper.py`. GitHub keeps the prebuilt helper.

## 0.1.0: WIP prototype (2026-10-02)
First public release, as a work-in-progress prototype.

### bridge/
- Offline spec-to-plugin builder: codec, stable FormIDs, `@EditorID` references to indexed masters,
  template-cloned records, validation, install with backups.
- GECK control through `Probe.Mcp.exe`: status, plugin load, cell verification, record model edits, save,
  script compilation with GECK's compiler, and actor photos (macOS).
- New for the release:
  - `launcher` profile setting (`crossover` / `wine` / `native`) for Linux and Windows
  - a cross-platform lock
  - example profiles and the vanilla `hello-wasteland` spec
  - author paths removed
- Tested end to end only on macOS + CrossOver with vanilla GECK.

### windows/
- Fallout: New Vegas port of GECK MCP for Fallout 3 (maksimka2432fr23, MIT): FNV paths, NVSE loader,
  FalloutNV plugins.txt, works with MCP SDK 1.x and 2.x. Unit tests pass. Not yet run against the FNV GECK.

### Known gaps
- No Windows or Linux end-to-end run yet. GECK Extender is untested.
- The bridge loads FalloutNV.esm only (no DLC masters).
- Photos are macOS-only.
