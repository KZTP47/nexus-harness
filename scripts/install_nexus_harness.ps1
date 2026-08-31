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

function Invoke-OptionalNativeCommand {
    param(
        [Parameter(Mandatory = $true)] [string] $FilePath,
        [Parameter(Mandatory = $true)] [string] $Arguments,
        [AllowNull()] [AllowEmptyString()] [string] $StandardInput = $null,
        [hashtable] $EnvironmentOverrides = @{},
        [ValidateRange(1000, 60000)] [int] $TimeoutMilliseconds = 15000
    )

    # PowerShell 5.1 can promote native stderr to a terminating error when the
    # caller uses ErrorActionPreference=Stop. These probes are optional, so run
    # them outside PowerShell's native-command pipeline and treat every failure
    # (including start failure and timeout) as an ordinary nonzero result.
    $process = $null
    $timer = [System.Diagnostics.Stopwatch]::StartNew()
    try {
        $startInfo = New-Object System.Diagnostics.ProcessStartInfo
        $startInfo.FileName = $FilePath
        # Arguments are installer-owned fixed literals, never downloaded data.
        $startInfo.Arguments = $Arguments
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true
        $startInfo.RedirectStandardInput = $null -ne $StandardInput
        foreach ($name in $EnvironmentOverrides.Keys) {
            $startInfo.EnvironmentVariables[[string]$name] = [string]$EnvironmentOverrides[$name]
        }

        $process = New-Object System.Diagnostics.Process
        $process.StartInfo = $startInfo
        if (-not $process.Start()) {
            return [pscustomobject]@{ ExitCode = -1; StdOut = '' }
        }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if ($startInfo.RedirectStandardInput) {
            $process.StandardInput.Write($StandardInput)
            $process.StandardInput.Close()
        }
        $remaining = $TimeoutMilliseconds - [int]$timer.ElapsedMilliseconds
        if ($remaining -le 0 -or -not $process.WaitForExit($remaining)) {
            try { $process.Kill($true) } catch { try { $process.Kill() } catch {} }
            try { $process.StandardOutput.Close() } catch {}
            try { $process.StandardError.Close() } catch {}
            return [pscustomobject]@{ ExitCode = -2; StdOut = '' }
        }
        # A child spawned by gh/git can inherit redirected handles after the
        # direct process exits. Bound that drain by the same end-to-end deadline
        # instead of blocking forever on Task.GetResult().
        while ((-not $stdoutTask.IsCompleted -or -not $stderrTask.IsCompleted) -and
               $timer.ElapsedMilliseconds -lt $TimeoutMilliseconds) {
            [System.Threading.Thread]::Sleep(10)
        }
        if (-not $stdoutTask.IsCompleted -or -not $stderrTask.IsCompleted) {
            try { $process.StandardOutput.Close() } catch {}
            try { $process.StandardError.Close() } catch {}
            return [pscustomobject]@{ ExitCode = -2; StdOut = '' }
        }
        $stdout = [string]$stdoutTask.GetAwaiter().GetResult()
        # Drain stderr concurrently to prevent pipe deadlocks, but never surface
        # optional credential diagnostics (which can contain sensitive details).
        [void]$stderrTask.GetAwaiter().GetResult()
        return [pscustomobject]@{ ExitCode = [int]$process.ExitCode; StdOut = $stdout }
    } catch {
        if ($null -ne $process) {
            try {
                if (-not $process.HasExited) {
                    try { $process.Kill($true) } catch { $process.Kill() }
                }
            } catch {}
        }
        return [pscustomobject]@{ ExitCode = -1; StdOut = '' }
    } finally {
        if ($null -ne $process) {
            try { $process.Dispose() } catch {}
        }
    }
}

function Get-GitHubHeaders {
    $headers = @{ Accept = 'application/vnd.github+json'; 'User-Agent' = 'Nexus-Harness-Installer' }
    $token = [string]$env:GH_TOKEN
    if (-not $token) { $token = [string]$env:GITHUB_TOKEN }
    if (-not $token) {
        $gh = Get-Command gh.exe -ErrorAction SilentlyContinue
        if (-not $gh) { $gh = Get-Command gh -ErrorAction SilentlyContinue }
        if ($gh -and $gh.CommandType -eq 'Application' -and $gh.Source) {
            $ghResult = Invoke-OptionalNativeCommand -FilePath $gh.Source -Arguments 'auth token'
            if ($ghResult.ExitCode -eq 0) {
                $token = ([string]$ghResult.StdOut).Trim()
            }
        }
    }
    if (-not $token) {
        # Someone who cloned a private repository commonly already has a Git
        # Credential Manager login. Ask non-interactively; never print or save it.
        $git = Get-Command git.exe -ErrorAction SilentlyContinue
        if (-not $git) { $git = Get-Command git -ErrorAction SilentlyContinue }
        if ($git -and $git.CommandType -eq 'Application' -and $git.Source) {
            $gitResult = Invoke-OptionalNativeCommand -FilePath $git.Source -Arguments 'credential fill' `
                -StandardInput "protocol=https`nhost=github.com`n`n" `
                -EnvironmentOverrides @{ GCM_INTERACTIVE = 'Never' }
            if ($gitResult.ExitCode -eq 0) {
                $lines = @(([string]$gitResult.StdOut) -split '\r?\n')
                $password = @($lines | Where-Object { $_ -like 'password=*' } | Select-Object -First 1)
                if ($password.Count -eq 1) { $token = $password[0].Substring(9) }
            }
        }
    }
    if ($token) { $headers.Authorization = "Bearer $token" }
    return $headers
}

function Get-InstallationFailureMessage(
    [string] $Message, [hashtable] $Headers, [bool] $LatestReleaseMetadataDownloaded
) {
    if (-not $LatestReleaseMetadataDownloaded -and -not $Headers.Authorization -and
        $Message -ceq 'GitHub returned HTTP 404 while downloading a release asset.') {
        return 'No installable Nexus Harness release is visible to this installer. Either no stable release has been published yet, or the repository is private and this shell has no usable GitHub login. Check the Releases page in a signed-in browser. If no stable release is listed, publish one before retrying. If the repository is private, sign in with GitHub CLI or Git Credential Manager, or set GH_TOKEN for this process, then retry.'
    }
    return $Message
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
$latestReleaseMetadataDownloaded = $false
try {
    $releaseMetadata = Join-Path $temporary 'release-metadata.json'
    Download-ReleaseAsset "https://api.github.com/repos/$repository/releases/latest" `
        $releaseMetadata 2097152 $githubHeaders 'application/vnd.github+json'
    $latestReleaseMetadataDownloaded = $true
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
    $message = Get-InstallationFailureMessage ([string]$_.Exception.Message) `
        $githubHeaders $latestReleaseMetadataDownloaded
    Write-Host "Installation stopped safely: $message" -ForegroundColor Red
    Write-Host 'Release page: https://github.com/KZTP47/nexus-harness/releases'
    exit 1
} finally {
    if (Test-Path -LiteralPath $temporary) {
        Remove-Item -LiteralPath $temporary -Recurse -Force
    }
}
