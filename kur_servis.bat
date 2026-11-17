@echo off
chcp 65001 >nul
cd /d "%~dp0"
set DEST=C:\ProgramData\UzaktanPC

net session >nul 2>&1
if errorlevel 1 (
  echo Bu dosyaya sag tikla ve "Yonetici olarak calistir" sec.
  pause
  exit /b 1
)

echo.
echo Uzaktan PC servis kurulumu
echo ----------------------------
echo Not: baslat.bat penceresi aciksa once onu kapat.
echo.

set PY=py -3
%PY% --version >nul 2>nul || set PY=python
%PY% --version >nul 2>nul
if errorlevel 1 (
  echo Python bulunamadi. Once python.org adresinden Python kur.
  pause
  exit /b 1
)
for /f "delims=" %%i in ('%PY% -c "import sys;print(sys.base_prefix)"') do set BP=%%i
echo "%BP%" | find /i "WindowsApps" >nul
if not errorlevel 1 (
  echo Microsoft Store Python'u gorunuyor. Servis icin python.org surumu gerekli.
  pause
  exit /b 1
)

echo [1/6] Eski servis durduruluyor...
schtasks /End /TN "UzaktanPC" >nul 2>nul
timeout /t 3 /nobreak >nul

echo [2/6] Dosyalar kopyalaniyor...
if not exist "%DEST%" mkdir "%DEST%"
copy /Y "%~dp0server.py" "%DEST%\" >nul 2>nul
copy /Y "%~dp0servis.py" "%DEST%\" >nul 2>nul
copy /Y "%~dp0index.html" "%DEST%\" >nul 2>nul
copy /Y "%~dp0requirements.txt" "%DEST%\" >nul 2>nul
copy /Y "%~dp0gorev_kaydet.ps1" "%DEST%\" >nul 2>nul
if not exist "%DEST%\config.json" if exist "%~dp0config.json" copy "%~dp0config.json" "%DEST%\config.json" >nul

echo [3/6] Python paketleri kuruluyor (birkac dakika surebilir)...
if not exist "%DEST%\.venv\Scripts\python.exe" %PY% -m venv "%DEST%\.venv"
"%DEST%\.venv\Scripts\python.exe" -m pip install -q -r "%DEST%\requirements.txt"
if errorlevel 1 (
  echo Paket kurulumu basarisiz oldu.
  pause
  exit /b 1
)

echo [4/6] Sifre...
if not exist "%DEST%\config.json" goto yeni
choice /c EH /m "Sifreyi degistirmek ister misin"
if errorlevel 2 goto devam
"%DEST%\.venv\Scripts\python.exe" "%DEST%\server.py" --ayarla --sifre
goto devam
:yeni
"%DEST%\.venv\Scripts\python.exe" "%DEST%\server.py" --ayarla
:devam
if not exist "%DEST%\config.json" (
  echo Sifre belirlenmedi, kurulum iptal.
  pause
  exit /b 1
)

echo [5/6] Guvenlik duvari ve klasor izinleri...
netsh advfirewall firewall delete rule name="UzaktanPC" >nul 2>nul
netsh advfirewall firewall add rule name="UzaktanPC" dir=in action=allow protocol=TCP localport=8765 remoteip=LocalSubnet,100.64.0.0/10 profile=any >nul
icacls "%DEST%" /inheritance:r /grant:r "SYSTEM:(OI)(CI)F" "Administrators:(OI)(CI)F" >nul

echo [6/6] Bilgisayar acilisinda baslayacak gorev olusturuluyor...
powershell -NoProfile -ExecutionPolicy Bypass -File "%DEST%\gorev_kaydet.ps1"
schtasks /Run /TN "UzaktanPC" >nul

echo.
echo Kurulum tamam. Uzaktan PC artik bilgisayar her acildiginda kendiliginden baslar,
echo kilit ekraninda ve giris ekraninda da calisir. Pencereyi kapatabilirsin.
echo Adresin degismedi: telefondaki Tailscale adresi (100.x.x.x:8765).
echo Kayit dosyalari: %DEST%\server.log ve servis.log
echo.
pause
