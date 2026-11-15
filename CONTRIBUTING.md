# Contributing

Thank you for helping. fnv-mcp runs from a clone on Windows; set it up with
[bridge/docs/FNV-MCP-SETUP.md](bridge/docs/FNV-MCP-SETUP.md) first.

## Tests

From `bridge`:

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests
```

The whole suite takes about 11 minutes. One file: `.venv\Scripts\python.exe -m unittest tests.test_every_tool -v`.
`python tests\ci_run.py --require-engine` runs it as CI does: it prints why tests were skipped and fails when the
engine companion tests were skipped because Zig was not found (set `FNV_MCP_ZIG`). No test needs a running game;
the live tests run only with `FNV_LIVE=1` and then change the game that is running.

Lint: `python -m ruff check .` from `bridge` (the rules are in `bridge/pyproject.toml`).

## Changes

- Write the test first, and see it fail for the reason you expect.
- Follow [FNV-TOOL-CONVENTIONS.md](bridge/docs/FNV-TOOL-CONVENTIONS.md): answers are envelopes, every refusal has a
  code and a sentence that says what to do, and act tools are journaled so they can be undone.
- Engine addresses, offsets and layouts go in `bridge/geck_mcp/fnv_facts.py` with the evidence for each (conventions
  rule 6). Many modules still write addresses inline; when you change such a module, move the addresses you touch.
- New tools or new actions on a tool need an issue first: the tool count is being reduced, so additions are
  discussed before they are written.
- Do not commit game files, the unpacked FalloutNV.exe, files generated from the decompilation (`engine_map.json`,
  the symbol database) or anything under `bridge/state`.
- Code that rebuilds decompiled game functions (`fnv_code`) is for local use; do not add its output to the
  repository.

## Reporting bugs

Open a GitHub issue with the tool call, its answer (the envelope) and, for a crash, `fnv_crash_explain`'s answer.
Check [KNOWN-ISSUES.md](bridge/docs/KNOWN-ISSUES.md) first. Security problems: see [SECURITY.md](SECURITY.md).
