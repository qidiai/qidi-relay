' Launch qoder2api proxy detached (ASCII only, no BOM).
' Scheduled-task friendly: wscript host keeps python alive outside any tool Job Object.
Dim sh, fso, here
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = here
Dim pyexe
If fso.FileExists(here & "\python\python.exe") Then
    pyexe = here & "\python\python.exe"
Else
    pyexe = "python"
End If
sh.Run """" & pyexe & """ """ & here & "\qoder_proxy.py"" --port 8790 --pid-file """ & here & "\qoder-proxy.pid""", 0, False