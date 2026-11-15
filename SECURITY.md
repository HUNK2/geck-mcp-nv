# Security

## Reporting a problem

Please report a security problem privately, through GitHub's "Report a vulnerability" button on the repository's
Security tab, not in a public issue. Say which version (the server announces it to the MCP client when it connects; `bridge/geck_mcp/__init__.py`
has it), what an agent or a file can
make happen, and the steps to show it. You will get an answer when the report has been read; a fix is released as
soon as it is ready, and the report is credited unless you ask otherwise.

## What fnv-mcp trusts, and what it does not

fnv-mcp gives an AI agent tools that read and change a running game, files in the game's mod setup, and code
inside the game process. The boundaries it keeps:

- **Engine control is off by default.** With `fnv.engine_control` at `"read"` (the default and the example
  project's value), every tool that changes the game, the dev profile or the game process refuses with
  `CONTROL_DISABLED`. `"full"` allows them; set it only for a game and profile you are willing to have changed.
- **Paths stay inside their roots.** Files a tool writes stay under the project's state folder, the MO2 dev
  instance or the game's `Data` folder; `..`, device names, network (UNC) paths and links that point outside are
  refused.
- **Text from files and the game is data.** Record text, plugin text, logs and console output are returned as data;
  the server instructions tell the agent not to follow instructions found in them.
- **Native code runs in the game.** `fnv_native`, `fnv_code` and `fnv_plugin_build` compile C with Zig and load it
  into the game process with full rights. Treat them like running a program: only with `"full"` control, on a
  machine and game you control.
- **The engine companion's pipe** is a local named pipe. The tools talk only to a pipe served by the running
  FalloutNV.exe.

Out of scope: problems that need an attacker who can already write to the clone, the project file, the game folder
or the MO2 instance, and anything an agent does with `"full"` control that the user asked for.
