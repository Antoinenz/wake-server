#!/usr/bin/env python3
"""
wake-server setup assistant

Commands:
  python3 setup.py list
  python3 setup.py add
  python3 setup.py remove <id-or-name>
  python3 setup.py scan [--port 8765]
  python3 setup.py install-daemon <user@host>
"""
import argparse
import concurrent.futures
import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / 'config.json'
DEFAULT_CONFIG = {'server_port': 8081, 'pcs': []}

# ── Config helpers ─────────────────────────────────────────────────────────────

def load():
    if not CONFIG_PATH.exists():
        return dict(DEFAULT_CONFIG)
    with open(CONFIG_PATH) as f:
        return json.load(f)


def save(cfg):
    with open(CONFIG_PATH, 'w') as f:
        json.dump(cfg, f, indent=2)
    print(f'Saved → {CONFIG_PATH}')


def slugify(name):
    s = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
    return s or 'pc'


def unique_id(cfg, base):
    ids = {p['id'] for p in cfg['pcs']}
    if base not in ids:
        return base
    for n in range(2, 99):
        cand = f'{base}-{n}'
        if cand not in ids:
            return cand
    return f'{base}-{os.getpid()}'


# ── Commands ───────────────────────────────────────────────────────────────────

def cmd_list(args):
    cfg = load()
    pcs = cfg.get('pcs', [])
    if not pcs:
        print('No PCs configured.  Run: python3 setup.py add')
        return
    fmt = '{:<20} {:<20} {:<18} {:<19} {}'
    print(fmt.format('ID', 'Name', 'IP', 'MAC', 'Sleep port'))
    print('-' * 82)
    for p in pcs:
        print(fmt.format(p['id'], p['name'], p['ip'], p['mac'], p.get('sleep_port', 8765)))


def cmd_add(args):
    cfg = load()
    print('Add a PC  (Ctrl-C to cancel)\n')

    name = ask('Display name', 'Gaming PC')
    ip   = ask('IP address')
    mac  = ask('MAC address', discover_mac(ip))
    port = ask('Sleep listener port', '8765')

    mac = mac.upper().replace('-', ':')
    pc_id = unique_id(cfg, slugify(name))

    cfg['pcs'].append({
        'id':         pc_id,
        'name':       name,
        'ip':         ip,
        'mac':        mac,
        'sleep_port': int(port),
    })
    save(cfg)
    print(f'\n✓ Added "{name}"  (id: {pc_id})')


def cmd_remove(args):
    cfg = load()
    target = args.id.lower()
    before = len(cfg['pcs'])
    cfg['pcs'] = [p for p in cfg['pcs']
                  if p['id'] != target and p['name'].lower() != target]
    if len(cfg['pcs']) == before:
        print(f'No PC found with id or name "{args.id}"')
        sys.exit(1)
    save(cfg)
    print(f'✓ Removed "{args.id}"')


def cmd_scan(args):
    port = args.port
    print(f'Scanning local subnet for wake daemons on port {port}…\n')

    # Determine local IP via a dummy UDP connect (no packets sent)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(('8.8.8.8', 80))
            local_ip = s.getsockname()[0]
    except OSError:
        print('Could not determine local IP address.')
        sys.exit(1)

    subnet = '.'.join(local_ip.split('.')[:3])
    ips    = [f'{subnet}.{i}' for i in range(1, 255)]

    def probe(ip):
        try:
            with socket.create_connection((ip, port), timeout=0.4):
                return ip
        except OSError:
            return None

    found = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=64) as ex:
        for result in ex.map(probe, ips):
            if result:
                found.append(result)
    found.sort()

    if not found:
        print(f'No wake daemons found on {subnet}.0/24 port {port}.')
        print('Make sure sleep-listener.py is running on your Windows PC.')
        return

    print(f'Found {len(found)} wake daemon(s):\n')
    for ip in found:
        print(f'  {ip}')
    print('\nRun "python3 setup.py add" and enter one of the IPs above.')


def cmd_install(args):
    host      = args.host
    win_dir   = Path(__file__).parent / 'windows'
    remote    = '~/wake-daemon'

    print(f'Installing wake daemon on {host} …')
    print('Requires OpenSSH access to the Windows machine.\n')

    # Create remote directory
    _ssh(host, f'powershell -NoProfile -Command "New-Item -Force -ItemType Directory {remote} | Out-Null"')

    # Copy files
    for fname in ('sleep-listener.py', 'install.ps1'):
        local = win_dir / fname
        if not local.exists():
            print(f'Missing local file: {local}')
            sys.exit(1)
        print(f'  Copying {fname} …')
        r = subprocess.run(['scp', str(local), f'{host}:{remote}/{fname}'])
        if r.returncode != 0:
            print(f'scp failed for {fname}')
            sys.exit(1)

    # Run the installer
    print('\nRunning installer on Windows …\n')
    r = subprocess.run([
        'ssh', host,
        f'powershell -NoProfile -ExecutionPolicy Bypass -File {remote}/install.ps1',
    ])
    if r.returncode == 0:
        print('\n✓ Wake daemon installed and started!')
        print('  It will auto-start at every login via Task Scheduler.')
    else:
        print('\n✗ Installer returned an error — see output above.')
        sys.exit(1)


# ── Helpers ────────────────────────────────────────────────────────────────────

def ask(label, default=''):
    suffix = f' [{default}]' if default else ''
    while True:
        val = input(f'{label}{suffix}: ').strip()
        if val:
            return val
        if default:
            return default
        print('  (required)')


def discover_mac(ip):
    """Ping then read MAC from the ARP table (Linux /proc/net/arp)."""
    try:
        subprocess.run(['ping', '-c', '1', '-W', '1', ip], capture_output=True)
        with open('/proc/net/arp') as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) >= 4 and parts[0] == ip and parts[3] != '00:00:00:00:00:00':
                    return parts[3].upper()
    except OSError:
        pass
    return ''


def _ssh(host, cmd):
    subprocess.run(['ssh', host, cmd], capture_output=True)


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='wake-server setup assistant',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest='cmd', required=True)

    sub.add_parser('list', help='List configured PCs')
    sub.add_parser('add',  help='Add a PC interactively')

    rm = sub.add_parser('remove', help='Remove a PC by id or name')
    rm.add_argument('id', help='PC id or display name')

    sc = sub.add_parser('scan', help='Scan local network for wake daemons')
    sc.add_argument('--port', type=int, default=8765)

    inst = sub.add_parser('install-daemon', help='Install daemon on a Windows PC via SSH')
    inst.add_argument('host', help='SSH target, e.g. Antoine@192.168.1.104')

    args = parser.parse_args()
    {
        'list':            cmd_list,
        'add':             cmd_add,
        'remove':          cmd_remove,
        'scan':            cmd_scan,
        'install-daemon':  cmd_install,
    }[args.cmd](args)


if __name__ == '__main__':
    main()
