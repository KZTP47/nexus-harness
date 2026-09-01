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
    if ($installers.Count -ne 1 -or $checksums.Count -ne 1) {
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

function Get-NexusDesktopFolders(
    [string] $DesktopFolder = '', [string] $CommonDesktopFolder = ''
) {
    if ([bool]$DesktopFolder -xor [bool]$CommonDesktopFolder) {
        throw 'Explicit user and common desktop folders must be supplied together.'
    }
    $candidates = if ($DesktopFolder) {
        @($DesktopFolder, $CommonDesktopFolder)
    } else {
        @(
            [Environment]::GetFolderPath([Environment+SpecialFolder]::DesktopDirectory),
            [Environment]::GetFolderPath([Environment+SpecialFolder]::CommonDesktopDirectory)
        )
    }
    $seen = [Collections.Generic.HashSet[string]]::new(
        [StringComparer]::OrdinalIgnoreCase
    )
    $folders = @()
    foreach ($candidate in $candidates) {
        $canonical = Get-NexusCanonicalPath $candidate 'A Windows desktop folder'
        if (-not (Test-Path -LiteralPath $canonical -PathType Container)) {
            throw "Windows did not provide an existing desktop folder: $canonical"
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
    foreach ($desktop in $desktopFolders) {
        foreach ($link in @(Get-ChildItem -LiteralPath $desktop -Filter 'Nexus Harness*.lnk' -File -Force)) {
            $linkPath = Get-NexusCanonicalPath $link.FullName 'A Nexus Harness desktop shortcut'
            if ($seenLinks.Add($linkPath)) { $links += $linkPath }
        }
    }
    if ($links.Count -ne 1 -or
        -not [StringComparer]::OrdinalIgnoreCase.Equals($links[0], $shortcutPath)) {
        throw "The installer returned success but did not create exactly one visible desktop shortcut at ${shortcutPath}. Found: $($links -join ', ')"
    }

    $shell = $null
    $shortcut = $null
    try {
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($shortcutPath)
        $target = Get-NexusCanonicalPath ([string]$shortcut.TargetPath) 'The desktop shortcut target'
        $arguments = [string]$shortcut.Arguments
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
        IconPath = $icon
        IconIndex = $iconIndex
    }
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
    $releaseAssets = Get-NexusStableReleaseAssets $release
    $installers = @($releaseAssets.Installer)
    $checksums = @($releaseAssets.Checksum)
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
    $process = Start-Process -FilePath $installer -ArgumentList '/currentuser' -Wait -PassThru
    if ($process.ExitCode -ne 0) { throw "The Windows installer stopped with code $($process.ExitCode)." }
    $installedApplication = Get-NexusInstalledApplication $releaseAssets.Version
    $verifiedShortcut = Assert-NexusDesktopShortcut $installedApplication
    Write-Host "Desktop shortcut verified: $($verifiedShortcut.ShortcutPath)"
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
