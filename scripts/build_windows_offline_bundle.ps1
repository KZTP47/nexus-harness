[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $InstallerPath,
    [string] $OutputDirectory = ''
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$repositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$maximumInstallerBytes = 367001600
$contractFingerprint = 'd85e8a719bc8d49df4fbac3b617736b12aa10b7ff1418d5b6462e26e4d6f55cb'

function Get-CanonicalPath([string] $Path, [string] $What) {
    if (-not $Path -or -not [IO.Path]::IsPathRooted($Path)) {
        throw "$What is not an absolute path: $Path"
    }
    try {
        $full = [IO.Path]::GetFullPath($Path)
        $root = [IO.Path]::GetPathRoot($full)
        if ([StringComparer]::OrdinalIgnoreCase.Equals($full, $root)) {
            return $root
        }
        return $full.TrimEnd(
            [IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar
        )
    } catch {
        throw "$What is not a valid absolute path: $Path"
    }
}

function Get-FileSha256([Parameter(Mandatory = $true)] [string] $Path) {
    $stream = $null
    $sha = $null
    try {
        $stream = [IO.File]::Open(
            $Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read
        )
        $sha = [Security.Cryptography.SHA256]::Create()
        return -join ($sha.ComputeHash($stream) | ForEach-Object { $_.ToString('x2') })
    } finally {
        if ($null -ne $sha) { $sha.Dispose() }
        if ($null -ne $stream) { $stream.Dispose() }
    }
}

$installer = Get-Item -LiteralPath (Get-CanonicalPath $InstallerPath 'The stable installer')
if (-not $installer -or $installer.PSIsContainer) {
    throw "The stable installer does not exist: $InstallerPath"
}
$nameMatch = [Regex]::Match(
    $installer.Name,
    '\ANexus-Harness-Setup-(?<version>[0-9]+(?:\.[0-9]+){2})(?<unsigned>-UNSIGNED)?\.exe\z',
    [Text.RegularExpressions.RegexOptions]::CultureInvariant
)
if (-not $nameMatch.Success) {
    throw "The offline bundle accepts only a stable versioned installer, never a development artifact: $($installer.Name)"
}
$version = $nameMatch.Groups['version'].Value
$packageVersion = [string](
    Get-Content -Raw -LiteralPath (Join-Path $repositoryRoot 'desktop\package.json') |
        ConvertFrom-Json
).version
if ($version -cne $packageVersion) {
    throw "The installer version $version does not match this product source version $packageVersion."
}
if ($installer.Length -le 0 -or $installer.Length -gt $maximumInstallerBytes) {
    throw 'The stable installer has an unsafe or unexpected size.'
}

$checksum = Get-Item -LiteralPath "$($installer.FullName).sha256"
if (-not $checksum -or $checksum.PSIsContainer -or $checksum.Length -le 0 -or $checksum.Length -gt 131072) {
    throw 'The stable installer checksum is missing, empty, or unexpectedly large.'
}
$digest = Get-FileSha256 $installer.FullName
$checksumRecord = (Get-Content -Raw -LiteralPath $checksum.FullName).Trim()
$escapedName = [Regex]::Escape($installer.Name)
$checksumMatch = [Regex]::Match(
    $checksumRecord, "\A(?<hash>[0-9a-fA-F]{64})[ \t]+\*?$escapedName\z",
    [Text.RegularExpressions.RegexOptions]::CultureInvariant
)
if (-not $checksumMatch.Success -or
    $checksumMatch.Groups['hash'].Value.ToLowerInvariant() -cne $digest) {
    throw 'The checksum must contain exactly one matching record for the exact stable installer.'
}

$publisherFile = Join-Path $repositoryRoot 'release\windows-authenticode-publisher.txt'
$signerCertificateSha256File = Join-Path $repositoryRoot `
    'release\windows-authenticode-certificate-sha256.txt'
$publisher = (Get-Content -Raw -LiteralPath $publisherFile).Trim()
$signerCertificateSha256 = (
    Get-Content -Raw -LiteralPath $signerCertificateSha256File
).Trim().ToLowerInvariant()
$publisherConfigured = [bool]$publisher -and -not $publisher.StartsWith('UNCONFIGURED')
$signerCertificateSha256Configured = (
    $signerCertificateSha256 -cmatch '\A[0-9a-f]{64}\z'
)
$publisherExplicitlyUnconfigured = (
    [bool]$publisher -and $publisher.StartsWith('UNCONFIGURED')
)
$signerCertificateSha256ExplicitlyUnconfigured = (
    [bool]$signerCertificateSha256 -and
    $signerCertificateSha256.StartsWith('unconfigured')
)
if ($publisherConfigured -xor $signerCertificateSha256Configured) {
    throw 'Authenticode pin configuration is partial: configure publisher Subject and signer certificate SHA-256 together.'
}
if (-not $publisherConfigured -and
    (-not $publisherExplicitlyUnconfigured -or
     -not $signerCertificateSha256ExplicitlyUnconfigured)) {
    throw 'Authenticode pin configuration is malformed: both pin files must be configured together or explicitly UNCONFIGURED.'
}
$signatureMode = if ($nameMatch.Groups['unsigned'].Success) { 'unsigned' } else { 'signed' }
if (($signatureMode -ceq 'signed') -ne $publisherConfigured) {
    throw 'The stable installer filename and this source tree''s pinned publisher mode disagree.'
}

if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $repositoryRoot 'desktop\build-output'
}
if (-not [IO.Path]::IsPathRooted($OutputDirectory)) {
    $OutputDirectory = Join-Path $repositoryRoot $OutputDirectory
}
if (-not (Test-Path -LiteralPath $OutputDirectory -PathType Container)) {
    New-Item -ItemType Directory -Path $OutputDirectory | Out-Null
}
$outputRoot = Get-CanonicalPath $OutputDirectory 'The offline bundle output folder'
$bundleName = "Nexus-Harness-Windows-Offline-$version"
$archivePath = Join-Path $outputRoot "$bundleName.zip"
if (Test-Path -LiteralPath $archivePath) {
    throw "The offline bundle already exists; refusing to overwrite it: $archivePath"
}

$stage = Join-Path $outputRoot ('.nexus-offline-stage-' + [Guid]::NewGuid().ToString('N'))
$bundleRoot = Join-Path $stage $bundleName
try {
    New-Item -ItemType Directory -Path (Join-Path $bundleRoot 'scripts') -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $bundleRoot 'release') -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $repositoryRoot 'Install Nexus Harness.cmd') -Destination $bundleRoot
    Copy-Item -LiteralPath (Join-Path $repositoryRoot 'scripts\install_nexus_harness.ps1') -Destination (Join-Path $bundleRoot 'scripts')
    Copy-Item -LiteralPath $publisherFile -Destination (Join-Path $bundleRoot 'release')
    Copy-Item -LiteralPath $signerCertificateSha256File -Destination (Join-Path $bundleRoot 'release')
    Copy-Item -LiteralPath $installer.FullName -Destination $bundleRoot
    Copy-Item -LiteralPath $checksum.FullName -Destination $bundleRoot

    $bundledBootstrap = Join-Path $bundleRoot 'scripts\install_nexus_harness.ps1'
    $bootstrapSource = Get-Content -Raw -LiteralPath $bundledBootstrap
    if ([Regex]::Matches(
            $bootstrapSource, [Regex]::Escape('__NEXUS_OFFLINE_BUNDLE_VERSION__')
        ).Count -ne 1 -or
        [Regex]::Matches(
            $bootstrapSource, [Regex]::Escape('__NEXUS_OFFLINE_INSTALLER_SHA256__')
        ).Count -ne 1) {
        throw 'The offline bootstrap product-binding placeholders are missing or ambiguous.'
    }
    $bootstrapSource = $bootstrapSource.Replace('__NEXUS_OFFLINE_BUNDLE_VERSION__', $version)
    $bootstrapSource = $bootstrapSource.Replace('__NEXUS_OFFLINE_INSTALLER_SHA256__', $digest)
    Set-Content -LiteralPath $bundledBootstrap -Value $bootstrapSource -Encoding utf8 -NoNewline

    # The outer CMD is patched only after the final bootstrap bytes exist. It
    # verifies this exact digest before PowerShell parses or executes the PS1.
    $bootstrapDigest = Get-FileSha256 $bundledBootstrap
    $bundledCmd = Join-Path $bundleRoot 'Install Nexus Harness.cmd'
    $cmdSource = Get-Content -Raw -LiteralPath $bundledCmd
    if ([Regex]::Matches(
            $cmdSource, [Regex]::Escape('__NEXUS_OFFLINE_MODE__')
        ).Count -ne 1 -or
        [Regex]::Matches(
            $cmdSource, [Regex]::Escape('__NEXUS_OFFLINE_BOOTSTRAP_SHA256__')
        ).Count -ne 1) {
        throw 'The CMD offline-mode/bootstrap-digest placeholders are missing or ambiguous.'
    }
    $cmdSource = $cmdSource.Replace('__NEXUS_OFFLINE_MODE__', '1')
    $cmdSource = $cmdSource.Replace('__NEXUS_OFFLINE_BOOTSTRAP_SHA256__', $bootstrapDigest)
    Set-Content -LiteralPath $bundledCmd -Value $cmdSource -Encoding ascii -NoNewline

    [ordered]@{
        schema_version = 2
        contract = 'nexus-harness.windows-offline-bundle'
        contract_fingerprint = $contractFingerprint
        product = 'Nexus Harness'
        version = $version
        installer = $installer.Name
        checksum = $checksum.Name
        installer_bytes = $installer.Length
        installer_sha256 = $digest
        signature_mode = $signatureMode
        publisher = if ($publisherConfigured) { $publisher } else { '' }
        signer_certificate_sha256 = if ($publisherConfigured) {
            $signerCertificateSha256
        } else { '' }
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $bundleRoot 'Nexus-Harness-Offline-Bundle.json') -Encoding utf8
    @(
        'Nexus Harness offline Windows installer',
        '',
        'Keep every file and subfolder together, then double-click Install Nexus Harness.cmd.',
        'The CMD verifies the exact bundled PowerShell bootstrap before execution. That bootstrap',
        'then verifies the exact installer hash, Windows product metadata, release version,',
        'and both Authenticode publisher/certificate pins when signing is configured.',
        'No network, Python, Node.js, or administrator account is required.',
        '',
        'For an unsigned release, provenance still depends on obtaining this complete ZIP from the',
        'trusted Nexus Harness GitHub Release or an authenticated company distribution channel.',
        'The outer ZIP and CMD are not self-authenticating; a modified CMD could remove its checks.'
    ) | Set-Content -LiteralPath (Join-Path $bundleRoot 'README-OFFLINE.txt') -Encoding utf8

    # The bundle validates itself with the Windows-owned host that customers'
    # top-level CMD uses.  Never let the build working directory or PATH select
    # an unrelated executable with this privileged product-packaging role.
    $windowsPowerShell = Join-Path ([Environment]::SystemDirectory) `
        'WindowsPowerShell\v1.0\powershell.exe'
    if (-not (Test-Path -LiteralPath $windowsPowerShell -PathType Leaf)) {
        throw 'Windows did not provide its built-in Windows PowerShell host for offline-bundle validation.'
    }
    & $windowsPowerShell -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
        -File $bundledBootstrap -BundleRoot $bundleRoot -ValidateOfflineBundleOnly
    if ($LASTEXITCODE -ne 0) {
        throw "The staged offline bundle failed its own pre-execution validation (exit $LASTEXITCODE)."
    }

    $stagedArchive = Join-Path $stage "$bundleName.zip"
    Compress-Archive -LiteralPath $bundleRoot -DestinationPath $stagedArchive -CompressionLevel NoCompression
    if (-not (Test-Path -LiteralPath $stagedArchive -PathType Leaf)) {
        throw 'The offline bundle archive was not created.'
    }
    Move-Item -LiteralPath $stagedArchive -Destination $archivePath
    Write-Host "Product-bound offline bundle created: $archivePath"
    [pscustomobject]@{
        SchemaVersion = 2
        Version = $version
        InstallerSha256 = $digest
        BootstrapSha256 = $bootstrapDigest
        ArchivePath = $archivePath
    }
} finally {
    if (Test-Path -LiteralPath $stage) {
        Remove-Item -LiteralPath $stage -Recurse -Force
    }
}
