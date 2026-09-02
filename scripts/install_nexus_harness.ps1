[CmdletBinding()]
param(
    [string] $BundleRoot = '',
    [switch] $LoadFunctionsOnly,
    [switch] $ValidateOfflineBundleOnly,
    [switch] $OfflineOnly,
    [string] $BootstrapResourceRoot = ''
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$repository = 'KZTP47/nexus-harness'
$allowedHosts = @('api.github.com', 'github.com', 'objects.githubusercontent.com', 'release-assets.githubusercontent.com')
$maximumInstallerBytes = 367001600
$offlineBundleManifestName = 'Nexus-Harness-Offline-Bundle.json'
$offlineBundleContract = 'nexus-harness.windows-offline-bundle'
$offlineBundleSchemaVersion = 2
# SHA-256 of the immutable v2 contract field sequence documented beside the
# release workflow. It prevents a future bootstrap from guessing at a changed
# manifest or installation contract.
$offlineBundleContractFingerprint = 'd85e8a719bc8d49df4fbac3b617736b12aa10b7ff1418d5b6462e26e4d6f55cb'
# Stable release packaging replaces these two exact placeholders only in the
# bootstrap copied into that product-built offline ZIP. A source checkout has
# no authority to trust a sibling unsigned executable merely because that same
# folder also supplies its checksum and manifest.
$offlineBundlePinnedVersion = '__NEXUS_OFFLINE_BUNDLE_VERSION__'
$offlineBundlePinnedInstallerSha256 = '__NEXUS_OFFLINE_INSTALLER_SHA256__'
$bootstrapScriptRoot = if ($BootstrapResourceRoot) {
    if (-not [IO.Path]::IsPathRooted($BootstrapResourceRoot)) {
        throw 'The verified bootstrap resource root is not absolute; nothing was run.'
    }
    [IO.Path]::GetFullPath($BootstrapResourceRoot)
} else {
    $PSScriptRoot
}
if (-not $bootstrapScriptRoot) {
    throw 'The installer bootstrap resource root is unavailable; nothing was run.'
}
$publisherFile = Join-Path $bootstrapScriptRoot '..\release\windows-authenticode-publisher.txt'
$signerCertificateSha256File = Join-Path $bootstrapScriptRoot '..\release\windows-authenticode-certificate-sha256.txt'
if (-not (Test-Path -LiteralPath $publisherFile -PathType Leaf)) {
    throw 'The pinned Windows publisher identity is missing; nothing was run.'
}
if (-not (Test-Path -LiteralPath $signerCertificateSha256File -PathType Leaf)) {
    throw 'The pinned Authenticode signer certificate SHA-256 is missing; nothing was run.'
}
$expectedPublisher = (Get-Content -Raw -LiteralPath $publisherFile).Trim()
$expectedSignerCertificateSha256 = (
    Get-Content -Raw -LiteralPath $signerCertificateSha256File
).Trim().ToLowerInvariant()
$publisherConfigured = (
    [bool]$expectedPublisher -and -not $expectedPublisher.StartsWith('UNCONFIGURED')
)
$signerCertificateSha256Configured = (
    $expectedSignerCertificateSha256 -cmatch '\A[0-9a-f]{64}\z'
)
$publisherExplicitlyUnconfigured = (
    [bool]$expectedPublisher -and $expectedPublisher.StartsWith('UNCONFIGURED')
)
$signerCertificateSha256ExplicitlyUnconfigured = (
    [bool]$expectedSignerCertificateSha256 -and
    $expectedSignerCertificateSha256.StartsWith('unconfigured')
)
if ($publisherConfigured -xor $signerCertificateSha256Configured) {
    throw 'Authenticode pin configuration is partial: configure both the exact publisher Subject and signer certificate SHA-256, or leave both explicitly UNCONFIGURED.'
}
if (-not $publisherConfigured -and
    (-not $publisherExplicitlyUnconfigured -or
     -not $signerCertificateSha256ExplicitlyUnconfigured)) {
    throw 'Authenticode pin configuration is malformed: both pin files must be configured together or explicitly UNCONFIGURED.'
}

function Get-GitHubHeaders {
    $headers = @{ Accept = 'application/vnd.github+json'; 'User-Agent' = 'Nexus-Harness-Installer' }
    $token = [string]$env:GH_TOKEN
    if (-not $token) { $token = [string]$env:GITHUB_TOKEN }
    # Credential authority is explicit only. Never execute PATH-resolved gh,
    # git, credential helpers, profiles, or unrelated company-PC commands.
    if ($token) { $headers.Authorization = "Bearer $token" }
    return $headers
}

function Get-InstallationFailureMessage(
    [string] $Message, [hashtable] $Headers, [bool] $LatestReleaseMetadataDownloaded
) {
    if (-not $LatestReleaseMetadataDownloaded -and
        $Message -ceq 'GitHub returned HTTP 404 while downloading a release asset.') {
        return 'No installable Nexus Harness release is available. GitHub returned HTTP 404 for the repository''s latest stable release before any release metadata was downloaded. No Windows installer ran, so no desktop shortcut could be created. Publish a stable release, or if one already exists verify that this process can see the configured repository, then retry.'
    }
    return $Message
}

function Get-NexusStableReleaseAssets([Parameter(Mandatory = $true)] [object] $Release) {
    $tag = [string]$Release.tag_name
    if ($Release.draft -or $Release.prerelease -or -not $tag) {
        throw 'There is no stable Nexus Harness release to install yet.'
    }
    $tagMatch = [Regex]::Match(
        $tag, '\Av(?<version>[0-9]+(?:\.[0-9]+){2})\z',
        [Text.RegularExpressions.RegexOptions]::CultureInvariant
    )
    if (-not $tagMatch.Success) {
        throw "The latest stable release tag '$tag' is not an exact Nexus Harness version; nothing was run."
    }
    $releaseVersion = $tagMatch.Groups['version'].Value
    $installerPattern = '\ANexus-Harness-Setup-(?<version>[0-9]+(?:\.[0-9]+){2})(?:-UNSIGNED)?\.exe\z'
    $checksumPattern = '\ANexus-Harness-Setup-[0-9]+(?:\.[0-9]+){2}(?:-UNSIGNED)?\.exe\.sha256\z'
    $productInstallerAssets = @($Release.assets | Where-Object {
        [string]$_.name -match '\ANexus-Harness-Setup-.*\.exe\z'
    })
    $productChecksumAssets = @($Release.assets | Where-Object {
        [string]$_.name -match '\ANexus-Harness-Setup-.*\.exe\.sha256\z'
    })
    $installers = @($Release.assets | Where-Object {
        [Regex]::IsMatch(
            [string]$_.name, $installerPattern,
            [Text.RegularExpressions.RegexOptions]::CultureInvariant
        )
    })
    $checksums = @($Release.assets | Where-Object {
        [Regex]::IsMatch(
            [string]$_.name, $checksumPattern,
            [Text.RegularExpressions.RegexOptions]::CultureInvariant
        )
    })
    if ($productInstallerAssets.Count -ne 1 -or $productChecksumAssets.Count -ne 1 -or
        $installers.Count -ne 1 -or $checksums.Count -ne 1) {
        throw 'The stable release does not contain exactly one versioned installer and checksum; nothing was run.'
    }
    $installerMatch = [Regex]::Match(
        [string]$installers[0].name, $installerPattern,
        [Text.RegularExpressions.RegexOptions]::CultureInvariant
    )
    if (-not $installerMatch.Success -or
        $installerMatch.Groups['version'].Value -cne $releaseVersion) {
        throw "The latest stable release tag '$tag' does not match installer '$($installers[0].name)'; nothing was run."
    }
    if ([string]$checksums[0].name -cne "$($installers[0].name).sha256") {
        throw 'The stable release checksum does not name the exact versioned installer; nothing was run.'
    }
    return [pscustomobject]@{
        Version = $releaseVersion
        Installer = $installers[0]
        Checksum = $checksums[0]
    }
}

function Get-NexusCanonicalPath([string] $Path, [string] $What) {
    if (-not $Path) { throw "$What is empty." }
    if (-not [IO.Path]::IsPathRooted($Path)) { throw "$What is not an absolute path: $Path" }
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

function Get-NexusFileSha256([Parameter(Mandatory = $true)] [string] $Path) {
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

function Get-NexusLocalBundleAssets([string] $RequestedBundleRoot = '') {
    $candidateRoot = if ($RequestedBundleRoot) {
        $RequestedBundleRoot
    } else {
        Join-Path $bootstrapScriptRoot '..'
    }
    $root = Get-NexusCanonicalPath $candidateRoot 'The offline bundle folder'
    if (-not (Test-Path -LiteralPath $root -PathType Container)) {
        throw "The offline bundle folder does not exist: $root"
    }

    # Discovery is deliberately shallow. The .cmd owns only the files beside
    # it; it must never wander through a source checkout or another download.
    $files = @(Get-ChildItem -LiteralPath $root -File -Force)
    $manifests = @($files | Where-Object { $_.Name -ceq $offlineBundleManifestName })
    $manifestLike = @($files | Where-Object {
        $_.Name -like 'Nexus-Harness-Offline-Bundle*.json'
    })
    $installerLike = @($files | Where-Object {
        $_.Name -like 'Nexus-Harness-Setup-*.exe'
    })
    $checksumLike = @($files | Where-Object {
        $_.Name -like 'Nexus-Harness-Setup-*.exe.sha256'
    })
    $hasLocalMaterial = (
        $manifestLike.Count -gt 0 -or
        $installerLike.Count -gt 0 -or
        $checksumLike.Count -gt 0
    )
    if (-not $hasLocalMaterial) { return $null }

    # Once any product installer material is present, fail closed locally.
    # Silently downloading some other build would conceal an incomplete copy,
    # corrupt download, or accidental development artifact.
    if ($manifests.Count -ne 1 -or $manifestLike.Count -ne 1) {
        throw "Offline Nexus Harness installer material is present, but there is not exactly one case-exact $offlineBundleManifestName; the online fallback was not used."
    }
    if ($installerLike.Count -ne 1 -or $checksumLike.Count -ne 1) {
        throw 'The offline bundle must contain exactly one stable Nexus Harness installer and its checksum; the online fallback was not used.'
    }

    try {
        $manifest = Get-Content -Raw -LiteralPath $manifests[0].FullName | ConvertFrom-Json
    } catch {
        throw "The offline bundle manifest is not valid JSON: $($_.Exception.Message)"
    }
    if ($null -eq $manifest -or $manifest -is [Array]) {
        throw 'The offline bundle manifest must be one JSON object.'
    }
    $requiredProperties = @(
        'schema_version', 'contract', 'contract_fingerprint', 'product', 'version',
        'installer', 'checksum', 'installer_bytes', 'installer_sha256',
        'signature_mode', 'publisher', 'signer_certificate_sha256'
    )
    $actualProperties = @($manifest.PSObject.Properties.Name)
    if ($actualProperties.Count -ne $requiredProperties.Count -or
        @($requiredProperties | Where-Object { $actualProperties -cnotcontains $_ }).Count -ne 0) {
        throw 'The offline bundle manifest fields do not exactly match the supported v2 contract.'
    }
    $schemaVersion = $manifest.schema_version
    if (($schemaVersion -isnot [int] -and $schemaVersion -isnot [long]) -or
        [long]$schemaVersion -ne $offlineBundleSchemaVersion) {
        throw "The offline bundle schema version is unsupported; expected $offlineBundleSchemaVersion."
    }
    if ([string]$manifest.contract -cne $offlineBundleContract -or
        [string]$manifest.contract_fingerprint -cne $offlineBundleContractFingerprint) {
        throw 'The offline bundle contract fingerprint is unsupported; nothing was run.'
    }
    if ([string]$manifest.product -cne 'Nexus Harness') {
        throw 'The offline bundle does not identify the exact Nexus Harness product; nothing was run.'
    }
    $version = [string]$manifest.version
    if ($version -notmatch '\A[0-9]+(?:\.[0-9]+){2}\z') {
        throw "The offline bundle version is invalid: $version"
    }
    if ($offlineBundlePinnedVersion -notmatch '\A[0-9]+(?:\.[0-9]+){2}\z' -or
        $version -cne $offlineBundlePinnedVersion) {
        throw 'This bootstrap is not product-bound to the offline bundle version; nothing was run. Use the complete product-built offline ZIP without replacing its scripts.'
    }
    $manifestDigest = [string]$manifest.installer_sha256
    if ($manifestDigest -cnotmatch '\A[0-9a-f]{64}\z' -or
        $offlineBundlePinnedInstallerSha256 -cnotmatch '\A[0-9a-f]{64}\z' -or
        $manifestDigest -cne $offlineBundlePinnedInstallerSha256) {
        throw 'This bootstrap does not trust the offline installer digest; nothing was run. Use the complete product-built offline ZIP without replacing its scripts.'
    }
    $signatureMode = [string]$manifest.signature_mode
    if ($signatureMode -cne 'signed' -and $signatureMode -cne 'unsigned') {
        throw "The offline bundle signature mode is invalid: $signatureMode"
    }
    $expectedInstallerName = if ($signatureMode -ceq 'unsigned') {
        "Nexus-Harness-Setup-$version-UNSIGNED.exe"
    } else {
        "Nexus-Harness-Setup-$version.exe"
    }
    $installerName = [string]$manifest.installer
    $checksumName = [string]$manifest.checksum
    if ($installerName -cne $expectedInstallerName -or
        $checksumName -cne "$expectedInstallerName.sha256") {
        throw 'The offline bundle version, signature mode, installer, and checksum names do not match exactly; nothing was run.'
    }
    if ([IO.Path]::GetFileName($installerName) -cne $installerName -or
        [IO.Path]::GetFileName($checksumName) -cne $checksumName) {
        throw 'The offline bundle manifest may name only direct sibling files; nothing was run.'
    }
    if ($installerLike[0].Name -cne $installerName -or
        $checksumLike[0].Name -cne $checksumName) {
        throw 'The offline bundle manifest does not own the exact sibling installer and checksum; nothing was run.'
    }

    $declaredBytes = $manifest.installer_bytes
    if (($declaredBytes -isnot [int] -and $declaredBytes -isnot [long]) -or
        [long]$declaredBytes -le 0 -or [long]$declaredBytes -gt $maximumInstallerBytes -or
        [long]$installerLike[0].Length -ne [long]$declaredBytes) {
        throw 'The offline installer size does not match its bounded manifest declaration; nothing was run.'
    }
    $declaredPublisher = [string]$manifest.publisher
    $declaredSignerCertificateSha256 = (
        [string]$manifest.signer_certificate_sha256
    ).ToLowerInvariant()
    if ($signatureMode -ceq 'signed') {
        if (-not $publisherConfigured) {
            throw 'The offline bundle requires a signed installer, but this bootstrap has no complete Authenticode identity pin; nothing was run.'
        }
        if ($declaredPublisher -cne $expectedPublisher) {
            throw 'The offline bundle publisher does not match the bootstrap pinned publisher; nothing was run.'
        }
        if ($declaredSignerCertificateSha256 -cne $expectedSignerCertificateSha256) {
            throw 'The offline bundle signer certificate SHA-256 does not match the bootstrap pin; nothing was run.'
        }
    } else {
        if ($publisherConfigured) {
            throw 'This bootstrap pins a Windows publisher and therefore rejects an unsigned offline bundle; nothing was run.'
        }
        if ($declaredPublisher -cne '' -or $declaredSignerCertificateSha256 -cne '') {
            throw 'An unsigned offline bundle must declare empty publisher and signer certificate pins; nothing was run.'
        }
    }

    return [pscustomobject]@{
        Source = 'offline bundle'
        Version = $version
        Tag = "v$version"
        InstallerName = $installerName
        ChecksumName = $checksumName
        InstallerPath = Get-NexusCanonicalPath $installerLike[0].FullName 'The offline installer'
        ChecksumPath = Get-NexusCanonicalPath $checksumLike[0].FullName 'The offline checksum'
        TrustedInstallerSha256 = $manifestDigest
        IsExplicitlyUnsigned = $signatureMode -ceq 'unsigned'
    }
}

function Get-NexusExpectedChecksum(
    [Parameter(Mandatory = $true)] [string] $ChecksumPath,
    [Parameter(Mandatory = $true)] [string] $InstallerName
) {
    $escapedName = [Regex]::Escape($InstallerName)
    $contents = (Get-Content -Raw -LiteralPath $ChecksumPath).Trim()
    $match = [Regex]::Match(
        $contents, "\A(?<hash>[0-9a-fA-F]{64})[ \t]+\*?$escapedName\z",
        [Text.RegularExpressions.RegexOptions]::CultureInvariant
    )
    if (-not $match.Success) {
        throw 'The checksum file must contain exactly one record naming the exact installer; nothing was run.'
    }
    return $match.Groups['hash'].Value.ToLowerInvariant()
}

function Get-NexusAuthenticodeSignature([Parameter(Mandatory = $true)] [string] $Path) {
    # A company profile (or an application host) can prepend PowerShell 7
    # modules to PSModulePath even while this bootstrap runs Windows PowerShell
    # 5.1. Import the security module from this host's own PSHOME explicitly so
    # module shadowing cannot disable signature verification.
    $securityModule = Join-Path $PSHOME `
        'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1'
    if (-not (Test-Path -LiteralPath $securityModule -PathType Leaf)) {
        throw 'Windows PowerShell did not provide its built-in Authenticode verification module; nothing was run.'
    }
    Import-Module -Name $securityModule -Force -ErrorAction Stop
    return Microsoft.PowerShell.Security\Get-AuthenticodeSignature -LiteralPath $Path
}

function Get-NexusCertificateSha256([Parameter(Mandatory = $true)] [object] $Certificate) {
    if ($null -eq $Certificate.RawData -or ([byte[]]$Certificate.RawData).Length -le 0) {
        throw 'The Authenticode signer certificate has no DER data; nothing was run.'
    }
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return -join (
            $sha.ComputeHash([byte[]]$Certificate.RawData) |
                ForEach-Object { $_.ToString('x2') }
        )
    } finally {
        $sha.Dispose()
    }
}

function Assert-NexusInstallerSignature(
    [Parameter(Mandatory = $true)] [string] $InstallerPath,
    [Parameter(Mandatory = $true)] [bool] $IsExplicitlyUnsigned,
    [string] $ArtifactName = 'installer'
) {
    if (-not $publisherConfigured -and -not $IsExplicitlyUnsigned) {
        throw 'No Windows publisher is pinned, but the installer is not explicitly named UNSIGNED; nothing was run.'
    }
    if ($publisherConfigured -and $IsExplicitlyUnsigned) {
        throw 'A Windows publisher is pinned, but the installer is marked UNSIGNED; nothing was run.'
    }
    $signature = Get-NexusAuthenticodeSignature $InstallerPath
    if ($publisherConfigured) {
        if ($signature.Status -ne 'Valid' -or -not $signature.SignerCertificate.Subject) {
            throw "The $ArtifactName does not have a valid Windows signature ($($signature.Status)); nothing was run."
        }
        if ($signature.SignerCertificate.Subject -cne $expectedPublisher) {
            throw "The $ArtifactName is signed by an unexpected publisher ($($signature.SignerCertificate.Subject)); expected $expectedPublisher. Nothing was run."
        }
        $actualSignerCertificateSha256 = Get-NexusCertificateSha256 `
            $signature.SignerCertificate
        if ($actualSignerCertificateSha256 -cne $expectedSignerCertificateSha256) {
            throw "The $ArtifactName signer certificate SHA-256 is unexpected ($actualSignerCertificateSha256); nothing was run."
        }
        Write-Host "Windows signature and signer certificate verified: $($signature.SignerCertificate.Subject) ($actualSignerCertificateSha256)"
    } else {
        if ($signature.Status -ne 'NotSigned' -or $signature.SignerCertificate.Subject) {
            throw "The checksum-only $ArtifactName has an unexpected Windows signature state ($($signature.Status)); nothing was run."
        }
        Write-Warning "This $ArtifactName is not Authenticode-signed. Its release mode is explicitly unsigned."
        Write-Host "Unsigned release mode verified for the $ArtifactName."
    }
}

function Test-NexusFileVersion([string] $Value, [string] $ExpectedVersion) {
    try {
        $actual = [Version]$Value
        $expected = [Version]$ExpectedVersion
        return (
            $actual.Major -eq $expected.Major -and
            $actual.Minor -eq $expected.Minor -and
            $actual.Build -eq $expected.Build -and
            ($actual.Revision -eq -1 -or $actual.Revision -eq 0)
        )
    } catch {
        return $false
    }
}

function Assert-NexusInstallerVersionInfo(
    [Parameter(Mandatory = $true)] [string] $InstallerPath,
    [Parameter(Mandatory = $true)] [string] $ExpectedVersion,
    [string] $ArtifactName = 'installer'
) {
    $versionInfo = (Get-Item -LiteralPath $InstallerPath).VersionInfo
    if ([string]$versionInfo.ProductName -cne 'Nexus Harness' -or
        [string]$versionInfo.CompanyName -cne 'Nexus Harness' -or
        [string]$versionInfo.FileDescription -cne 'Desktop window for the Nexus Harness control panel') {
        throw "The $ArtifactName Windows product metadata does not identify the exact Nexus Harness application; nothing was run."
    }
    if (-not (Test-NexusFileVersion ([string]$versionInfo.FileVersion) $ExpectedVersion) -or
        -not (Test-NexusFileVersion ([string]$versionInfo.ProductVersion) $ExpectedVersion)) {
        throw "The $ArtifactName Windows product version does not match $ExpectedVersion; nothing was run."
    }
}

function Assert-NexusInstalledApplicationMetadata(
    [Parameter(Mandatory = $true)] [string] $ExpectedVersion,
    [Parameter(Mandatory = $true)] [object] $InstallMetadata,
    [Parameter(Mandatory = $true)] [object] $UninstallMetadata
) {
    if ($ExpectedVersion -notmatch '\A[0-9]+(?:\.[0-9]+){2}\z') {
        throw "The expected installed version is invalid: $ExpectedVersion"
    }
    if ([string]$InstallMetadata.KeepShortcuts -cne 'true' -or
        [string]$InstallMetadata.ShortcutName -cne 'Nexus Harness') {
        throw 'The exact Nexus Harness install metadata is missing its shortcut ownership contract.'
    }
    if ([string]$UninstallMetadata.DisplayName -cne 'Nexus Harness' -or
        [string]$UninstallMetadata.Publisher -cne 'Nexus Harness' -or
        [string]$UninstallMetadata.DisplayVersion -cne $ExpectedVersion) {
        throw 'The exact Nexus Harness uninstall metadata does not match the requested product and version.'
    }

    $installLocation = Get-NexusCanonicalPath `
        ([string]$InstallMetadata.InstallLocation) `
        'The installed Nexus Harness location'
    if (-not (Test-Path -LiteralPath $installLocation -PathType Container)) {
        throw "The exact Nexus Harness install location does not exist: $installLocation"
    }
    $application = Get-NexusCanonicalPath `
        (Join-Path $installLocation 'Nexus Harness.exe') `
        'The installed Nexus Harness application'
    $uninstaller = Get-NexusCanonicalPath `
        (Join-Path $installLocation 'Uninstall Nexus Harness.exe') `
        'The installed Nexus Harness uninstaller'
    if (-not (Test-Path -LiteralPath $application -PathType Leaf)) {
        throw "The installer returned success but did not create the application: $application"
    }
    if (-not (Test-Path -LiteralPath $uninstaller -PathType Leaf)) {
        throw "The installer returned success but did not create the uninstaller: $uninstaller"
    }

    $uninstallMatch = [Regex]::Match(
        [string]$UninstallMetadata.UninstallString,
        '\A"(?<path>.+)" /currentuser\z',
        [Text.RegularExpressions.RegexOptions]::CultureInvariant
    )
    $quietMatch = [Regex]::Match(
        [string]$UninstallMetadata.QuietUninstallString,
        '\A"(?<path>.+)" /currentuser /S\z',
        [Text.RegularExpressions.RegexOptions]::CultureInvariant
    )
    if (-not $uninstallMatch.Success -or -not $quietMatch.Success) {
        throw 'The exact Nexus Harness uninstall metadata is not bound to current-user mode.'
    }
    $registeredUninstaller = Get-NexusCanonicalPath `
        $uninstallMatch.Groups['path'].Value `
        'The registered Nexus Harness uninstaller'
    $registeredQuietUninstaller = Get-NexusCanonicalPath `
        $quietMatch.Groups['path'].Value `
        'The registered quiet Nexus Harness uninstaller'
    if (-not [StringComparer]::OrdinalIgnoreCase.Equals($registeredUninstaller, $uninstaller) -or
        -not [StringComparer]::OrdinalIgnoreCase.Equals($registeredQuietUninstaller, $uninstaller)) {
        throw 'The exact Nexus Harness uninstall metadata points outside the installed application.'
    }
    return $application
}

function Get-NexusInstalledApplication(
    [Parameter(Mandatory = $true)] [string] $ExpectedVersion
) {
    # This is electron-builder's existing UUIDv5 for appId local.ourharness.desktop.
    # It is pinned in desktop/package.json so upgrades keep the same exact keys.
    $applicationGuid = 'e52322ab-f15e-5dc0-963b-7588e3739e89'
    $installSubkey = "Software\$applicationGuid"
    $uninstallSubkey = "Software\Microsoft\Windows\CurrentVersion\Uninstall\$applicationGuid"
    $baseKey = $null
    $installKey = $null
    $uninstallKey = $null
    try {
        $baseKey = [Microsoft.Win32.RegistryKey]::OpenBaseKey(
            [Microsoft.Win32.RegistryHive]::CurrentUser,
            [Microsoft.Win32.RegistryView]::Registry64
        )
        $installKey = $baseKey.OpenSubKey($installSubkey, $false)
        $uninstallKey = $baseKey.OpenSubKey($uninstallSubkey, $false)
        if ($null -eq $installKey -or $null -eq $uninstallKey) {
            throw 'The installer returned success but did not create the exact current-user Nexus Harness registry metadata.'
        }
        $installMetadata = [pscustomobject]@{
            InstallLocation = $installKey.GetValue('InstallLocation')
            KeepShortcuts = $installKey.GetValue('KeepShortcuts')
            ShortcutName = $installKey.GetValue('ShortcutName')
        }
        $uninstallMetadata = [pscustomobject]@{
            DisplayName = $uninstallKey.GetValue('DisplayName')
            DisplayVersion = $uninstallKey.GetValue('DisplayVersion')
            Publisher = $uninstallKey.GetValue('Publisher')
            UninstallString = $uninstallKey.GetValue('UninstallString')
            QuietUninstallString = $uninstallKey.GetValue('QuietUninstallString')
        }
    } finally {
        if ($null -ne $uninstallKey) { $uninstallKey.Dispose() }
        if ($null -ne $installKey) { $installKey.Dispose() }
        if ($null -ne $baseKey) { $baseKey.Dispose() }
    }
    return Assert-NexusInstalledApplicationMetadata `
        $ExpectedVersion $installMetadata $uninstallMetadata
}

function Assert-NexusDesktopFolderListable(
    [Parameter(Mandatory = $true)] [string] $Path
) {
    $enumerator = $null
    try {
        $enumerator = [IO.Directory]::EnumerateFileSystemEntries($Path).GetEnumerator()
        [void]$enumerator.MoveNext()
    } finally {
        if ($null -ne $enumerator) { $enumerator.Dispose() }
    }
}

function Get-NexusDesktopFolders(
    [string] $DesktopFolder = '', [string] $CommonDesktopFolder = ''
) {
    if ($CommonDesktopFolder -and -not $DesktopFolder) {
        throw 'An explicit common desktop folder requires an explicit user desktop folder.'
    }
    $candidates = if ($DesktopFolder) {
        @(
            [pscustomobject]@{ Path = $DesktopFolder; Required = $true },
            [pscustomobject]@{ Path = $CommonDesktopFolder; Required = $false }
        )
    } else {
        @(
            [pscustomobject]@{
                Path = [Environment]::GetFolderPath([Environment+SpecialFolder]::DesktopDirectory)
                Required = $true
            },
            [pscustomobject]@{
                Path = [Environment]::GetFolderPath([Environment+SpecialFolder]::CommonDesktopDirectory)
                Required = $false
            }
        )
    }
    $seen = [Collections.Generic.HashSet[string]]::new(
        [StringComparer]::OrdinalIgnoreCase
    )
    $folders = @()
    foreach ($candidate in $candidates) {
        if (-not [string]$candidate.Path) {
            if ($candidate.Required) {
                throw 'Windows did not provide a user desktop folder.'
            }
            continue
        }
        $canonical = $null
        try {
            $canonical = Get-NexusCanonicalPath `
                ([string]$candidate.Path) 'A Windows desktop folder'
            if (-not (Test-Path -LiteralPath $canonical -PathType Container)) {
                throw "Windows did not provide an existing desktop folder: $canonical"
            }
            # Enumerate one entry to surface directory-list denial now. Empty is
            # fine. The user desktop is required; the machine-wide desktop is a
            # best-effort duplicate scan because standard users may be denied.
            Assert-NexusDesktopFolderListable $canonical
        } catch {
            if ($candidate.Required) { throw }
            Write-Verbose "The Common Desktop is unavailable and was skipped: $($_.Exception.Message)"
            continue
        }
        if ($seen.Add($canonical)) { $folders += $canonical }
    }
    if ($folders.Count -eq 0) {
        throw 'Windows did not provide any desktop folders.'
    }
    return $folders
}

