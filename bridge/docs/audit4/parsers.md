# PLAN-3 exit gate, round-4 audit: file parsers, record data, plugin writing

Auditor scope (same as round 3): geck_mcp/esp/ (codec, validate, records, build, generic, dialogue, formids,
assets; scripts.py and diff.py read for context), fnv_data.py, fnv_records.py, fnv_save.py, fnv_naming.py,
authoring.py. Focus: the round-3 fixes X1..X3 and the batch reviews touching this area (d7ad2a7, 72b9af4 = review
I3; 3e940ba; dd16e89; 203e556 = review I2, INDEX_VERSION 3), and what round 3 missed. Read-only; findings checked
against the code at a0a74d0 and, where marked, with small offline snippets run with bridge\.venv (scratch folder
%TEMP%\audit4-parsers, removed). The working tree's uncommitted fnv_create.py change is outside this scope.

Counts: high 0, medium 0, low 4.

---

## HIGH

None.

---

## MEDIUM

None.

---

## LOW

### L1. X3 fix does not cover plugin names with non-ASCII letters: SQLite `lower()` folds only ASCII
- **Where:** geck_mcp/fnv_data.py:202 (`select id from files where lower(name) = ? and path != ?`, bound with
  `path.name.lower()`).
- **What happens:** Python's `str.lower()` folds every letter, SQLite's built-in `lower()` only A-Z. For
  `Ärger.esp` the stored name stays `Ärger.esp` under SQL `lower()`, the parameter is `ärger.esp`, so no row
  matches and the other copy is never deleted. Steps: index `A\Ärger.esp` (EDID CopyA), then `B\Ärger.esp` (EDID
  CopyBB) with one RecordIndex. Result (run with the venv): both copies' rows stay under owner `ärger.esp`, and
  the `_form`-style query (`owner = ? and local = ? limit 1`) returns `CopyA`, the copy that is not in use. The
  same steps with `Plain.esp` leave only the B rows, as intended.
- **Why it matters:** for such plugins the X3 symptom comes back unchanged (stale EditorIDs and types in
  record_show forms, record_users, conflicts, the save namer; refs `known` built from both copies). Only plugin
  names with non-ASCII letters are affected.
- **Fix idea:** compare in Python (select id, name and filter by `name.lower()`), or store a Python-lowered
  `name_key` column and match on that.

### L2. X3 handles only "two copies of one name"; a plugin that left the load order keeps its rows, and unfiltered lookups still serve them
- **Where:** geck_mcp/fnv_data.py:197-209 (rows are deleted only on a rebuild, for the same path or the same name);
  unfiltered lookups at fnv_data.py:449 (`_form`), fnv_records.py:257-261 (user names, `names.setdefault`),
  :288 (target type/EditorID), :352 (conflict EditorIDs), fnv_save.py:423 (`Namer.edid`). Round 3 M2 was titled
  "a plugin path that left the load order"; the fix (BUG-LOG X3 details) covers only the two-copies case.
