# Run only on a clean GitHub Windows runner; each matrix job owns its installation.
[CmdletBinding()]
param(
  [Parameter(Mandatory=$true)]
  [ValidateSet('app', 'long-horizon', 'team-chat', 'shortcuts')]
  [string] $Mode,
  [Parameter(Mandatory=$true)]
  [string] $ArtifactDirectory
)
$ErrorActionPreference = 'Stop'
if ($env:GITHUB_ACTIONS -cne 'true') { throw 'Installed release acceptance requires a clean GitHub runner.' }

$installers = @(Get-ChildItem -LiteralPath $ArtifactDirectory -Filter 'Nexus-Harness-Setup-*.exe')
if ($installers.Count -ne 1) { throw "Expected one installer, found $($installers.Count)." }
$installer = $installers[0]
$checksumPath = $installer.FullName + '.sha256'
$checksum = (Get-Content -Raw -LiteralPath $checksumPath).Trim()
$pattern = '\A(?<hash>[0-9a-fA-F]{64})\s+\*?' + [Regex]::Escape($installer.Name) + '\z'
if ($checksum -cnotmatch $pattern) { throw 'The installer checksum does not name the exact artifact.' }
$expectedHash = $Matches['hash']
if ((Get-FileHash -Algorithm SHA256 -LiteralPath $installer.FullName).Hash -ine $expectedHash) {
  throw 'The downloaded installer bytes do not match the verified build artifact.'
}
$expectedVersion = (Get-Content -Raw -LiteralPath desktop/package.json | ConvertFrom-Json).version
$applicationGuid = 'e52322ab-f15e-5dc0-963b-7588e3739e89'
$installed = $null

function Get-CanonicalPath([string] $Path, [string] $What) {
  if (-not $Path -or -not [IO.Path]::IsPathRooted($Path)) { throw "$What is not an absolute path: $Path" }
  $full = [IO.Path]::GetFullPath($Path)
  $root = [IO.Path]::GetPathRoot($full)
  if ([StringComparer]::OrdinalIgnoreCase.Equals($full, $root)) { return $root }
  return $full.TrimEnd(
    [IO.Path]::DirectorySeparatorChar, [IO.Path]::AltDirectorySeparatorChar
  )
}

function Get-InstalledApplication {
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
      throw 'The exact current-user Nexus Harness registry metadata is missing.'
    }
    $locationValue = [string]$installKey.GetValue('InstallLocation')
    $keepShortcuts = [string]$installKey.GetValue('KeepShortcuts')
    $shortcutName = [string]$installKey.GetValue('ShortcutName')
    $displayName = [string]$uninstallKey.GetValue('DisplayName')
    $displayVersion = [string]$uninstallKey.GetValue('DisplayVersion')
    $publisher = [string]$uninstallKey.GetValue('Publisher')
    $uninstallString = [string]$uninstallKey.GetValue('UninstallString')
    $quietUninstallString = [string]$uninstallKey.GetValue('QuietUninstallString')
  } finally {
    if ($null -ne $uninstallKey) { $uninstallKey.Dispose() }
    if ($null -ne $installKey) { $installKey.Dispose() }
    if ($null -ne $baseKey) { $baseKey.Dispose() }
  }
  if ($keepShortcuts -cne 'true' -or $shortcutName -cne 'Nexus Harness' -or
      $displayName -cne 'Nexus Harness' -or $publisher -cne 'Nexus Harness' -or
      $displayVersion -cne $expectedVersion) {
    throw 'The exact current-user Nexus Harness product/version metadata is invalid.'
  }
  $location = Get-CanonicalPath $locationValue 'The installed application location'
  if (-not (Test-Path -LiteralPath $location -PathType Container)) {
    throw "The installed application location does not exist: $location"
  }
  $application = Get-CanonicalPath (Join-Path $location 'Nexus Harness.exe') 'The installed application'
  $uninstaller = Get-CanonicalPath (Join-Path $location 'Uninstall Nexus Harness.exe') 'The installed uninstaller'
  if (-not (Test-Path -LiteralPath $application -PathType Leaf) -or
      -not (Test-Path -LiteralPath $uninstaller -PathType Leaf)) {
    throw "The exact installed application or uninstaller is missing below $location."
  }
  $uninstallMatch = [Regex]::Match($uninstallString, '\A"(?<path>.+)" /currentuser\z')
  $quietMatch = [Regex]::Match($quietUninstallString, '\A"(?<path>.+)" /currentuser /S\z')
  if (-not $uninstallMatch.Success -or -not $quietMatch.Success) {
    throw 'The installed package metadata is not bound to current-user mode.'
  }
  $registeredUninstaller = Get-CanonicalPath $uninstallMatch.Groups['path'].Value 'The registered uninstaller'
  $registeredQuietUninstaller = Get-CanonicalPath $quietMatch.Groups['path'].Value 'The registered quiet uninstaller'
  if (-not [StringComparer]::OrdinalIgnoreCase.Equals($registeredUninstaller, $uninstaller) -or
      -not [StringComparer]::OrdinalIgnoreCase.Equals($registeredQuietUninstaller, $uninstaller)) {
    throw 'The installed package metadata points outside its exact install location.'
  }
  return $application
}

