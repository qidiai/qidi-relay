' Launch Codely points check-in with no console window (Task Scheduler wrapper).
' wscript.exe is a GUI-subsystem host, so no window flashes.
' Run args: 0 = hidden window, False = do not wait.
' Keep this file ASCII-only; paths are resolved from this script's own folder.
' python.exe uses an absolute path because a scheduled task's PATH is unreliable.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
here = fso.GetParentFolderName(WScript.ScriptFullName)
cmd = Chr(34) & "C:\Program Files\Python312\python.exe" & Chr(34) & " " & Chr(34) & here & "\codely_points_checkin.py" & Chr(34)
sh.Run cmd, 0, False
