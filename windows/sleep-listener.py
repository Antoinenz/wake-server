"""
Sleep listener for PC Control.
Runs on the Windows PC and puts it to sleep when requested.

Setup:
  1. Install Python from python.org (tick "Add to PATH")
  2. Copy this file anywhere (e.g. C:\pc-control\sleep-listener.py)
  3. Copy start-on-boot.bat to the same folder
  4. Press Win+R, type shell:startup, and drop a shortcut to start-on-boot.bat there
  5. Make sure port 8765 is allowed in Windows Firewall (the script tries to do this automatically)
"""

import http.server
import subprocess
import threading
import sys
import os


PORT = 8765


def sleep_pc():
    subprocess.run(
        ['powershell', '-Command',
         'Add-Type -AssemblyName System.Windows.Forms; '
         '[System.Windows.Forms.Application]::SetSuspendState("Suspend", $false, $false)'],
        check=False
    )


class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == '/sleep':
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'OK')
            # Sleep after the response is flushed
            threading.Timer(0.5, sleep_pc).start()
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self):
        if self.path == '/ping':
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'awake')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass


def add_firewall_rule():
    subprocess.run(
        ['netsh', 'advfirewall', 'firewall', 'add', 'rule',
         'name=PC-Control-Sleep-Listener',
         'dir=in', 'action=allow', 'protocol=TCP',
         f'localport={PORT}'],
        capture_output=True
    )


if __name__ == '__main__':
    add_firewall_rule()
    server = http.server.HTTPServer(('0.0.0.0', PORT), Handler)
    print(f'Sleep listener running on port {PORT}')
    server.serve_forever()