function Get-DesktopFolders {
  $candidates = @(
    [Environment]::GetFolderPath([Environment+SpecialFolder]::DesktopDirectory),
    [Environment]::GetFolderPath([Environment+SpecialFolder]::CommonDesktopDirectory)
  )
  $seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
  $folders = @()
  foreach ($candidate in $candidates) {
    $folder = Get-CanonicalPath $candidate 'A Windows desktop known folder'
    if (-not (Test-Path -LiteralPath $folder -PathType Container)) {
      throw "Windows did not provide an existing desktop known folder: $folder"
    }
    if ($seen.Add($folder)) { $folders += $folder }
  }
  if ($folders.Count -eq 0) { throw 'Windows did not provide any desktop known folders.' }
  return $folders
}

$desktopFolders = @(Get-DesktopFolders)
$desktop = $desktopFolders[0]
$shortcut = Join-Path $desktop 'Nexus Harness.lnk'

function Assert-InstalledShortcut {
  $installedPath = Get-CanonicalPath $installed 'The installed application'
  $seenLinks = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
  $links = @()
  foreach ($folder in $desktopFolders) {
    foreach ($candidate in @(Get-ChildItem -LiteralPath $folder -Filter 'Nexus Harness*.lnk' -File -Force)) {
      $candidatePath = Get-CanonicalPath $candidate.FullName 'A Nexus Harness desktop shortcut'
      if ($seenLinks.Add($candidatePath)) { $links += $candidatePath }
    }
  }
  if ($links.Count -ne 1 -or
      -not [StringComparer]::OrdinalIgnoreCase.Equals($links[0], $shortcut)) {
    throw "Expected exactly $shortcut and no duplicate visible Nexus Harness links; found $($links -join ', ')."
  }
  $shortcutItem = Get-Item -LiteralPath $shortcut -Force
  $unsafeAttributes = (
    [int][IO.FileAttributes]::Hidden -bor
    [int][IO.FileAttributes]::System -bor
    [int][IO.FileAttributes]::ReparsePoint
  )
  if ($shortcutItem.PSIsContainer -or
      (([int]$shortcutItem.Attributes -band $unsafeAttributes) -ne 0)) {
    throw "The installed Desktop shortcut is not a visible ordinary file: $shortcut"
  }
  $shell = New-Object -ComObject WScript.Shell
  $link = $null
  try {
    $link = $shell.CreateShortcut($shortcut)
    $target = Get-CanonicalPath ([string]$link.TargetPath) 'The shortcut target'
    $arguments = [string]$link.Arguments
    $workingDirectory = Get-CanonicalPath ([string]$link.WorkingDirectory) 'The shortcut working directory'
    $iconLocation = ([string]$link.IconLocation).Trim()
    $iconIndex = 0
    if ($iconLocation -match '^(.*),\s*(-?\d+)$') {
      $iconLocation = $Matches[1].Trim()
      $iconIndex = [int]$Matches[2]
    }
    $icon = Get-CanonicalPath $iconLocation.Trim('"') 'The shortcut icon source'
  } finally {
    if ($null -ne $link -and [Runtime.InteropServices.Marshal]::IsComObject($link)) {
      [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($link)
    }
    if ([Runtime.InteropServices.Marshal]::IsComObject($shell)) {
      [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
    }
  }
  if (-not [StringComparer]::OrdinalIgnoreCase.Equals($target, $installedPath)) {
    throw "Shortcut target '$target' does not match '$installedPath'."
  }
  if ($arguments -cne '') {
    throw "Shortcut contains unexpected launch arguments: '$arguments'."
  }
  $installedDirectory = Get-CanonicalPath (Split-Path -Parent $installedPath) 'The installed application directory'
  if (-not [StringComparer]::OrdinalIgnoreCase.Equals($workingDirectory, $installedDirectory)) {
    throw "Shortcut working directory '$workingDirectory' does not match '$installedDirectory'."
  }
  if (-not (Test-Path -LiteralPath $icon -PathType Leaf)) {
    throw "Shortcut icon source does not exist: $icon"
  }
  if (-not [StringComparer]::OrdinalIgnoreCase.Equals($icon, $installedPath) -or
      $iconIndex -ne 0) {
    throw "Shortcut icon '$icon,$iconIndex' does not use the installed application exactly."
  }
  return [pscustomobject]@{
    ShortcutPath = $shortcut; TargetPath = $target; Arguments = $arguments
    WorkingDirectory = $workingDirectory; IconPath = $icon; IconIndex = $iconIndex
  }
}

function Get-InstalledProcesses {
  return @(Get-CimInstance Win32_Process | Where-Object {
    $_.ExecutablePath -and [StringComparer]::OrdinalIgnoreCase.Equals(
      [IO.Path]::GetFullPath($_.ExecutablePath), [IO.Path]::GetFullPath($installed)
    )
  })
}

function Stop-InstalledProcessTrees {
  $deadline = [DateTime]::UtcNow.AddSeconds(20)
  do {
    $remaining = @(Get-InstalledProcesses)
    if ($remaining.Count -eq 0) { return }
    foreach ($process in $remaining) {
      try {
        [void](Start-Process -FilePath "$env:SystemRoot\System32\taskkill.exe" `
          -ArgumentList @('/PID', [string]$process.ProcessId, '/T', '/F') `
          -Wait -PassThru -WindowStyle Hidden)
      } catch {
        # The bounded exact-path readback below remains authoritative.
      }
    }
    if ([DateTime]::UtcNow -lt $deadline) { Start-Sleep -Milliseconds 250 }
  } while ([DateTime]::UtcNow -lt $deadline)
  $remaining = @(Get-InstalledProcesses)
  if ($remaining.Count -ne 0) {
    throw "The shortcut-launched installed process tree did not stop: $($remaining.ProcessId -join ', ')."
  }
}

$process = Start-Process -FilePath $installers[0].FullName -ArgumentList @('/S', '/currentuser') -Wait -PassThru -WindowStyle Hidden
if ($process.ExitCode -ne 0) { throw "Installer stopped with code $($process.ExitCode)." }
$installed = Get-InstalledApplication
[void](Assert-InstalledShortcut)
Push-Location desktop
try {
  if ($Mode -eq 'app') {
    npm run smoke:built -- "$installed"
    if ($LASTEXITCODE -ne 0) { throw 'Installed app acceptance failed.' }
    npm run e2e:multi-vendor -- "$installed"
    if ($LASTEXITCODE -ne 0) { throw 'Installed multi-vendor acceptance failed.' }
  } elseif ($Mode -eq 'long-horizon') {
    npm run smoke:long-horizon -- "$installed"
    if ($LASTEXITCODE -ne 0) { throw 'Installed long-horizon acceptance failed.' }
  } elseif ($Mode -eq 'team-chat') {
    $env:NEXUS_TEAM_PROTOCOL_FAULT = '1'
    $env:NEXUS_TEAM_TOOL_FAULT = '1'
    npm run smoke:team-chat -- "$installed"
    if ($LASTEXITCODE -ne 0) { throw 'Installed two-agent chat acceptance failed.' }
  }
} finally { Pop-Location }

if ($Mode -ne 'shortcuts') {
  Write-Output "INSTALLED_ACCEPTANCE_PASS $Mode"
  return
}

Remove-Item -LiteralPath $shortcut -Force
if (Test-Path -LiteralPath $shortcut) { throw "Could not remove $shortcut for the reinstall check." }
$reinstall = Start-Process -FilePath $installers[0].FullName -ArgumentList @('/S', '/currentuser') -Wait -PassThru -WindowStyle Hidden
if ($reinstall.ExitCode -ne 0) { throw "Reinstall stopped with code $($reinstall.ExitCode)." }
$reinstalled = Get-InstalledApplication
if (-not [StringComparer]::OrdinalIgnoreCase.Equals($reinstalled, $installed)) {
  throw "Reinstall moved the exact current-user application from '$installed' to '$reinstalled'."
}
[void](Assert-InstalledShortcut)

# A previous development build can leave a same-name link. NSIS's
# KeepShortcuts upgrade path used to preserve that stale target.
$shell = New-Object -ComObject WScript.Shell
$stale = $null
try {
  $stale = $shell.CreateShortcut($shortcut)
  $stale.TargetPath = $installers[0].FullName
  $stale.Arguments = '--stale-development-shortcut'
  $stale.WorkingDirectory = Split-Path -Parent $installers[0].FullName
  $stale.IconLocation = "$($installers[0].FullName),7"
  $stale.Save()
} finally {
  if ($null -ne $stale -and [Runtime.InteropServices.Marshal]::IsComObject($stale)) {
    [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($stale)
  }
  if ([Runtime.InteropServices.Marshal]::IsComObject($shell)) {
    [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)
  }
}
$staleReinstall = Start-Process -FilePath $installers[0].FullName -ArgumentList @('/S', '/currentuser') -Wait -PassThru -WindowStyle Hidden
if ($staleReinstall.ExitCode -ne 0) { throw "Stale-shortcut reinstall stopped with code $($staleReinstall.ExitCode)." }
$verifiedShortcut = Assert-InstalledShortcut

$alreadyRunning = @(Get-InstalledProcesses)
if ($alreadyRunning.Count -ne 0) {
  throw "A prior package smoke left the installed app running: $($alreadyRunning.ProcessId -join ', ')."
}
$launched = @()
try {
  [void](Start-Process -FilePath $verifiedShortcut.ShortcutPath -PassThru -WindowStyle Hidden)
  $deadline = [DateTime]::UtcNow.AddSeconds(30)
  do {
    $launched = @(Get-InstalledProcesses)
    if ($launched.Count -eq 0) { Start-Sleep -Milliseconds 250 }
  } while ($launched.Count -eq 0 -and [DateTime]::UtcNow -lt $deadline)
  if ($launched.Count -eq 0) { throw "The recreated desktop shortcut did not launch the installed app." }
} finally {
  Stop-InstalledProcessTrees
}

Write-Output "INSTALLED_ACCEPTANCE_PASS $Mode"
