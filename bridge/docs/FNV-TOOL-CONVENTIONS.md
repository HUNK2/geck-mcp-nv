# Conventions for the action-style fnv_* tools

New fnv tools are named `fnv_<domain>` and take an `action` argument: `fnv_warnings(action="read", ...)`. One tool
carries many actions. This keeps the tool list short. The helpers are in `geck_mcp/fnv_conv.py`.

## The rules

1. **One tool per domain, one `action` argument.** Read actions never change anything. Act actions change the
   running game, need `fnv.engine_control` set to `full`, and are journaled. Control actions change files or start
   and stop the game. The tool's kind in `FNV_LIVE_TOOLS` is the strongest kind of its actions. Pass the kind of
   the called action to `invoke(name, fn, kind)`, and pass `action=` when the tool has one.
   **What `read` allows (one rule):** anything that puts agent-chosen code or files into the game or its mod profile
   needs `full`, whatever its kind. Those control actions are listed in `FULL_ONLY_ACTIONS`
   (`fnv_live_tools.py`); every other control tool has a line in `READ_LEVEL_CONTROL` saying why it may run under
   `read`, and `tests/test_stab_gate.py` fails for a control tool in neither table. `read` allows looking at the
   game, starting and stopping the dev game, installing the companion, and writing into the bridge state folder.
   `full` allows changing the game and running code in it; it is code execution as the Windows user
   (`fnv_native` compiles and loads C into the game).
2. **Errors say what to do next.** An error has a code and a plain message. When the code is known, the message
   ends with "Next: ...". An unknown action lists the valid actions.
3. **Answer first.** A result starts with `verdict` (one short sentence) and `reason`. The evidence follows.
   A missing value is `null`, never 0.
4. **Every list is paged the same way.** Keys `total`, `offset`, `count`, `more`, `rows`. Arguments `limit`,
   `offset`, `fields` (keep only these keys) and `brief` (a short row).
5. **One way to name a reference.** A form id (`0x0001F00D`), `Plugin.esp:LOCALID`, an EditorID, `player`,
   `crosshair`, or `$name` saved by an earlier call. The resolver is `LiveService.resolve`; `resolve_ref` adds the
   `$name` store (kept in memory until the server stops). It adds no engine code. `crosshair` already works
   through the existing resolver, so there is nothing more to add for it.
6. **Facts.** Each tool records the engine functions and layouts it relies on, and how each was checked in game, in
   `fnv_facts.py`.
7. **Long work** (crawls, bisects, test suites) should return an operation id and be polled. The helpers do not
   cover this yet.

## The helpers

| Helper | Use |
| --- | --- |
| `dispatch(action, table)` | Call `table[action]()`. Raises `ConvError('UNKNOWN_ACTION')` with the valid actions |
| `page(rows, limit, offset, fields, brief, brief_fields)` | The paged list shape (rule 4). `limit` is at most 500 |
| `verdict(text, reason, **evidence)` | A dict whose first keys are `verdict` and `reason` |
| `next_step(code)` | The standard next step for a known code (game not running, companion missing, control not full, engine map missing) |
| `ConvError(code, message, evidence)` | An error that adds the next step. `LiveService.run` reports its `code` |
| `resolve_ref(live, ref, save_as)` | Resolve a reference; `save_as="door"` saves it as `$door` |

## How a new tool uses them

```python
from .fnv_conv import dispatch, page, verdict, resolve_ref

class DoorTools:
    def __init__(self, live):
        self.live = live

    def run(self, action, ref=None, limit=50, offset=0, brief=False):
        return dispatch(action, {
            'list': lambda: self.list(limit, offset, brief),
            'show': lambda: self.show(ref)})

    def list(self, limit, offset, brief):
        rows = self.find_doors()
        return verdict('%d doors' % len(rows), None,
                       **page(rows, limit, offset, brief=brief, brief_fields=['ref', 'name']))

    def show(self, ref):
        door = resolve_ref(self.live, ref, save_as='door')
        return verdict('Door %s' % door['formId'], 'Saved as $door.', ref=door)
```

In `fnv_live_tools.py`, add the tool to `FNV_LIVE_TOOLS` and register it with one `@server.tool`, then call
`invoke('fnv_door', lambda: tools.run(action, ...), 'read')`. Use `'act'` or `'control'` for actions that change
things. Give the tool a docstring that names every action in one sentence each. Add a fake-host test and an entry
in `docs/FNV-LIVE-TOOLS.md`. If the new tool solves a common problem, mention it in a guide
(`geck_mcp/guides/`) so that `fnv_guide` can point to it.
