@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ========================================
echo   11Lab Voice Tool - Setup
echo ========================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Khong tim thay Python!
    echo Tai: https://www.python.org/downloads/
    echo Nho TICK "Add Python to PATH" khi cai!
    pause
    exit /b 1
)
echo [OK] Python found

echo.
echo Cai package (tu requirements.txt)...
python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Cai package that bai! Kiem tra mang/pip.
    pause
    exit /b 1
)
echo [OK] Packages installed

echo.
echo Tai trinh duyet antidetect Camoufox cho MODE C (~100MB, 1 lan)...
python -m camoufox fetch
if errorlevel 1 (
    echo [CANH BAO] Tai Camoufox that bai - MODE C se khong chay.
    echo Chay lai sau bang: python -m camoufox fetch
) else (
    echo [OK] Camoufox san sang
)

echo.
echo Tai Chromium cho Playwright (du phong)...
python -m playwright install chromium >nul 2>&1

if not exist "config" mkdir config
if not exist "logs" mkdir logs
if not exist "output" mkdir output

echo.
echo Tao shortcut chay tool...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0make_shortcut.ps1"

echo.
echo ========================================
echo   SETUP XONG!
echo ========================================
echo.
echo Chay tool: double-click "START_GUI" (trong thu muc) hoac
echo            shortcut "11Lab Voice Tool" ngoai Desktop.
echo.
echo LUU Y may moi:
echo  - MODE C (mac dinh): tao voice KHONG can tai khoan. Chi can 4G proxy chay.
echo    Xem huong dan chi tiet trong MODE_C_SETUP.md
echo  - Tab "4G Proxy" -^> "Cai dat 4G": chinh IP/cong cho dung.
echo  - Tab "Auto Convert" -^> "Cai dat nang cao": doi "Ten Google Sheet".
echo  - Google Chrome chi can cho MODE MASTER (khong bat buoc voi Mode C).
echo.
pause
