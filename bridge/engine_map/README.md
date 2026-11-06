# engine_map

Generates `engine_map.json` (and optionally a C header) for Fallout: New Vegas 1.4.0.525 from your own
fnv_decomp checkout, fnvsym's proof table and your own unpacked exe. Nothing generated here is committed
or shipped: write it under `bridge/state/` (ignored by git).

```
python -m engine_map.generate --decomp PATH [--verified CSV] [--exe EXE] [--nvse-src PATH]
                              [--plugin-src LABEL=PATH] --out state/engine_map.json [--header H]
```

| Module | Reads |
| --- | --- |
| `generate.py` | `build/units.json` (functions, sizes, symbols), `verified.csv`, the exe (image probes, call edges, imports, settings) |
| `source.py` | `// @fnv 0x...` markers and the definition below each |
| `headers.py` | class/struct definitions, `// +0x` offsets, `// slot` comments, `FNV_STATIC_ASSERT` sizes and offsets |
| `demangle.py` | MSVC symbols (dbghelp on Windows; a name-only fallback elsewhere) |
| `commands.py` | vanilla `CommandInfo` tables in the exe; `DEFINE_COMMAND*` macros in script-extender sources |
| `pe.py` | minimal PE32 reader |

Used by the `fnv_*` MCP tools; see [docs/FNV-ENGINE-TOOLS.md](../docs/FNV-ENGINE-TOOLS.md).
