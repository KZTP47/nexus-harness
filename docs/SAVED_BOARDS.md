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

## Where they are kept

With you, not with the project:

```
%APPDATA%\our-harness\swarms\
```

Up to 60 of them, which is far more than anybody has and still a number rather
than a folder that quietly fills up.

Two names that differ only in capitals — `Friday` and `friday` — stay two boards.
A file name on Windows does not care about capitals, so without that one of them
would quietly become the other.

A saved board goes back in through the same door as any other change, so one
saved by an older version of the app is checked and tidied rather than trusted.
