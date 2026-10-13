' Launch token_sentinel.py with no console window (Task Scheduler wrapper).
' wscript.exe is a GUI-subsystem host, so no window flashes.
' Run args: 0 = hidden window, False = do not wait.
' All ASCII on purpose; paths are resolved from this script's own folder.
' python.exe uses an absolute path because a scheduled task's PATH is unreliable.
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")
here = fso.GetParentFolderName(WScript.ScriptFullName)
sh.Run """C:\Program Files\Python312\python.exe"" """ & here & """\token_sentinel.py""", 0, False
