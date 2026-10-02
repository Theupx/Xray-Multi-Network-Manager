# Xray Multi-Network Manager

[![CI](https://github.com/Theupx/Xray-Multi-Network-Manager/actions/workflows/ci.yml/badge.svg)](https://github.com/Theupx/Xray-Multi-Network-Manager/actions/workflows/ci.yml)
![Platform](https://img.shields.io/badge/platform-Windows-blue)
![Python](https://img.shields.io/badge/python-3.9%2B-blue)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

> Windows console tool that load-balances traffic across **multiple network adapters** using **Xray**.

Xray Multi-Network Manager is a management layer on top of Xray and Windows networking. It generates the Xray config, configures the system, monitors the connection, and restores everything when you stop. It is **not** Xray itself.

## ✨ Features

- 🔀 Multi-adapter selection and load balancing (Xray `roundRobin` balancer)
- 🌐 Three routing modes: B-PROXY, V-PROXY, TUN
- 🔗 Proxy import via `vless://`, `vmess://`, `trojan://`, `ss://` or a full Xray JSON config
- 📊 Live monitoring: per-adapter ping, speed, data usage and connection status
- 📡 Mobile Hotspot control from the monitor screen (`H` key)
- 🔄 Safe cleanup: system proxy, DNS, routes and hotspot are restored on exit
- 🧹 Stale `xray.exe` processes from this folder are cleaned before each start
- 🇮🇷 Persian text support in the console

## 🧱 Architecture

```text
Xray Multi-Network Manager
│
├── Python (main.py)
│   ├── Interactive console menu
│   ├── Xray config generator
│   ├── URL / JSON proxy parser
│   └── Monitoring (ping, speed, status)
│
├── Xray (xray.exe)
│   ├── SOCKS inbound / TUN inbound
│   └── roundRobin balancer -> one outbound per adapter
│
└── Windows integration
    ├── System proxy, DNS and routes (netsh / registry)
    ├── Wintun adapter (wintun.dll)
    └── Mobile Hotspot (PowerShell / WinRT / ICS)
```

## 🛣️ Routing Modes

| Mode | Description |
|------|-------------|
| **B-PROXY** | Balancing only, no VPN. Traffic is spread across the selected adapters through a local SOCKS inbound and the Windows system proxy. |
| **V-PROXY** | Same as B-PROXY, but every adapter's outbound goes through your imported proxy server. |
| **TUN** | System-wide capture through Xray's TUN inbound (Wintun). Aborts and rolls back if the TUN adapter does not come up in time. |

V-PROXY and TUN require an imported proxy server.

## 📁 Project Structure

```text
.
├── main.py              # Application (menu, config generation, monitoring)
├── run.bat              # Windows launcher with dependency checks
├── requirements.txt     # Python dependencies
├── .github/workflows/
│   └── ci.yml           # Windows syntax and lint checks
├── LICENSE
└── README.md
```

## 📦 Requirements

- Windows 10 / 11
- Python 3.9+ (`run.bat` can install Python 3.11 through winget)
- Administrator privileges
- `xray.exe` and `wintun.dll` next to `main.py` (not included)

## 🚀 Installation

```bash
git clone https://github.com/Theupx/Xray-Multi-Network-Manager.git
cd Xray-Multi-Network-Manager
pip install -r requirements.txt
```

### Xray and Wintun binaries

The binaries are intentionally **not** part of this repository:

1. **Xray-core**: https://github.com/XTLS/Xray-core/releases (take `xray.exe` from the Windows 64 build)
2. **Wintun**: https://www.wintun.net (take `wintun.dll` from `wintun/bin/amd64/`)

Place both files in the project folder.

## ▶️ Usage

Double-click **`run.bat`**. It requests Administrator rights, checks Python, packages and binaries, then starts the app. Or run manually from an Administrator terminal:

```bash
python main.py
```

```text
[1] Select Adapters
[2] Edit Local Port        (default: 10808)
[3] Proxy Server Settings  (add / view & ping / remove)
[4] Toggle Routing Mode    (B-PROXY -> V-PROXY -> TUN)
[5] START
[0] EXIT
```

While running: `H` toggles the hotspot, `Ctrl+C` stops Xray and restores your network settings.

## 💾 Runtime Files

Created next to `main.py` and git-ignored:

```text
data.json       # settings and saved proxy server
config.json     # generated Xray config (deleted on stop)
app_log.log     # error log
```

## 🔐 Security

Never commit:

```text
data.json
config.json
app_log.log
xray.exe
wintun.dll
```

`data.json` may contain your proxy server credentials. This tool changes system proxy, DNS and routing settings; it restores them on a normal stop or exit, but if the process is killed abruptly you may need to reset network settings manually. Use it only with networks and servers you are authorized to use.

## 🛠️ Development Status

Early release. Windows only.

```text
├── GUI front-end
├── Split main.py into modules
├── Automated tests
└── Packaged release (exe)
```

## 🧩 Third-party Components

- [Xray-core](https://github.com/XTLS/Xray-core) (MPL-2.0)
- [Wintun](https://www.wintun.net) (see its own license)
- [psutil](https://pypi.org/project/psutil/), [arabic-reshaper](https://pypi.org/project/arabic-reshaper/), [python-bidi](https://pypi.org/project/python-bidi/)

## 📄 License

Released under the [MIT License](LICENSE).