$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut("C:\Users\Matth\OneDrive\Desktop\RuneSync.lnk")
$s.TargetPath = "pythonw"
$s.Arguments = '"C:\Users\Matth\RuneSync\watcher.py"'
$s.WorkingDirectory = "C:\Users\Matth\RuneSync"
$s.WindowStyle = 7
$s.Description = "RuneSync - Auto Rune Importer"
$s.Save()
echo "Shortcut updated"
