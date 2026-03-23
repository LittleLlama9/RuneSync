$ws = New-Object -ComObject WScript.Shell

# 1. Fix taskbar pinned Brave shortcut
$taskbar = "$env:APPDATA\Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar\Brave.lnk"
$lnk = $ws.CreateShortcut($taskbar)
$lnk.Arguments = "--remote-debugging-port=9222"
$lnk.Save()
echo "Taskbar shortcut updated"

# 2. Fix desktop Brave shortcut
$desktop = "$env:USERPROFILE\OneDrive\Desktop\Brave.lnk"
if (Test-Path $desktop) {
    $lnk2 = $ws.CreateShortcut($desktop)
    $lnk2.Arguments = "--remote-debugging-port=9222"
    $lnk2.Save()
    echo "Desktop shortcut updated"
}

# 3. Fix startup registry - use py instead of pythonw
Set-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "RuneSync" -Value 'py "C:\Users\Matth\RuneSync\watcher.py"'
echo "Startup registry fixed (py instead of pythonw)"

echo "All done"
