' START.vbs - Mo 11Lab Voice Tool (an, khong hien CMD).
' Dung duong dan TUONG DOI -> copy sang may nao cung chay (khong phu thuoc user/path).
Dim fso, sh, dir
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = dir
' Uu tien pythonw (khong console). 0 = chay an, False = khong cho.
On Error Resume Next
sh.Run "pythonw.exe """ & dir & "\run.py""", 0, False
If Err.Number <> 0 Then
    ' pythonw khong co tren PATH -> thu py launcher
    sh.Run "pyw.exe """ & dir & "\run.py""", 0, False
End If
