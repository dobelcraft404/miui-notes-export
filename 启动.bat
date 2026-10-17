@echo off
rem ============================================================
rem  小米笔记备份导出工具 —— 双击本文件即可打开图形界面
rem  备份包在界面里手动选择（不会自动处理任何文件）
rem ============================================================
setlocal
cd /d "%~dp0"

set "PY="
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if not defined PY where python >nul 2>nul && set "PY=python"
if not defined PY where py >nul 2>nul && set "PY=py -3"

if not defined PY (
    echo [错误] 没有找到 Python。
    echo 请先安装 Python 3.9 或更高版本，安装时勾选 "Add Python to PATH"。
    echo 下载地址：https://www.python.org/downloads/windows/
    echo.
    pause
    exit /b 1
)

%PY% "%~dp0main.py" %*
if errorlevel 1 (
    echo.
    echo [提示] 图形界面启动失败，可以先在命令行里试试：
    echo     %PY% -m miui_notes_bak "备份包.zip" -o "导出结果" -f all
    echo.
    pause
)
endlocal