- **What happens:** a mod that overrides vanilla records and renames them (EDID/FULL) is indexed while active.
  The user disables it (fnv_profile_set or MO2). `_active` never touches its `files` row again, so its records
  stay, stored under owner `falloutnv.esm` (overrides keep the master's owner). Then fnv_record_users of a form
  that the vanilla record references lists the user record with the disabled mod's EditorID and name whenever
  SQLite returns that row first (`names.setdefault` takes the first row of any file), and fnv_conflicts /
  `Namer.edid` / the record_users target line can show the same stale EditorID. With an active plugin whose
  master was disabled (missing master), `_form` and refs `known` also accept forms that exist only in the
  disabled plugin.
- **Why it matters:** labels only (types and form ids stay right, and lookups that filter by `file in (...)`
  are correct); found by code reading, not run.
- **Fix idea:** filter these lookups by the active file ids (as `_edid_lookup` and record_find do), or drop
  `files` rows whose path is not in the current active list at the end of `_active`.

### L3. fnv_name_export can replace fnvsym's own Frida trace: "replaces only an earlier trace" accepts any fnvsym-trace format
- **Where:** geck_mcp/fnv_naming.py:463-469 (`str(old.get('format', '')).startswith('fnvsym-trace/')`), write at
  :571-572 (`path.write_text`, not atomic); registration fnv_live_tools.py:1452 (`annotations=control`, which is
  `destructiveHint=False`, fnv_tools.py:931).
- **What happens:** `C:\DecompModGithub\fnvsym\traces\fnv.json` is fnvsym's Frida trace (`"format":
  "fnvsym-trace/1"`, 3.2 MB, from a recorded run; it is not an fnv-mcp export). `fnv_name_export(path=
  "C:\\DecompModGithub\\fnvsym\\traces\\fnv.json")` passes every check (a .json under fnvsym/traces, an existing
  file whose format starts with `fnvsym-trace/`) and the file is truncated and rewritten with the fnvsym-trace/2
  export. The tool's own description says the export is "a superset of fnvsym's Frida trace", which invites
  exactly this path. A crash during `write_text` also leaves a half-written trace.
- **Why it matters:** loss of recorded evidence in the user's decomp tree; it needs an agent-named path (the
  default name `fnvmcp-YYYYMMDD.json` cannot collide with it), so low. X1's fix is otherwise sound: resolved path,
  .json suffix, symlinks and junctions out of the roots are refused, state folder itself is excluded (review I3).
- **Fix idea:** replace only a file whose `tool` field is fnv-mcp's (`format == 'fnvsym-trace/2'` and `tool`
  starting with `fnv-mcp`), write with atomic_write, and mark the tool destructiveHint=True (it can replace a file).

### L4. One unreadable plugin in the active load order makes every index-based record tool fail; a header shorter than 8 bytes also breaks fnv_loadorder
- **Where:** geck_mcp/fnv_data.py:322-327 (`_active` ensures every active entry with a path, also ones load_order
  marked with `error`), scan_plugin at :232-236 (`FormatError` for a non-TES4 file, raw `struct.error` for a
  header under 8 bytes), plugin_masters at :123-127 with the catch at :106 (`except (OSError, codec.FormatError)`,
  no `struct.error`).
- **What happens:** confirmed with the venv: a 0-byte `empty.esp` gives `FormatError not a TES4 plugin` from both
  plugin_masters and scan_plugin; a 6-byte `TES4\0\0` file gives raw `struct.error` from both. Steps: an
  active plugin of the MO2 profile is a 0-byte file (failed download, a copy in progress). fnv_loadorder reports it
  under problems, but fnv_record_find, fnv_record_show, fnv_record_users, fnv_conflicts, fnv_script_lint
  (check_forms) and fnv_scripts_compile's lookups all fail with "not a TES4 plugin" until it is fixed, because
  `_active` raises before any lookup. For the 6-byte case the struct.error escapes load_order itself, so
  fnv_loadorder, the tool meant to show the problem, fails too.
- **Why it matters:** a full outage of the record tools from one damaged file; nothing is written and the error
  names the cause, so low (same class as X7, which covers only scan_references).
- **Fix idea:** skip (and report in `indexBuilt`/problems) entries whose plugin_masters failed or whose scan
  raises, and add `struct.error` to the plugin_masters catch (or bound the header read and raise FormatError).

---

## Round-3 fixes re-checked (no regression found)
- **X1** (d7ad2a7, 72b9af4): the given path is resolved before the root check, so `..`, symlinks and junctions
  out of fnvsym/traces or `<state>/traces` are refused; the `.json` check runs on the resolved target (a link
  named x.json pointing at a plugin is refused); a directory named x.json is refused; the check runs before any
  work; the tool is kind `control` in both the kind table and the annotation. Remaining gap: L3.
- **X2** (3e940ba): `os.path.normpath` collapses `..` the way Win32 does. Checked with the venv that the OS does
  not treat `.. `, `. ` or `...` as parent or current folder (`System32\. \..\win.ini` is not
  `C:\Windows\win.ini`), so the lexical spot cannot be steered out of Data; the folder check now precedes
  `is_file()`.
- **X3** (dd16e89, 203e556 I2): the stale-copy delete runs inside the same BEGIN IMMEDIATE as the insert, drops
  records, files and refs rows of the other path, and INDEX_VERSION 3 forces old two-copy indexes to rebuild;
  an older v2 server process sharing the file sees v3 rows as fresh (no rebuild ping-pong). Refs rows another
  thread inserts for a just-deleted file id are orphans that no lookup reads (ids are never reused). Gaps: L1, L2.

## Checked and found sound (no finding)
- build: every type the spec accepts (BUILDERS and GENERIC_TYPES) is in TOP_ORDER, so no record is dropped from
  the file while the receipt lists its FormID; DIAL topic + type-7 children order; CELL then type-6 group with
  persistent (8) / temporary (9) refs; interior block/sub-block; HEDR count = records + groups; output re-parsed
  and validated before anything is written; FormID map saved only after a successful write.
- records/dialogue layouts: TES4 HEDR/CNAM/SNAM/MAST/DATA, STAT, LIGH DATA (32 bytes), MSTT, CELL XCLL (40 bytes),
  REFR/ACHR/ACRE DATA (24), CTDA 28 bytes (comparison bits, OR flag, run-on enum), TRDT 24 bytes, INFO DATA flag
  split over the two flag bytes, condition function indices, SCHR 20 bytes, SLSD 24 bytes with the type byte at
  16, the 0x2000 result-script record flag.
- codec: XXXX read and write, compressed records re-encoded from the current subrecords (builder records never
  carry a stale `_compressed`), lazy round-trip.
- compile_spec_scripts / result scripts: wrapper bytecode checks, runtime ids mapped back to master-relative ids
  through the loaded list, cache written atomically; build reuses a cached script only with matching source,
  FormID, verification and compile context.
- authoring.install: generated-marker, foreign and edited-since checks, verified backup, re-check of both files
  before the atomic write, history written after; build output and receipt only under build/.
- fnv_save: every parse error becomes SaveFormatError; the screenshot goes only to state/fnv/screenshots.
- Index/SQLite: per-path ensure lock, re-check inside BEGIN IMMEDIATE, file ids never reused (meta
  last_file_id), `return`/`continue` inside write_txn commit normally, refs built in one transaction per plugin.
