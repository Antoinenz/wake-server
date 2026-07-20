"""
Wake daemon for Windows — sleep listener
Listens on port 8765 for HTTP commands from wake-server.

Endpoints:
  GET  /ping   → 200 "awake"  (used to confirm daemon is running)
  POST /sleep  → 200 "OK"     (puts the PC to sleep)

Setup (manual):
  1. Install Python from https://python.org (check "Add to PATH")
  2. Place this file somewhere permanent, e.g. C:\\wake-daemon\\sleep-listener.py
  3. Place start-on-boot.bat in the same folder and add a shortcut to:
       shell:startup
  — OR — use install.ps1 for automatic setup via Task Scheduler.
"""
import http.server
import logging
import os
import subprocess
import threading

PORT     = 8765
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wake-daemon.log')

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s  %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)


def sleep_pc():
    logging.info('Executing sleep command')
    subprocess.run(
        ['powershell', '-Command',
         'Add-Type -AssemblyName System.Windows.Forms; '
         '[System.Windows.Forms.Application]::SetSuspendState("Suspend",$false,$false)'],
        check=False,
    )


def add_firewall_rule():
    subprocess.run(
        ['netsh', 'advfirewall', 'firewall', 'add', 'rule',
         'name=WakeDaemon', 'dir=in', 'action=allow',
         'protocol=TCP', f'localport={PORT}'],
        capture_output=True,
    )


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/ping':
            self._respond(200, b'awake')
        else:
            self._respond(404, b'Not found')

    def do_POST(self):
        if self.path == '/sleep':
            logging.info(f'Sleep request from {self.client_address[0]}')
            self._respond(200, b'OK')
            threading.Timer(0.5, sleep_pc).start()
        else:
            self._respond(404, b'Not found')

    def _respond(self, code, body):
        self.send_response(code)
        self.send_header('Content-Length', len(body))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass   # suppress console noise; everything goes to the log file


if __name__ == '__main__':
    add_firewall_rule()
    server = http.server.HTTPServer(('0.0.0.0', PORT), Handler)
    logging.info(f'Wake daemon started on port {PORT}')
    print(f'Wake daemon listening on port {PORT}  (log: {LOG_FILE})')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logging.info('Wake daemon stopped')
