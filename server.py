#!/usr/bin/env python3
"""wake-server — multi-PC wake/sleep control server"""
import concurrent.futures
import http.server
import json
import queue
import re
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


def slugify(name):
    s = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
    return s or 'pc'


def unique_id(cfg, base):
    ids = {p['id'] for p in cfg.get('pcs', [])}
    if base not in ids:
        return base
    for n in range(2, 99):
        cand = f'{base}-{n}'
        if cand not in ids:
            return cand
    return f'{base}-{int(time.time())}'


# ── Network ───────────────────────────────────────────────────────────────────

def send_wol(mac, ip):
    mac_bytes = bytes.fromhex(mac.replace(':', '').replace('-', ''))
    magic = b'\xff' * 6 + mac_bytes * 16
    parts = ip.split('.')
    bcast = f'{parts[0]}.{parts[1]}.{parts[2]}.255'
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for addr in ('255.255.255.255', bcast):
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


def discover_pcs():
    """Scan local /24 subnet for wake daemons on port 8765."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('8.8.8.8', 80))
            local_ip = s.getsockname()[0]
    except OSError:
        return []

    subnet = '.'.join(local_ip.split('.')[:3])
    ips = [f'{subnet}.{i}' for i in range(1, 255)]

    def probe(ip):
        try:
            with socket.create_connection((ip, 8765), timeout=0.4):
                return ip
        except OSError:
            return None

    found = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as ex:
        for result in ex.map(probe, ips):
            if result:
                found.append(result)

    configured_ips = {p['ip'] for p in load_config().get('pcs', [])}
    out = []
    for ip in sorted(found):
        mac = ''
        try:
            with open('/proc/net/arp') as f:
                for line in f.readlines()[1:]:
                    parts = line.split()
                    if len(parts) >= 4 and parts[0] == ip \
                            and parts[3] not in ('00:00:00:00:00:00', ''):
                        mac = parts[3].upper()
                        break
        except OSError:
            pass

        hostname = ip
        try:
            hostname = socket.gethostbyaddr(ip)[0].split('.')[0].upper()
        except (socket.herror, socket.gaierror):
            pass

        out.append({'ip': ip, 'mac': mac, 'hostname': hostname,
                    'already_added': ip in configured_ips})
    return out


# ── Status cache ──────────────────────────────────────────────────────────────

_cache: dict = {}
_lock = threading.Lock()
CACHE_TTL = 8


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
  <meta name="apple-mobile-web-app-status-bar-style" content="default">
  <meta name="apple-mobile-web-app-title" content="Wake">
  <meta name="theme-color" content="#ffffff">
  <link rel="manifest" href="/manifest.json">
  <link rel="apple-touch-icon" href="/icon-touch.svg">
  <title>Wake</title>
  <style>
    *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
    body{font-family:system-ui,-apple-system,sans-serif;background:#fff;color:#111;
         height:100dvh;display:flex;flex-direction:column;overflow:hidden}

    /* ── banner ── */
    #banner{background:#c0392b;color:#fff;text-align:center;padding:.4rem 1rem;
            font-size:.75rem;font-weight:500;transform:translateY(-100%);
            transition:transform .2s;position:fixed;top:0;left:0;right:0;z-index:200}
    #banner.show{transform:translateY(0)}

    /* ── layout ── */
    .app{display:flex;flex:1;overflow:hidden;
         padding-top:env(safe-area-inset-top,0);
         padding-bottom:env(safe-area-inset-bottom,0)}

    /* ── sidebar ── */
    .sidebar{width:190px;flex-shrink:0;border-right:1px solid #e8e8e8;
             display:flex;flex-direction:column;overflow:hidden}
    .sidebar-head{padding:1.25rem 1rem .75rem;border-bottom:1px solid #f2f2f2}
    .sidebar-head span{font-size:.7rem;font-weight:600;letter-spacing:.1em;
                       text-transform:uppercase;color:#bbb}
    .pc-list{flex:1;overflow-y:auto;padding:.35rem 0}
    .pc-item{display:flex;align-items:center;gap:.6rem;padding:.5rem 1rem;
             cursor:pointer;font-size:.85rem;color:#555;transition:background .1s;
             user-select:none}
    .pc-item:hover{background:#f9f9f9}
    .pc-item.active{background:#f4f4f4;color:#111;font-weight:500}
    .pc-label{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .sidebar-foot{padding:.75rem 1rem;border-top:1px solid #f2f2f2}
    .add-btn{width:100%;padding:.45rem;border:1px solid #e0e0e0;border-radius:7px;
             background:#fff;color:#555;font-size:.8rem;cursor:pointer;transition:background .1s}
    .add-btn:hover{background:#f5f5f5}

    /* ── main ── */
    .main{flex:1;display:flex;align-items:center;justify-content:center;
          padding:2rem;overflow:auto}

    /* ── PC view ── */
    .pc-view{text-align:center}
    .dot{width:8px;height:8px;border-radius:50%;background:#ddd;
         display:inline-block;transition:background .3s;flex-shrink:0}
    .dot.on{background:#111}
    .dot.spin{animation:blink 1s ease-in-out infinite}
    .dot.lg{width:10px;height:10px;margin-bottom:.8rem}
    @keyframes blink{50%{opacity:.2}}
    .pc-title{font-size:1.4rem;color:#111;margin-bottom:.3rem;
              cursor:text;display:inline-block}
    .pc-title:hover{opacity:.7}
    .name-input{font-size:1.4rem;border:none;border-bottom:2px solid #111;
                outline:none;text-align:center;width:100%;padding:.1rem 0}
    .pc-state-row{font-size:.85rem;color:#aaa;margin-bottom:2rem}
    .pc-ip{font-size:.78rem;color:#ccc;margin-top:.15rem}
    .pc-btns{display:flex;gap:.5rem;justify-content:center;margin-bottom:.75rem}
    .pc-btns button{padding:.55rem 1.35rem;border:1px solid #e0e0e0;border-radius:6px;
                    background:#fff;color:#111;font-size:.875rem;cursor:pointer;
                    transition:background .1s}
    .pc-btns button:hover{background:#f5f5f5}
    .pc-btns button:active{background:#ececec}
    .pc-msg{font-size:.75rem;color:#bbb;min-height:1.2em;margin-bottom:1.25rem}
    .pc-msg.err{color:#c0392b}
    .pc-links{display:flex;gap:1.25rem;justify-content:center;font-size:.73rem}
    .pc-links a{color:#ccc;cursor:pointer;text-decoration:none}
    .pc-links a:hover{color:#999}
    .empty-state{text-align:center;color:#ccc;line-height:1.9}
    .empty-state a{color:#bbb;cursor:pointer;text-decoration:underline}

    /* ── overlay / modal ── */
    .overlay{position:fixed;inset:0;background:rgba(0,0,0,.2);
             display:flex;align-items:center;justify-content:center;
             z-index:100;padding:1rem}
    .overlay.hidden{display:none}
    .modal{background:#fff;border-radius:12px;width:100%;max-width:460px;
           max-height:88dvh;overflow-y:auto;
           box-shadow:0 16px 48px rgba(0,0,0,.14)}
    .modal-hdr{display:flex;align-items:center;justify-content:space-between;
               padding:1.1rem 1.25rem .7rem;border-bottom:1px solid #f0f0f0}
    .modal-hdr h3{font-size:.95rem;font-weight:600}
    .close-btn{background:none;border:none;font-size:1.3rem;color:#bbb;
               cursor:pointer;line-height:1;padding:.2rem .4rem}
    .close-btn:hover{color:#888}
    .modal-body{padding:1.1rem 1.25rem 1.4rem}

    /* ── install section ── */
    .install-box{background:#f9f9f9;border:1px solid #efefef;border-radius:8px;
                 padding:.75rem .9rem;margin-bottom:1.1rem}
    .section-label{font-size:.72rem;color:#aaa;font-weight:500;
                   text-transform:uppercase;letter-spacing:.07em;margin-bottom:.45rem}
    .cmd-row{display:flex;align-items:center;gap:.5rem;background:#fff;
             border:1px solid #e8e8e8;border-radius:6px;padding:.4rem .65rem}
    .cmd-code{font-family:ui-monospace,monospace;font-size:.65rem;color:#444;
              flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .copy-btn{flex-shrink:0;padding:.2rem .55rem;border:1px solid #e0e0e0;
              border-radius:4px;background:#fff;font-size:.68rem;cursor:pointer;
              white-space:nowrap}
    .copy-btn:hover{background:#f5f5f5}

    /* ── discovery table ── */
    .discover-table{width:100%;border-collapse:collapse;font-size:.8rem;margin:.5rem 0 .75rem}
    .discover-table th{text-align:left;font-size:.67rem;font-weight:500;
                       text-transform:uppercase;letter-spacing:.05em;color:#bbb;
                       padding:.2rem .4rem .5rem 0;border-bottom:1px solid #f0f0f0}
    .discover-table td{padding:.5rem .4rem .5rem 0;border-bottom:1px solid #f7f7f7;
                       vertical-align:middle}
    .mono{font-family:ui-monospace,monospace;font-size:.72rem;color:#888}
    .already td{opacity:.45}
    .add-row-btn{padding:.22rem .6rem;border:1px solid #e0e0e0;border-radius:5px;
                 background:#fff;font-size:.73rem;cursor:pointer}
    .add-row-btn:hover{background:#f5f5f5}
    .added-badge{font-size:.7rem;color:#ccc}
    .no-results{text-align:center;color:#bbb;padding:1rem 0;font-size:.8rem}
    .modal-foot{display:flex;justify-content:space-between;align-items:center;
                margin-top:.85rem}
    .link{font-size:.75rem;color:#bbb;cursor:pointer;text-decoration:none}
    .link:hover{color:#888}

    /* ── form ── */
    .form-group{margin-bottom:.8rem}
    .form-label{display:block;font-size:.73rem;color:#888;margin-bottom:.3rem}
    .text-input{width:100%;padding:.5rem .65rem;border:1px solid #e0e0e0;
                border-radius:7px;font-size:.875rem;outline:none;
                transition:border-color .15s}
    .text-input:focus{border-color:#aaa}
    .form-btns{display:flex;gap:.5rem;margin-top:1rem}
    .btn-primary{flex:1;padding:.5rem;border:1px solid #111;border-radius:7px;
                 background:#111;color:#fff;font-size:.85rem;cursor:pointer}
    .btn-primary:hover{background:#333}
    .btn-secondary{padding:.5rem 1rem;border:1px solid #e0e0e0;border-radius:7px;
                   background:#fff;color:#555;font-size:.85rem;cursor:pointer}
    .btn-secondary:hover{background:#f5f5f5}
    .pick-meta{font-size:.75rem;color:#bbb;margin:.45rem 0 .75rem}
    .err-note{font-size:.75rem;color:#c0392b;margin-top:.5rem}

    /* ── spinner ── */
    .spinner{width:22px;height:22px;border:2px solid #eee;border-top-color:#999;
             border-radius:50%;animation:spin .65s linear infinite;margin:1.5rem auto}
    @keyframes spin{to{transform:rotate(360deg)}}

    /* ── mobile ── */
    @media(max-width:580px){
      body{overflow:auto}
      .app{flex-direction:column;height:auto;overflow:visible}
      .sidebar{width:100%;border-right:none;border-bottom:1px solid #e8e8e8;flex-direction:column}
      .sidebar-head{padding:.75rem 1rem .5rem}
      .pc-list{display:flex;flex-direction:row;overflow-x:auto;padding:.35rem .5rem;
               gap:.25rem;flex:none}
      .pc-item{flex-shrink:0;border-radius:6px;padding:.35rem .75rem;white-space:nowrap}
      .sidebar-foot{display:flex;justify-content:flex-end;padding:.5rem .75rem;border-top:none}
      .add-btn{width:auto;padding:.35rem .85rem}
      .main{padding:2rem 1rem;min-height:60dvh;overflow:visible}
    }
  </style>
</head>
<body>
  <div id="banner"></div>
  <div class="app">
    <aside class="sidebar">
      <div class="sidebar-head"><span>Wake</span></div>
      <ul class="pc-list" id="pcList"></ul>
      <div class="sidebar-foot">
        <button class="add-btn" onclick="openModal()">+ Add PC</button>
      </div>
    </aside>
    <main class="main" id="main">
      <div class="empty-state" style="color:#ddd">Connecting…</div>
    </main>
  </div>

  <!-- Add PC modal -->
  <div class="overlay hidden" id="overlay" onclick="overlayClick(event)">
    <div class="modal">
      <div class="modal-hdr">
        <h3 id="modalTitle">Add PC</h3>
        <button class="close-btn" onclick="closeModal()">&#x2715;</button>
      </div>
      <div class="modal-body" id="modalBody"></div>
    </div>
  </div>

  <script>
  'use strict';
  window.$ = id => document.getElementById(id);

  let pcs    = [];
  let status = {};   // {id: {awake, ts}}
  let sel    = null; // selected pc id
  let polls  = {};   // id -> timer

  const INSTALL_CMD = 'powershell -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/Antoinenz/wake-server/main/windows/install.ps1 | iex"';

  const esc = s => String(s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');

  // ── SSE ─────────────────────────────────────────────────────────────────────

  function connect() {
    const es = new EventSource('/events');
    es.onopen    = () => hideErr();
    es.onerror   = () => showErr('Disconnected — reconnecting…');
    es.onmessage = e => {
      let d; try { d = JSON.parse(e.data); } catch { return; }
      if (d.type === 'hello') {
        pcs = d.pcs;
        if (!sel && pcs.length) sel = pcs[0].id;
        renderSidebar(); renderMain();
      } else if (d.type === 'status') {
        status[d.id] = { awake: d.awake, ts: Date.now() };
        applyStatus(d.id, d.awake);
      } else if (d.type === 'reload') {
        fetch('/api/pcs').then(r => r.json()).then(list => {
          pcs = list;
          if (!pcs.find(p => p.id === sel)) sel = pcs.length ? pcs[0].id : null;
          renderSidebar(); renderMain();
        }).catch(() => {});
      }
    };
  }

  // ── Status ───────────────────────────────────────────────────────────────────

  function applyStatus(id, awake) {
    const sd = $('sd-' + id);
    if (sd) sd.className = 'dot' + (awake ? ' on' : '');
    if (id === sel) {
      const md = $('md'), ms = $('ms');
      if (md) md.className = 'dot lg' + (awake ? ' on' : '');
      if (ms) ms.textContent = awake ? 'online' : 'offline';
    }
    if (awake && polls[id]) {
      clearTimeout(polls[id]); delete polls[id];
      if (id === sel) setMsg('✓ Online', false);
    }
  }

  // ── Sidebar ──────────────────────────────────────────────────────────────────

  function renderSidebar() {
    $('pcList').innerHTML = pcs.map(p => {
      const st  = status[p.id];
      const cls = 'dot' + (st && st.awake ? ' on' : '');
      return '<li class="pc-item' + (p.id === sel ? ' active' : '') + '"'
           + ' onclick="selectPc(\'' + p.id + '\')">'
           + '<span class="' + cls + '" id="sd-' + p.id + '"></span>'
           + '<span class="pc-label">' + esc(p.name) + '</span></li>';
    }).join('');
  }

  function selectPc(id) {
    sel = id; renderSidebar(); renderMain();
  }

  // ── Main view ─────────────────────────────────────────────────────────────────

  function renderMain() {
    const main = $('main');
    if (!pcs.length) {
      main.innerHTML = '<div class="empty-state">No PCs configured.<br>'
        + '<a onclick="openModal()">Add one</a></div>';
      return;
    }
    const pc  = pcs.find(p => p.id === sel) || pcs[0];
    if (!pc) return;
    sel = pc.id;
    const st     = status[pc.id];
    const awake  = st ? st.awake : null;
    const dotCls = 'dot lg' + (awake === true ? ' on' : awake === false ? '' : ' spin');
    const stTxt  = awake === true ? 'online' : awake === false ? 'offline' : '…';

    main.innerHTML =
      '<div class="pc-view">'
    + '<span class="' + dotCls + '" id="md"></span>'
    + '<div><span class="pc-title" id="pcName" onclick="startRename(\'' + pc.id + '\', this)">'
    + esc(pc.name) + '</span></div>'
    + '<div class="pc-state-row"><span id="ms">' + stTxt + '</span></div>'
    + '<div class="pc-ip">' + esc(pc.ip) + '</div>'
    + '<div style="margin-top:2rem">'
    + '<div class="pc-btns">'
    + '<button onclick="doWake(\'' + pc.id + '\')">Wake</button>'
    + '<button onclick="doSleep(\'' + pc.id + '\')">Sleep</button>'
    + '</div>'
    + '<p class="pc-msg" id="pmsg"></p>'
    + '<div class="pc-links">'
    + '<a onclick="startRename(\'' + pc.id + '\', $(\'pcName\'))">Rename</a>'
    + '<a onclick="removePc(\'' + pc.id + '\')">Remove</a>'
    + '</div></div></div>';
  }

  // ── Wake ──────────────────────────────────────────────────────────────────────

  async function doWake(id) {
    setMsg('Sending magic packet…', false);
    try {
      const r = await fetch('/api/pcs/' + id + '/wake', { method: 'POST' });
      if (!r.ok) throw new Error('Server error ' + r.status);
      setMsg('Waiting for PC to boot…', false);
      startPoll(id, 0);
    } catch (e) { setMsg('✗ ' + fmtErr(e), true); }
  }

  function startPoll(id, n) {
    if (n > 40) { setMsg('✗ No response after 2 min — check the PC', true); return; }
    polls[id] = setTimeout(async () => {
      try {
        const d = await (await fetch('/api/pcs/' + id + '/status')).json();
        if (d.awake) { applyStatus(id, true); return; }
      } catch {}
      startPoll(id, n + 1);
    }, 3000);
  }

  // ── Sleep ─────────────────────────────────────────────────────────────────────

  async function doSleep(id) {
    setMsg('Sending sleep command…', false);
    try {
      const r = await fetch('/api/pcs/' + id + '/sleep', { method: 'POST' });
      if (r.ok) setMsg('Sleep command sent', false);
      else if (r.status === 502) setMsg('✗ Daemon not responding — is sleep-listener.py running?', true);
      else setMsg('✗ Server error ' + r.status, true);
    } catch (e) { setMsg('✗ ' + fmtErr(e), true); }
  }

  // ── Rename ────────────────────────────────────────────────────────────────────

  function startRename(id, el) {
    if (!el || el.tagName === 'INPUT') return;
    const orig = el.textContent;
    const inp  = document.createElement('input');
    inp.value     = orig;
    inp.className = 'name-input';
    el.replaceWith(inp);
    inp.focus(); inp.select();
    const finish = async () => {
      const v = inp.value.trim() || orig;
      inp.replaceWith(el);
      el.textContent = v;
      if (v !== orig) {
        fetch('/api/pcs/' + id, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: v }),
        }).catch(() => {});
      }
    };
    inp.onblur   = finish;
    inp.onkeydown = e => {
      if (e.key === 'Enter')  { inp.blur(); }
      if (e.key === 'Escape') { inp.value = orig; inp.blur(); }
    };
  }

  // ── Remove ────────────────────────────────────────────────────────────────────

  async function removePc(id) {
    const pc = pcs.find(p => p.id === id);
    if (!pc || !confirm('Remove "' + pc.name + '"?')) return;
    try {
      await fetch('/api/pcs/' + id, { method: 'DELETE' });
    } catch (e) { setMsg('✗ ' + fmtErr(e), true); }
  }

  // ── Add PC modal ──────────────────────────────────────────────────────────────

  function openModal() {
    $('modalTitle').textContent = 'Add PC';
    $('overlay').classList.remove('hidden');
    showDiscover();
  }

  function closeModal() { $('overlay').classList.add('hidden'); }
  function overlayClick(e) { if (e.target === $('overlay')) closeModal(); }

  async function showDiscover() {
    $('modalTitle').textContent = 'Add PC';
    $('modalBody').innerHTML =
      installBox()
    + '<p class="section-label" style="margin-bottom:.6rem">2. Scanning network…</p>'
    + '<div class="spinner"></div>'
    + '<div class="modal-foot"><span></span>'
    + '<a class="link" onclick="showManual()">→ Manual setup</a></div>';

    try {
      const r = await fetch('/api/discover');
      if (!r.ok) throw new Error();
      showDiscoverResults(await r.json());
    } catch {
      $('modalBody').innerHTML =
        installBox()
      + '<p class="section-label">2. Select a PC</p>'
      + '<p class="err-note">Scan failed — check server logs or use manual setup.</p>'
      + '<div class="modal-foot"><a class="link" onclick="showDiscover()">↺ Retry</a>'
      + '<a class="link" onclick="showManual()">→ Manual setup</a></div>';
    }
  }

  function showDiscoverResults(found) {
    const rows = found.length
      ? found.map(pc => {
          const cls = pc.already_added ? ' class="already"' : '';
          const action = pc.already_added
            ? '<span class="added-badge">Added</span>'
            : '<button class="add-row-btn" onclick="pickPc(\''
              + esc(pc.ip) + '\',\'' + esc(pc.mac) + '\',\'' + esc(pc.hostname) + '\')">Add</button>';
          return '<tr' + cls + '><td>' + esc(pc.hostname) + '</td>'
               + '<td class="mono">' + esc(pc.ip) + '</td>'
               + '<td class="mono">' + esc(pc.mac) + '</td>'
               + '<td>' + action + '</td></tr>';
        }).join('')
      : '<tr><td colspan="4" class="no-results">No PCs found with daemon running.</td></tr>';

    $('modalBody').innerHTML =
      installBox()
    + '<p class="section-label">2. Select a PC:</p>'
    + '<table class="discover-table"><thead><tr>'
    + '<th>Hostname</th><th>IP</th><th>MAC</th><th></th></tr></thead>'
    + '<tbody>' + rows + '</tbody></table>'
    + '<div class="modal-foot">'
    + '<a class="link" onclick="showDiscover()">↺ Rescan</a>'
    + '<a class="link" onclick="showManual()">→ Manual setup</a></div>';
  }

  function pickPc(ip, mac, hostname) {
    $('modalTitle').textContent = 'Name this PC';
    $('modalBody').innerHTML =
      '<p class="form-label">Display name</p>'
    + '<input class="text-input" id="pickName" value="' + esc(hostname) + '">'
    + '<p class="pick-meta">' + esc(ip) + ' · ' + esc(mac) + '</p>'
    + '<div class="form-btns">'
    + '<button class="btn-secondary" onclick="showDiscover()">← Back</button>'
    + '<button class="btn-primary" onclick="submitPick(\'' + esc(ip) + '\',\'' + esc(mac) + '\')">Add PC</button>'
    + '</div>';
    const inp = $('pickName');
    inp.focus(); inp.select();
    inp.onkeydown = e => { if (e.key === 'Enter') submitPick(ip, mac); };
  }

  async function submitPick(ip, mac) {
    const name = $('pickName').value.trim();
    if (!name) { $('pickName').focus(); return; }
    await addPc(name, ip, mac, 8765);
  }

  function showManual() {
    $('modalTitle').textContent = 'Manual setup';
    $('modalBody').innerHTML =
      '<div class="form-group"><label class="form-label">Display name'
    + '<input class="text-input" id="fName" placeholder="Gaming PC"></label></div>'
    + '<div class="form-group"><label class="form-label">IP address'
    + '<input class="text-input" id="fIp" placeholder="192.168.1.x"></label></div>'
    + '<div class="form-group"><label class="form-label">MAC address'
    + '<input class="text-input" id="fMac" placeholder="AA:BB:CC:DD:EE:FF"></label></div>'
    + '<div class="form-group"><label class="form-label">Sleep listener port'
    + '<input class="text-input" id="fPort" value="8765"></label></div>'
    + '<p class="err-note" id="manualErr" style="display:none"></p>'
    + '<div class="form-btns">'
    + '<button class="btn-secondary" onclick="showDiscover()">← Back</button>'
    + '<button class="btn-primary" onclick="submitManual()">Add PC</button></div>';
    $('fName').focus();
  }

  async function submitManual() {
    const name = $('fName').value.trim(), ip   = $('fIp').value.trim();
    const mac  = $('fMac').value.trim(), port = parseInt($('fPort').value) || 8765;
    const errEl = $('manualErr');
    if (!name || !ip || !mac) {
      errEl.textContent = 'Name, IP and MAC are required.';
      errEl.style.display = ''; return;
    }
    await addPc(name, ip, mac, port, errEl);
  }

  async function addPc(name, ip, mac, port, errEl) {
    try {
      const r = await fetch('/api/pcs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, ip, mac, sleep_port: port }),
      });
      if (!r.ok) throw new Error('Server error ' + r.status);
      const d = await r.json();
      sel = d.id;
      closeModal();
    } catch (e) {
      const msg = '✗ ' + fmtErr(e);
      if (errEl) { errEl.textContent = msg; errEl.style.display = ''; }
      else alert(msg);
    }
  }

  function installBox() {
    return '<div class="install-box">'
    + '<p class="section-label">1. Install the daemon on the Windows PC:</p>'
    + '<div class="cmd-row">'
    + '<code class="cmd-code" id="cmdCode">' + esc(INSTALL_CMD) + '</code>'
    + '<button class="copy-btn" id="copyBtn" onclick="copyCmd()">Copy</button>'
    + '</div></div>';
  }

  function copyCmd() {
    const done = () => {
      const b = $('copyBtn'); if (b) { b.textContent = 'Copied!'; setTimeout(() => { if ($('copyBtn')) $('copyBtn').textContent = 'Copy'; }, 2000); }
    };
    navigator.clipboard ? navigator.clipboard.writeText(INSTALL_CMD).then(done).catch(fallback) : fallback();
    function fallback() {
      const t = document.createElement('textarea');
      t.value = INSTALL_CMD; document.body.appendChild(t); t.select();
      try { document.execCommand('copy'); } catch {}
      document.body.removeChild(t); done();
    }
  }

  // ── Helpers ───────────────────────────────────────────────────────────────────

  function setMsg(t, isErr) {
    const e = $('pmsg'); if (!e) return;
    e.textContent = t; e.className = 'pc-msg' + (isErr ? ' err' : '');
  }
  function showErr(t) { const b = $('banner'); b.textContent = t; b.classList.add('show'); }
  function hideErr()  { $('banner').classList.remove('show'); }
  function fmtErr(e) {
    if (!e) return 'Unknown error';
    if (e.name === 'TypeError' || (e.message && e.message.includes('fetch'))) return 'Server unreachable';
    return e.message || 'Unknown error';
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
    'background_color': '#ffffff',
    'theme_color': '#ffffff',
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
const V='wake-v2';
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
            ct, fn = self._STATIC[self.path]
            return self._send(200, ct, fn())

        if self.path == '/events':
            return self._handle_sse()

        cfg = load_config()

        if self.path == '/api/pcs':
            return self._json([{'id': p['id'], 'name': p['name'], 'ip': p['ip']}
                                for p in cfg['pcs']])

        if self.path == '/api/discover':
            return self._json(discover_pcs())

        pc_id = extract_id(self.path, '/api/pcs/', '/status')
        if pc_id is not None:
            pc = find_pc(cfg, pc_id)
            return self._json({'awake': cached_ping(pc)}) if pc \
                else self._send(404, 'text/plain', b'Not found')

        self._send(404, 'text/plain', b'Not found')

    def do_POST(self):
        cfg = load_config()

        if self.path == '/api/pcs':
            body = self._body()
            name = body.get('name', '').strip()
            ip   = body.get('ip',   '').strip()
            mac  = body.get('mac',  '').strip().upper().replace('-', ':')
            port = int(body.get('sleep_port', 8765))
            if not (name and ip and mac):
                return self._send(400, 'text/plain', b'Missing required fields')
            pc_id = unique_id(cfg, slugify(name))
            cfg['pcs'].append({'id': pc_id, 'name': name, 'ip': ip,
                               'mac': mac, 'sleep_port': port})
            save_config(cfg)
            _broadcast({'type': 'reload'})
            return self._json({'ok': True, 'id': pc_id})

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
            return self._send(200 if ok else 502, 'application/json',
                              json.dumps({'ok': ok}).encode())

        self._send(404, 'text/plain', b'Not found')

    def do_PATCH(self):
        pc_id = extract_id(self.path, '/api/pcs/', '')
        if pc_id is None:
            return self._send(404, 'text/plain', b'Not found')
        body = self._body()
        cfg  = load_config()
        pc   = find_pc(cfg, pc_id)
        if not pc:
            return self._send(404, 'text/plain', b'Not found')
        if 'name' in body:
            pc['name'] = body['name'].strip() or pc['name']
        save_config(cfg)
        _broadcast({'type': 'reload'})
        self._json({'ok': True})

    def do_DELETE(self):
        pc_id = extract_id(self.path, '/api/pcs/', '')
        if pc_id is None:
            return self._send(404, 'text/plain', b'Not found')
        cfg    = load_config()
        before = len(cfg['pcs'])
        cfg['pcs'] = [p for p in cfg['pcs'] if p['id'] != pc_id]
        if len(cfg['pcs']) == before:
            return self._send(404, 'text/plain', b'Not found')
        bust(pc_id)
        save_config(cfg)
        _broadcast({'type': 'reload'})
        self._json({'ok': True})

    # ── SSE ───────────────────────────────────────────────────────────────────

    def _handle_sse(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.send_header('X-Accel-Buffering', 'no')
        self.end_headers()

        client_q: queue.Queue = queue.Queue(maxsize=50)
        cfg      = load_config()
        pcs_list = cfg.get('pcs', [])
        hello    = {'type': 'hello',
                    'pcs': [{'id': p['id'], 'name': p['name'], 'ip': p['ip']}
                             for p in pcs_list]}
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
                    frame = b': ping\n\n'
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

    def _sse_write(self, data):
        self.wfile.write(('data: ' + json.dumps(data) + '\n\n').encode())

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _body(self):
        n = int(self.headers.get('Content-Length', 0))
        try:
            return json.loads(self.rfile.read(n)) if n else {}
        except (json.JSONDecodeError, ValueError):
            return {}

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


# ── Threaded server ───────────────────────────────────────────────────────────

class ThreadedServer(http.server.ThreadingHTTPServer):
    daemon_threads = True


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    cfg  = load_config()
    port = cfg.get('server_port', 8081)

    threading.Thread(target=_monitor, daemon=True, name='monitor').start()

    server = ThreadedServer(('0.0.0.0', port), Handler)
    print(f'Wake server → http://0.0.0.0:{port}')
    server.serve_forever()
