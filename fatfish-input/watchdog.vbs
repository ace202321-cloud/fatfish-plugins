' FatFish Input watchdog
Option Explicit
Dim fso, here, logPath, svc, procClass
On Error Resume Next
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
logPath = here & "\watchdog.log"
Set svc = GetObject("winmgmts:\\.\root\cimv2")
Set procClass = GetObject("winmgmts:\\.\root\cimv2:Win32_Process")
If Err.Number <> 0 Then
    WScript.Quit 1
End If

Do
    On Error Resume Next
    If Not IsRunning() Then
        procClass.Create "wscript.exe """ & here & "\start.vbs"""
        LogLine "restart rc=" & Err.Number
        WScript.Sleep 6000
    End If
    WScript.Sleep 60000
Loop

Function IsRunning()
    Dim procs, p, alive
    alive = False
    Set procs = svc.ExecQuery("SELECT * FROM Win32_Process WHERE Name='pythonw.exe'")
    For Each p In procs
        If Not IsNull(p.CommandLine) Then
            If InStr(LCase(p.CommandLine), "fatfish_input.py") > 0 Then alive = True
        End If
    Next
    IsRunning = alive
End Function

Sub LogLine(msg)
    Dim f
    On Error Resume Next
    Set f = fso.OpenTextFile(logPath, 8, True)
    f.WriteLine Now & "  " & msg
    f.Close
End Sub