# Look it up

Three questions you ask all day:

- **Where is it?** - take me to where this is defined.
- **What uses it?** - who calls this, before I change it.
- **What is it?** - what does this take, what does it give back.

Open `harness ui` and go to **Look it up**.

---

## Exact answers and guesses

This is the whole point, so it is said on every answer.

**Exact** means a tool built for that language was asked, and it read the code
the way a compiler reads it. Two things with the same name are two different
things to it.

**A guess** means the files were read and the text matched. It is often right,
and it is right in a way that cannot tell `add_up` in your code from `add_up` in
a comment or in somebody else's library.

A guess called a guess is useful. A guess called an answer sends you to the
wrong place, so this never does that.

---

## How to get an exact answer

Give it a **file and a line**, not only a name. There is nothing for a tool to
point at in a bare name, so a name on its own is always a search.

The quickest way: type a name, press **Where is it?**, then click one of the
places it found. That fills in the file and the line for you. Press any of the
three again and the answer is exact.

---

## The tools

One per language. Each is free, needs no account, and is the same tool your
editor already uses. The panel lists them and says which are installed, and how
to get the ones that are not.

| Language | Tool | How to get it |
| --- | --- | --- |
| Python | pylsp | `python -m pip install python-lsp-server` |
| Python | Pyright | `npm install -g pyright` |
| TypeScript, JavaScript | typescript-language-server | `npm install -g typescript typescript-language-server` |
| C, C++ | clangd | Your package manager, or with LLVM |
| Rust | rust-analyzer | `rustup component add rust-analyzer` |
| Go | gopls | `go install golang.org/x/tools/gopls@latest` |

Installing one is enough. The harness picks the one that fits the file you
asked about.

If a tool is installed as a Python package but its launcher is not on your
path - which happens often on Windows - the harness finds it anyway.

---

## What it will not do

- It changes nothing. It reads.
- It cannot look outside your project.
- It starts a tool, asks one thing, and stops it again. Nothing is left
  running.
- Without a tool for that language, it says so and gives you the guess, rather
  than pretending.

---

## From the command line

```bash
harness look-up --asking where-is-it --name add_up
```

```bash
harness look-up --asking what-uses-it --path src/basket.py --line 42 --column 5
```

`--asking` takes `where-is-it`, `what-uses-it` or `what-is-it`.
