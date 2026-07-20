# wake-server

A minimal web server (Python, no dependencies) that lets you **wake** and **sleep** a PC from any browser on your local network.

## How it works

- `server.py` runs on a always-on machine (e.g. a Raspberry Pi or NAS)
- It serves a small web UI on port `8081`
- **Wake** sends a [Wake-on-LAN](https://en.wikipedia.org/wiki/Wake-on-LAN) magic packet via UDP broadcast
- **Sleep** forwards an HTTP request to `windows/sleep-listener.py`, a tiny listener that must be running on the target PC

## Setup

### Server (Linux)

1. Edit `PC_IP` and `PC_MAC` at the top of `server.py` to match your target PC.
2. Run directly:
   ```bash
   python3 server.py
   ```
   Or install as a systemd service:
   ```bash
   sudo cp wake-server.service /etc/systemd/system/
   sudo systemctl enable --now wake-server
   ```

### Windows PC

1. Copy the `windows/` folder to the target PC.
2. Run `start-on-boot.bat` once (or add it to Task Scheduler / startup) to launch `sleep-listener.py` in the background — it listens on port `8765` for sleep commands.

## Files

| File | Description |
|------|-------------|
| `server.py` | Main HTTP server (wake + sleep + status UI) |
| `wake-server.service` | systemd unit file |
| `windows/sleep-listener.py` | Tiny HTTP listener on the Windows PC that triggers sleep |
| `windows/start-on-boot.bat` | Launches the listener silently on startup |
