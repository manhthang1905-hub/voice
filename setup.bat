@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ========================================
echo   11Lab Voice Tool - Setup
echo ========================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found!
    echo Download: https://www.python.org/downloads/
    echo Tick "Add Python to PATH" when installing!
    pause
    exit /b 1
)
echo [OK] Python found

echo.
echo Installing packages (tu requirements.txt)...
python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Cai package that bai! Kiem tra mang/pip.
    pause
    exit /b 1
)
echo [OK] Packages installed

if not exist "config" mkdir config
if not exist "logs" mkdir logs
if not exist "output" mkdir output

echo.
echo ========================================
echo   SETUP DONE!
echo ========================================
echo.
echo LUU Y khi chay may khac:
echo  - Mo tab "4G Proxy" -^> nut "Cai dat 4G" de chinh IP/cong cho dung may.
echo  - Can cai san: Google Chrome (cho login master) va ffmpeg co san trong thu muc ffmpeg/.
echo.
echo Run: python run.py   (hoac double-click START.bat)
echo.
pause
