# Check a whole plugin
Summary: Visit every cell a plugin touches and every line its NPCs say, unattended, and find what is broken.
Words: check plugin, crawl, every cell, all cells, qa, regression, voice, lip, missing files, dialogue check, release check

Use this before a release or after a big change. Status: checked in the game on 2026-11-11 (both test plugins'
cells, and Easy Pete's dialogue with voice and lip files).

## Steps

1. **Is the machine healthy?** `fnv_selftest`: anything broken there makes later results unreliable.
2. **Cells.** `fnv_crawl` action `cells` with `plugin`. Each cell is visited and settled, then checked: script
   errors, engine warnings, files not found, references with no 3D, load doors that lead nowhere, frame rate, and a
   photo. Rows come worst first. `fnv_undo` puts the player back.
3. **Dialogue.** `fnv_crawl` action `dialogue` with `npc` for each speaker: every line written for the NPC, whether
   the engine allows it now and which condition fails, and the voice and lip files with whether they exist.
4. **Quests and items.** `fnv_crawl` action `quest` with `quest` and `path` (the stages, in order): the stage
   reached, the objectives shown, targets that do not resolve, script errors. Action `items` with `plugin`: each new
   item added, equipped or used, its model checked, then taken back.
5. **Scripts that never ran.** `fnv_script_coverage` action `run` with `folder` (the plugin's tests) and `plugin`:
   per script, the lines no test reached.
6. **Meshes and textures before shipping.** `fnv_asset` action `nif` / `dds` on each new file.
7. **Follow up.** A missing file: `fnv_asset where` and `fnv_asset file_log`. A warning: `fnv_warnings read`. A
   failing condition: `fnv_dialogue action=tree` and `fnv_condition_eval`. A crash or regression after a load-order
   change: `fnv_bisect` with the failing `fnv_test steps` steps.

## What the answers mean

- `missingFiles`: files the engine asked for while the cell loaded and found nowhere (loose or archive).
- `refsWithout3D`: enabled references of visible types whose 3D did not load (bad mesh path or missing mesh).
- `engineSays: blocked` with `failing`: the line cannot be said now, and the condition that stops it.
- `voiceFound: false`: the voice file the engine will ask for does not exist; the player gets silence.

## Known traps

- A crawl moves the player: `fnv_undo` moves the player back.
- Dialogue verdicts are for now (time, quest stages, disposition); a line blocked now may be fine later.

## Checks that prove the fix

- Crawl again: the cell's row has no problems and the line's files are found.
