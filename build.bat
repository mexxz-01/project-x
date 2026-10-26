@echo off
REM Project X -> single-file winhost.exe. tkinter plugin is REQUIRED or blackout breaks.
python -m nuitka ^
  --onefile ^
  --assume-yes-for-downloads ^
  --windows-disable-console ^
  --enable-plugin=tk-inter ^
  --include-module=sqlite3 ^
  --output-filename=winhost.exe ^
  --output-dir=dist ^
  x.pyw
echo.
echo built dist\winhost.exe
pause
