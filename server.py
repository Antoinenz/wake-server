#!/usr/bin/env python3
"""wake-server — multi-PC wake/sleep control server"""
import http.server
import json
import socket
import subprocess
import threading
import time
import urllib.request
import urllib.error
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

CONFIG_PATH = Path(__file__).parent / 'config.json'
DEFAULT_CONFIG = {'server_port': 8081, 'pcs': []}


def load_config():
    if not CONFIG_PATH.exists():
        save_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)
    with open(CONFIG_PATH) as f:
        return json.load(f)


def save_config(cfg):
    with open(CONFIG_PATH, 'w') as f:
        json.dump(cfg, f, indent=2)


# ── Network ───────────────────────────────────────────────────────────────────

def send_wol(mac, ip):
    mac_bytes = bytes.fromhex(mac.replace(':', '').replace('-', ''))
    magic = b'\xff' * 6 + mac_bytes * 16
    parts = ip.split('.')
    subnet_bcast = f'{parts[0]}.{parts[1]}.{parts[2]}.255'
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for addr in ('255.255.255.255', subnet_bcast):
            s.sendto(magic, (addr, 9))


def ping(ip):
    return subprocess.run(
        ['ping', '-c', '1', '-W', '1', ip], capture_output=True
    ).returncode == 0


def request_sleep(ip, port):
    try:
        urllib.request.urlopen(
            urllib.request.Request(f'http://{ip}:{port}/sleep', method='POST'),
            timeout=5,
        )
        return True
    except urllib.error.URLError:
        return False


# ── Status cache ──────────────────────────────────────────────────────────────

_cache: dict = {}
_lock = threading.Lock()
CACHE_TTL = 5   # seconds


def cached_ping(pc):
    pid, ip = pc['id'], pc['ip']
    now = time.time()
    with _lock:
        if pid in _cache and now - _cache[pid][0] < CACHE_TTL:
            return _cache[pid][1]
    awake = ping(ip)
    with _lock:
        _cache[pid] = (now, awake)
    return awake


def bust(pc_id):
    with _lock:
        _cache.pop(pc_id, None)


# ── HTML / PWA assets ─────────────────────────────────────────────────────────

HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <meta name="apple-mobile-web-app-title" content="Wake">
  <meta name="theme-color" content="#111111">
  <link rel="manifest" href="/manifest.json">
  <link rel="apple-touch-icon" href="/icon-touch.svg">
  <title>Wake</title>
  <style>
    *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
    :root{
      --bg:#111;--surface:#1c1c1c;--border:#2a2a2a;
      --text:#e8e8e8;--sub:#666;--green:#4ade80;
      --btn:#222;--btn-h:#2c2c2c;
    }
    @media(prefers-color-scheme:light){
      :root{--bg:#f2f2f7;--surface:#fff;--border:#e5e5ea;
            --text:#1c1c1e;--sub:#8e8e93;--btn:#fff;--btn-h:#f2f2f7}
    }
    body{
      font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
      background:var(--bg);color:var(--text);min-height:100dvh;
      padding:max(env(safe-area-inset-top,0px),1.5rem)
              max(env(safe-area-inset-right,0px),1rem)
              max(env(safe-area-inset-bottom,0px),1rem)
              max(env(safe-area-inset-left,0px),1rem);
    }
    h1{text-align:center;font-size:.75rem;font-weight:500;letter-spacing:.12em;
       text-transform:uppercase;color:var(--sub);padding:1.5rem 0 1.25rem}
    .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));
          gap:.75rem;max-width:900px;margin:0 auto}
    .card{background:var(--surface);border:1px solid var(--border);
          border-radius:14px;padding:1.2rem 1.25rem 1rem}
    .row{display:flex;align-items:center;gap:.65rem;margin-bottom:.75rem}
    .dot{width:8px;height:8px;border-radius:50%;background:var(--border);
         flex-shrink:0;transition:background .4s}
    .dot.on{background:var(--green)}
    .dot.blink{animation:blink 1s ease-in-out infinite}
    @keyframes blink{50%{opacity:.2}}
    .pc-name{font-weight:600;font-size:.95rem;flex:1}
    .pc-state{font-size:.72rem;color:var(--sub)}
    .pc-ip{font-size:.7rem;color:var(--sub);font-family:ui-monospace,monospace;margin-bottom:.85rem}
    .btns{display:flex;gap:.45rem}
    button{
      flex:1;padding:.5rem 0;border:1px solid var(--border);border-radius:9px;
      background:var(--btn);color:var(--text);font-size:.78rem;cursor:pointer;
      transition:background .12s
    }
    button:hover{background:var(--btn-h)}
    button:active{opacity:.6}
    .msg{font-size:.68rem;color:var(--sub);margin-top:.6rem;min-height:1em}
    .empty{text-align:center;color:var(--sub);padding:5rem 1rem;line-height:1.8}
    code{font-family:ui-monospace,monospace;font-size:.85em}
  </style>
