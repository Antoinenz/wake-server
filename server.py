#!/usr/bin/env python3
"""wake-server — multi-PC wake/sleep control server"""
import concurrent.futures
import http.server
import json
import queue
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
CACHE_TTL = 8  # slightly under the 10 s monitor interval


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


# ── SSE broadcast ─────────────────────────────────────────────────────────────

_sse_clients: list = []
_sse_lock = threading.Lock()


def _broadcast(data: dict):
    frame = ('data: ' + json.dumps(data) + '\n\n').encode()
    with _sse_lock:
        dead = []
        for q in _sse_clients:
            try:
                q.put_nowait(frame)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _sse_clients.remove(q)


# ── Background monitor ────────────────────────────────────────────────────────

def _monitor():
    """Parallel-ping all PCs every 10 s; push status changes to SSE clients."""
    prev: dict = {}
    while True:
        try:
            cfg = load_config()
            pcs = cfg.get('pcs', [])
            if pcs:
                with concurrent.futures.ThreadPoolExecutor(max_workers=len(pcs)) as ex:
                    futures = {ex.submit(ping, pc['ip']): pc for pc in pcs}
                    for fut, pc in futures.items():
                        try:
                            awake = fut.result(timeout=5)
                        except Exception:
                            continue
                        pid = pc['id']
                        with _lock:
                            _cache[pid] = (time.time(), awake)
                        if prev.get(pid) != awake:
                            prev[pid] = awake
                            _broadcast({'type': 'status', 'id': pid, 'awake': awake})
        except Exception:
            pass
        time.sleep(10)


