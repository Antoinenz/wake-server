# wake-server

Minimal Python web server (no dependencies) to **wake and sleep multiple PCs** from any browser — including as an installable home-screen app on iPhone/Android via Tailscale.

## How it works

- `server.py` runs on an always-on machine (Raspberry Pi, NAS, etc.) on the same LAN as your PCs
- It serves a clean web UI on port `8081`
- **Wake** broadcasts a [Wake-on-LAN](https://en.wikipedia.org/wiki/Wake-on-LAN) magic packet on the local subnet
- **Sleep** sends an HTTP command to `sleep-listener.py`, a tiny daemon running on each Windows PC
- Status is polled every 20 s and live-tracked after wake/sleep actions

## Quick start

### 1. Configure your PCs

```bash
# Add a PC interactively (auto-discovers MAC via ARP if PC is online)
python3 setup.py add

# Or scan the network first to find PCs with the daemon already running
python3 setup.py scan

# List, remove
python3 setup.py list
python3 setup.py remove gaming-pc
```

`config.json` is created automatically and looks like:

```json
{
  "server_port": 8081,
  "pcs": [
    {
      "id": "gaming-pc",
      "name": "Gaming PC",
      "ip": "192.168.1.104",
      "mac": "40:8D:5C:1B:71:58",
      "sleep_port": 8765
    }
  ]
}
```

### 2. Run the server

```bash
python3 server.py
```

Or install as a systemd service (auto-starts on boot):

```bash
sudo cp wake-server.service /etc/systemd/system/
sudo systemctl enable --now wake-server
```

### 3. Install the Windows daemon

**One-command via SSH** (requires OpenSSH on the Windows PC):

```bash
python3 setup.py install-daemon Antoine@192.168.1.104
```

This copies `sleep-listener.py` and `install.ps1` to the Windows machine and registers a Task Scheduler task that starts the daemon at every logon.

**Manual** (if SSH isn't set up):
1. Copy the `windows/` folder to the PC
2. Run `install.ps1` once as your user (right-click → Run with PowerShell)

The daemon listens on port 8765 and adds its own Windows Firewall rule automatically.

---

## Accessing over Tailscale (iPhone / remote)

The server is already accessible over Tailscale — just use your machine's Tailscale IP or hostname:

```
http://wake-server:8081       # MagicDNS hostname
http://100.x.x.x:8081        # Tailscale IP
```

> **PWA / installable web app**: For the "Add to Home Screen" install prompt on iOS Safari, the app needs HTTPS. Enable it with one command on the server machine:
>
> ```bash
> tailscale serve 8081
> ```
>
> Then open `https://wake-server.your-tailnet.ts.net` in Safari → Share → Add to Home Screen.

---

## Files

| File | Description |
|------|-------------|
| `server.py` | HTTP server — web UI, REST API, PWA assets |
| `config.json` | PC list and server port (auto-created) |
| `setup.py` | CLI: add / remove / scan / install-daemon |
| `wake-server.service` | systemd unit file |
| `windows/sleep-listener.py` | Daemon on the Windows PC (sleep + ping endpoints) |
| `windows/install.ps1` | One-click installer (Task Scheduler, firewall rule) |
| `windows/start-on-boot.bat` | Lightweight alternative to Task Scheduler |

## API

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/api/pcs` | List all configured PCs |
| `GET`  | `/api/pcs/{id}/status` | `{"awake": true/false}` |
| `POST` | `/api/pcs/{id}/wake` | Send WoL magic packet |
| `POST` | `/api/pcs/{id}/sleep` | Forward sleep command to daemon |