</head>
<body>
  <h1>Wake</h1>
  <div class="grid" id="grid"><div class="empty">Loading…</div></div>
  <script>
  let pcs=[];
  const esc=s=>s.replace(/&/g,'&amp;').replace(/</g,'&lt;');
  const $=id=>document.getElementById(id);

  async function init(){
    pcs=await(await fetch('/api/pcs')).json();
    render();
    pcs.forEach(p=>refresh(p.id));
    setInterval(()=>pcs.forEach(p=>refresh(p.id)),20000);
  }

  function render(){
    const g=$('grid');
    if(!pcs.length){
      g.innerHTML='<div class="empty">No PCs configured.<br>Run <code>python3 setup.py add</code> to add one.</div>';
      return;
    }
    g.innerHTML=pcs.map(p=>`
      <div class="card">
        <div class="row">
          <span class="dot blink" id="d-${p.id}"></span>
          <span class="pc-name">${esc(p.name)}</span>
          <span class="pc-state" id="s-${p.id}">…</span>
        </div>
        <div class="pc-ip">${esc(p.ip)}</div>
        <div class="btns">
          <button onclick="doWake('${p.id}')">Wake</button>
          <button onclick="doSleep('${p.id}')">Sleep</button>
        </div>
        <div class="msg" id="m-${p.id}"></div>
      </div>`).join('');
  }

  async function refresh(id){
    try{
      const d=await(await fetch('/api/pcs/'+id+'/status')).json();
      const dot=$('d-'+id),st=$('s-'+id);
      if(!dot)return;
      dot.className='dot'+(d.awake?' on':'');
      st.textContent=d.awake?'online':'offline';
    }catch{}
  }

  async function doWake(id){
    msg(id,'Sending magic packet…');
    await fetch('/api/pcs/'+id+'/wake',{method:'POST'});
    msg(id,'Magic packet sent');
    poll(id,0);
  }

  async function doSleep(id){
    msg(id,'Sending sleep command…');
    const r=await fetch('/api/pcs/'+id+'/sleep',{method:'POST'});
    msg(id,r.ok?'Sleep command sent':'Sleep listener did not respond');
    if(r.ok)setTimeout(()=>refresh(id),4000);
  }

  async function poll(id,n){
    if(n>30)return;
    const dot=$('d-'+id);
    if(dot)dot.className='dot blink';
    try{
      const d=await(await fetch('/api/pcs/'+id+'/status')).json();
      if(d.awake){
        if(dot)dot.className='dot on';
        $('s-'+id).textContent='online';
        msg(id,'✓ Online');
        return;
      }
    }catch{}
    setTimeout(()=>poll(id,n+1),5000);
  }

  function msg(id,t){const e=$('m-'+id);if(e)e.textContent=t;}

  if('serviceWorker'in navigator)navigator.serviceWorker.register('/sw.js').catch(()=>{});
  init();
  </script>
