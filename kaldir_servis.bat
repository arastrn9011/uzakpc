@echo off
chcp 65001 >nul
net session >nul 2>&1
if errorlevel 1 (
  echo Bu dosyaya sag tikla ve "Yonetici olarak calistir" sec.
  pause
  exit /b 1
)
schtasks /End /TN "UzaktanPC" >nul 2>nul
schtasks /Delete /TN "UzaktanPC" /F >nul 2>nul
if exist "C:\ProgramData\UzaktanPC\server.pid" for /f %%p in (C:\ProgramData\UzaktanPC\server.pid) do taskkill /PID %%p /T /F >nul 2>nul
netsh advfirewall firewall delete rule name="UzaktanPC" >nul 2>nul
echo Servis kaldirildi. Dosyalar C:\ProgramData\UzaktanPC klasorunde duruyor, istersen silebilirsin.
pause
