$py = 'C:\Users\Matth\AppData\Local\Programs\Python\Python314\python.exe'
$script = 'C:\Users\Matth\game_watcher.py'
Set-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name 'RuneSync' -Value "`"$py`" `"$script`""
Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' | Select-Object RuneSync | Format-List
