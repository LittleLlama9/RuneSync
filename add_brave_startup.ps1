$bravePath = "$env:LOCALAPPDATA\BraveSoftware\Brave-Browser\Application\brave.exe"
$value = "`"$bravePath`" --remote-debugging-port=9222 --no-startup-window"
Set-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "BraveBrowser" -Value $value
Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" | Select-Object RuneSync, BraveBrowser
