$watcherExe = "C:\Users\Matth\RuneSync\dist\RuneSyncWatcher.exe"
Set-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "RuneSync" -Value "`"$watcherExe`""
$val = (Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run").RuneSync
echo "RuneSync startup is now: $val"