</body>
</html>"""

MANIFEST = json.dumps({
    'name': 'Wake Server',
    'short_name': 'Wake',
    'description': 'Wake and sleep your PCs remotely',
    'start_url': '/',
    'display': 'standalone',
    'background_color': '#111111',
    'theme_color': '#111111',
    'icons': [
        {'src': '/icon.svg',       'sizes': 'any', 'type': 'image/svg+xml', 'purpose': 'any'},
        {'src': '/icon-touch.svg', 'sizes': 'any', 'type': 'image/svg+xml', 'purpose': 'maskable'},
    ],
})

# Lightning bolt icons (no external deps)
ICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
    '<rect width="100" height="100" rx="22" fill="#111"/>'
    '<polygon points="58,10 28,55 50,55 42,90 72,45 50,45" fill="#fff"/>'
    '</svg>'
)
ICON_TOUCH_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 180 180">'
    '<rect width="180" height="180" rx="38" fill="#111"/>'
    '<polygon points="105,18 50,99 90,99 75,162 130,81 90,81" fill="#fff"/>'
    '</svg>'
)

SW_JS = """\
const V='wake-v1';
self.addEventListener('install',e=>e.waitUntil(caches.open(V).then(c=>c.add('/'))));
self.addEventListener('fetch',e=>{
  if(e.request.method!=='GET')return;
  e.respondWith(fetch(e.request).catch(()=>caches.match(e.request)));
});"""

# ── Routing helpers ───────────────────────────────────────────────────────────

def extract_id(path, prefix, suffix):
    """Return the {id} segment if path matches prefix + {id} + suffix, else None."""
    if not path.startswith(prefix):
        return None
    rest = path[len(prefix):]
    if suffix:
        if not rest.endswith(suffix):
            return None
        rest = rest[:-len(suffix)]
    if not rest or '/' in rest:
        return None
    return rest


def find_pc(cfg, pc_id):
    return next((p for p in cfg['pcs'] if p['id'] == pc_id), None)


# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(http.server.BaseHTTPRequestHandler):
    _STATIC = {
        '/':               ('text/html; charset=utf-8',  lambda: HTML.encode()),
        '/manifest.json':  ('application/manifest+json', lambda: MANIFEST.encode()),
        '/icon.svg':       ('image/svg+xml',             lambda: ICON_SVG.encode()),
        '/icon-touch.svg': ('image/svg+xml',             lambda: ICON_TOUCH_SVG.encode()),
        '/sw.js':          ('application/javascript',    lambda: SW_JS.encode()),
    }

    def do_GET(self):
        if self.path in self._STATIC:
            ct, body_fn = self._STATIC[self.path]
            return self._send(200, ct, body_fn())

        cfg = load_config()

        if self.path == '/api/pcs':
            pcs = [{'id': p['id'], 'name': p['name'], 'ip': p['ip']}
                   for p in cfg['pcs']]
            return self._json(pcs)

        pc_id = extract_id(self.path, '/api/pcs/', '/status')
        if pc_id is not None:
            pc = find_pc(cfg, pc_id)
            if pc:
                return self._json({'awake': cached_ping(pc)})
            return self._send(404, 'text/plain', b'Not found')

        self._send(404, 'text/plain', b'Not found')

    def do_POST(self):
        cfg = load_config()

        pc_id = extract_id(self.path, '/api/pcs/', '/wake')
        if pc_id is not None:
            pc = find_pc(cfg, pc_id)
            if not pc:
                return self._send(404, 'text/plain', b'Not found')
            send_wol(pc['mac'], pc['ip'])
            bust(pc['id'])
            return self._json({'ok': True})

        pc_id = extract_id(self.path, '/api/pcs/', '/sleep')
        if pc_id is not None:
            pc = find_pc(cfg, pc_id)
            if not pc:
                return self._send(404, 'text/plain', b'Not found')
            ok = request_sleep(pc['ip'], pc.get('sleep_port', 8765))
            bust(pc['id'])
            return self._send(
                200 if ok else 502, 'application/json',
                json.dumps({'ok': ok}).encode(),
            )

        self._send(404, 'text/plain', b'Not found')

    def _json(self, data):
        self._send(200, 'application/json', json.dumps(data).encode())

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    cfg = load_config()
    port = cfg.get('server_port', 8081)
    if not cfg.get('pcs'):
        print('No PCs configured yet.  Run: python3 setup.py add')
    server = http.server.HTTPServer(('0.0.0.0', port), Handler)
    print(f'Wake server → http://0.0.0.0:{port}')
    server.serve_forever()
