!macro customInit
  ${If} ${isForAllUsers}
    SetErrorLevel 2
    Quit
  ${EndIf}
  !insertmacro setInstallModePerUser
!macroend

!macro customUnInit
  ${If} ${isForAllUsers}
    SetErrorLevel 2
    Quit
  ${EndIf}
  !insertmacro setInstallModePerUser
!macroend

!macro customInstallMode
  StrCpy $isForceMachineInstall "0"
  StrCpy $isForceCurrentInstall "1"
!macroend

!macro customInstall
  # electron-builder intentionally preserves an existing same-name shortcut
  # when KeepShortcuts is set during an upgrade. That can leave a development
  # checkout target behind after the packaged app has installed successfully.
  # Recreate this exact installer-owned link from the newly installed binary.
  ClearErrors
  Delete "$newDesktopLink"
  ${If} ${Errors}
    DetailPrint "Could not replace the stale Nexus Harness desktop shortcut."
    SetErrorLevel 3
    Quit
  ${EndIf}
  SetOutPath "$INSTDIR"
  ClearErrors
  CreateShortCut "$newDesktopLink" "$appExe" "" "$appExe" 0 "" "" "${APP_DESCRIPTION}"
  ${If} ${Errors}
    DetailPrint "Could not create the Nexus Harness desktop shortcut."
    SetErrorLevel 4
    Quit
  ${EndIf}
  WinShell::SetLnkAUMI "$newDesktopLink" "${APP_ID}"
  System::Call 'Shell32::SHChangeNotify(i 0x8000000, i 0, i 0, i 0)'
!macroend