function Get-NexusShortcutIcon([string] $IconLocation) {
    $source = ([string]$IconLocation).Trim()
    $index = 0
    if ($source -match '^(.*),\s*(-?\d+)$') {
        $source = $Matches[1].Trim()
        $index = [int]$Matches[2]
    }
    return [pscustomobject]@{
        Path = $source.Trim('"')
        Index = $index
    }
}

function Assert-NexusDesktopShortcut(
    [Parameter(Mandatory = $true)] [string] $InstalledApplication,
    [string] $DesktopFolder = '', [string] $CommonDesktopFolder = ''
) {
    $installed = Get-NexusCanonicalPath $InstalledApplication 'The installed Nexus Harness application'
    if (-not (Test-Path -LiteralPath $installed -PathType Leaf)) {
        throw "The installer returned success but did not create the application: $installed"
    }
    $desktopFolders = @(Get-NexusDesktopFolders $DesktopFolder $CommonDesktopFolder)
    $shortcutPath = Join-Path $desktopFolders[0] 'Nexus Harness.lnk'
    $seenLinks = [Collections.Generic.HashSet[string]]::new(
        [StringComparer]::OrdinalIgnoreCase
    )
    $links = @()
    for ($desktopIndex = 0; $desktopIndex -lt $desktopFolders.Count; $desktopIndex++) {
        $desktop = $desktopFolders[$desktopIndex]
        try {
            $desktopLinks = @(
                Get-ChildItem -LiteralPath $desktop -Filter 'Nexus Harness*.lnk' -File -Force
            )
        } catch {
            if ($desktopIndex -eq 0) { throw }
            Write-Verbose "The Common Desktop became unavailable and was skipped: $($_.Exception.Message)"
            continue
        }
        foreach ($link in $desktopLinks) {
            $linkPath = Get-NexusCanonicalPath `
                $link.FullName 'A Nexus Harness desktop shortcut'
            if ($seenLinks.Add($linkPath)) { $links += $linkPath }
        }
    }
    if ($links.Count -ne 1 -or
        -not [StringComparer]::OrdinalIgnoreCase.Equals($links[0], $shortcutPath)) {
        throw "The installer returned success but did not create exactly one visible desktop shortcut at ${shortcutPath}. Found: $($links -join ', ')"
    }

    $shortcutItem = Get-Item -LiteralPath $shortcutPath -Force -ErrorAction Stop
    $invisibleOrIndirectAttributes = (
        [int][IO.FileAttributes]::Hidden -bor
        [int][IO.FileAttributes]::System -bor
        [int][IO.FileAttributes]::ReparsePoint
    )
    if ($shortcutItem.PSIsContainer -or
        (([int]$shortcutItem.Attributes -band $invisibleOrIndirectAttributes) -ne 0)) {
        throw "The exact Nexus Harness desktop shortcut is not a visible ordinary file: $shortcutPath"
    }

    $shell = $null
    $shortcut = $null
    $stageError = ''
    try {
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($shortcutPath)
        $target = Get-NexusCanonicalPath ([string]$shortcut.TargetPath) 'The desktop shortcut target'
        $arguments = [string]$shortcut.Arguments
        $workingDirectory = Get-NexusCanonicalPath `
            ([string]$shortcut.WorkingDirectory) 'The desktop shortcut working directory'
        $shortcutIcon = Get-NexusShortcutIcon ([string]$shortcut.IconLocation)
        $icon = Get-NexusCanonicalPath `
            ([string]$shortcutIcon.Path) `
            'The desktop shortcut icon source'
        $iconIndex = [int]$shortcutIcon.Index
    } catch {
        throw "Windows could not read the installed desktop shortcut at ${shortcutPath}: $($_.Exception.Message)"
    } finally {
        if ($null -ne $shortcut -and [Runtime.InteropServices.Marshal]::IsComObject($shortcut)) {
            [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut)
        }
        if ($null -ne $shell -and [Runtime.InteropServices.Marshal]::IsComObject($shell)) {
            [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
        }
    }

    if (-not [StringComparer]::OrdinalIgnoreCase.Equals($target, $installed)) {
        throw "The desktop shortcut points to '$target' instead of the installed application '$installed'."
    }
    if ($arguments -cne '') {
        throw "The desktop shortcut contains unexpected launch arguments: '$arguments'."
    }
    $installedDirectory = Get-NexusCanonicalPath `
        (Split-Path -Parent $installed) 'The installed Nexus Harness directory'
    if (-not [StringComparer]::OrdinalIgnoreCase.Equals(
            $workingDirectory, $installedDirectory)) {
        throw "The desktop shortcut working directory is '$workingDirectory' instead of the installed application directory '$installedDirectory'."
    }
    if (-not (Test-Path -LiteralPath $icon -PathType Leaf)) {
        throw "The desktop shortcut icon source does not exist: $icon"
    }
    if (-not [StringComparer]::OrdinalIgnoreCase.Equals($icon, $installed) -or
        $iconIndex -ne 0) {
        throw "The desktop shortcut does not use the installed application as its exact icon source: '$icon,$iconIndex'."
    }
    return [pscustomobject]@{
        ShortcutPath = $shortcutPath
        TargetPath = $target
        Arguments = $arguments
        WorkingDirectory = $workingDirectory
        IconPath = $icon
        IconIndex = $iconIndex
    }
}

function Repair-NexusDesktopShortcut(
    [Parameter(Mandatory = $true)] [string] $InstalledApplication,
    [string] $DesktopFolder = '', [string] $CommonDesktopFolder = ''
) {
    $installed = Get-NexusCanonicalPath `
        $InstalledApplication 'The installed Nexus Harness application'
    if (-not (Test-Path -LiteralPath $installed -PathType Leaf)) {
        throw "Cannot repair the desktop shortcut because the verified installed application is missing: $installed"
    }
    $desktopFolders = @(Get-NexusDesktopFolders $DesktopFolder $CommonDesktopFolder)
    $shortcutPath = Join-Path $desktopFolders[0] 'Nexus Harness.lnk'
    $seenLinks = [Collections.Generic.HashSet[string]]::new(
        [StringComparer]::OrdinalIgnoreCase
    )
    $links = @()
    for ($desktopIndex = 0; $desktopIndex -lt $desktopFolders.Count; $desktopIndex++) {
        try {
            $desktopLinks = @(
                Get-ChildItem -LiteralPath $desktopFolders[$desktopIndex] `
                    -Filter 'Nexus Harness*.lnk' -File -Force
            )
        } catch {
            if ($desktopIndex -eq 0) { throw }
            Write-Verbose "The Common Desktop became unavailable and was skipped: $($_.Exception.Message)"
            continue
        }
        foreach ($link in $desktopLinks) {
            $linkPath = Get-NexusCanonicalPath `
                $link.FullName 'A Nexus Harness desktop shortcut'
            if ($seenLinks.Add($linkPath)) { $links += $linkPath }
        }
    }
    $conflicts = @($links | Where-Object {
        -not [StringComparer]::OrdinalIgnoreCase.Equals($_, $shortcutPath)
    })
    if ($conflicts.Count -ne 0) {
        throw "Refusing to repair the exact current-user Nexus Harness shortcut while conflicting visible shortcuts exist: $($conflicts -join ', ')"
    }
    if ((Test-Path -LiteralPath $shortcutPath) -and
        -not (Test-Path -LiteralPath $shortcutPath -PathType Leaf)) {
        throw "The exact Nexus Harness desktop shortcut path is not a file: $shortcutPath"
    }

    $hadExistingShortcut = Test-Path -LiteralPath $shortcutPath -PathType Leaf
    if ($hadExistingShortcut) {
        $existingItem = Get-Item -LiteralPath $shortcutPath -Force -ErrorAction Stop
        if ($existingItem.PSIsContainer -or
            (([int]$existingItem.Attributes -band
                [int][IO.FileAttributes]::ReparsePoint) -ne 0)) {
            throw "Refusing to replace an indirect Nexus Harness desktop shortcut path: $shortcutPath"
        }
    }

    # A fresh Shell Link prevents stale opaque link blocks, file attributes, or
    # hard-link aliases from surviving the repair. Stage it in the same private,
    # current-user-only temporary folder used for installer execution. A killed
    # process can therefore leave neither an executable orphan nor a stale backup
    # on the visible Desktop. The backup deliberately has no .lnk extension.
    $transactionDirectory = New-NexusPrivateExecutionDirectory
    $candidatePath = Join-Path $transactionDirectory 'n.lnk'
    $backupPath = Join-Path $transactionDirectory 'o.bak'

    $shell = $null
    $shortcut = $null
    try {
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($candidatePath)
        $shortcut.TargetPath = $installed
        $shortcut.Arguments = ''
        $shortcut.WorkingDirectory = Split-Path -Parent $installed
        $shortcut.IconLocation = "$installed,0"
        $shortcut.Description = 'Nexus Harness'
        $shortcut.WindowStyle = 1
        $shortcut.Save()
    } catch {
        $stageError = [string]$_.Exception.Message
    } finally {
        if ($null -ne $shortcut -and [Runtime.InteropServices.Marshal]::IsComObject($shortcut)) {
            [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut)
        }
        if ($null -ne $shell -and [Runtime.InteropServices.Marshal]::IsComObject($shell)) {
            [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
        }
    }
    if ($stageError) {
        try {
            Remove-NexusPrivateExecutionDirectory $transactionDirectory
        } catch {
            throw "Windows could not stage a fresh current-user desktop shortcut at ${candidatePath}: $stageError Cleanup also failed; the private transaction remains at ${transactionDirectory}: $($_.Exception.Message)"
        }
        throw "Windows could not stage a fresh current-user desktop shortcut at ${candidatePath}: $stageError"
    }

    try {
        $candidateItem = Get-Item -LiteralPath $candidatePath -Force -ErrorAction Stop
    } catch {
        $candidateReadError = [string]$_.Exception.Message
        try {
            Remove-NexusPrivateExecutionDirectory $transactionDirectory
        } catch {
            throw "Windows could not inspect the staged desktop shortcut at ${candidatePath}: $candidateReadError Cleanup also failed; the private transaction remains at ${transactionDirectory}: $($_.Exception.Message)"
        }
        throw "Windows could not inspect the staged desktop shortcut at ${candidatePath}: $candidateReadError"
    }
    $unsafeCandidateAttributes = (
        [int][IO.FileAttributes]::Hidden -bor
        [int][IO.FileAttributes]::System -bor
        [int][IO.FileAttributes]::ReparsePoint
    )
    if ($candidateItem.PSIsContainer -or
        (([int]$candidateItem.Attributes -band $unsafeCandidateAttributes) -ne 0)) {
        try {
            Remove-NexusPrivateExecutionDirectory $transactionDirectory
        } catch {
            throw "Windows staged an unsafe desktop shortcut repair candidate and could not remove its private transaction at ${transactionDirectory}: $($_.Exception.Message)"
        }
        throw "Windows staged an unsafe desktop shortcut repair candidate: $candidatePath"
    }

    $oldMoved = $false
    $newPlaced = $false
    try {
        if ($hadExistingShortcut) {
            Move-Item -LiteralPath $shortcutPath -Destination $backupPath -ErrorAction Stop
            $oldMoved = $true
        }
        Move-Item -LiteralPath $candidatePath -Destination $shortcutPath -ErrorAction Stop
        $newPlaced = $true
        $verified = Assert-NexusDesktopShortcut `
            $installed $DesktopFolder $CommonDesktopFolder
    } catch {
        $repairError = [string]$_.Exception.Message
        $rollbackErrors = [Collections.Generic.List[string]]::new()
        if ($newPlaced) {
            if (Test-Path -LiteralPath $shortcutPath -PathType Leaf) {
                try {
                    Move-Item -LiteralPath $shortcutPath `
                        -Destination $candidatePath -ErrorAction Stop
                    $newPlaced = $false
                } catch {
                    $rollbackErrors.Add(
                        "could not withdraw the new shortcut: $($_.Exception.Message)"
                    )
                }
            } elseif (Test-Path -LiteralPath $shortcutPath) {
                $rollbackErrors.Add(
                    "could not withdraw the new shortcut because its exact path is no longer an ordinary file: $shortcutPath"
                )
            } else {
                $newPlaced = $false
            }
        }
        if ($oldMoved) {
            if (-not (Test-Path -LiteralPath $backupPath -PathType Leaf)) {
                $rollbackErrors.Add(
                    "could not restore the previous shortcut because its backup is missing or not an ordinary file: $backupPath"
                )
            } elseif (Test-Path -LiteralPath $shortcutPath) {
                $rollbackErrors.Add(
                    "could not restore the previous shortcut because its exact path is occupied; backup retained at $backupPath"
                )
            } else {
                try {
                    Move-Item -LiteralPath $backupPath `
                        -Destination $shortcutPath -ErrorAction Stop
                    $oldMoved = $false
                } catch {
                    $rollbackErrors.Add(
                        "could not restore the previous shortcut; backup retained at ${backupPath}: $($_.Exception.Message)"
                    )
                }
            }
        }
        if ($rollbackErrors.Count -eq 0) {
            try {
                Remove-NexusPrivateExecutionDirectory $transactionDirectory
            } catch {
                $rollbackErrors.Add(
                    "could not remove the private repair transaction at ${transactionDirectory}: $($_.Exception.Message)"
                )
            }
        }
        if ($rollbackErrors.Count -ne 0) {
            throw "Desktop shortcut repair failed: $repairError Rollback or private cleanup also failed: $($rollbackErrors -join '; ') The private transaction path is $transactionDirectory"
        }
        throw "Desktop shortcut repair failed safely and every owned change was rolled back: $repairError"
    }

    # The verified exact shortcut is now the committed state. Cleanup cannot
    # invalidate it; if Windows or endpoint software retains the private backup,
    # report that residue explicitly without moving the working Desktop link.
    try {
        Remove-NexusPrivateExecutionDirectory $transactionDirectory
    } catch {
        Write-Warning "The Desktop shortcut is verified, but Windows could not remove the private repair transaction at ${transactionDirectory}: $($_.Exception.Message)"
    }
    return $verified
}

function Ensure-NexusDesktopShortcut(
    [Parameter(Mandatory = $true)] [string] $InstalledApplication,
    [string] $DesktopFolder = '', [string] $CommonDesktopFolder = ''
) {
    try {
        return Assert-NexusDesktopShortcut `
            $InstalledApplication $DesktopFolder $CommonDesktopFolder
    } catch {
        Write-Host 'The exact current-user desktop shortcut is missing, hidden, or stale; repairing it from the verified installed application.'
        return Repair-NexusDesktopShortcut `
            $InstalledApplication $DesktopFolder $CommonDesktopFolder
    }
}

function Assert-GitHubAddress([string] $Address) {
    $uri = [Uri]$Address
    if ($uri.Scheme -ne 'https' -or $allowedHosts -notcontains $uri.Host) {
        throw 'GitHub returned an unexpected download address; nothing was run.'
    }
}

function Download-ReleaseAsset(
    [string] $Address, [string] $Destination, [long] $MaximumBytes,
    [hashtable] $Headers, [string] $Accept = 'application/octet-stream'
) {
    Assert-GitHubAddress $Address
    Add-Type -AssemblyName System.Net.Http
    $handler = [System.Net.Http.HttpClientHandler]::new()
    $handler.AllowAutoRedirect = $false
    $client = [System.Net.Http.HttpClient]::new($handler)
    $current = [Uri]$Address
    try {
        for ($hop = 0; $hop -le 5; $hop++) {
            Assert-GitHubAddress ([string]$current)
            $request = [System.Net.Http.HttpRequestMessage]::new(
                [System.Net.Http.HttpMethod]::Get, $current
            )
            [void]$request.Headers.TryAddWithoutValidation('User-Agent', 'Nexus-Harness-Installer')
            [void]$request.Headers.TryAddWithoutValidation('Accept', $Accept)
            # Bearer authority belongs only to the original GitHub API request.
            # Redirects are validated before following and never inherit it,
            # even when a future PowerShell/.NET version changes defaults.
            if ($hop -eq 0 -and $Headers.Authorization) {
                [void]$request.Headers.TryAddWithoutValidation('Authorization', [string]$Headers.Authorization)
            }
            $response = $client.SendAsync(
                $request, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead
            ).GetAwaiter().GetResult()
            $status = [int]$response.StatusCode
            if ($status -ge 300 -and $status -lt 400) {
                $location = $response.Headers.Location
                $response.Dispose()
                $request.Dispose()
                if (-not $location) { throw 'GitHub returned a redirect with no destination; nothing was run.' }
                $current = if ($location.IsAbsoluteUri) { $location } else { [Uri]::new($current, $location) }
                Assert-GitHubAddress ([string]$current)
                continue
            }
            if (-not $response.IsSuccessStatusCode) {
                throw "GitHub returned HTTP $status while downloading a release asset."
            }
            $inputStream = $response.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
            $outputStream = [IO.File]::Open($Destination, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
            try {
                $buffer = New-Object byte[] (1024 * 1024)
                [long]$length = 0
                while (($read = $inputStream.Read($buffer, 0, $buffer.Length)) -gt 0) {
                    $length += $read
                    if ($length -gt $MaximumBytes) {
                        throw 'A release asset had an unsafe or unexpected size; nothing was run.'
                    }
                    $outputStream.Write($buffer, 0, $read)
                }
                if ($length -le 0) { throw 'A release asset was empty; nothing was run.' }
            } finally {
                $outputStream.Dispose()
                $inputStream.Dispose()
                $response.Dispose()
                $request.Dispose()
            }
            return
        }
        throw 'GitHub redirected the release download too many times; nothing was run.'
    } finally {
        $client.Dispose()
        $handler.Dispose()
    }
}

function Assert-NexusInstallerCandidate([Parameter(Mandatory = $true)] [object] $Candidate) {
    $installer = Get-NexusCanonicalPath ([string]$Candidate.InstallerPath) 'The Nexus Harness installer'
    $checksum = Get-NexusCanonicalPath ([string]$Candidate.ChecksumPath) 'The Nexus Harness checksum'
    if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
        throw "The exact Nexus Harness installer is missing: $installer"
    }
    if (-not (Test-Path -LiteralPath $checksum -PathType Leaf)) {
        throw "The exact Nexus Harness checksum is missing: $checksum"
    }
    if ([IO.Path]::GetFileName($installer) -cne [string]$Candidate.InstallerName -or
        [IO.Path]::GetFileName($checksum) -cne [string]$Candidate.ChecksumName) {
        throw 'The candidate paths do not own the exact declared installer and checksum names; nothing was run.'
    }
    $installerBytes = [long](Get-Item -LiteralPath $installer).Length
    $checksumBytes = [long](Get-Item -LiteralPath $checksum).Length
    if ($installerBytes -le 0 -or $installerBytes -gt $maximumInstallerBytes) {
        throw 'The Nexus Harness installer has an unsafe or unexpected size; nothing was run.'
    }
    if ($checksumBytes -le 0 -or $checksumBytes -gt 131072) {
        throw 'The Nexus Harness checksum has an unsafe or unexpected size; nothing was run.'
    }

    $expected = Get-NexusExpectedChecksum $checksum ([string]$Candidate.InstallerName)
    $actual = Get-NexusFileSha256 $installer
    if ($actual -cne $expected) {
        throw 'The installer checksum did not match; nothing was run.'
    }
    $trustedInstallerSha256 = [string]$Candidate.TrustedInstallerSha256
    if ($trustedInstallerSha256 -and
        ($trustedInstallerSha256 -cne $expected -or
         $trustedInstallerSha256 -cne $actual)) {
        throw 'The installer does not match its previously trusted product digest; nothing was run.'
    }
    Assert-NexusInstallerVersionInfo $installer ([string]$Candidate.Version)
    Assert-NexusInstallerSignature $installer ([bool]$Candidate.IsExplicitlyUnsigned)
    return [pscustomobject]@{
        InstallerPath = $installer
        ChecksumPath = $checksum
        Sha256 = $actual
        InstallerBytes = $installerBytes
    }
}

function New-NexusPrivateExecutionDirectory {
    $temporaryRoot = Get-NexusCanonicalPath `
        ([IO.Path]::GetTempPath()) 'The current-user temporary folder'
    $privateDirectory = Join-Path $temporaryRoot `
        ('nexus-harness-execute-' + [Guid]::NewGuid().ToString('N'))
    try {
        $currentUser = [Security.Principal.WindowsIdentity]::GetCurrent().User
        if ($null -eq $currentUser) {
            throw 'Windows did not provide the current user security identity.'
        }
        $system = New-Object Security.Principal.SecurityIdentifier 'S-1-5-18'
        $security = New-Object Security.AccessControl.DirectorySecurity
        $security.SetOwner($currentUser)
        $security.SetAccessRuleProtection($true, $false)
        $inheritance = (
            [Security.AccessControl.InheritanceFlags]::ContainerInherit -bor
            [Security.AccessControl.InheritanceFlags]::ObjectInherit
        )
        foreach ($identity in @($currentUser, $system)) {
            $rule = [Security.AccessControl.FileSystemAccessRule]::new(
                $identity,
                [Security.AccessControl.FileSystemRights]::FullControl,
                $inheritance,
                [Security.AccessControl.PropagationFlags]::None,
                [Security.AccessControl.AccessControlType]::Allow
            )
            [void]$security.AddAccessRule($rule)
        }
        # Apply the protected ACL as the directory is created, never in a later
        # raceable Set-Acl step. Windows PowerShell 5.1 exposes the .NET Framework
        # Directory overload; PowerShell 7 exposes the equivalent ACL extension.
        $aclExtensions = [Type]::GetType(
            'System.IO.FileSystemAclExtensions, System.IO.FileSystem.AccessControl',
            $false
        )
        if ($null -ne $aclExtensions) {
            $createMethods = @($aclExtensions.GetMethods() | Where-Object {
                $_.Name -ceq 'CreateDirectory' -and
                $_.GetParameters().Count -eq 2 -and
                $_.GetParameters()[0].ParameterType -eq
                    [Security.AccessControl.DirectorySecurity] -and
                $_.GetParameters()[1].ParameterType -eq [string]
            })
            if ($createMethods.Count -ne 1) {
                throw 'PowerShell did not expose exactly one secure directory creation method.'
            }
            $directoryInfo = $createMethods[0].Invoke(
                $null,
                [object[]]@($security.PSObject.BaseObject, [string]$privateDirectory)
            )
        } else {
            $directoryInfo = [IO.Directory]::CreateDirectory(
                $privateDirectory, $security
            )
        }
        $directoryInfo.Refresh()
        if (($directoryInfo.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw 'The private installer execution folder is unexpectedly a reparse point.'
        }
        $accessSections = (
            [Security.AccessControl.AccessControlSections]::Access -bor
            [Security.AccessControl.AccessControlSections]::Owner
        )
        if ($null -ne $aclExtensions) {
            $getAccessMethods = @($aclExtensions.GetMethods() | Where-Object {
                $_.Name -ceq 'GetAccessControl' -and
                $_.GetParameters().Count -eq 2 -and
                $_.GetParameters()[0].ParameterType -eq [IO.DirectoryInfo] -and
                $_.GetParameters()[1].ParameterType -eq
                    [Security.AccessControl.AccessControlSections]
            })
            if ($getAccessMethods.Count -ne 1) {
                throw 'PowerShell did not expose exactly one secure directory ACL reader.'
            }
            $access = $getAccessMethods[0].Invoke(
                $null,
                [object[]]@($directoryInfo.PSObject.BaseObject, $accessSections)
            )
        } else {
            $access = $directoryInfo.GetAccessControl($accessSections)
        }
        if (-not $access.AreAccessRulesProtected -or
            -not $access.GetOwner([Security.Principal.SecurityIdentifier]).Equals($currentUser)) {
            throw 'The private installer execution folder owner or ACL protection is unexpected.'
        }
        $allowedSids = @($currentUser.Value, $system.Value)
        $rules = @($access.GetAccessRules(
            $true, $false, [Security.Principal.SecurityIdentifier]
        ))
        if ($rules.Count -lt 2) {
            throw 'The private installer execution folder ACL is incomplete.'
        }
        foreach ($rule in $rules) {
            if ($rule.IsInherited -or
                $rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow -or
                $allowedSids -cnotcontains $rule.IdentityReference.Value -or
                ($rule.FileSystemRights -band [Security.AccessControl.FileSystemRights]::FullControl) -ne
                    [Security.AccessControl.FileSystemRights]::FullControl) {
                throw 'The private installer execution folder ACL grants unexpected access.'
            }
        }
        foreach ($requiredSid in $allowedSids) {
            if (@($rules | Where-Object {
                        $_.IdentityReference.Value -ceq $requiredSid
                    }).Count -eq 0) {
                throw 'The private installer execution folder ACL is missing an owned principal.'
            }
        }
        return Get-NexusCanonicalPath `
            $privateDirectory 'The private installer execution folder'
    } catch {
        if (Test-Path -LiteralPath $privateDirectory) {
            Remove-NexusPrivateExecutionDirectory $privateDirectory
        }
        throw "Windows could not create a private current-user installer execution folder; nothing was run. $($_.Exception.Message)"
    }
}

function Copy-NexusZoneIdentifierIfPresent(
    [Parameter(Mandatory = $true)] [string] $SourcePath,
    [Parameter(Mandatory = $true)] [string] $DestinationPath
) {
    # .NET Framework path normalization rejects ADS names passed directly to
    # File.Open. Use this Windows PowerShell host's own FileSystem provider,
    # pinned by absolute module path, and compare exact bytes after writing.
    $managementModule = Join-Path $PSHOME `
        'Modules\Microsoft.PowerShell.Management\Microsoft.PowerShell.Management.psd1'
    if (-not (Test-Path -LiteralPath $managementModule -PathType Leaf)) {
        throw 'Windows PowerShell did not provide its built-in file-stream module; nothing was run.'
    }
    Import-Module -Name $managementModule -Force -ErrorAction Stop
    $streams = @(
        Microsoft.PowerShell.Management\Get-Item `
            -LiteralPath $SourcePath -Stream '*' -ErrorAction Stop
    )
    $zoneStreams = @($streams | Where-Object { [string]$_.Stream -ceq 'Zone.Identifier' })
    if ($zoneStreams.Count -eq 0) { return $false }
    if ($zoneStreams.Count -ne 1) {
        throw 'The installer has an ambiguous Mark-of-the-Web stream; nothing was run.'
    }
    [byte[]]$sourceBytes = @(
        Microsoft.PowerShell.Management\Get-Content `
            -LiteralPath $SourcePath -Stream 'Zone.Identifier' `
            -Encoding Byte -ErrorAction Stop
    )
    Microsoft.PowerShell.Management\Set-Content `
        -LiteralPath $DestinationPath -Stream 'Zone.Identifier' `
        -Value $sourceBytes -Encoding Byte -NoNewline -ErrorAction Stop
    [byte[]]$destinationBytes = @(
        Microsoft.PowerShell.Management\Get-Content `
            -LiteralPath $DestinationPath -Stream 'Zone.Identifier' `
            -Encoding Byte -ErrorAction Stop
    )
    if ($destinationBytes.Length -ne $sourceBytes.Length) {
        throw 'The installer Mark-of-the-Web stream size changed while making the private copy.'
    }
    for ($index = 0; $index -lt $sourceBytes.Length; $index++) {
        if ($destinationBytes[$index] -ne $sourceBytes[$index]) {
            throw 'The installer Mark-of-the-Web stream bytes changed while making the private copy.'
        }
    }
    return $true
}

function Remove-NexusPrivateExecutionDirectory(
    [Parameter(Mandatory = $true)] [string] $PrivateDirectory
) {
    $temporaryRoot = Get-NexusCanonicalPath `
        ([IO.Path]::GetTempPath()) 'The current-user temporary folder'
    $privateRoot = Get-NexusCanonicalPath `
        $PrivateDirectory 'The private installer execution folder'
    $temporaryPrefix = $temporaryRoot.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    if (-not $privateRoot.StartsWith($temporaryPrefix, [StringComparison]::OrdinalIgnoreCase) -or
        [IO.Path]::GetFileName($privateRoot) -cnotmatch '\Anexus-harness-execute-[0-9a-f]{32}\z') {
        throw "Refusing to clean an unexpected installer execution folder: $privateRoot"
    }
    if (Test-Path -LiteralPath $privateRoot) {
        $rootItem = Get-Item -LiteralPath $privateRoot -Force
        if (($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Refusing to traverse a reparse-point installer execution folder: $privateRoot"
        }
        $children = @(Get-ChildItem -LiteralPath $privateRoot -Force)
        if (@($children | Where-Object {
                    $_.PSIsContainer -or
                    (($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)
                }).Count -ne 0) {
            throw "Refusing to recursively clean unexpected installer execution content: $privateRoot"
        }
        foreach ($child in $children) {
            Remove-Item -LiteralPath $child.FullName -Force
        }
        Remove-Item -LiteralPath $privateRoot -Force
    }
}

function Copy-NexusInstallerToPrivateExecutionCandidate(
    [Parameter(Mandatory = $true)] [object] $Candidate,
    [Parameter(Mandatory = $true)] [object] $ValidatedCandidate,
    [scriptblock] $AfterCopyHook = $null
) {
    $privateDirectory = New-NexusPrivateExecutionDirectory
    try {
        $privateInstaller = Join-Path $privateDirectory ([string]$Candidate.InstallerName)
        $privateChecksum = Join-Path $privateDirectory ([string]$Candidate.ChecksumName)
        [IO.File]::Copy([string]$ValidatedCandidate.InstallerPath, $privateInstaller, $false)
        [void](Copy-NexusZoneIdentifierIfPresent `
            ([string]$ValidatedCandidate.InstallerPath) $privateInstaller)
        [IO.File]::Copy([string]$ValidatedCandidate.ChecksumPath, $privateChecksum, $false)
        if ($null -ne $AfterCopyHook) {
            [void](& $AfterCopyHook $privateInstaller $privateChecksum)
        }
        $privateCandidate = [pscustomobject]@{
            Source = 'private current-user execution copy'
            Version = [string]$Candidate.Version
            Tag = [string]$Candidate.Tag
            InstallerName = [string]$Candidate.InstallerName
            ChecksumName = [string]$Candidate.ChecksumName
            InstallerPath = $privateInstaller
            ChecksumPath = $privateChecksum
            TrustedInstallerSha256 = [string]$ValidatedCandidate.Sha256
            IsExplicitlyUnsigned = [bool]$Candidate.IsExplicitlyUnsigned
        }
        $privateValidated = Assert-NexusInstallerCandidate $privateCandidate
        if ([long]$privateValidated.InstallerBytes -ne
            [long]$ValidatedCandidate.InstallerBytes) {
            throw 'The private installer copy size changed after validation; nothing was run.'
        }
        return [pscustomobject]@{
            Directory = $privateDirectory
            Candidate = $privateCandidate
            Validated = $privateValidated
        }
    } catch {
        Remove-NexusPrivateExecutionDirectory $privateDirectory
        throw
    }
}

function Invoke-NexusInstallerAndVerify(
    [Parameter(Mandatory = $true)] [object] $Candidate,
    [scriptblock] $InstallInvoker = $null,
    [scriptblock] $InstalledApplicationResolver = $null,
    [scriptblock] $ShortcutVerifier = $null
) {
    $validated = Assert-NexusInstallerCandidate $Candidate
    $executionCopy = $null
    try {
        $executionCopy = Copy-NexusInstallerToPrivateExecutionCandidate `
            $Candidate $validated
        $installer = [string]$executionCopy.Validated.InstallerPath

        Write-Host "Installing Nexus Harness $($Candidate.Tag) from $($Candidate.Source)..."
        if ($null -ne $InstallInvoker) {
            [void](& $InstallInvoker $installer ([string]$Candidate.Version))
        } else {
            $silentSetting = [string]$env:NEXUS_INSTALLER_SILENT
            if ($silentSetting -and $silentSetting -cne '1') {
                throw 'NEXUS_INSTALLER_SILENT accepts only the exact value 1.'
            }
            $installerArguments = if ($silentSetting -ceq '1') {
                @('/S', '/currentuser')
            } else {
                @('/currentuser')
            }
            $startParameters = @{
                FilePath = $installer
                ArgumentList = $installerArguments
                Wait = $true
                PassThru = $true
            }
            if ($silentSetting -ceq '1') {
                $startParameters.WindowStyle = 'Hidden'
            }
            $process = Start-Process @startParameters
            if ($process.ExitCode -ne 0) {
                throw "The Windows installer stopped with code $($process.ExitCode)."
            }
        }

        $installedApplication = if ($null -ne $InstalledApplicationResolver) {
            & $InstalledApplicationResolver ([string]$Candidate.Version)
        } else {
            Get-NexusInstalledApplication ([string]$Candidate.Version)
        }
        $installedApplication = Get-NexusCanonicalPath `
            ([string]$installedApplication) 'The installed Nexus Harness application'
        Assert-NexusInstallerVersionInfo `
            $installedApplication ([string]$Candidate.Version) 'installed application'
        Assert-NexusInstallerSignature `
            $installedApplication ([bool]$Candidate.IsExplicitlyUnsigned) 'installed application'
        $verifiedShortcut = if ($null -ne $ShortcutVerifier) {
            & $ShortcutVerifier $installedApplication
        } else {
            Ensure-NexusDesktopShortcut $installedApplication
        }
        Write-Host "Desktop shortcut verified: $($verifiedShortcut.ShortcutPath)"
        Write-Host "Installed executable metadata, version, and release signature mode verified."
        Write-Host "Nexus Harness $($Candidate.Tag) is installed."
        return [pscustomobject]@{
            Source = [string]$Candidate.Source
            Version = [string]$Candidate.Version
            InstalledApplication = [string]$installedApplication
            Shortcut = $verifiedShortcut
        }
    } finally {
        if ($null -ne $executionCopy) {
            Remove-NexusPrivateExecutionDirectory ([string]$executionCopy.Directory)
        }
    }
}

function Invoke-NexusHarnessInstallation(
    [string] $RequestedBundleRoot = '',
    [scriptblock] $InstallInvoker = $null,
    [scriptblock] $InstalledApplicationResolver = $null,
    [scriptblock] $ShortcutVerifier = $null,
    [switch] $OfflineOnly
) {
    $local = Get-NexusLocalBundleAssets $RequestedBundleRoot
    if ($null -ne $local) {
        Write-Host "Using the verified offline bundle beside Install Nexus Harness.cmd: $($local.InstallerName)"
        return Invoke-NexusInstallerAndVerify $local $InstallInvoker `
            $InstalledApplicationResolver $ShortcutVerifier
    }

    if ($OfflineOnly) {
        throw 'This product-built offline bootstrap requires one complete local bundle and never falls back to the network; nothing was run.'
    }

    # Explicit environment-token handling and every network operation stay
    # below the local decision. A valid offline bundle needs only built-in
    # Windows PowerShell and never probes PATH, credentials, Python, or Node.
    $githubHeaders = Get-GitHubHeaders
    $temporary = Join-Path ([IO.Path]::GetTempPath()) `
        ('nexus-harness-install-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $temporary | Out-Null
    $latestReleaseMetadataDownloaded = $false
    try {
        $releaseMetadata = Join-Path $temporary 'release-metadata.json'
        Download-ReleaseAsset "https://api.github.com/repos/$repository/releases/latest" `
            $releaseMetadata 2097152 $githubHeaders 'application/vnd.github+json'
        $latestReleaseMetadataDownloaded = $true
        $release = Get-Content -Raw -LiteralPath $releaseMetadata | ConvertFrom-Json
        $releaseAssets = Get-NexusStableReleaseAssets $release
        $installerAsset = $releaseAssets.Installer
        $checksumAsset = $releaseAssets.Checksum
        $installer = Join-Path $temporary ([string]$installerAsset.name)
        $checksum = Join-Path $temporary ([string]$checksumAsset.name)
        $installerAddress = if ($githubHeaders.Authorization) {
            $installerAsset.url
        } else {
            $installerAsset.browser_download_url
        }
        $checksumAddress = if ($githubHeaders.Authorization) {
            $checksumAsset.url
        } else {
            $checksumAsset.browser_download_url
        }
        Write-Host "Downloading Nexus Harness $($release.tag_name) from GitHub Releases..."
        Download-ReleaseAsset $installerAddress $installer $maximumInstallerBytes $githubHeaders
        Download-ReleaseAsset $checksumAddress $checksum 131072 $githubHeaders
        $remote = [pscustomobject]@{
            Source = 'GitHub Releases'
            Version = [string]$releaseAssets.Version
            Tag = [string]$release.tag_name
            InstallerName = [string]$installerAsset.name
            ChecksumName = [string]$checksumAsset.name
            InstallerPath = $installer
            ChecksumPath = $checksum
            IsExplicitlyUnsigned = [string]$installerAsset.name -match '-UNSIGNED\.exe$'
        }
        return Invoke-NexusInstallerAndVerify $remote $InstallInvoker `
            $InstalledApplicationResolver $ShortcutVerifier
    } catch {
        $message = Get-InstallationFailureMessage ([string]$_.Exception.Message) `
            $githubHeaders $latestReleaseMetadataDownloaded
        throw $message
    } finally {
        if (Test-Path -LiteralPath $temporary) {
            Remove-Item -LiteralPath $temporary -Recurse -Force
        }
    }
}

if ($LoadFunctionsOnly) { return }

if ($ValidateOfflineBundleOnly) {
    try {
        $candidate = Get-NexusLocalBundleAssets $BundleRoot
        if ($null -eq $candidate) {
            throw 'No offline Nexus Harness bundle is present in the requested folder.'
        }
        $validated = Assert-NexusInstallerCandidate $candidate
        Write-Host "Offline bundle verified without execution: $($candidate.InstallerName) ($($validated.Sha256))"
        return
    } catch {
        Write-Host "Offline bundle validation stopped safely: $($_.Exception.Message)" -ForegroundColor Red
        exit 1
    }
}

try {
    [void](Invoke-NexusHarnessInstallation -RequestedBundleRoot $BundleRoot -OfflineOnly:$OfflineOnly)
} catch {
    Write-Host "Installation stopped safely: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host 'Release page: https://github.com/KZTP47/nexus-harness/releases'
    exit 1
}
