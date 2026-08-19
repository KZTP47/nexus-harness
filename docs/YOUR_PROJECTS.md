# Your projects

The harness works on one project at a time, and everything it keeps lives inside
that project's own folder — the automations, the timers, the checks, the team,
what it has learnt. That was always true. What was missing was anywhere saying
*which* project you were looking at, and any way to another one without stopping
the harness and starting it again.

---

## The bar under the title

It says what this project is called and where it is. Press it and the list of
your projects comes out from the left.

---

## Each project keeps its own everything

This is not a setting. It is how the harness has always worked: a project's
automations, timers, checks, team and memory are files inside that project's
`.harness` folder. Switching projects changes all of them at once, because they
were never shared in the first place.

So you can have a nightly suite on one project and none on another, a team of
three assistants here and one there, and neither knows about the other.

---

## Adding one

**Browse for the folder** opens a folder picker and fills the box in. It does not
add it behind your back: you see what was picked, then press **Add it**.

That button is only there in the Nexus Harness app. Opened in an ordinary
browser, the panel says so and takes a typed path instead — a web page is not
allowed to learn where a folder really is on your machine, which is the whole
point of that rule.

Adding a folder does not switch to it. Press **Work on this** when you want to.

---

## Naming one

**Rename** in the list. The name is written inside the project, so it travels
with it: clone the project onto another machine and it is still called the same
thing. Leave the name empty and it goes back to the folder's own name, which is
right often enough that most people never type one.

The list of *which* projects you have is different. That is about you and this
machine, so it lives beside your own settings and never inside a project — a list
of the folders on your computer is nobody else's business, and would be nonsense
to anybody who cloned your repository.

---

## Taking one off the list

**Take off the list** means the panel stops listing it. The folder, and
everything in it, is left exactly where it was.

There is no version of this that loses your work. That is why the word is
"forget" and not "delete", and why the button says so when you hover it.

---

## Where you like the list

**Show this list** at the bottom of the sidebar:

| | |
| --- | --- |
| Only when I ask for it | It slides out when you press the bar, and goes away again. The panel keeps its whole width the rest of the time. This is how it starts |
| Always, down the side | It stays there, the way an editor does it. The page moves over to make room |

Remembered on this machine, with the list — not in a project. Kept in a project,
the panel would change shape every time you switched.

---

## What it will not do

- **Switch while something is running.** An automation, a run, or the checks
  going means the button says no and says why. Halfway through, the run would be
  reading one project's settings and writing into another's.
- **Delete anything, ever.** See above. It is worth saying twice.
- **Add a folder that is not there.** A path with nothing at it is almost always
  a typo, and a list full of those is a list nobody trusts.
- **Keep the page as it was.** Switching reloads it. Everything on the page —
  the workflow, the checks, the automations, what it knows — belongs to the
  project it came from, and showing you one project's page with another
  project's name on it would be worse than a moment's wait.
