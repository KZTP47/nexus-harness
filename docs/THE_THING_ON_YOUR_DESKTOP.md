# The Nexus Harness icon on your desktop

For normal use, install the packaged Windows app. You do not need Python,
Node.js, a terminal, or administrator access.

## Install the offline release

Download `Nexus-Harness-Windows-Offline-<version>.zip` from the stable GitHub
Release or an authenticated company distribution channel. Extract the complete
ZIP, keep every file and subfolder together, and double-click
**`Install Nexus Harness.cmd`** inside it. Windows PowerShell then:

1. reads the versioned v2 identity manifest beside the `.cmd`;
2. requires exactly one stable installer and checksum;
3. verifies the build-bound version, byte length, SHA-256, and Windows product
   metadata;
4. enforces the bundle's pinned Authenticode mode; and
5. starts the per-user installer and verifies its real desktop shortcut.

This local path makes no network or credential probe and needs no Python,
Node.js, terminal, or administrator account. It works after the extracted
folder is copied to an arbitrary local or OneDrive path, including paths with
spaces and non-ASCII characters.

For unattended company deployment in the signed-in user's context, set
`NEXUS_INSTALLER_SILENT=1` before running `Install Nexus Harness.cmd`. Any other
value is rejected. Silent mode still performs the same bundle, version,
signature, installed-executable, registry, and desktop-shortcut verification.

An unsigned release is accepted only when its exact filename says `UNSIGNED`,
Windows reports `NotSigned`, and the bootstrap in that product-built ZIP is
bound to its exact version and digest. Provenance still depends on obtaining
the complete unsigned ZIP through a trusted distribution channel; a checksum
supplied by an otherwise untrusted folder is not a digital signature. A future
release with a pinned publisher must have a valid signature from that exact
publisher and pinned DER-certificate SHA-256.

Company policy remains an external boundary: AppLocker, WDAC, SmartScreen, or
endpoint security can block an unsigned installer or quarantine its bundled
runtime. Universal managed-PC deployment therefore requires an Authenticode
certificate matching the pinned publisher plus any IT allowlist or software
distribution approval. The bootstrap reports such a block but cannot bypass
it.

## Install from a cloned repository

Double-click **`Install Nexus Harness.cmd`** at the repository root. If no
Nexus installer material is beside it, the helper uses Windows PowerShell to
download the latest stable installer and matching checksum from GitHub,
validate them under the same product/signature contract, and install them. A
source checkout intentionally cannot authenticate a hand-assembled unsigned
offline bundle: use the complete product-built ZIP, including its `scripts`
and `release` subfolders.

If any local Nexus bundle material is present but missing, corrupt, ambiguous,
version-mismatched, or development-only (`-UNSIGNED-DEV.exe`), the helper stops
without running anything and without silently switching to GitHub.

After installation, open **Nexus Harness** from the desktop or Start menu. The
installed app carries its own Python runtime and dependencies, so the cloned
repository can be moved or removed without breaking that shortcut.

The repository is currently public, so a published public release needs no
GitHub login. The helper never invokes `gh`, `git`, or a credential manager. If
a private fork or a future visibility change is involved, provide a
process-scoped `GH_TOKEN` or `GITHUB_TOKEN`; otherwise the request is anonymous.
You can instead download both release assets in a signed-in browser and verify
the checksum manually.

If you download the two `0.2.4` assets in a browser, open PowerShell in the
download folder and run:

```powershell
$actual = (Get-FileHash -Algorithm SHA256 -LiteralPath '.\Nexus-Harness-Setup-0.2.4-UNSIGNED.exe').Hash.ToLowerInvariant()
$expected = ((Get-Content -Raw -LiteralPath '.\Nexus-Harness-Setup-0.2.4-UNSIGNED.exe.sha256') -split '\s+')[0].ToLowerInvariant()
$actual -eq $expected
```

The last command must print `True` before you run the installer.

## If installation stops

The helper stops safely and prints the exact reason. Common causes are:

- **No stable release exists.** Open the repository's
  [Releases page](https://github.com/KZTP47/nexus-harness/releases) and
  confirm a release is published rather than only a source tag.
- **GitHub says the repository or release was not found.** For this public
  repository, first check the Releases page: authentication cannot create a
  release that has not been published. For a private fork with a confirmed
  release, provide a process-scoped `GH_TOKEN` or `GITHUB_TOKEN`, then run it
  again. Signing in to a CLI, credential manager, or browser does not
  authenticate the helper; in the browser case, download both assets there.
- **The checksum or signature mode does not match.** Do not run the downloaded
  file. Download both assets again; if the mismatch remains, report the release
  as broken.
- **The local material is not product-bound.** Do not combine an installer with
  the `.cmd` from a source checkout. Re-extract the complete offline ZIP so its
  manifest and digest-bound bootstrap stay together.
- **Windows warns about an unknown publisher.** That is expected only for a
  release whose filename and release metadata explicitly say `UNSIGNED`.
- **There is not enough disk space.** The measured `0.2.3` development installer
  is about 234 MiB and the app about 810 MiB unpacked. Confirm a published
  release's exact sizes in its notes and allow at least 2 GiB free during
  installation.

Running `Install Nexus Harness.cmd` again is safe: it revalidates the same
offline bundle (or downloads and validates the stable release when no local
material exists) before starting it. A normal reinstall also
restores a missing **Nexus Harness** desktop shortcut and verifies that exactly
one such link exists, targets the installed app, and uses that same executable
as its icon before reporting success.

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

The source browser-window fallback supports **Work on project files** through
the authenticated local server journal. It verifies the exact request, chat,
project, lead, and intent receipt before clearing the draft, and an interrupted
prepare remains visible for explicit reconciliation after a server restart or
port change. A packaged window still writes the Electron outbox first as a
second durable authority. If an Electron bridge object exists but is missing
any outbox operation, Nexus stops before the backend prepare instead of
silently downgrading a broken packaged build to browser behavior.

The shared icon lives at `desktop/nexus-harness.ico` and is embedded in the
packaged application and its installed shortcuts.

## Build the distributable offline ZIP

After producing a stable-named installer and its exact sibling `.sha256`, a
release builder can assemble the same artifact locally without network access:

```powershell
$installer = Join-Path (Get-Location) 'Nexus-Harness-Setup-1.2.3-UNSIGNED.exe'
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\build_windows_offline_bundle.ps1 -InstallerPath $installer
```

The script rejects `-UNSIGNED-DEV.exe`, version drift, checksum drift, and an
existing output ZIP. The tagged Windows release workflow invokes this same
script and publishes the resulting offline ZIP alongside the installer and
checksum.
