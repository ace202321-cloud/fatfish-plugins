' 肥鱼输入器 - 静默启动器（不弹控制台，不依赖任何宿主程序）
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
Set ws = CreateObject("WScript.Shell")
ws.CurrentDirectory = here
ws.Run "pythonw.exe """ & here & "\fatfish_input.py""", 0, False
