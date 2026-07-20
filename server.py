#!/usr/bin/env python3
import http.server
import json
import socket
import subprocess
import urllib.request
import urllib.error
import threading

PC_IP  = '192.168.1.104'
PC_MAC = '40:8d:5c:1b:71:58'
PC_SLEEP_PORT = 8765
SERVER_PORT   = 8081

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PC</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: system-ui, sans-serif;
      background: #fff;
      color: #111;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
    }
    .wrap { text-align: center; }
    .dot {
      width: 8px; height: 8px;
      border-radius: 50%;
      background: #ddd;
      display: inline-block;
      margin-bottom: .75rem;
      transition: background .3s;
    }
    .dot.online  { background: #111; }
    .dot.offline { background: #ddd; }
    #statusText { display: block; font-size: 1.5rem; color: #aaa; margin-bottom: 2rem; }
    .buttons { display: flex; gap: .5rem; justify-content: center; }
    button {
      padding: .55rem 1.25rem;
      border: 1px solid #e0e0e0;
      border-radius: 6px;
      background: #fff;
      color: #111;
      font-size: .875rem;
      cursor: pointer;
      transition: background .15s;
    }
    button:hover  { background: #f5f5f5; }
    button:active { background: #ececec; }
    #msg { margin-top: 1rem; font-size: .75rem; color: #bbb; min-height: 1em; }
  </style>
</head>
<body>
  <div class="wrap">
    <span class="dot" id="dot"></span>
    <span id="statusText">-</span>
    <div class="buttons">
      <button onclick="wake()">Wake</button>
      <button onclick="sendSleep()">Sleep</button>
    </div>
    <p id="msg"></p>
  </div>

  <script>
    async function checkStatus() {
      try {
        const d = await (await fetch('/status')).json();
        document.getElementById('dot').className = 'dot ' + (d.awake ? 'online' : 'offline');
        document.getElementById('statusText').textContent = d.awake ? 'online' : 'offline';
      } catch {
        document.getElementById('dot').className = 'dot';
        document.getElementById('statusText').textContent = '-';
      }
    }

    async function wake() {
      await fetch('/wake', { method: 'POST' });
      setMsg('magic packet sent');
    }

    async function sendSleep() {
      try {
        const r = await fetch('/sleep', { method: 'POST' });
        setMsg(r.ok ? 'sleep command sent' : 'listener did not respond');
      } catch { setMsg('failed'); }
    }

    function setMsg(t) { document.getElementById('msg').textContent = t; }

    checkStatus();
    setInterval(checkStatus, 5000);
  </script>
</body>
</html>
"""


def send_wol():
    mac_bytes = bytes.fromhex(PC_MAC.replace(':', ''))
    magic = b'\xff' * 6 + mac_bytes * 16
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.sendto(magic, ('192.168.1.255', 9))
        s.sendto(magic, ('255.255.255.255', 9))


def is_awake():
    result = subprocess.run(
        ['ping', '-c', '1', '-W', '1', PC_IP],
        capture_output=True
    )
    return result.returncode == 0


def send_sleep():
    try:
        req = urllib.request.Request(
            f'http://{PC_IP}:{PC_SLEEP_PORT}/sleep', method='POST'
        )
        urllib.request.urlopen(req, timeout=10)
        return True
    except urllib.error.URLError:
        return False


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/':
            body = HTML.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', len(body))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == '/status':
            body = json.dumps({'awake': is_awake()}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', len(body))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == '/wake':
            send_wol()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'OK')
        elif self.path == '/sleep':
            ok = send_sleep()
            self.send_response(200 if ok else 502)
            self.end_headers()
            self.wfile.write(b'OK' if ok else b'FAILED')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass


if __name__ == '__main__':
    server = http.server.HTTPServer(('0.0.0.0', SERVER_PORT), Handler)
    print(f'PC control server running → http://0.0.0.0:{SERVER_PORT}')
    server.serve_forever()
