' Launch keepalive.ps1 with no console window (Task Scheduler wrapper).
' wscript.exe is a GUI-subsystem host, so no window flashes.
' Run args: 0 = hidden window, False = do not wait.
' All ASCII on purpose; paths are resolved from this script's own folder.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
here = fso.GetParentFolderName(WScript.ScriptFullName)
sh.Run "powershell -NoProfile -ExecutionPolicy Bypass -File """ & here & "\keepalive.ps1""", 0, False
