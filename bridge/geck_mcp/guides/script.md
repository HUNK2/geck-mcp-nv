# A script does not run or does the wrong thing
Summary: Find out why a script does not run, fails or gives a wrong result, change it and prove the fix.
Words: script, scripts, does not run, not running, wrong result, quest script, object script, effect script, GameMode, variable, compile, error, stage, NVSE, expression

Use this when a script has no visible effect, a quest does not advance, a variable has a wrong value, or the game
shows a script error. Status: written from the tool descriptions in this repository. The tests check that every
tool name below exists; they do not play the game.

## Steps

1. **Is the game ready?** Call `fnv_status`. You need phase `in_game`. If not, call `fnv_game launch`.
2. **Read the errors first.** Call `fnv_script_errors` with since `start`: script, mod and message of every script
   error. Also call `fnv_warnings` with category `SCRIPTS` (when the warning log is on) and `fnv_logs` with source
   `console`. A script that fails at run time prints its reason there.
3. **Does it compile?** Call `fnv_script_lint` with the source: unknown commands, wrong argument counts and unknown
   forms, checked against the vanilla, xNVSE and JIP tables. Then `fnv_compile_ingame`: it compiles with the
   running game's own compiler and shows the variables with their indexes and the bytecode.
4. **Is the script alive?** Call `fnv_script_instances` with script set to the script name, or `of` set to a
   reference. For a quest script call `fnv_quest`: is the quest running, which stage, which objectives. An object
   script runs only while its reference is in a loaded cell. An effect script runs only while the effect is active
   (`fnv_effects`).
5. **Which lines run?** Call `fnv_script_trace` with the script and its source text. It records each statement that
   runs, whether it succeeded and which reference it ran on, and lists the lines that did not run. Read it again
   after the event you wait for: later calls return only new statements. Call it with action `stop` when done.
   To stop the game on one statement, call `fnv_script_debug` with break, the script and the line; status then shows
   the reference and the variables (vars), eval runs a snippet on it, and continue or step line go on. Lines are
   source lines (the game itself counts statements, as in its runtime error messages: blank lines, comments and
   declarations do not count); a stop shows both, with the line's text. Quest scripts do not run while a menu is
   open (the pause menu opens when the game window loses focus): close it with `fnv_ui_do` first. A statement
   reached during save or load is passed on and counted. `fnv_script_log` with commands (mode start, then read) keeps
   the statements of every script in a ring without stopping the game; events shows the block flags as they are
   raised.
6. **Does it run often enough, or too much?** `fnv_script_profile` shows calls and time per script. A script
   with zero calls never runs; one with a high cost per frame is a performance problem.
7. **What are the values?** `fnv_script_instances of=` shows the script variables of a quest or reference by name. `fnv_run_script`
   evaluates an expression in the game. `fnv_watch` samples values over time to see when one changes. To find who
   changed a field or a reference, call `fnv_blame` (field with the address path, or ref) and then report: each hit
   names the script line or the engine function that wrote it. A global's value is at `<global> + 0x24`; a write by
   a script names the exact statement and its source line.
8. **What does a command really do?** `fnv_script_explain` shows what the commands of a line do in the engine.
   `fnv_command_info` gives the parameters of one command.
9. **Try a fix without a restart.** `fnv_script_hot_swap` replaces the running script with new source and keeps the
   variable values. It refuses when variables would move or disappear. The change is journaled: `fnv_undo` swaps
   the old code back. Use `fnv_script_set` to set a quest stage or variable so you can test the next branch.

## What the answers mean

- A compile error from `fnv_compile_ingame` about an EditorID the game does not know: the running game keeps few
  EditorIDs. Use the form id. `fnv_record_find` finds it.
- Instances exist but the trace shows no statements: the script is not being called. Look at the quest running
  flag, the start-game-enabled flag and the delay.
- Statements run but a branch never does: the condition is false. Read the values with `fnv_script_instances of=` and compare with
  what you expect.
- A variable has a value that your script never sets: another script or a console line changed it. `fnv_journal`
  lists the changes made through the act tools. For other writers use `fnv_field_watch` on the field.

## Known traps

- Scripts see only references in loaded cells. A reference in an unloaded cell does nothing.
- Changing the order or the type of variables of a running quest script breaks the saved values. Add new variables
  at the end.
- A script compiled in the editor and the same text compiled in game can differ in the forms it can name. Test with
  `fnv_compile_ingame`.
- Console lines and `fnv_run_script` change the running game: they are act tools, which need
  `fnv.engine_control` set to `full`. `fnv_undo all=true` undoes the journaled changes.

## Checks that prove the fix

1. `fnv_script_errors` returns no new error after the event that failed before.
2. `fnv_script_trace` shows the statements of the branch that did not run before.
3. `fnv_script_instances of=` (or `fnv_quest`) shows the value or stage you expect.
4. Write the case as a YAML scenario and run it with `fnv_test`: it must pass from a fresh start
   (use a test save from `fnv_test_save`). Run `fnv_test run_all` to see that other tests still pass.
5. `fnv_warnings` read shows no new SCRIPTS warning that names your plugin.
