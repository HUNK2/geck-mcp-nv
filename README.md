# fnv-mcp

**MCP tools that let an AI agent build, run, debug and change Fallout: New Vegas: the GECK, the running game and the
engine itself.**

fnv-mcp is a standalone continuation of [geck-mcp-nv](https://github.com/2Shay96/geck-mcp-nv) by **2Shay**, which
it is built on and keeps the full history of. Everything 2Shay made (the GECK bridge, the plugin builder, the script
compiler, the console companion) is here unchanged in purpose; see "Built on geck-mcp-nv" below.

What fnv-mcp adds, on top of the 44 `geck_*` tools: **160 `fnv_*` tools** for the running game (204 in all).

- **Engine knowledge**: function and class lookup from the decompilation, a symbol database with source lines and
  named locals for the retail exe, crash explanation, engine warnings.
- **Live control**: time control (pause, step, frame advance), breakpoints and a debugger, screenshots and photo
  sheets ("eyes"), walking, talking, using doors and fast travel ("hands"), forms and scripts changed in place.
- **Engine changes**: C modules hooked into the running game, decomp functions recompiled with the game's own
  compiler and swapped in, both exportable as normal xNVSE plugins.
- **Performance and checks**: a sampling profiler, code coverage, thread, memory and object views, animation, audio,
  physics and formula inspectors, whole-plugin crawls, load-order bisection and a self-test.

Start with [bridge/docs/FNV-MCP-SETUP.md](bridge/docs/FNV-MCP-SETUP.md) and
[bridge/docs/FNV-LIVE-TOOLS.md](bridge/docs/FNV-LIVE-TOOLS.md). The `fnv_guide` tool has step-by-step guides.

Nothing from the game, the GECK or the decompilation is in this repository: each user builds the engine map and
symbol database from their own copies.

## Credits

- **2Shay**: author of [geck-mcp-nv](https://github.com/2Shay96/geck-mcp-nv), the project this repository is built
  on: `bridge/` (GECK bridge, plugin builder, script compiler, runtime diagnostics, console companion).
- **HUNK2**: the `fnv_*` live, engine and debugging tools and the engine companion.
- The [fnv_decomp](https://github.com/momusdigital/fnv_decomp) project, xNVSE, JIP LN NVSE and Yvile's Crash Logger,
  whose work these tools read at runtime; see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

## Built on geck-mcp-nv

The `geck_*` tools are 2Shay's [geck-mcp-nv](https://github.com/2Shay96/geck-mcp-nv) (version 0.4.0 Beta): a GECK
bridge, an offline plugin builder from a JSON spec, script compilation with GECK's own compiler, runtime
diagnostics and an opt-in console companion. They need the GECK and the small 32-bit helper that
`bridge/build_helper.py` builds; the `fnv_*` tools need neither. See [bridge/README.md](bridge/README.md) and
[bridge/docs/MCP-QUICKSTART.md](bridge/docs/MCP-QUICKSTART.md). Its own release history is in
[CHANGELOG.md](CHANGELOG.md).

Open problems are listed in [bridge/docs/KNOWN-ISSUES.md](bridge/docs/KNOWN-ISSUES.md).

## Licence

- MIT. See [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
- `bridge/` (the GECK side) © 2026 2Shay. The `fnv_*` tools and the engine companion © 2026 HUNK2.
- Not affiliated with or endorsed by Bethesda Softworks, ZeniMax, Microsoft, Anthropic or OpenAI.
  Fallout and G.E.C.K. are trademarks of their owners.
