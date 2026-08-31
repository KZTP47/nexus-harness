# Releasing the Windows desktop app

Public Windows releases are versioned, clean-runner builds. The GitHub Actions
workflow downloads the pinned CPython 3.11 runtime, installs the exact packages
from `requirements-runtime.lock` into it, builds NSIS, exercises a genuinely
fresh first run with `--project`, silently installs the package, and exercises
that installed executable too.

## Release trust modes

The repository cannot manufacture a trustworthy publisher identity. When the
project has an Authenticode certificate, configure both repository Actions
secrets before pushing a `vX.Y.Z` tag:

- `WINDOWS_CERTIFICATE_BASE64`: the base64 bytes of the organisation's
  password-protected Authenticode `.pfx` certificate.
- `WINDOWS_CERTIFICATE_PASSWORD`: that certificate's password.

Also replace the `UNCONFIGURED` sentinel in
`release/windows-authenticode-publisher.txt` with the certificate's exact
Windows `SignerCertificate.Subject` (for example the complete `CN=..., O=...`
string). This committed value is the installer trust anchor. Both CI and the
source install helpers require an exact, case-sensitive match; a merely valid
signature from some other publisher is rejected. Do not guess this value.

Electron Builder receives the certificate through `CSC_LINK` and
`CSC_KEY_PASSWORD`. A signed tag build fails after packaging unless Windows
reports a valid Authenticode signature with the exact pinned signer. The source
installer checks both the published SHA-256 file and Authenticode before it
executes a signed release.

When those secrets are absent, a tag deliberately produces
`*-UNSIGNED.exe`, verifies that Windows reports `NotSigned`, installs and
smoke-tests that exact package on the clean runner, and publishes it with an
explicit unknown-publisher warning plus SHA-256. The source bootstrap accepts
that mode only while the publisher file remains `UNCONFIGURED`, and only when
the asset is explicitly named `-UNSIGNED.exe`; it never silently downgrades a
configured signed release. The checksum proves downloaded bytes match the
immutable GitHub release, but it is not a publisher identity and Windows may
show SmartScreen or unknown-publisher warnings.

An untagged manual `workflow_dispatch` artifact and local `npm run build`
remain `*-UNSIGNED-DEV.exe` development output. They are not published by the
release job or accepted by the public-release bootstrap.

## Download size and updates

The private CPython runtime and locked dependencies deliberately make the
Windows installer substantial: the measured 0.2.1 development installer is
about 234 MiB and the unpacked application is about 810 MiB. Release notes must
state the final measured sizes; users should allow at least 2 GiB while
installing for the download, temporary unpacking, and installed app. Nexus does not silently
auto-update. The running About and diagnostics
page shows the installed version/commit; obtain updates from the same official
GitHub Releases page and let the new per-user installer replace the old build.

## Immutable publication contract

The tag must equal `v` plus `desktop/package.json`'s version. The publish job
refuses to run when a release with that tag already exists and never uses an
overwrite/clobber option. Correct a failed candidate with a new version and a
new tag; do not replace assets beneath an existing version.

Do not push a tag until the local test suite and the mandatory persistent
memory post-work deployment gate pass.
