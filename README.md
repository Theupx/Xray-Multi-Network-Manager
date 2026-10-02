# Xray Multi-Network Manager

A Windows console tool that load-balances traffic across **multiple network adapters** using **Xray**, with optional routing through a proxy server (VLESS / VMess / Trojan / Shadowsocks) in system-proxy or TUN mode.

> This project is **not** Xray itself. It is a management layer written in Python that generates the Xray config, configures Windows networking, monitors the connection, and restores everything on exit.

## Features

- **Multi-adapter selection**: pick any active Windows network adapters to use at the same time
- **Load balancing**: Xray `roundRobin` balancer, with one outbound bound to each adapter's IP (`sendThrough`)
- **Three routing modes**: B-PROXY, V-PROXY, TUN
- **Proxy import**: paste a `vless://`, `vmess://`, `trojan://`, `ss://` link, or a full Xray JSON config
- **Live monitoring**: per-adapter ping, speed and data usage, connection status
- **Mobile Hotspot control**: toggle from the monitor screen with the `H` key (PowerShell / WinRT / Internet Connection Sharing)
- **Safe cleanup**: on stop or exit, Xray is killed, system proxy / DNS / routes are restored, the hotspot is turned off if the app enabled it, and the temporary `config.json` is deleted
- **Stale process cleanup**: leftover `xray.exe` processes started from this project's folder are killed before each start (other Xray instances are left alone)
- **Persian text support** in the console (via `arabic-reshaper` and `python-bidi`)

## Routing modes

| Mode | What it does |
|------|--------------|
| **B-PROXY** | Balancing only, no VPN. Traffic is spread across the selected adapters and exposed through a local SOCKS inbound; the Windows system proxy is pointed at it. |
| **V-PROXY** | Same as B-PROXY, but every adapter's outbound goes through your imported proxy server. |
| **TUN** | Uses Xray's TUN inbound (via Wintun) for system-wide capture. The app waits for the TUN adapter, applies the routes, and aborts and rolls back if TUN does not come up in time. |

V-PROXY and TUN require an imported proxy server. Switch modes from the main menu (`[4] Toggle Routing Mode`).

## Requirements

- Windows 10 / 11
- Python 3.9+ (3.11 is installed automatically by `run.bat` through winget if Python is missing)
- Administrator privileges (routes, DNS, TUN and hotspot control)
- `xray.exe` and `wintun.dll` placed next to `main.py` (not included, see below)

## Installation

```bash
git clone https://github.com/Theupx/Xray-Multi-Network-Manager.git
cd Xray-Multi-Network-Manager
pip install -r requirements.txt
```

### Xray and Wintun binaries

The binaries are **not** included in this repository. Download them and place them in the project folder:

1. **Xray-core**: https://github.com/XTLS/Xray-core/releases (take `xray.exe` from the Windows 64 build)
2. **Wintun**: https://www.wintun.net (take `wintun.dll` from `wintun/bin/amd64/`)

Expected layout:

```text
Xray-Multi-Network-Manager/
├── main.py
├── run.bat
├── requirements.txt
├── xray.exe      <- you add this
└── wintun.dll    <- you add this
```

## Usage

The easiest way is to double-click **`run.bat`**. It will:

1. Ask for Administrator privileges
2. Check Python and the required packages (and offer to install what is missing)
3. Verify that `xray.exe` and `wintun.dll` exist
4. Launch the manager

Or run it manually from an Administrator terminal:

```bash
python main.py
```

### Main menu

```text
[1] Select Adapters
[2] Edit Local Port        (default: 10808)
[3] Proxy Server Settings  (add / view & ping / remove)
[4] Toggle Routing Mode    (B-PROXY -> V-PROXY -> TUN)
[5] START
[0] EXIT
```

### While running

- `H`: toggle Mobile Hotspot
- `Ctrl+C`: stop Xray, restore network settings and return to the menu

## Files created at runtime

These are git-ignored: `data.json` (your settings and saved server), `config.json` (generated Xray config, deleted on stop), `app_log.log` (error log).

## Project structure

```text
.
├── main.py            # Application (menu, Xray config generation, monitoring, Windows integration)
├── run.bat            # Windows launcher with dependency checks
├── requirements.txt   # Python dependencies
└── README.md
```

## Dependencies

- [psutil](https://pypi.org/project/psutil/)
- [arabic-reshaper](https://pypi.org/project/arabic-reshaper/)
- [python-bidi](https://pypi.org/project/python-bidi/)

## Status

Early release. Windows only. Issues and feedback are welcome.

## Disclaimer

This tool changes system proxy, DNS and routing settings. It restores them on a normal stop or exit, but if the process is killed abruptly your network settings may need to be reset manually. Use at your own risk, and only with servers and networks you are allowed to use.

## Third-party components

- [Xray-core](https://github.com/XTLS/Xray-core) (MPL-2.0)
- [Wintun](https://www.wintun.net) (see its own license)

## License

See [LICENSE](LICENSE).
