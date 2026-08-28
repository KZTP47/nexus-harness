# Releasing the Windows desktop app

Public Windows releases are versioned, clean-runner builds. The GitHub Actions
workflow downloads the pinned CPython 3.11 runtime, installs the exact packages
from `requirements-runtime.lock` into it, builds NSIS, exercises a genuinely
fresh first run with `--project`, silently installs the package, and exercises
that installed executable too.

## Signing is a release precondition

The repository cannot manufacture a trustworthy publisher identity. Before
pushing a `vX.Y.Z` tag, configure both repository Actions secrets:

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
`CSC_KEY_PASSWORD`. A tag build fails before packaging if either secret is
missing, and fails after packaging unless Windows reports a valid
Authenticode signature with a signer certificate. The source installer checks
both the published SHA-256 file and Authenticode before it executes anything.

A manual `workflow_dispatch` may run without a certificate for engineering
diagnosis. Its artifact is renamed `*-UNSIGNED-DEV.exe`; it is never published
as a GitHub release and must not be presented as an installable public build.
Local `npm run build` output is likewise unsigned development output unless a
real certificate was explicitly supplied.

## Download size and updates

The private CPython runtime and locked dependencies deliberately make the
Windows installer substantial: the current 0.2.0 development installer is
about 126 MiB and the unpacked application is about 434 MiB. Release notes must
state the measured sizes; users should allow at least 600 MiB while installing
for the app plus temporary installation space. Nexus does not silently
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
