# Hello Quest: a mod built and proven end to end (fnv-mcp milestone 9)

A small quest mod made from a one-line description with geck-mcp-nv's spec builder, its script compiled
by the running game, and proven by a scenario test:

> A small room with three Hello Tokens. Picking up all three completes the quest and pays 100 caps.

| File | What it is |
| --- | --- |
| `hello-quest.spec.json` | The geck-mcp-nv spec: a token (MISC), a light, the quest (QUST, start game enabled, stages 10 and 20), its script, and a room with a coc marker, three floor tiles and three persistent tokens |
| `scripts/HelloQuestScript.gek` | The quest script: stage 10 at start; with three tokens, 100 caps and stage 20, once |
| `tests/hello-quest.yaml` | The scenario: picks up two tokens (no reward yet), then the third, and checks stage 20, 3 tokens, 100 caps, `rewarded == 1` and no script errors |

## The loop, over MCP

GECK is not needed. The builder emits a script uncompiled until its compiled-script cache has it, and
`fnv_scripts_compile` fills that cache from the running game:

1. `fnv_script_lint` on the script (offline).
2. `geck_spec_validate`, then `geck_plugin_build`: `build/HelloQuest.esp` with the script uncompiled.
3. `fnv_install build/HelloQuest.esp` (MO2 dev profile), `fnv_launch coc:HelloQuestRoom`: the plugin's forms
   now exist in the game.
4. `fnv_scripts_compile`: the game compiles the script; its bytecode, variables and references are mapped
   to the plugin (master-relative form ids, xNVSE's leading scriptname statement dropped), checked with the
   builder's own `verify_script`, and cached with an in-game provenance.
5. `geck_plugin_build` again: the script is now compiled (2307 bytes instead of 2061).
6. `fnv_quit`, `fnv_install`, `fnv_test_run tests/hello-quest.yaml`: PASSED, 16 steps.

The cache entry is tied to the build's compile context (spec, sources, indexes): changing the spec means
steps 2 to 5 again.

## What the tools found on the way

- The first in-game compile failed silently: the game prints compiler errors only while its console is
  visible. `fnv_compile_ingame` now sets the console flags for that frame and returns the compiler's own
  messages ("Unknown variable 'undeclared'", with the line).
- The running game keeps no quest EditorIDs, so `Quest.variable` references cannot be compiled in game. The
  quest script avoids them (it reads its own variables and calls `GetStage`/`SetStage`).
- A launch straight into the room from the main menu takes 2 to 3.5 minutes; a launch into other cells takes
  10 to 15 s, and `coc HelloQuestRoom` inside a running game is immediate. `fnv_hang_doctor`, sampled during
  the wait, shows the main thread in the audio update (`Main::OnIdle_UpdateAudio` → `Owner::AddSound` →
  `BSWin32GameSound::CopyFrom`) creating DirectSound buffers through the Windows audio service, again and
  again. Moving the player's spawn to a coc marker and lifting the tokens above the floor did not change
  it; the cause is still open.
