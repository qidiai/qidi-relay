' Launch opencode serve detached (scheduled-task friendly, no console).
' Mirrors run_mihomo_hidden.vbs pattern: wscript host keeps the server
' alive independent of any tool Job Object.
CreateObject("WScript.Shell").CurrentDirectory = "C:\Users\28970\AppData\Roaming\npm"
CreateObject("WScript.Shell").Run """C:\Users\28970\AppData\Roaming\npm\opencode.cmd"" serve --port 4096 --hostname 127.0.0.1", 0, False
