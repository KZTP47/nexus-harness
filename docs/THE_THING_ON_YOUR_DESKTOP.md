# The Nexus Harness icon on your desktop

For normal use, install the packaged Windows app. You do not need Python,
Node.js, a terminal, or administrator access.

## Install from a cloned repository

Double-click **`Install Nexus Harness.cmd`** at the top of the repository. The
helper uses Windows PowerShell to:

1. find the latest stable release on GitHub;
2. download its Windows installer and matching `.sha256` file;
3. verify the SHA-256 before anything is run;
4. enforce the signature mode declared by that release; and
5. start the per-user installer.

The current `0.2.1` release is explicitly unsigned. Its installer must therefore
be named `Nexus-Harness-Setup-0.2.1-UNSIGNED.exe`, match the published checksum,
and have Windows' `NotSigned` status. Windows may display an unknown-publisher
warning. A future release that pins a publisher certificate will instead be
required to have a valid signature from that exact publisher.

After installation, open **Nexus Harness** from the desktop or Start menu. The
installed app carries its own Python runtime and dependencies, so the cloned
repository can be moved or removed without breaking that shortcut.

The repository is currently private. On a new computer, either:

- sign in with GitHub CLI (`gh auth login`);
- have Git Credential Manager already signed in;
- provide a process-scoped `GH_TOKEN`; or
- download both release assets in a signed-in browser and verify the checksum
  manually.

The helper reuses authentication but does not print or save the credential.

If you download the two `0.2.1` assets in a browser, open PowerShell in the
download folder and run:

```powershell
$actual = (Get-FileHash -Algorithm SHA256 -LiteralPath '.\Nexus-Harness-Setup-0.2.1-UNSIGNED.exe').Hash.ToLowerInvariant()
$expected = ((Get-Content -Raw -LiteralPath '.\Nexus-Harness-Setup-0.2.1-UNSIGNED.exe.sha256') -split '\s+')[0].ToLowerInvariant()
$actual -eq $expected
```

The last command must print `True` before you run the installer.

## If installation stops

The helper stops safely and prints the exact reason. Common causes are:

- **No stable release exists.** Open the repository's
  [Releases page](https://github.com/KZTP47/nexus-harness/releases/latest) and
  confirm a release is published rather than only a source tag.
- **GitHub says the repository or release was not found.** Authenticate the
  helper with GitHub CLI, Git Credential Manager, or a process-scoped
  `GH_TOKEN`, then run it again. Signing in only inside a browser does not
  authenticate the helper; in that case download both assets in the browser.
- **The checksum or signature mode does not match.** Do not run the downloaded
  file. Download both assets again; if the mismatch remains, report the release
  as broken.
- **Windows warns about an unknown publisher.** That is expected only for a
  release whose filename and release metadata explicitly say `UNSIGNED`.
- **There is not enough disk space.** The `0.2.1` installer is about 234 MiB and
  the app about 810 MiB unpacked. Allow at least 2 GiB free during installation.

Running `Install Nexus Harness.cmd` again is safe: it downloads and validates
the current stable installer again before starting it.

## Source-development shortcut

Developers who intentionally want a shortcut tied to this checkout can run:

```bash
python scripts/put_it_on_your_desktop.py
```

That command is separate from the packaged-app installer. It chooses the best
compatible local target: a desktop app built in this checkout first, a
compatible installed app second, and a source browser-window fallback when no
desktop app is usable. The shortcut is tied to the current clone, so moving or
deleting the repository breaks it. Normal users should use the packaged app
instead.

The shared icon lives at `desktop/nexus-harness.ico` and is embedded in the
packaged application and its installed shortcuts.
