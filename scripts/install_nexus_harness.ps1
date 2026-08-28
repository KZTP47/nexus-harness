$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$repository = 'KZTP47/nexus-harness'
$allowedHosts = @('github.com', 'objects.githubusercontent.com', 'release-assets.githubusercontent.com')
$maximumInstallerBytes = 367001600
$publisherFile = Join-Path $PSScriptRoot '..\release\windows-authenticode-publisher.txt'
if (-not (Test-Path -LiteralPath $publisherFile -PathType Leaf)) {
    throw 'The pinned Windows publisher identity is missing; nothing was downloaded.'
}
$expectedPublisher = (Get-Content -Raw -LiteralPath $publisherFile).Trim()
if (-not $expectedPublisher -or $expectedPublisher.StartsWith('UNCONFIGURED')) {
    throw 'This checkout has no pinned Windows publisher yet, so it cannot safely install a public release.'
}

function Assert-GitHubAddress([string] $Address) {
    $uri = [Uri]$Address
    if ($uri.Scheme -ne 'https' -or $allowedHosts -notcontains $uri.Host) {
        throw 'GitHub returned an unexpected download address; nothing was run.'
    }
}

function Download-ReleaseAsset([string] $Address, [string] $Destination, [long] $MaximumBytes) {
    Assert-GitHubAddress $Address
    $response = Invoke-WebRequest -Uri $Address -Headers @{
        Accept = 'application/octet-stream'; 'User-Agent' = 'Nexus-Harness-Installer'
    } -MaximumRedirection 5 -OutFile $Destination -PassThru
    $final = $response.BaseResponse.ResponseUri
    if (-not $final -and $response.BaseResponse.RequestMessage) {
        $final = $response.BaseResponse.RequestMessage.RequestUri
    }
    Assert-GitHubAddress ([string]$final)
    $length = (Get-Item -LiteralPath $Destination).Length
    if ($length -le 0 -or $length -gt $MaximumBytes) {
        throw 'A release asset had an unsafe or unexpected size; nothing was run.'
    }
}

$temporary = Join-Path ([IO.Path]::GetTempPath()) ('nexus-harness-install-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temporary | Out-Null
try {
    $release = Invoke-RestMethod -Uri "https://api.github.com/repos/$repository/releases/latest" -Headers @{
        Accept = 'application/vnd.github+json'; 'User-Agent' = 'Nexus-Harness-Installer'
    }
    if ($release.draft -or $release.prerelease -or -not $release.tag_name) {
        throw 'There is no stable Nexus Harness release to install yet.'
    }
    $installers = @($release.assets | Where-Object { $_.name -match '^Nexus-Harness-Setup-[0-9][0-9.]*\.exe$' })
    $checksums = @($release.assets | Where-Object { $_.name -match '^Nexus-Harness-Setup-[0-9][0-9.]*\.exe\.sha256$' })
    if ($installers.Count -ne 1 -or $checksums.Count -ne 1) {
        throw 'The stable release does not contain exactly one versioned installer and checksum; nothing was run.'
    }

    $installer = Join-Path $temporary $installers[0].name
    $checksum = Join-Path $temporary $checksums[0].name
    Write-Host "Downloading Nexus Harness $($release.tag_name) from GitHub Releases..."
    Download-ReleaseAsset $installers[0].browser_download_url $installer $maximumInstallerBytes
    Download-ReleaseAsset $checksums[0].browser_download_url $checksum 131072

    $escapedName = [Regex]::Escape($installers[0].name)
    $line = Get-Content -LiteralPath $checksum | Where-Object { $_ -match "^[0-9a-fA-F]{64}\s+\*?$escapedName$" } | Select-Object -First 1
    if (-not $line) { throw 'The checksum file does not name the exact installer; nothing was run.' }
    $expected = ($line -split '\s+')[0].ToLowerInvariant()
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $installer).Hash.ToLowerInvariant()
    if ($actual -ne $expected) { throw 'The installer checksum did not match; nothing was run.' }

    $signature = Get-AuthenticodeSignature -LiteralPath $installer
    if ($signature.Status -ne 'Valid' -or -not $signature.SignerCertificate.Subject) {
        throw "The installer does not have a valid Windows signature ($($signature.Status)); nothing was run."
    }
    if ($signature.SignerCertificate.Subject -cne $expectedPublisher) {
        throw "The installer is signed by an unexpected publisher ($($signature.SignerCertificate.Subject)); expected $expectedPublisher. Nothing was run."
    }
    Write-Host "Checksum and Windows signature verified: $($signature.SignerCertificate.Subject)"
    $process = Start-Process -FilePath $installer -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw "The Windows installer stopped with code $($process.ExitCode)." }
    Write-Host "Nexus Harness $($release.tag_name) is installed."
} catch {
    Write-Error $_.Exception.Message
    Write-Host 'Release page: https://github.com/KZTP47/nexus-harness/releases'
    exit 1
} finally {
    if (Test-Path -LiteralPath $temporary) {
        Remove-Item -LiteralPath $temporary -Recurse -Force
    }
}
