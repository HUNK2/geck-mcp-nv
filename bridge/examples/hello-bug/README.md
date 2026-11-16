# Hello Bug: scenario S4 (a script misbehaves)

`HelloBugQuest` counts the global `HelloBugLevel` up every 0.1 s and reaches stage 10 at 100. It never does:
`HelloBugJanitorScript` was meant to keep its own timer in range and resets `HelloBugLevel` too (line 8).

The investigation, with the tools:

1. `fnv_quest HelloBugQuest`: stage 0; `fnv_globals HelloBug`: the level stays between 0 and 41.
2. `fnv_script_debug break script=HelloBugQuest line=8`, then `status`, `eval HelloBugLevel`, `step`.
3. `fnv_blame field "HelloBugLevel + 0x24"`, then `report`: script 0D000803 (the janitor) line 8,
   `set HelloBugLevel to 0`.
4. `fnv_script_hot_swap HelloBugJanitorScript` with the line removed.
5. `fnv_test tests/hello-bug-finishes.yaml`: failed before the fix, passes after.

The plugin ships the bug on purpose (it is the exercise); the fixed janitor is the same script without line 8.
