# More than one board

The agent board was always written down and always came back. One board, the
same one, whatever you were working on.

That is fine until you want two: a pair of agents for the thing you are shipping
this week, and a different pair for the project you go back to on Fridays. Until
now the second one meant taking the first apart — and building it again from
memory on Monday.

## Saving one

On the left of the board: **Save this board**. Give it a name. It appears in the
list underneath with how many agents and projects are on it.

The board you are working on still comes back on its own. Saving is for keeping a
shape you will want again, not something you have to remember to do.

## Opening one

Press its name. The app asks first, because what is on the board now is replaced
— somebody who has spent ten minutes arranging it should not lose it to one
press. Save that one first if you want it back.

Nothing is merged. A board holding the pieces of two arrangements belongs to
neither.

## Deleting one

**Delete** next to its name. It asks first, and it cannot be undone.

## Moving a board to another computer

Press **Export** beside a saved board to write one portable JSON file. Press
**Import board JSON** to add that file to the saved-board library without
replacing the board currently open. Nexus validates the complete UTF-8 file and
rejects malformed bytes or values it would have to shorten; it does not repair
canonical input silently. The installed desktop writes very large valid boards
in small ordered chunks, so Electron's per-message limit is not a hidden export
limit.

Project folder paths are absolute because they identify the local directories
agents may edit, so the JSON reveals those paths. After opening an imported
board on a different computer, press the gear on each unavailable project and
choose **Use a different folder on this computer**. Rebinding preserves that
project's stable ID, tasks, agent assignments, communication lines, and chat
identity. It deliberately clears local test-command approval; review and
approve the commands for the new path before Nexus executes project code.

## Where they are kept

With you, not with the project:

```
%APPDATA%\our-harness\swarms\
```

There is no arbitrary count limit. Each board is still checked as a separate,
bounded JSON document. The disclosed 768,000,000-byte UTF-8 envelope covers the
worst valid combination of the board's per-field limits, including JSON
escaping; ordinary filesystem errors are reported rather than becoming a
smaller hidden product cap.

Two names that differ only in capitals — `Friday` and `friday` — stay two boards.
A file name on Windows does not care about capitals, so without that one of them
would quietly become the other.

A saved board goes back in through the same door as any other change, so one
saved by an older version of the app is checked and tidied rather than trusted.
