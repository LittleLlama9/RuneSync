$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut("$env:USERPROFILE\OneDrive\Desktop\Brave.lnk")
$lnk.TargetPath = $lnk.TargetPath
$lnk.Arguments = "--remote-debugging-port=9222"
$lnk.Save()
echo "Brave shortcut updated with debug port"
# Also update Start Menu shortcut if it exists
$sm = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Brave Browser.lnk"
if (Test-Path $sm) {
    $lnk2 = $ws.CreateShortcut($sm)
    $lnk2.Arguments = "--remote-debugging-port=9222"
    $lnk2.Save()
    echo "Start menu shortcut also updated"
}
