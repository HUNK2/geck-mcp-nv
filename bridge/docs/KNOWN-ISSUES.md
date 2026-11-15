# Known issues

Open problems a user can meet, one line each. None loses data in normal use; most need an unusual order of calls or a
damaged file. Each is tracked as a GitHub issue.

## Client

- The Claude app does not fetch the tool list again after the server announces a change; restart the client after
  updating the server.

## Undo and the journal

- An undo made between a change and its confirmation is opened again by the confirmation.
- `fnv_undo` with a count can stop at a newest pending entry whose operation cannot be repeated.
- When journal compaction falls back, it rewrites `journal.jsonl` in place rather than atomically.
- One journal line with a field of the wrong type makes every journal read fail.
- Undoing a probe removal reads and writes `probe_session.json` outside a single lock.
- A game speed change from an out-of-range multiplier is not journaled.
- The undo of a pause ignores what the resume reports.
- `fnv_revert_all` slows down with many entries (about 22 s for 3,000).

## Debugger, hooks and traces

- A `thread=main` trap break that falls back to `int3` parks every thread, not only the main one.
- A failed unhook ends the action or hold without saying so; clearing a break drops its record anyway.
- Changing a hook's action rewrites a live skip stub in place.
- A stop on a fault does not reload the thread's debug registers after the park.
- `fnv_flow` can leave a parked stop when the function is hit between the last poll and the break being cleared,
  and its trace reader can take the lowest new step stop of any thread.
- A hook's result can be counted as applied when the shadow stack was full.
- A slot claimed for a predicate stays claimed when setting the predicate fails.
- A main-thread stop that closed the frame gate opens it again on release, dropping a hold sent in the meantime.
- Setting a function break again on a removed hook can fall back to a trap break.
- Two traces at the same time may share one parked-stop record.
- Some read actions install a hook for the length of the call (`fnv_detection` recompute, `fnv_anim` events,
  `fnv_audio` log and subtitles).
- `fnv_perk_entries evaluate` ends a recording the caller had started.
- Two engine AI calls at once with the same hook label can lose records.

## Game control

- After a launch in which no game started, `fnv_status` says the game was terminated from outside.
- The audio-cache setting can stay off when writing it back fails.
- The quit guard of `fnv_run_script` does not catch `(QuitGame)` or a quit on a later line.
- `fnv_crawl cells` moves the player with a `coc` that is not journaled.
- `fnv_snapshot` has no cap on the number of items or on console items.
- `fnv_window persist` and `fnv_warnings enable` write files although `fnv.engine_control` is `"read"`.

## Plugins and data files

- Record text accepts the control characters U+0080 to U+009F.
- Generic text fields and dialogue responses can be written with an embedded NUL.
- A plugin with very deeply nested groups stops the parser with a recursion error.
- A truncated subrecord or a truncated BSA archive gives a raw `struct.error` instead of a clear message.
- The build's missing-file check does not look inside BSAs of enabled MO2 mods.
- Plugin names are matched case-insensitively for ASCII letters only.
- When a plugin name appears more than twice, or a disabled plugin is still indexed, record lookups can name the wrong
  plugin.
- One unreadable active plugin breaks every record tool that uses the index, `fnv_loadorder` included.

## Naming tools

- `fnv_name_export` can overwrite fnvsym's own Frida trace file.
- The purity check refuses some functions it could accept (`movsx`, `movss` and `movsd` count as string writes).
