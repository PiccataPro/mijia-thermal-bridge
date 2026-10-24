' 静默启动温控服务（无黑窗）。开机自启：把快捷方式放进 shell:startup。
' 用法：改 EXE / SCRIPT 两行为你自己的路径即可。
On Error Resume Next

Dim shell, exe, script, wmi, procs, p, running, i
Set shell = CreateObject("WScript.Shell")

' ===== 改成你的路径 =====
exe    = "C:\mijia-thermal-bridge\.venv\Scripts\pythonw.exe"
script = "C:\mijia-thermal-bridge\thermal_link.py"
' ========================

' 连续两次检测（间隔 400ms）避免双击竞态导致重复实例。
' WMI 查询失败时不阻断启动（On Error Resume Next 兜底）；
' 真正的单实例保障在 thermal_link.py 的 PID 锁里。
For i = 1 To 2
    running = False
    Set wmi = GetObject("winmgmts:\\.\root\cimv2")
    If Err.Number = 0 Then
        Set procs = wmi.ExecQuery("SELECT CommandLine FROM Win32_Process WHERE Name='pythonw.exe'")
        If Err.Number = 0 Then
            For Each p In procs
                If Not IsNull(p.CommandLine) Then
                    If InStr(p.CommandLine, "thermal_link.py") > 0 Then running = True
                End If
            Next
        End If
    End If
    Err.Clear
    If running Then WScript.Quit 0
    If i = 1 Then WScript.Sleep 400
Next

shell.CurrentDirectory = Left(script, InStrRev(script, "\") - 1)
' 0 = 隐藏窗口, False = 不等待
shell.Run """" & exe & """ """ & script & """ --loop", 0, False

If Err.Number <> 0 Then
    MsgBox "启动失败: " & Err.Description, 16, "mijia-thermal-bridge"
End If
