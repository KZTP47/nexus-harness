$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$repository = 'KZTP47/nexus-harness'
$allowedHosts = @('api.github.com', 'github.com', 'objects.githubusercontent.com', 'release-assets.githubusercontent.com')
$maximumInstallerBytes = 367001600
$publisherFile = Join-Path $PSScriptRoot '..\release\windows-authenticode-publisher.txt'
if (-not (Test-Path -LiteralPath $publisherFile -PathType Leaf)) {
    throw 'The pinned Windows publisher identity is missing; nothing was downloaded.'
}
$expectedPublisher = (Get-Content -Raw -LiteralPath $publisherFile).Trim()
$publisherConfigured = [bool]$expectedPublisher -and -not $expectedPublisher.StartsWith('UNCONFIGURED')

function Get-GitHubHeaders {
    $headers = @{ Accept = 'application/vnd.github+json'; 'User-Agent' = 'Nexus-Harness-Installer' }
    $token = [string]$env:GH_TOKEN
    if (-not $token) { $token = [string]$env:GITHUB_TOKEN }
    if (-not $token) {
        $gh = Get-Command gh.exe -ErrorAction SilentlyContinue
        if (-not $gh) { $gh = Get-Command gh -ErrorAction SilentlyContinue }
        if ($gh) {
            $token = [string](& $gh.Source auth token 2>$null)
            if ($LASTEXITCODE -ne 0) { $token = '' }
        }
    }
    if (-not $token) {
        # Someone who cloned a private repository commonly already has a Git
        # Credential Manager login. Ask non-interactively; never print or save it.
        $git = Get-Command git.exe -ErrorAction SilentlyContinue
        if (-not $git) { $git = Get-Command git -ErrorAction SilentlyContinue }
        if ($git) {
            $oldInteractive = $env:GCM_INTERACTIVE
            try {
                $env:GCM_INTERACTIVE = 'Never'
                $lines = "protocol=https`nhost=github.com`n`n" | & $git.Source credential fill 2>$null
                if ($LASTEXITCODE -eq 0) {
                    $password = @($lines | Where-Object { $_ -like 'password=*' } | Select-Object -First 1)
                    if ($password.Count -eq 1) { $token = $password[0].Substring(9) }
                }
            } finally {
                $env:GCM_INTERACTIVE = $oldInteractive
            }
        }
    }
    if ($token) { $headers.Authorization = "Bearer $token" }
    return $headers
}

$githubHeaders = Get-GitHubHeaders

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

$temporary = Join-Path ([IO.Path]::GetTempPath()) ('nexus-harness-install-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temporary | Out-Null
try {
    $releaseMetadata = Join-Path $temporary 'release-metadata.json'
    Download-ReleaseAsset "https://api.github.com/repos/$repository/releases/latest" `
        $releaseMetadata 2097152 $githubHeaders 'application/vnd.github+json'
    $release = Get-Content -Raw -LiteralPath $releaseMetadata | ConvertFrom-Json
    if ($release.draft -or $release.prerelease -or -not $release.tag_name) {
        throw 'There is no stable Nexus Harness release to install yet.'
    }
    $installers = @($release.assets | Where-Object { $_.name -match '^Nexus-Harness-Setup-[0-9][0-9.]*(?:-UNSIGNED)?\.exe$' })
    $checksums = @($release.assets | Where-Object { $_.name -match '^Nexus-Harness-Setup-[0-9][0-9.]*(?:-UNSIGNED)?\.exe\.sha256$' })
    if ($installers.Count -ne 1 -or $checksums.Count -ne 1) {
        throw 'The stable release does not contain exactly one versioned installer and checksum; nothing was run.'
    }
    $isExplicitlyUnsigned = $installers[0].name -match '-UNSIGNED\.exe$'
    if (-not $publisherConfigured -and -not $isExplicitlyUnsigned) {
        throw 'No Windows publisher is pinned, but the release is not explicitly named UNSIGNED; nothing was run.'
    }
    if ($publisherConfigured -and $isExplicitlyUnsigned) {
        throw 'A Windows publisher is pinned, but the release is marked UNSIGNED; nothing was run.'
    }

    $installer = Join-Path $temporary $installers[0].name
    $checksum = Join-Path $temporary $checksums[0].name
    $installerAddress = if ($githubHeaders.Authorization) { $installers[0].url } else { $installers[0].browser_download_url }
    $checksumAddress = if ($githubHeaders.Authorization) { $checksums[0].url } else { $checksums[0].browser_download_url }
    Write-Host "Downloading Nexus Harness $($release.tag_name) from GitHub Releases..."
    Download-ReleaseAsset $installerAddress $installer $maximumInstallerBytes $githubHeaders
    Download-ReleaseAsset $checksumAddress $checksum 131072 $githubHeaders

    $escapedName = [Regex]::Escape($installers[0].name)
    $line = Get-Content -LiteralPath $checksum | Where-Object { $_ -match "^[0-9a-fA-F]{64}\s+\*?$escapedName$" } | Select-Object -First 1
    if (-not $line) { throw 'The checksum file does not name the exact installer; nothing was run.' }
    $expected = ($line -split '\s+')[0].ToLowerInvariant()
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $installer).Hash.ToLowerInvariant()
    if ($actual -ne $expected) { throw 'The installer checksum did not match; nothing was run.' }

    $signature = Get-AuthenticodeSignature -LiteralPath $installer
    if ($publisherConfigured) {
        if ($signature.Status -ne 'Valid' -or -not $signature.SignerCertificate.Subject) {
            throw "The installer does not have a valid Windows signature ($($signature.Status)); nothing was run."
        }
        if ($signature.SignerCertificate.Subject -cne $expectedPublisher) {
            throw "The installer is signed by an unexpected publisher ($($signature.SignerCertificate.Subject)); expected $expectedPublisher. Nothing was run."
        }
        Write-Host "Checksum and Windows signature verified: $($signature.SignerCertificate.Subject)"
    } else {
        if ($signature.Status -ne 'NotSigned' -or $signature.SignerCertificate.Subject) {
            throw "The checksum-only release has an unexpected Windows signature state ($($signature.Status)); nothing was run."
        }
        Write-Warning 'This installer is not Authenticode-signed. Its exact bytes match the SHA-256 published with the immutable GitHub release.'
        Write-Host 'SHA-256 verified for the explicitly named UNSIGNED installer.'
    }
    $process = Start-Process -FilePath $installer -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw "The Windows installer stopped with code $($process.ExitCode)." }
    Write-Host "Nexus Harness $($release.tag_name) is installed."
} catch {
    $message = $_.Exception.Message
    if (-not $githubHeaders.Authorization -and $message -match '404|Not Found') {
        $message = 'This repository is private and no existing GitHub login was available. Sign in with GitHub CLI or Git Credential Manager, set GH_TOKEN for this process, or download the installer and checksum from the Releases page in your signed-in browser.'
    }
    Write-Host "Installation stopped safely: $message" -ForegroundColor Red
    Write-Host 'Release page: https://github.com/KZTP47/nexus-harness/releases'
    exit 1
} finally {
    if (Test-Path -LiteralPath $temporary) {
        Remove-Item -LiteralPath $temporary -Recurse -Force
    }
}