# ── HTML ──────────────────────────────────────────────────────────────────────

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
      --btn:#222;--btn-h:#2c2c2c;--err:#b91c1c;
    }
    @media(prefers-color-scheme:light){
      :root{--bg:#f2f2f7;--surface:#fff;--border:#e5e5ea;
            --text:#1c1c1e;--sub:#8e8e93;--btn:#fff;--btn-h:#f2f2f7}
    }
    body{
      font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
      background:var(--bg);color:var(--text);min-height:100dvh;
      padding:max(env(safe-area-inset-top,0px),2.5rem)
              max(env(safe-area-inset-right,0px),1rem)
              max(env(safe-area-inset-bottom,0px),1rem)
              max(env(safe-area-inset-left,0px),1rem);
    }
    /* ── disconnected banner ── */
    #banner{
      position:fixed;top:0;left:0;right:0;
      background:var(--err);color:#fff;
      text-align:center;padding:.5rem 1rem;
      font-size:.75rem;font-weight:500;
      transform:translateY(-100%);transition:transform .25s ease;
      z-index:100;
    }
    #banner.show{transform:translateY(0)}
    /* ── layout ── */
    h1{text-align:center;font-size:.75rem;font-weight:500;letter-spacing:.12em;
       text-transform:uppercase;color:var(--sub);padding:1.5rem 0 1.25rem}
    .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));
          gap:.75rem;max-width:900px;margin:0 auto}
    /* ── card ── */
    .card{background:var(--surface);border:1px solid var(--border);
          border-radius:14px;padding:1.2rem 1.25rem 1rem}
    .row{display:flex;align-items:center;gap:.65rem;margin-bottom:.75rem}
    .dot{width:8px;height:8px;border-radius:50%;background:var(--border);
         flex-shrink:0;transition:background .4s}
    .dot.on{background:var(--green)}
    .dot.spin{animation:blink 1s ease-in-out infinite}
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
    button:disabled{opacity:.35;cursor:default}
    .msg{font-size:.68rem;color:var(--sub);margin-top:.6rem;min-height:1em;
         word-break:break-word}
    .msg.err{color:#f87171}
    /* ── empty / loading ── */
    .empty{text-align:center;color:var(--sub);padding:5rem 1rem;line-height:1.8}
    code{font-family:ui-monospace,monospace;font-size:.85em}
  </style>
</head>
<body>
  <div id="banner"></div>
  <h1>Wake</h1>
  <div class="grid" id="grid"><div class="empty">Connecting…</div></div>
  <script>
  'use strict';
  let pcs = [];
  let polls = {};           // pc_id -> setTimeout handle (aggressive post-wake polling)
  const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;');
  const $   = id => document.getElementById(id);

  // ── SSE ─────────────────────────────────────────────────────────────────────

  function connect() {
    const es = new EventSource('/events');

    es.onopen = () => {
      hideError();
    };

    es.onmessage = e => {
      let d;
      try { d = JSON.parse(e.data); } catch { return; }
      if (d.type === 'hello') {
        pcs = d.pcs;
        render();
      } else if (d.type === 'status') {
        applyStatus(d.id, d.awake);
      }
    };

    es.onerror = () => {
      // EventSource reconnects automatically; we just show the banner
      showError('Disconnected from server — reconnecting…');
    };
  }

  // ── Status ───────────────────────────────────────────────────────────────────

  function applyStatus(id, awake) {
    const dot = $('d-' + id), st = $('s-' + id);
    if (!dot) return;
    dot.className = 'dot' + (awake ? ' on' : '');
    st.textContent = awake ? 'online' : 'offline';
    // Cancel aggressive poll if we just came online
    if (awake && id in polls) {
      clearTimeout(polls[id]);
      delete polls[id];
      setMsg(id, '✓ Online', false);
    }
  }

  // ── Wake ─────────────────────────────────────────────────────────────────────

  async function doWake(id) {
    setMsg(id, 'Sending magic packet…', false);
    try {
      const r = await fetch('/api/pcs/' + id + '/wake', { method: 'POST' });
      if (!r.ok) {
        const txt = await r.text().catch(() => '');
        throw new Error('Server ' + r.status + (txt ? ': ' + txt : ''));
      }
      setMsg(id, 'Waiting for PC to boot…', false);
      startPoll(id, 0);
    } catch (e) {
      setMsg(id, '✗ ' + friendlyErr(e), true);
    }
  }

  // Aggressive fetch-poll every 3 s while waking (SSE will also fire when online)
  function startPoll(id, n) {
    if (n > 40) { setMsg(id, '✗ No response after 2 min — check the PC', true); return; }
    polls[id] = setTimeout(async () => {
      try {
        const r  = await fetch('/api/pcs/' + id + '/status');
        if (!r.ok) throw new Error();
        const d = await r.json();
        if (d.awake) { applyStatus(id, true); return; }
      } catch {}
      startPoll(id, n + 1);
    }, 3000);
  }

  // ── Sleep ────────────────────────────────────────────────────────────────────

  async function doSleep(id) {
    setMsg(id, 'Sending sleep command…', false);
    try {
      const r = await fetch('/api/pcs/' + id + '/sleep', { method: 'POST' });
      if (r.ok) {
        setMsg(id, 'Sleep command sent', false);
      } else if (r.status === 502) {
        setMsg(id, '✗ Daemon not responding — is sleep-listener.py running?', true);
      } else {
        setMsg(id, '✗ Server error ' + r.status, true);
      }
    } catch (e) {
      setMsg(id, '✗ ' + friendlyErr(e), true);
    }
  }

  // ── Render ────────────────────────────────────────────────────────────────────

  function render() {
    const g = $('grid');
    if (!pcs.length) {
      g.innerHTML = '<div class="empty">No PCs configured.<br>'
                  + 'Run <code>python3 setup.py add</code> to add one.</div>';
      return;
    }
    g.innerHTML = pcs.map(p => `
      <div class="card">
        <div class="row">
          <span class="dot spin" id="d-${p.id}"></span>
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

  // ── Helpers ───────────────────────────────────────────────────────────────────

  function showError(t) { const b = $('banner'); b.textContent = t; b.classList.add('show'); }
  function hideError()  { $('banner').classList.remove('show'); }

  function setMsg(id, t, isErr) {
    const e = $('m-' + id);
    if (!e) return;
    e.textContent = t;
    e.className   = 'msg' + (isErr ? ' err' : '');
  }

  function friendlyErr(e) {
    if (!e || !e.message) return 'Unknown error';
    if (e.message.includes('fetch') || e.name === 'TypeError') return 'Server unreachable';
    return e.message;
  }

  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
  connect();
  </script>
</body>
</html>"""

# ── PWA assets ────────────────────────────────────────────────────────────────

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

        if self.path == '/events':
            return self._handle_sse()

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

    # ── SSE ───────────────────────────────────────────────────────────────────

    def _handle_sse(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.send_header('X-Accel-Buffering', 'no')   # disable nginx buffering if proxied
        self.end_headers()

        client_q: queue.Queue = queue.Queue(maxsize=50)

        # Send hello (pc list) + any cached statuses immediately
        cfg = load_config()
        pcs_list = cfg.get('pcs', [])
        hello = {
            'type': 'hello',
            'pcs': [{'id': p['id'], 'name': p['name'], 'ip': p['ip']} for p in pcs_list],
        }
        try:
            self._sse_write(hello)
            for pc in pcs_list:
                with _lock:
                    entry = _cache.get(pc['id'])
                if entry:
                    _, awake = entry
                    self._sse_write({'type': 'status', 'id': pc['id'], 'awake': awake})
            self.wfile.flush()
        except OSError:
            return

        with _sse_lock:
            _sse_clients.append(client_q)

        try:
            while True:
                try:
                    frame = client_q.get(timeout=25)
                except queue.Empty:
                    frame = b': ping\n\n'    # keep-alive heartbeat
                self.wfile.write(frame)
                self.wfile.flush()
        except OSError:
            pass
        finally:
            with _sse_lock:
                try:
                    _sse_clients.remove(client_q)
                except ValueError:
                    pass

    def _sse_write(self, data: dict):
        self.wfile.write(('data: ' + json.dumps(data) + '\n\n').encode())

    # ── Helpers ───────────────────────────────────────────────────────────────

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


# ── Threaded server (required for concurrent SSE connections) ─────────────────

class ThreadedServer(http.server.ThreadingHTTPServer):
    daemon_threads = True


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    cfg = load_config()
    port = cfg.get('server_port', 8081)

    monitor = threading.Thread(target=_monitor, daemon=True, name='monitor')
    monitor.start()

    if not cfg.get('pcs'):
        print('No PCs configured yet.  Run: python3 setup.py add')

    server = ThreadedServer(('0.0.0.0', port), Handler)
    print(f'Wake server → http://0.0.0.0:{port}')
    server.serve_forever()
