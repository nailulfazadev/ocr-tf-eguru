@echo off
REM Windows: klik dua kali. Membuat dist\Cek Bukti TF e-guru.id\Cek Bukti TF e-guru.id.exe (sekali per versi).
cd /d %~dp0
set "APP=Cek Bukti TF e-guru.id"
if not exist .venv py -m venv .venv
call .venv\Scripts\activate
pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --windowed --name "%APP%" --collect-data googleapiclient --collect-data certifi main.py
if exist vendor xcopy /E /I /Y vendor "dist\%APP%\vendor" >nul
echo.
echo Selesai. Aplikasi: %cd%\dist\%APP%\%APP%.exe
explorer "dist\%APP%"
pause
