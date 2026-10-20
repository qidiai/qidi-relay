' Launch antigravity2api bridge detached (ASCII only, no BOM).
Dim sh, fso, here
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = here
sh.Run """C:\Program Files\Python312\python.exe"" """ & here & "\antigravity2api.py""", 0, False