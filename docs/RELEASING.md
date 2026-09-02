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

Also replace both committed `UNCONFIGURED` sentinels:

- `release/windows-authenticode-publisher.txt` is the certificate's exact
  Windows `SignerCertificate.Subject` (for example the complete `CN=..., O=...`
  string).
- `release/windows-authenticode-certificate-sha256.txt` is the lowercase
  SHA-256 of that end-entity certificate's DER `RawData`, not the PFX file and
  not its SHA-1 thumbprint.

The two values are one versioned signer-identity pin. CI and the install helper
require both or neither, compare both exactly, and reject a valid certificate
with a lookalike Subject. Do not guess either value. The release workflow reads
the password-protected PFX and verifies both pins before giving it to Electron
Builder.

Electron Builder receives the certificate through `CSC_LINK` and
`CSC_KEY_PASSWORD`. A signed tag build fails after packaging unless Windows
reports a valid Authenticode signature with the exact pinned Subject and
certificate SHA-256. The source installer checks the published SHA-256 file,
Windows product metadata, and both Authenticode pins before it executes a signed
release. It repeats those metadata/version/signature checks against the
installed `Nexus Harness.exe` before accepting the shortcut.

When those secrets are absent, a tag deliberately produces
`*-UNSIGNED.exe`, verifies that Windows reports `NotSigned`, installs and
smoke-tests that exact package on the clean runner, and publishes it with an
explicit unknown-publisher warning plus SHA-256. The source bootstrap accepts
that mode only while both signer pin files remain `UNCONFIGURED`, and only when
the asset is explicitly named `-UNSIGNED.exe`; it never silently downgrades a
configured signed release. The checksum proves downloaded bytes match the
immutable GitHub release, but it is not a publisher identity and Windows may
show SmartScreen or unknown-publisher warnings.

An untagged manual `workflow_dispatch` artifact and local `npm run build`
remain `*-UNSIGNED-DEV.exe` development output. They are not published by the
release job or accepted by the public-release bootstrap.

## Download size and updates

The private CPython runtime and locked dependencies deliberately make the
Windows installer substantial: the measured 0.2.3 development installer is
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

The offline manifest contract is schema v2. Its exact ordered field contract is
`nexus-harness.windows-offline-bundle:v2|schema_version|contract|contract_fingerprint|product|version|installer|checksum|installer_bytes|installer_sha256|signature_mode|publisher|signer_certificate_sha256`,
whose lowercase SHA-256 is
`d85e8a719bc8d49df4fbac3b617736b12aa10b7ff1418d5b6462e26e4d6f55cb`.
The bundle builder first writes the final product-bound PowerShell bootstrap,
then pins that exact file's SHA-256 into the bundled CMD. The bundled CMD hashes
an open stream, decodes those same verified bytes, and executes them in memory
without resolving the mutable source path again. It supplies the explicit
bundle resource root and offline-only mode; incomplete local material never
turns into an online download.

That inner binding does not make an unsigned outer ZIP or CMD
self-authenticating: an attacker who can replace the CMD can remove its check.
Distribute the ZIP only through the immutable GitHub Release or an authenticated
company channel. Signed releases additionally retain the operating system's
publisher trust.

Immediately before execution, the bootstrap copies the already validated
installer and checksum into a random per-user temporary directory created with
a protected current-user+SYSTEM ACL. It preserves and byte-verifies an existing
`Zone.Identifier` stream, then rechecks name, size, version metadata, hash, and
signature on that private copy. It deletes the private directory after the
installer exits, including failure paths.

The GitHub account must also be able to allocate both Windows and Ubuntu Actions
runners. A source tag is not a release. After publication, the workflow polls
GitHub's latest-release API without credentials, requires the exact tag,
installer, checksum, and product-bound offline ZIP, anonymously downloads all
three assets, compares their bytes with the verified build artifacts, and
verifies the published SHA-256. Publication also requires the release tag and
public `master` to identify the same commit. It then starts a second clean
Windows runner, downloads GitHub's actual public `master` source ZIP into an
awkward path, and invokes the unmodified top-level
`Install Nexus Harness.cmd` from an unrelated working directory with no token
or sibling installer. That final
consumer-path check must install the published version, read back the exact
desktop shortcut target, working folder, arguments, and icon, and launch the
installed executable through the `.lnk`.
The real-package Windows job installs the package, validates the desktop
shortcut, removes it, reinstalls to prove it is recreated exactly once, and
launches the app through that shortcut before publication may proceed. The
operator should still inspect the completed release page before distributing
its link.

The field bootstrap intentionally does not launch and kill the application:
Electron single-instance routing means a shortcut launch can attach to a
pre-existing user process, which the bootstrap cannot safely claim or stop.
Bootstrap acceptance proves installed executable identity and exact shortcut
target/icon wiring. Clean-runner release CI (and the local packaged deployment
gate) owns the bounded first-launch/runtime acceptance with isolated process
tracking.

The field verifier treats the current user's policy-redirected Desktop as the
owned shortcut location. It scans the Common Desktop for duplicates when that
folder is present and listable, but Common Desktop is optional because company
policy can deny standard users access to it.

`scripts/install_nexus_harness.py` is retained only as a fail-closed
compatibility import/CLI shim. It does not download or execute an installer.
This prevents an older Python invocation from bypassing the product-bound CMD
and PowerShell trust path; users must use `Install Nexus Harness.cmd`.
