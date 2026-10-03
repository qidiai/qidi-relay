' Launch workbuddy2api keepalive with no console window (Task Scheduler wrapper).
' wscript is a GUI-subsystem host: no window flash. Run args: 0=hidden, False=do not wait.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
here = fso.GetParentFolderName(WScript.ScriptFullName)
sh.Run "powershell -NoProfile -ExecutionPolicy Bypass -File """ & here & "\\keepalive.ps1""", 0, False
