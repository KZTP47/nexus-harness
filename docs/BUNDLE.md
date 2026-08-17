# Sending somebody your evidence

When a check fails on your machine and nobody else can reproduce it, the useful
thing to send is not a screenshot of a terminal.

```bash
harness bundle
```

That writes one zip under `.harness/bundles/`, holding:

| Part | What it is |
|---|---|
| `checks` | The check suites themselves, so somebody else can run them |
| `runs` | The last few runs, with what each check saw, including screenshots |
| `history` | How the checks have behaved over time |
| `settings` | The project settings, with credentials taken out |
| `machine` | System, Python version, harness version |

Choose parts when you want less:

```bash
harness bundle --part checks --part runs
harness bundle --part all --runs 20
harness bundle --output reports/support.zip
```

## What it does about secrets

Every text file that goes in is passed through the same credential remover the
harness uses everywhere else, so keys, tokens, and passwords come out as
`[REDACTED]`. Your own home folder name is replaced with `~`, because a support
zip should not tell a stranger your user name.

That is not a promise that the contents are harmless. Read the zip before you
send it on. The note inside says the same.

## What is left out, and why you are told

A single file over 5 MB, or anything over 50 MB in total, is left out. Nothing
is dropped quietly: every skipped file is listed in `manifest.json` inside the
zip, and printed when the bundle is built.

Nothing from `.git` ever goes in, because that folder can hold credentials.

## Reading one back

`manifest.json` inside the zip lists the parts, every file, and everything left
out. The harness reads that file back with the same list of part names it used
to write it, so a bundle from a newer version that holds a part this one does
not know is refused with the name of that part, instead of being half read.

This is worth saying plainly because the older tool this replaces got it wrong:
the screen that asked for the file name saved it under one name and the part
that built the zip read a different one, so the name a person typed was quietly
ignored and the whole feature never worked as written. One list of names, used
everywhere, is the fix.
