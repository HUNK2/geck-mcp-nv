# Adding the GECK tools

The `geck_*` tools drive the GECK and build plugins. They are optional: set up the server first with
[bridge/docs/FNV-MCP-SETUP.md](../bridge/docs/FNV-MCP-SETUP.md), the one setup guide. Until the steps below are
done, every `geck_*` tool answers `NOT_CONFIGURED` and names the settings it is missing.

The source download contains source and documentation only: **no DLL, EXE, game files, saves, master indexes or
personal project state**. Extract it outside the game's Data folder. It is a development toolkit, not a Vortex
install.

## The GECK settings and the helper

From the `bridge` folder, add the GECK settings to your project file (`projects\my-fnv.json`):

- `plugin`: the `.esp` the tools work on;
- `spec` (a JSON spec the plugin is built from) or `records` (the STAT records the record tools may edit);
- `helper`: `../Probe.Mcp.exe`, the 32-bit helper built below.

`projects\example-windows.json` is a complete example. Then build the helper from this download (it uses
Windows' .NET Framework 4 C# compiler):

```powershell
.venv\Scripts\python.exe build_helper.py --project projects\my-fnv.json
```

Stop the MCP server first so an old helper is not locked, and restart the client afterwards. Begin with the
read-only `geck_status` and `geck_capabilities` (it gives the tool count of the running server); do not discard
unsaved GECK work. See [bridge/README.md](../bridge/README.md#setup) for indexing and full client examples.

## Optional in-game console companion (legacy toolset)

The `geck_console_*` and `geck_runtime_*` tools are in the legacy toolset, off by default: set
`"legacy_tools": true` in the project file (or start the server with `--legacy`) to use them. The `fnv_*` tools do
these jobs through the engine companion.

Editor work needs no companion DLL. Console execution and typed inventory reads require a DLL built locally for
your installed project plugin. Supported configuration: Windows, Steam FNV 1.4.0.525, xNVSE 6.4.9+ 6.x and a locally
provided [Zig 0.14.1](https://ziglang.org/download/0.14.1/) compiler, named by `fnv.zig` in the project file (or
`FNV_MCP_ZIG`). The tools do not download a compiler or install xNVSE for you.

With the project plugin installed, ask your agent to hash that file and use:

1. `geck_console_setup(mode="preflight", expected_plugin_hash=HASH, request_key=KEY)`.
2. `mode="build"` and a new key. Keep its returned build receipt.
3. Quit FNV normally. Use `mode="install"`, a new key and that `build_receipt`. Owned previous files are backed
   up; unresolved commands block installation.
4. Launch through xNVSE, load a disposable save, leave gameplay unpaused and require `geck_console_readiness` to
   report `ready` before commands.

Changing the anchor plugin requires a guarded rebuild/install/restart. Other loaded plugins use explicit
plugin/local_form/sha256 bindings without another DLL. Keep `target.started` as a decimal string. Retain each
operation ID, poll status, and never retry an uncertain command automatically. A `coc` also needs explicit
authorization and a matching originating CELL binding. Verify interior/exterior classification first: exact-cell
proof does not establish collision-safe landing. See the complete
[console workflow](../bridge/docs/CONSOLE-COMMANDS.md).

The separate optional read-only quest telemetry companion has its own
[build/setup instructions](../bridge/docs/AUTOMATIC-RUNTIME.md).

## Limits

Windows editor and disposable in-game workflows have live evidence. Large-script/reference/branch workloads and
minimized or disconnected sessions still need further live validation. GECK Extender and non-English GECK are not
tested.
