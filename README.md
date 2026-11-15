# fnv-mcp

**MCP tools that let an AI agent build, run, debug and change Fallout: New Vegas: the running game, the engine
itself and the GECK.**

fnv-mcp is a standalone continuation of [geck-mcp-nv](https://github.com/2Shay96/geck-mcp-nv) by **2Shay**, which
it is built on and keeps the full history of; see "Built on geck-mcp-nv" below.

## What it is

One MCP server with <!-- counts -->**165 `fnv_*` tools** for the game and its engine and **28 `geck_*` tools** for the
GECK (193 in all; the legacy toolset adds 15 more)<!-- /counts -->. The counts are checked against the server's own
tool list by the tests.

- **Engine knowledge**: function and class lookup from the decompilation, a symbol database with source lines and
  named locals for the retail exe, crash explanation, engine warnings.
- **Live control**: time control (pause, step, frame advance), breakpoints and a debugger, screenshots and photo
  sheets ("eyes"), walking, talking, using doors and fast travel ("hands"), forms and scripts changed in place.
- **Engine changes**: C modules hooked into the running game, decomp functions recompiled with the game's own
  compiler and swapped in, both exportable as normal xNVSE plugins.
- **Performance and checks**: a sampling profiler, code coverage, thread, memory and object views, animation, audio,
  physics and formula inspectors, whole-plugin crawls, load-order bisection and a self-test.

## What it needs

- **Windows** 10 or 11 (Windows only), Python 3.12 and an MCP client (Claude Code, Claude Desktop, Codex...).
- Fallout: New Vegas from Steam (FalloutNV.exe 1.4.0.525), xNVSE, JIP LN NVSE, Yvile's Crash Logger and a portable
  Mod Organizer 2 instance for a dev profile of its own.
- Zig 0.14.1, to build the engine companion that runs inside the game.
- A checkout of the [fnv_decomp](https://github.com/momusdigital/fnv_decomp) decompilation, from which you generate
  the engine map on your own machine.
- For the `geck_*` tools only: the GECK and a small helper you build locally.

Nothing from the game, the GECK or the decompilation is in this repository: each user builds the engine map and
symbol database from their own copies.

## Start

Follow [bridge/docs/FNV-MCP-SETUP.md](bridge/docs/FNV-MCP-SETUP.md), the one setup guide. It starts from
`bridge/projects/example-fnv.json` and ends with the agent reading the running game. fnv-mcp is not a pip package:
run it from a clone with `python run_mcp.py --project <file>` (the guide registers exactly that with your MCP
client). Then a first prompt:

> Call fnv_setup_check and fix what it reports with me, then launch the game with fnv_launch and show me
> fnv_status.

The tool references are [FNV-ENGINE-TOOLS.md](bridge/docs/FNV-ENGINE-TOOLS.md) and
[FNV-LIVE-TOOLS.md](bridge/docs/FNV-LIVE-TOOLS.md); the `fnv_guide` tool has step-by-step guides. Open problems are
listed in [KNOWN-ISSUES.md](bridge/docs/KNOWN-ISSUES.md). To remove it again, see "Uninstall" in the setup guide.

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
`bridge/build_helper.py` builds; the `fnv_*` tools need neither. See [bridge/README.md](bridge/README.md),
[bridge/docs/MCP-QUICKSTART.md](bridge/docs/MCP-QUICKSTART.md) and [docs/SOURCE-SETUP.md](docs/SOURCE-SETUP.md).
Its own release history is in [CHANGELOG.md](CHANGELOG.md).

## Licence

- MIT. See [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
- `bridge/` (the GECK side) © 2026 2Shay. The `fnv_*` tools and the engine companion © 2026 HUNK2.
- Not affiliated with or endorsed by Bethesda Softworks, ZeniMax, Microsoft, Anthropic or OpenAI.
  Fallout and G.E.C.K. are trademarks of their owners.
