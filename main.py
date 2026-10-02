import os
import sys
import time
import json
import socket
import base64
import copy
import atexit
import threading
import subprocess
import signal
import logging
import ctypes
import winreg
import tempfile
import textwrap
from urllib.parse import urlparse, parse_qs, unquote

import psutil

try:
    import msvcrt
except ImportError:
    msvcrt = None


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "data.json")
XRAY_CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
XRAY_EXECUTABLE = os.path.join(BASE_DIR, "xray.exe")
LOG_FILE = os.path.join(BASE_DIR, "app_log.log")

logging.basicConfig(
    level=logging.ERROR,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, delay=True, encoding="utf-8")]
)

TUN_NAME = "xraytun"
TUN_IP = "198.18.0.2"
TUN_GW = "198.18.0.1"
TUN_MASK = "255.255.255.0"
TUN_METRIC = 1
ADAPTER_METRIC = 15
XRAY_START_GRACE_SECONDS = 0.8
TUN_WAIT_TIMEOUT_SECONDS = 10
TUN_POLL_INTERVAL_SECONDS = 0.1
ROUTE_RETRY_INTERVAL_SECONDS = 0.2
CREATE_NO_WINDOW = 0x08000000

try:
    import arabic_reshaper
    from bidi.algorithm import get_display
    HAS_PERSIAN_SUPPORT = True
except ImportError:
    HAS_PERSIAN_SUPPORT = False

xray_process = None

def fix_persian(text):
    """Reshapes Persian/Arabic text for correct display in Windows CMD."""
    if HAS_PERSIAN_SUPPORT and text:
        try:
            reshaped = arabic_reshaper.reshape(text)
            return get_display(reshaped)
        except Exception as e:
            logging.error(f"Persian text reshape failed: {e}")
    return text


def kill_zombie_xray():
    """Finds and kills leftover Xray processes started from OUR executable path,
    to avoid killing an unrelated 'xray.exe' process owned by another app.

    Note: only requests 'pid' and 'name' from process_iter (cheap, cached by
    psutil), and calls the comparatively expensive .exe() only on the few
    processes that actually match by name. Asking process_iter for 'exe'
    directly would force psutil to open every single process on the system
    up front, which is noticeably slower.
    """
    target_path = os.path.normcase(os.path.abspath(XRAY_EXECUTABLE))
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            name = (proc.info.get('name') or "").lower()
            if name != "xray.exe":
                continue
            try:
                exe_path = proc.exe()
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                exe_path = None
            if exe_path and os.path.normcase(os.path.abspath(exe_path)) != target_path:
                continue
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass
        except Exception as e:
            logging.error(f"kill_zombie_xray error: {e}")


def delete_temp_config():
    """Deletes the temporary Xray config file to keep it secure and clean."""
    if os.path.exists(XRAY_CONFIG_FILE):
        try:
            os.remove(XRAY_CONFIG_FILE)
        except Exception as e:
            logging.error(f"Failed to delete config.json: {e}")


def format_size(bytes_val):
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_val < 1024.0:
            return f"{bytes_val:.2f} {unit}"
        bytes_val /= 1024.0
    return f"{bytes_val:.2f} PB"


def format_speed(kbps_val):
    if kbps_val >= 1024.0:
        return f"{kbps_val/1024.0:.2f} MB/s"
    return f"{kbps_val:.1f} KB/s"


def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin() != 0
    except Exception as e:
        logging.error(f"Admin check failed: {e}")
        return False


def poll_hotkeys():
    """Drains any pending keypresses from the console input buffer and
    returns the set of recognized hotkeys pressed since the last poll
    (currently only 'h', for the Mobile Hotspot toggle). No-op if msvcrt
    is unavailable (e.g. non-interactive/non-Windows execution)."""
    pressed = set()
    if msvcrt is None:
        return pressed
    while msvcrt.kbhit():
        try:
            ch = msvcrt.getch()
        except Exception:
            break
        if ch in (b'h', b'H'):
            pressed.add('h')
    return pressed


def clear_screen():
    os.system('cls')


def run_hidden(cmd, **kwargs):
    """subprocess.run wrapper that suppresses the console window flash on
    Windows for helper processes (netsh/route), on top of whatever stdio
    redirection the caller passed in."""
    return subprocess.run(cmd, creationflags=CREATE_NO_WINDOW, **kwargs)


def netsh_batch(command_lines):
    """Runs a list of 'netsh ...' command lines (without the leading 'netsh')
    in a SINGLE netsh process via 'netsh -f <scriptfile>', instead of
    spawning a brand-new netsh.exe (which is slow to initialize, ~300-800ms
    each) per command. This is the main lever for making TUN mode start/stop
    noticeably faster when several adapters/routes are touched at once.

    Returns True if the batch executed without a non-zero exit code.
    """
    if not command_lines:
        return True
    script_path = None
    try:
        fd, script_path = tempfile.mkstemp(prefix="netsh_batch_", suffix=".txt", dir=BASE_DIR)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(command_lines) + "\n")
        res = run_hidden(
            ["netsh", "-f", script_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return res.returncode == 0
    except Exception as e:
        logging.error(f"netsh_batch failed: {e}")
        return False
    finally:
        if script_path and os.path.exists(script_path):
            try:
                os.remove(script_path)
            except Exception:
                pass

def safe_int_input(prompt, min_val=None, max_val=None):
    """Prompts the user for an integer with validation. Returns None on cancel."""
    try:
        raw = input(prompt).strip()
    except (KeyboardInterrupt, EOFError):
        return None
    if not raw:
        print("\n[!] Input cannot be empty.")
        return None
    try:
        value = int(raw)
    except ValueError:
        print("\n[!] Please enter a valid integer number.")
        return None
    if min_val is not None and value < min_val:
        print(f"\n[!] Value must be >= {min_val}.")
        return None
    if max_val is not None and value > max_val:
        print(f"\n[!] Value must be <= {max_val}.")
        return None
    return value


try:
    kernel32 = ctypes.windll.kernel32
    kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
except Exception:
    pass

if not is_admin():
    print("[!] Administrator privileges required.")
    try:
        params = " ".join([f'"{arg}"' for arg in sys.argv])
        ret = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
        if int(ret) <= 32:
            print("[!] Elevation was cancelled or failed. Exiting.")
    except Exception as e:
        logging.error(f"Elevation attempt failed: {e}")
        print("[!] Could not request administrator privileges.")
    sys.exit()


def load_data():
    default_data = {
        "settings": {
            "local_port": 10808,
            "selected_adapters": [],
            "app_mode": "b-proxy",
            "proxy_remark": "",
        },
        "custom_proxy": None
    }
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if "settings" in loaded:
                    default_data["settings"].update(loaded["settings"])
                if "custom_proxy" in loaded:
                    default_data["custom_proxy"] = loaded["custom_proxy"]
        except Exception as e:
            logging.error(f"Failed to load data.json: {e}")
    return default_data

def save_data(data):
    try:
        temp_file = DATA_FILE + ".tmp"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        os.replace(temp_file, DATA_FILE)
    except Exception as e:
        logging.error(f"Failed to save data.json: {e}")

class WindowsManager:
    def __init__(self):
        self.modified_adapters = []
        self.proxy_enabled_by_us = False
        self.tun_routes_applied = False
        self.proxy_bypass_ip = None

        self.original_proxy_enable = 0
        self.original_proxy_server = ""
        self.original_dns_servers = {}
        self._backup_system_proxy_state()

    def get_active_adapters(self):
        adapters = {}
        try:
            stats = psutil.net_if_stats()
            addrs = psutil.net_if_addrs()
            for name, stat in stats.items():
                if stat.isup and name in addrs:
                    for addr in addrs[name]:
                        if addr.family == socket.AF_INET and not addr.address.startswith("127."):
                            adapters[name] = addr.address
        except Exception as e:
            logging.error(f"Error fetching active adapters: {e}")
        return adapters

    def set_metric(self, adapter_name, metric=ADAPTER_METRIC):
        """Sets the metric for a single adapter. Prefer set_metrics_batch()
        when touching multiple adapters at once -- each call here spawns its
        own netsh.exe process (slow)."""
        try:
            cmd = ["netsh", "interface", "ipv4", "set", "interface", adapter_name, f"metric={metric}"]
            run_hidden(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if adapter_name not in self.modified_adapters:
                self.modified_adapters.append(adapter_name)
            return True
        except Exception as e:
            logging.error(f"Set metric failed for {adapter_name}: {e}")
            return False

    def set_metrics_batch(self, adapter_names, metric=ADAPTER_METRIC):
        """Sets the metric for several adapters in a single netsh process
        (via a netsh script), instead of one netsh.exe launch per adapter.
        This is the main reason multi-adapter startup used to feel slow."""
        if not adapter_names:
            return True
        lines = [f'interface ipv4 set interface "{name}" metric={metric}' for name in adapter_names]
        ok = netsh_batch(lines)
        for name in adapter_names:
            if name not in self.modified_adapters:
                self.modified_adapters.append(name)
        return ok

    def reset_metrics(self):
        if not self.modified_adapters:
            return
        lines = [f'interface ipv4 set interface "{adapter}" metric=auto' for adapter in self.modified_adapters]
        netsh_batch(lines)
        self.modified_adapters.clear()

    def _backup_system_proxy_state(self):
        """Backs up existing system proxy settings from registry."""
        try:
            reg_key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
                0, winreg.KEY_READ
            )
            try:
                self.original_proxy_enable, _ = winreg.QueryValueEx(reg_key, "ProxyEnable")
            except FileNotFoundError:
                self.original_proxy_enable = 0
            try:
                self.original_proxy_server, _ = winreg.QueryValueEx(reg_key, "ProxyServer")
            except FileNotFoundError:
                self.original_proxy_server = ""
            winreg.CloseKey(reg_key)
        except Exception as e:
            logging.error(f"Registry backup failed: {e}")

    def _notify_proxy_change(self):
        try:
            ctypes.windll.Wininet.InternetSetOptionW(0, 39, 0, 0)
            ctypes.windll.Wininet.InternetSetOptionW(0, 37, 0, 0)
        except Exception as e:
            logging.error(f"Proxy change notification failed: {e}")

    def restore_system_proxy_state(self):
        """Restores registry proxy settings to the state before script execution."""
        try:
            reg_key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
                0, winreg.KEY_WRITE
            )
            winreg.SetValueEx(reg_key, "ProxyEnable", 0, winreg.REG_DWORD, int(self.original_proxy_enable))
            winreg.SetValueEx(reg_key, "ProxyServer", 0, winreg.REG_SZ, self.original_proxy_server or "")
            winreg.CloseKey(reg_key)
            self._notify_proxy_change()
        except Exception as e:
            logging.error(f"Registry proxy restore failed: {e}")

    def set_system_proxy(self, port, enable=True):
        try:
            if enable:
                reg_key = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
                    0, winreg.KEY_WRITE
                )
                winreg.SetValueEx(reg_key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
                winreg.SetValueEx(reg_key, "ProxyServer", 0, winreg.REG_SZ, f"127.0.0.1:{port}")
                winreg.CloseKey(reg_key)
                self.proxy_enabled_by_us = True
                self._notify_proxy_change()
            else:
                self.restore_system_proxy_state()
                self.proxy_enabled_by_us = False
        except Exception as e:
            logging.error(f"Failed modifying system proxy: {e}")

    def get_default_gateway(self):
        try:
            output = subprocess.check_output(
                ["route", "print", "0.0.0.0"], stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW
            ).decode("utf-8", errors="ignore")
            candidates = []
            for line in output.split('\n'):
                parts = line.strip().split()
                if len(parts) >= 5 and parts[0] == "0.0.0.0" and parts[1] == "0.0.0.0":
                    try:
                        metric = int(parts[4])
                    except ValueError:
                        metric = 9999
                    candidates.append((metric, parts[2]))
            if candidates:
                candidates.sort(key=lambda x: x[0])
                return candidates[0][1]
        except Exception as e:
            logging.error(f"Failed to get default gateway: {e}")
        return None

    def apply_dns_leak_protection(self, adapters):
        """Sets physical adapters' DNS to loopback (127.0.0.2) to prevent DNS
        leaks in TUN mode. All adapters are batched into a single netsh
        process instead of one netsh.exe launch per adapter."""
        if not adapters:
            return
        lines = [
            f'interface ipv4 set dns name="{adapter}" static 127.0.0.2 validate=no'
            for adapter in adapters
        ]
        ok = netsh_batch(lines)
        if not ok:
            logging.error("DNS leak protection batch command failed (see netsh_batch log).")
        for adapter in adapters:
            self.original_dns_servers[adapter] = True

    def restore_dns_settings(self):
        """Restores physical adapter DNS settings back to DHCP, batched into
        a single netsh process."""
        adapters = list(self.original_dns_servers.keys())
        if not adapters:
            return
        lines = [f'interface ipv4 set dns name="{adapter}" dhcp' for adapter in adapters]
        netsh_batch(lines)
        self.original_dns_servers.clear()

    def is_tun_ready(self):
        try:
            stats = psutil.net_if_stats()
            return TUN_NAME in stats and stats[TUN_NAME].isup
        except Exception as e:
            logging.error(f"is_tun_ready check failed: {e}")
            return False

    @staticmethod
    def _run_concurrent(cmd_list, timeout=5):
        """Launches several independent commands (e.g. multiple 'route add')
        WITHOUT waiting for each one before starting the next, then waits for
        all of them at the end. route.exe is lightweight to spawn, but doing
        this still turns N sequential round-trips into effectively one
        (bounded by the slowest single command) instead of N."""
        procs = []
        for cmd in cmd_list:
            try:
                p = subprocess.Popen(
                    cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=CREATE_NO_WINDOW
                )
                procs.append(p)
            except Exception as e:
                logging.error(f"Failed to launch {cmd}: {e}")
        results = []
        for p in procs:
            try:
                p.wait(timeout=timeout)
                results.append(p.returncode)
            except Exception as e:
                logging.error(f"Command wait failed: {e}")
                results.append(-1)
        return results

    def apply_tun_routes(self, proxy_ip, selected_adapters):
        """Applies TUN routing. Returns True on (best-effort) success.

        Performance note: the TUN interface's address/DNS/metric are set via
        a single batched netsh script (one netsh.exe launch instead of
        three), and the independent 'route add' commands are fired
        concurrently instead of waited on one-by-one.
        """
        netsh_batch([
            f'interface ip set address name="{TUN_NAME}" source=static addr={TUN_IP} mask={TUN_MASK} gateway={TUN_GW}',
            f'interface ip set dns name="{TUN_NAME}" source=static addr=8.8.8.8',
            f'interface ipv4 set interface "{TUN_NAME}" metric={TUN_METRIC}',
        ])
        if TUN_NAME not in self.modified_adapters:
            self.modified_adapters.append(TUN_NAME)

        route_ok = False
        default_route_cmd = ["route", "add", "0.0.0.0", "mask", "128.0.0.0", TUN_GW, "metric", "1"]
        for _ in range(6):
            res = run_hidden(default_route_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if res.returncode == 0:
                route_ok = True
                break
            time.sleep(ROUTE_RETRY_INTERVAL_SECONDS)

        self.tun_routes_applied = True

        concurrent_cmds = [["route", "add", "128.0.0.0", "mask", "128.0.0.0", TUN_GW, "metric", "1"]]

        gw = self.get_default_gateway()
        if gw:
            concurrent_cmds.append(["route", "add", "8.8.8.8", "mask", "255.255.255.255", gw, "metric", "1"])
            concurrent_cmds.append(["route", "add", "1.1.1.1", "mask", "255.255.255.255", gw, "metric", "1"])
            if proxy_ip:
                concurrent_cmds.append(["route", "add", proxy_ip, "mask", "255.255.255.255", gw, "metric", "1"])
                self.proxy_bypass_ip = proxy_ip
        else:
            logging.error("Could not determine default gateway; DNS/proxy bypass routes not added.")

        self._run_concurrent(concurrent_cmds)

        self.apply_dns_leak_protection(selected_adapters)
        return route_ok

    def remove_tun_routes(self):
        self.restore_dns_settings()

        delete_cmds = []
        if self.tun_routes_applied:
            delete_cmds.append(["route", "delete", "0.0.0.0", "mask", "128.0.0.0", TUN_GW])
            delete_cmds.append(["route", "delete", "128.0.0.0", "mask", "128.0.0.0", TUN_GW])
            self.tun_routes_applied = False

        delete_cmds.append(["route", "delete", "8.8.8.8"])
        delete_cmds.append(["route", "delete", "1.1.1.1"])

        if self.proxy_bypass_ip:
            delete_cmds.append(["route", "delete", self.proxy_bypass_ip])
            self.proxy_bypass_ip = None

        self._run_concurrent(delete_cmds)

    def revert_all(self):
        self.reset_metrics()
        self.remove_tun_routes()
        if self.proxy_enabled_by_us:
            self.set_system_proxy(0, enable=False)

sys_mgr = WindowsManager()


def graceful_exit(signum=None, frame=None):
    """Graceful shutdown handler for terminal signals."""
    global hotspot_enabled_by_us
    try:
        if hotspot_enabled_by_us:
            hotspot_stop()
            hotspot_enabled_by_us = False
    except Exception:
        pass
    kill_zombie_xray()
    sys_mgr.revert_all()
    delete_temp_config()
    if signum is not None:
        os._exit(0)

signal.signal(signal.SIGTERM, graceful_exit)
atexit.register(graceful_exit)

def _split_userinfo_netloc(netloc):
    """Splits 'user_info@host:port' safely, including IPv6 host literals
    like 'user_info@[2001:db8::1]:443'. Returns (user_info, address, port)."""
    if "@" not in netloc:
        return None
    user_info, host_part = netloc.split("@", 1)

    if host_part.startswith("["):
        end = host_part.find("]")
        if end == -1:
            return None
        address = host_part[1:end]
        rest = host_part[end + 1:]
        if not rest.startswith(":"):
            return None
        port_str = rest[1:]
    else:
        if ":" not in host_part:
            return None
        address, port_str = host_part.rsplit(":", 1)

    if not port_str.isdigit():
        return None
    return user_info, address, int(port_str)

def parse_universal_url(url):
    url = url.strip()
    try:
        if url.startswith("{") and url.endswith("}"):
            data = json.loads(url)

            if "outbounds" in data and isinstance(data["outbounds"], list) and len(data["outbounds"]) > 0:
                proxy_outbound = None

                for ob in data["outbounds"]:
                    protocol = ob.get("protocol", "").lower()
                    if protocol not in ["freedom", "blackhole", "dns"]:
                        proxy_outbound = ob
                        break

                if not proxy_outbound:
                    proxy_outbound = data["outbounds"][0]

                remark = data.get("remarks", "Full JSON Config")

                address = "127.0.0.1"
                port = 80
                protocol = proxy_outbound.get("protocol", "").lower()
                try:
                    if protocol in ["vless", "vmess"]:
                        address = proxy_outbound["settings"]["vnext"][0]["address"]
                        port = proxy_outbound["settings"]["vnext"][0]["port"]
                    elif protocol in ["trojan", "shadowsocks"]:
                        address = proxy_outbound["settings"]["servers"][0]["address"]
                        port = proxy_outbound["settings"]["servers"][0]["port"]
                except Exception:
                    pass

                return {"outbound": proxy_outbound, "remark": remark, "address": address, "port": int(port)}
            else:
                return {"outbound": data, "remark": "Custom JSON", "address": "127.0.0.1", "port": 80}

        if url.startswith("vmess://"):
            b64_str = url[8:]
            b64_str += "=" * ((4 - len(b64_str) % 4) % 4)
            conf = json.loads(base64.b64decode(b64_str).decode('utf-8'))

            outbound = {
                "protocol": "vmess",
                "settings": {
                    "vnext": [{
                        "address": conf.get("add"),
                        "port": int(conf.get("port")),
                        "users": [{"id": conf.get("id"), "alterId": int(conf.get("aid", 0)), "security": conf.get("scy", "auto")}]
                    }]
                },
                "streamSettings": {
                    "network": conf.get("net", "tcp"),
                    "security": conf.get("tls", "none")
                }
            }
            if conf.get("net") == "ws":
                outbound["streamSettings"]["wsSettings"] = {
                    "path": conf.get("path", "/"),
                    "headers": {"Host": conf.get("host", conf.get("add"))}
                }
            elif conf.get("net") == "grpc":
                outbound["streamSettings"]["grpcSettings"] = {
                    "serviceName": conf.get("path", ""),
                    "multiMode": True
                }
            if conf.get("tls") == "tls":
                outbound["streamSettings"]["tlsSettings"] = {
                    "serverName": conf.get("sni", conf.get("host", conf.get("add")))
                }
            return {"outbound": outbound, "remark": conf.get("ps", "VMESS Server"), "address": conf.get("add"), "port": int(conf.get("port"))}

        elif url.startswith("vless://"):
            parsed = urlparse(url)
            split_result = _split_userinfo_netloc(parsed.netloc)
            if not split_result:
                return None
            uuid, address, port = split_result
            params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            remark = unquote(parsed.fragment) if parsed.fragment else "VLESS Server"

            outbound = {
                "protocol": "vless",
                "settings": {
                    "vnext": [{
                        "address": address,
                        "port": port,
                        "users": [{"id": uuid, "encryption": params.get("encryption", "none")}]
                    }]
                },
                "streamSettings": {
                    "network": params.get("type", "tcp"),
                    "security": params.get("security", "none")
                }
            }

            if params.get("security") == "tls":
                outbound["streamSettings"]["tlsSettings"] = {
                    "serverName": params.get("sni", address),
                    "fingerprint": params.get("fp", "chrome")
                }
            elif params.get("security") == "reality":
                outbound["streamSettings"]["realitySettings"] = {
                    "serverName": params.get("sni", address),
                    "publicKey": params.get("pbk", ""),
                    "shortId": params.get("sid", ""),
                    "fingerprint": params.get("fp", "chrome"),
                    "spiderX": params.get("spx", "/")
                }

            network = params.get("type", "tcp")
            if network == "tcp" and params.get("headerType") == "http":
                outbound["streamSettings"]["tcpSettings"] = {
                    "header": {
                        "type": "http",
                        "request": {
                            "version": "1.1",
                            "method": "GET",
                            "path": [params.get("path", "/")],
                            "headers": {"Host": [params.get("host", address)]}
                        }
                    }
                }
            elif network == "ws":
                outbound["streamSettings"]["wsSettings"] = {
                    "path": params.get("path", "/"),
                    "headers": {"Host": params.get("host", address)}
                }
            elif network == "grpc":
                outbound["streamSettings"]["grpcSettings"] = {
                    "serviceName": params.get("serviceName", ""),
                    "multiMode": True if params.get("mode", "multi") == "multi" else False
                }
            return {"outbound": outbound, "remark": remark, "address": address, "port": port}

        elif url.startswith("trojan://"):
            parsed = urlparse(url)
            split_result = _split_userinfo_netloc(parsed.netloc)
            if not split_result:
                return None
            pwd, address, port = split_result
            pwd = unquote(pwd)
            params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            remark = unquote(parsed.fragment) if parsed.fragment else "TROJAN Server"

            outbound = {
                "protocol": "trojan",
                "settings": {
                    "servers": [{"address": address, "port": port, "password": pwd}]
                },
                "streamSettings": {
                    "network": params.get("type", "tcp"),
                    "security": params.get("security", "tls")
                }
            }
            if outbound["streamSettings"]["security"] == "tls":
                outbound["streamSettings"]["tlsSettings"] = {
                    "serverName": params.get("sni", address)
                }
            if params.get("type") == "ws":
                outbound["streamSettings"]["wsSettings"] = {
                    "path": params.get("path", "/"),
                    "headers": {"Host": params.get("host", address)}
                }
            return {"outbound": outbound, "remark": remark, "address": address, "port": port}

        elif url.startswith("ss://"):
            parsed = urlparse(url)
            remark = unquote(parsed.fragment) if parsed.fragment else "Shadowsocks Server"

            netloc = parsed.netloc
            method = None
            password = None

            if "@" in netloc:
                userinfo, host_part = netloc.split("@", 1)
                try:
                    padded = userinfo + "=" * ((4 - len(userinfo) % 4) % 4)
                    decoded = base64.urlsafe_b64decode(padded).decode('utf-8')
                except Exception:
                    decoded = unquote(userinfo)
                if ":" not in decoded:
                    return None
                method, password = decoded.split(":", 1)

                if host_part.startswith("["):
                    end = host_part.find("]")
                    if end == -1:
                        return None
                    address = host_part[1:end]
                    rest = host_part[end + 1:]
                    if not rest.startswith(":"):
                        return None
                    port_str = rest[1:]
                else:
                    if ":" not in host_part:
                        return None
                    address, port_str = host_part.rsplit(":", 1)
                if not port_str.isdigit():
                    return None
                port = int(port_str)
            else:
                b64_full = netloc
                padded = b64_full + "=" * ((4 - len(b64_full) % 4) % 4)
                decoded = base64.urlsafe_b64decode(padded).decode('utf-8')
                if "@" not in decoded or ":" not in decoded:
                    return None
                cred_part, host_part = decoded.rsplit("@", 1)
                if ":" not in cred_part:
                    return None
                method, password = cred_part.split(":", 1)
                if ":" not in host_part:
                    return None
                address, port_str = host_part.rsplit(":", 1)
                if not port_str.isdigit():
                    return None
                port = int(port_str)

            outbound = {
                "protocol": "shadowsocks",
                "settings": {
                    "servers": [{
                        "address": address,
                        "port": port,
                        "method": method,
                        "password": password
                    }]
                },
                "streamSettings": {
                    "network": "tcp"
                }
            }
            return {"outbound": outbound, "remark": remark, "address": address, "port": port}

    except Exception as e:
        logging.error(f"URL parsing failed: {e}")
    return None

def get_outbound_endpoint(outbound):
    """Extracts (address, port) from a raw outbound dict, for any supported protocol."""
    protocol = (outbound.get("protocol") or "").lower()
    try:
        if protocol in ["vless", "vmess"]:
            v = outbound["settings"]["vnext"][0]
            return v["address"], v["port"]
        elif protocol in ["trojan", "shadowsocks"]:
            s = outbound["settings"]["servers"][0]
            return s["address"], s["port"]
    except Exception as e:
        logging.error(f"get_outbound_endpoint failed: {e}")
    return None, None

def set_outbound_address(outbound, new_address):
    """Rewrites the server address inside an outbound dict in-place."""
    protocol = (outbound.get("protocol") or "").lower()
    try:
        if protocol in ["vless", "vmess"]:
            outbound["settings"]["vnext"][0]["address"] = new_address
        elif protocol in ["trojan", "shadowsocks"]:
            outbound["settings"]["servers"][0]["address"] = new_address
    except Exception as e:
        logging.error(f"set_outbound_address failed: {e}")

def quick_ping(host, port, bind_ip=None):
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2.0)
        if bind_ip:
            s.bind((bind_ip, 0))
        start = time.time()
        s.connect((host, port))
        latency = f"{int((time.time() - start) * 1000)} ms"
        return latency
    except Exception:
        return "Timeout"
    finally:
        if s is not None:
            try:
                s.close()
            except Exception:
                pass

HOTSPOT_STATE_POLL_TIMEOUT = 10
HOTSPOT_ACTION_TIMEOUT = 90

_HOTSPOT_PS_HEADER_TEMPLATE = r'''
$ErrorActionPreference = "Stop"
try {
    [Windows.System.UserProfile.LockScreen, Windows.System.UserProfile, ContentType=WindowsRuntime] | Out-Null
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
    function Await($WinRtTask, $ResultType) {
        $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
        $netTask = $asTask.Invoke($null, @($WinRtTask))
        $netTask.Wait(-1) | Out-Null
        return $netTask.Result
    }

    $profile = [Windows.Networking.Connectivity.NetworkInformation, Windows.Networking.Connectivity, ContentType=WindowsRuntime]::GetInternetConnectionProfile()
    if ($null -eq $profile) { Write-Output "ERROR:NoInternetProfile"; exit 1 }
    $mgr = [Windows.Networking.NetworkOperators.NetworkOperatorTetheringManager, Windows.Networking.NetworkOperators, ContentType=WindowsRuntime]::CreateFromConnectionProfile($profile)

    function Get-UpAdapterNames {
        @(Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object { $_.Status -eq 'Up' } | Select-Object -ExpandProperty Name)
    }

    function Get-HotspotApAdapterName($beforeUp) {
        for ($i = 0; $i -lt 20; $i++) {
            $upNow = @(Get-NetAdapter -ErrorAction SilentlyContinue | Where-Object { $_.Status -eq 'Up' })
            $newAp = $upNow | Where-Object { $beforeUp -notcontains $_.Name } | Select-Object -First 1
            if ($newAp) { return $newAp.Name }
            $descMatch = $upNow | Where-Object {
                $_.InterfaceDescription -match 'Wi-Fi Direct Virtual' -or
                $_.InterfaceDescription -match 'Hosted Network Virtual' -or
                $_.InterfaceDescription -match 'Virtual WiFi'
            } | Select-Object -First 1
            if ($descMatch) { return $descMatch.Name }
            Start-Sleep -Milliseconds 500
        }
        return $null
    }

    function Ensure-IcsServiceRunning {
        try {
            $svc = Get-Service -Name SharedAccess -ErrorAction Stop
        } catch {
            return "SharedAccess service not found on this system."
        }
        if ($svc.Status -eq 'Running') { return $null }
        try {
            if ($svc.StartType -eq 'Disabled') {
                Set-Service -Name SharedAccess -StartupType Manual -ErrorAction Stop
            }
            Start-Service -Name SharedAccess -ErrorAction Stop
            for ($i = 0; $i -lt 10; $i++) {
                Start-Sleep -Milliseconds 300
                $svc.Refresh()
                if ($svc.Status -eq 'Running') { return $null }
            }
            return "SharedAccess service did not reach Running state (stuck at $($svc.Status))."
        } catch {
            $extra = ""
            try {
                $rras = Get-Service -Name RemoteAccess -ErrorAction SilentlyContinue
                if ($rras -and $rras.Status -ne 'Stopped') {
                    $extra = " The 'Routing and Remote Access' (RemoteAccess) service is $($rras.Status) and is known to block ICS from starting - disable/stop it and try again."
                }
            } catch {}
            return ("Could not start SharedAccess service: " + $_.Exception.Message + $extra)
        }
    }

    function Bind-IcsToTun($beforeUp) {
        $apName = Get-HotspotApAdapterName $beforeUp
        if (-not $apName) { return "NOAPADAPTER" }
        $svcErr = Ensure-IcsServiceRunning
        if ($svcErr) { return ("SVCFAIL:" + $svcErr) }
        try {
            regsvr32.exe /s hnetcfg.dll
            $netShare = New-Object -ComObject HNetCfg.HNetShare
        } catch {
            return ("NOCOM:" + $_.Exception.Message)
        }
        $conns = $netShare.EnumEveryConnection
        $pubConn = $null; $privConn = $null
        $seenNames = @()
        foreach ($c in $conns) {
            try {
                $name = $netShare.NetConnectionProps.Invoke($c).Name
                $seenNames += $name
                if ($name -eq "__TUN_NAME__") { $pubConn = $c }
                elseif ($name -eq $apName) { $privConn = $c }
            } catch {}
        }
        if (-not $pubConn) { return ("NOTUNCONN:" + ($seenNames -join ',')) }
        if (-not $privConn) { return ("NOAPCONN:" + $apName) }
        foreach ($c in $conns) {
            if ($c -eq $privConn) { continue }
            try {
                $cfg = $netShare.INetSharingConfigurationForINetConnection.Invoke($c)
                if ($cfg.SharingEnabled) { $cfg.DisableSharing() }
            } catch {}
        }
        Start-Sleep -Milliseconds 500
        $pubConfig = $netShare.INetSharingConfigurationForINetConnection.Invoke($pubConn)
        $privConfig = $netShare.INetSharingConfigurationForINetConnection.Invoke($privConn)
        $bound = $false
        $lastErr = ""
        for ($attempt = 0; $attempt -lt 6 -and -not $bound; $attempt++) {
            try {
                $pubConfig.EnableSharing(0)
                $privConfig.EnableSharing(1)
                $bound = $true
            } catch {
                $lastErr = $_.Exception.Message
                if ($attempt -eq 2) {
                    try {
                        Restart-Service -Name SharedAccess -Force -ErrorAction Stop
                        Start-Sleep -Milliseconds 1500
                        $netShare = New-Object -ComObject HNetCfg.HNetShare
                        $pubConfig = $netShare.INetSharingConfigurationForINetConnection.Invoke($pubConn)
                        $privConfig = $netShare.INetSharingConfigurationForINetConnection.Invoke($privConn)
                    } catch {}
                } else {
                    Start-Sleep -Milliseconds 1000
                }
            }
        }
        if (-not $bound) { return ("BINDEXC:" + $lastErr) }
        Start-Sleep -Milliseconds 500
        try {
            $verifyPub = $netShare.INetSharingConfigurationForINetConnection.Invoke($pubConn)
            if (-not [bool]$verifyPub.SharingEnabled) { return "NOTSTUCK" }
        } catch {
            return ("VERIFYEXC:" + $_.Exception.Message)
        }
        for ($i = 0; $i -lt 6; $i++) {
            $liveState = [int]$mgr.TetheringOperationalState
            if ($liveState -eq 1) { return "OK" }
            if ($liveState -ne 3) { break }
            Start-Sleep -Milliseconds 500
        }
        return ("HOTSPOTDIED:" + [int]$mgr.TetheringOperationalState)
    }

    function Unbind-Ics {
        try {
            regsvr32.exe /s hnetcfg.dll
            $netShare = New-Object -ComObject HNetCfg.HNetShare
            foreach ($c in $netShare.EnumEveryConnection) {
                try {
                    $cfg = $netShare.INetSharingConfigurationForINetConnection.Invoke($c)
                    if ($cfg.SharingEnabled) { $cfg.DisableSharing() }
                } catch {}
            }
        } catch {}
    }
'''

_HOTSPOT_PS_FOOTER = r'''
} catch {
    Write-Output ("ERROR:" + $_.Exception.Message)
    exit 1
}
'''

_HOTSPOT_STATE_ACTION = r'''
    Write-Output ("STATE:" + [int]$mgr.TetheringOperationalState)
    exit 0
'''

_HOTSPOT_START_ACTION = r'''
    $beforeUp = Get-UpAdapterNames
    $r = Await ($mgr.StartTetheringAsync()) ([Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult])
    $status = [int]$r.Status
    if ($status -eq 0 -or $status -eq 9) {
        $bindResult = Bind-IcsToTun $beforeUp
        if ($bindResult.StartsWith("HOTSPOTDIED") -and $status -ne 9) {
            try { Await ($mgr.StopTetheringAsync()) ([Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult]) | Out-Null } catch {}
            Start-Sleep -Milliseconds 1000
            $beforeUp2 = Get-UpAdapterNames
            $r2 = Await ($mgr.StartTetheringAsync()) ([Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult])
            $status2 = [int]$r2.Status
            if ($status2 -eq 0 -or $status2 -eq 9) {
                $bindResult = Bind-IcsToTun $beforeUp2
            } else {
                Write-Output ("STATUS:" + $status2)
                exit 0
            }
        }
        if ($bindResult -eq "OK") {
            Write-Output "STATUS:0"
        } else {
            try { Await ($mgr.StopTetheringAsync()) ([Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult]) | Out-Null } catch {}
            Write-Output ("STATUS:ICSFAIL:" + $bindResult)
        }
    } else {
        Write-Output ("STATUS:" + $status)
    }
    exit 0
'''

_HOTSPOT_STOP_ACTION = r'''
    Unbind-Ics
    $r = Await ($mgr.StopTetheringAsync()) ([Windows.Networking.NetworkOperators.NetworkOperatorTetheringOperationResult])
    Write-Output ("STATUS:" + [int]$r.Status)
    exit 0
'''

_HOTSPOT_STATE_MAP = {"0": "unknown", "1": "on", "2": "off", "3": "transition"}

_HOTSPOT_STATUS_MESSAGES = {
    "0": "Success", "1": "Unknown error", "2": "Mobile broadband adapter is off",
    "3": "Wi-Fi adapter is off", "4": "Entitlement check timed out",
    "5": "Entitlement check failed (carrier does not allow tethering)",
    "6": "Operation already in progress", "7": "Bluetooth is off",
    "8": "Limited network connectivity", "9": "Already on",
    "10": "Frequency band restricted by radio hardware",
    "11": "Frequency band conflicts with the main connection",
}

_HOTSPOT_ICSFAIL_MESSAGES = {
    "NOAPADAPTER": "Could not find the Mobile Hotspot's own virtual Wi-Fi "
                   "adapter (it never came up). The hotspot may not have "
                   "actually started broadcasting - try toggling it again.",
    "NOCOM": "Could not access Windows' Internet Connection Sharing (ICS) "
             "component (hnetcfg.dll / HNetCfg.HNetShare).",
    "NOTUNCONN": "Windows' network-sharing list doesn't contain \"xraytun\" "
                 "as a nameable connection - TUN mode may not be fully up yet.",
    "NOAPCONN": "Found the hotspot's virtual adapter, but Windows' sharing "
                "list doesn't expose it as a nameable connection.",
    "SVCFAIL": "The underlying 'Internet Connection Sharing (ICS)' Windows "
               "service (SharedAccess) could not be started.",
    "BINDEXC": "Windows refused to enable Internet Connection Sharing - this "
               "is almost always caused by the SharedAccess (ICS) service "
               "not actually running, or by RRAS (Routing and Remote Access) "
               "being installed/running, which blocks ICS from starting.",
    "NOTSTUCK": "Sharing was enabled but Windows reverted it immediately "
                "(another network policy may be overriding it).",
    "VERIFYEXC": "Could not verify whether sharing was actually enabled.",
    "HOTSPOTDIED": "The Mobile Hotspot access point itself was silently "
                   "switched off by Windows (icssvc) as a side effect of "
                   "rebinding Internet Connection Sharing to xraytun - "
                   "Windows' Quick Settings will correctly show it as OFF "
                   "even though our first attempt reported success. Retried "
                   "once automatically; if you still see this, try again "
                   "or reboot the Wi-Fi adapter.",
}

def _describe_icsfail(value):
    """Turns a raw 'ICSFAIL:<CODE>[:<detail>]' status value into a
    human-readable explanation, keeping the raw detail for troubleshooting."""
    rest = value.split(":", 1)[1] if ":" in value else ""
    code, _, detail = rest.partition(":")
    base = _HOTSPOT_ICSFAIL_MESSAGES.get(code, f"Unknown reason ({code})")
    msg = ("Hotspot started, but could not force it to share the tunneled "
           f"(xraytun) connection - turned it back off. Reason: {base}")
    if detail:
        msg += f" [{detail[:80]}]"
    return msg

def _run_hotspot_powershell(action_script, timeout):
    """Runs a small PowerShell/WinRT snippet to query or control Mobile
    Hotspot. Returns (tag, value) where tag is one of 'STATE', 'STATUS',
    or 'ERROR', and value is the associated string payload."""
    full_script = (
        _HOTSPOT_PS_HEADER_TEMPLATE.replace("__TUN_NAME__", TUN_NAME)
        + action_script
        + _HOTSPOT_PS_FOOTER
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", full_script],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW, timeout=timeout
        )
        stdout = result.stdout.decode("utf-8", errors="ignore").strip()
        stderr = result.stderr.decode("utf-8", errors="ignore").strip()
        last_line = ""
        for line in stdout.splitlines():
            if line.strip():
                last_line = line.strip()
        if not last_line:
            last_line = stderr or "Empty response"
        if ":" in last_line:
            tag, _, value = last_line.partition(":")
            tag = tag.strip().upper()
            if tag in ("STATE", "STATUS", "ERROR"):
                return tag, value.strip()
        return "ERROR", last_line
    except subprocess.TimeoutExpired:
        logging.error("Hotspot PowerShell call timed out")
        return "ERROR", "Timeout"
    except Exception as e:
        logging.error(f"Hotspot PowerShell call failed: {e}")
        return "ERROR", str(e)

def hotspot_get_state():
    """Returns (state, message) where state is 'on'/'off'/'transition'/
    'unknown'/'error'."""
    tag, value = _run_hotspot_powershell(_HOTSPOT_STATE_ACTION, HOTSPOT_STATE_POLL_TIMEOUT)
    if tag == "STATE":
        return _HOTSPOT_STATE_MAP.get(value, "unknown"), ""
    return "error", value

def hotspot_start():
    """Returns (success, message)."""
    tag, value = _run_hotspot_powershell(_HOTSPOT_START_ACTION, HOTSPOT_ACTION_TIMEOUT)
    if tag != "STATUS":
        return False, value
    if value.startswith("ICSFAIL"):
        return False, _describe_icsfail(value)
    msg = _HOTSPOT_STATUS_MESSAGES.get(value, f"Status {value}")
    return value in ("0", "9"), msg

def hotspot_stop():
    """Returns (success, message)."""
    tag, value = _run_hotspot_powershell(_HOTSPOT_STOP_ACTION, HOTSPOT_ACTION_TIMEOUT)
    if tag != "STATUS":
        return False, value
    msg = _HOTSPOT_STATUS_MESSAGES.get(value, f"Status {value}")
    return value == "0", msg

hotspot_state = "unknown"
hotspot_busy = False
hotspot_last_message = ""
hotspot_enabled_by_us = False

def hotspot_toggle_worker():
    """Runs in a background thread so the 1-second speed display never
    blocks on the (relatively slow) PowerShell/WinRT round-trip.

    Note: the caller is responsible for setting hotspot_busy = True BEFORE
    starting this thread (acting as a simple lock so a second 'H' press
    can't fire an overlapping toggle); this function only clears it when done.
    """
    global hotspot_state, hotspot_busy, hotspot_last_message, hotspot_enabled_by_us
    try:
        if hotspot_state == "on":
            ok, msg = hotspot_stop()
            hotspot_state = "off" if ok else "error"
            if ok:
                hotspot_enabled_by_us = False
        else:
            ok, msg = hotspot_start()
            hotspot_state = "on" if ok else "error"
            if ok:
                hotspot_enabled_by_us = True
        hotspot_last_message = msg
        if hotspot_state == "error":
            logging.error(f"Hotspot toggle failed: {msg}")
    except Exception as e:
        hotspot_last_message = str(e)
        hotspot_state = "error"
        logging.error(f"Hotspot toggle raised exception: {e}")
    finally:
        hotspot_busy = False

def hotspot_refresh_state_worker():
    """Background one-shot state query, used only when entering TUN
    monitoring so the very first frame doesn't block on PowerShell startup."""
    global hotspot_state, hotspot_last_message
    state, msg = hotspot_get_state()
    hotspot_state = state
    if state == "error":
        hotspot_last_message = msg

def proxy_menu(app_data):
    while True:
        clear_screen()
        print("=" * 65)
        print("-"*20+">PROXY SERVER MANAGEMENT<"+"-"*20)
        print("=" * 65)
        print("[1] Add Server via URL    (Support Xray Configs)")
        print("[2] View Current Server & Test Ping")
        print("[3] Remove Server")
        print("[0] Back to Main Menu")
        print("=" * 65)

        try:
            c = input("Select an option: ")
        except (KeyboardInterrupt, EOFError):
            print("\n[!] Canceled.")
            time.sleep(1)
            continue

        if c == '1':
            print("\n"+"-"*26+" Add  Server "+"-"*26)
            print("Paste your URL/Config and press Enter:")
            print(" [#] Support vless, vmess, trojan, ss")
            try:
                first_line = input("> ").strip()
                if first_line.startswith("{") and not first_line.endswith("}"):
                    lines = [first_line]
                    while True:
                        line = input()
                        if line.strip() == "":
                            break
                        lines.append(line)
                    url = "".join(lines).strip()
                else:
                    url = first_line
            except (KeyboardInterrupt, EOFError):
                print("\n[!] Canceled.")
                time.sleep(1)
                continue

            parsed_data = parse_universal_url(url)

            if parsed_data:
                app_data["custom_proxy"] = parsed_data["outbound"]
                app_data["settings"]['proxy_remark'] = parsed_data["remark"]

                if app_data["settings"]['app_mode'] == "b-proxy":
                    app_data["settings"]['app_mode'] = "v-proxy"

                save_data(app_data)
                print(f"\n[+] Imported Successfully: {fix_persian(parsed_data['remark'])}")
            else:
                print("\n[!] Invalid format or unsupported protocol.")
            time.sleep(2)

        elif c == '2':
            if app_data.get("custom_proxy"):
                try:
                    config = app_data["custom_proxy"]
                    protocol = config.get("protocol", "unknown").upper()
                    addr, port = get_outbound_endpoint(config)
                    addr = addr or "N/A"
                    port = port or 0

                    net_type = config.get("streamSettings", {}).get("network", "tcp")
                    sec_type = config.get("streamSettings", {}).get("security", "none")

                    print("\n--- Server Details ---")
                    remark = app_data["settings"].get('proxy_remark', 'Unknown')
                    print(f"Name/Remark: {fix_persian(remark)}")
                    print(f"Protocol:    {protocol}")
                    print(f"Address:     {addr}:{port}")
                    print(f"Network:     {net_type.upper()} | Security: {sec_type.upper()}")
                    print("\n" + "-" * 65)
                    print("Testing ping per adapter... please wait...\n")

                    selected = app_data["settings"].get("selected_adapters", [])
                    active_adapters = sys_mgr.get_active_adapters()

                    if not selected:
                        latency = quick_ping(addr, port)
                        print(f" Global Ping: {latency}")
                    else:
                        for name in selected:
                            ip_addr = active_adapters.get(name)
                            if ip_addr:
                                lat = quick_ping(addr, port, bind_ip=ip_addr)
                                print(f" [*] {name:<18} ({ip_addr}) | Ping: {lat}")
                            else:
                                print(f" [*] {name:<18} | Offline/No IP")

                except Exception as e:
                    logging.error(f"Error displaying server details: {e}")
                    print("\n[!] Error reading config structure.")
            else:
                print("\n[!] No server configured yet.")
            try:
                input("\nPress Enter to continue...")
            except (KeyboardInterrupt, EOFError):
                pass

        elif c == '3':
            if app_data.get("custom_proxy"):
                app_data["custom_proxy"] = None

            app_data["settings"]['app_mode'] = "b-proxy"
            app_data["settings"]['proxy_remark'] = ""
            save_data(app_data)
            print("\n[+] Server deleted. App mode changed to B-PROXY (Load balancing only).")
            time.sleep(2)

        elif c == '0':
            break

def generate_xray_config(app_data, adapters_dict):
    outbounds = []
    selected = app_data["settings"]["selected_adapters"]
    app_mode = app_data["settings"]["app_mode"]
    resolved_proxy_ip = None

    if app_mode in ["v-proxy", "tun"]:
        custom_proxy = app_data.get("custom_proxy")
        if not custom_proxy:
            print("\n[!] No proxy server found! Please go to Proxy Management to add one.")
            print("    Or switch to B-PROXY mode for raw network load-balancing.")
            return False, None

        custom_outbound = copy.deepcopy(custom_proxy)
        proxy_address_original, _ = get_outbound_endpoint(custom_outbound)

        if proxy_address_original:
            try:
                socket.setdefaulttimeout(2.0)
                resolved_proxy_ip = socket.gethostbyname(proxy_address_original)
                set_outbound_address(custom_outbound, resolved_proxy_ip)
            except Exception as e:
                logging.error(f"DNS resolution failed for proxy: {e}")
                resolved_proxy_ip = proxy_address_original
            finally:
                socket.setdefaulttimeout(None)

        for adapter_name in selected:
            ip_addr = adapters_dict.get(adapter_name)
            if ip_addr:
                adapter_outbound = copy.deepcopy(custom_outbound)
                adapter_outbound["tag"] = adapter_name
                adapter_outbound["sendThrough"] = ip_addr
                outbounds.append(adapter_outbound)

    else:
        for adapter_name in selected:
            ip_addr = adapters_dict.get(adapter_name)
            if ip_addr:
                outbounds.append({
                    "tag": adapter_name,
                    "protocol": "freedom",
                    "sendThrough": ip_addr,
                    "streamSettings": {
                        "sockopt": {
                            "tcpKeepAliveInterval": 15,
                            "tcpNoDelay": True
                        }
                    }
                })

    if not outbounds:
        print("\n[!] None of the selected adapters are currently active/online.")
        return False, None

    outbounds.append({
        "tag": "dns-out",
        "protocol": "dns"
    })

    inbounds = [
        {
            "port": app_data["settings"]["local_port"],
            "listen": "127.0.0.1",
            "protocol": "socks",
            "settings": {"auth": "noauth", "udp": True},
            "sniffing": {"enabled": False if app_mode == "b-proxy" else True, "destOverride": ["http", "tls"]}
        }
    ]

    if app_mode == "tun":
        inbounds.append({
            "tag": "tun-in",
            "protocol": "tun",
            "settings": {
                "name": TUN_NAME,
                "mtu": 1350
            },
            "sniffing": {
                "enabled": True,
                "destOverride": ["http", "tls", "fakedns"]
            }
        })

    routing_rules = []

    if app_mode == "tun":
        routing_rules.append({
            "type": "field",
            "inboundTag": ["tun-in"],
            "port": 53,
            "network": "udp",
            "outboundTag": "dns-out"
        })
    elif app_mode == "b-proxy" and selected:
        routing_rules.append({
            "type": "field",
            "port": 53,
            "network": "tcp,udp",
            "outboundTag": selected[0]
        })

    routing_rules.append({
        "type": "field",
        "network": "tcp,udp",
        "balancerTag": "main_balancer"
    })

    config = {
        "log": {"loglevel": "warning"},
        "dns": {
            "servers": ["fakedns", "8.8.8.8", "1.1.1.1", "localhost"],
            "queryStrategy": "UseIP"
        },
        "fakedns": [
            {
                "ipPool": "198.18.0.0/15",
                "poolSize": 65535
            }
        ],
        "inbounds": inbounds,
        "outbounds": outbounds,
        "observatory": {
            "subjectSelector": selected,
            "probeURL": "http://1.1.1.1",
            "probeInterval": "2s"
        },
        "routing": {
            "domainStrategy": "IPIfNonMatch",
            "balancers": [{"tag": "main_balancer", "selector": selected, "strategy": {"type": "roundRobin"}}],
            "rules": routing_rules
        }
    }

    try:
        with open(XRAY_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4)
        try:
            os.chmod(XRAY_CONFIG_FILE, 0o600)
        except Exception:
            pass
        return True, resolved_proxy_ip
    except Exception as e:
        logging.error(f"Failed to write config.json: {e}")
        return False, None

monitoring_active = False
adapter_pings = {}

def ping_worker_per_adapter(host, port, adapters_dict, selected_adapters):
    global adapter_pings, monitoring_active

    target_ip = host
    try:
        socket.setdefaulttimeout(2.0)
        target_ip = socket.gethostbyname(host)
    except Exception:
        pass
    finally:
        socket.setdefaulttimeout(None)

    while monitoring_active:
        for name in selected_adapters:
            if not monitoring_active:
                break
            ip_addr = adapters_dict.get(name)
            if ip_addr:
                adapter_pings[name] = quick_ping(target_ip, port, bind_ip=ip_addr)
            else:
                adapter_pings[name] = "Offline"

        for _ in range(20):
            if not monitoring_active:
                break
            time.sleep(0.1)

def ip_monitor_worker(selected_adapters, app_data, proxy_ip):
    global xray_process
    global monitoring_active
    last_adapters_dict = sys_mgr.get_active_adapters()
    while monitoring_active:
        time.sleep(5)
        if not monitoring_active:
            break
        current_adapters_dict = sys_mgr.get_active_adapters()
        changed = False
        for adapter in selected_adapters:
            old_ip = last_adapters_dict.get(adapter)
            new_ip = current_adapters_dict.get(adapter)
            if old_ip != new_ip:
                changed = True
                break
        if changed:
            success, _ = generate_xray_config(app_data, current_adapters_dict)
            if success:
                try:
                    if xray_process:
                        xray_process.kill()
                except Exception:
                    pass
                kill_zombie_xray()
                new_proc = start_xray_process()
                if new_proc:
                    xray_process = new_proc
                last_adapters_dict = current_adapters_dict

def speed_monitor(selected_adapters, app_data, proxy_ip=None):
    global monitoring_active, adapter_pings
    global hotspot_state, hotspot_busy, hotspot_last_message, hotspot_enabled_by_us
    monitoring_active = True
    adapter_pings = {adp: "N/A" for adp in selected_adapters}
    proxy_port = None

    app_mode = app_data["settings"].get("app_mode")
    monitored_adapters = selected_adapters.copy()
    if app_mode == "tun" and TUN_NAME not in monitored_adapters:
        monitored_adapters.append(TUN_NAME)

    adapters_dict = sys_mgr.get_active_adapters()

    if app_mode in ["v-proxy", "tun"] and app_data.get("custom_proxy"):
        try:
            _, proxy_port = get_outbound_endpoint(app_data["custom_proxy"])
            if proxy_ip and proxy_port:
                threading.Thread(
                    target=ping_worker_per_adapter,
                    args=(proxy_ip, proxy_port, adapters_dict, selected_adapters),
                    daemon=True
                ).start()
        except Exception as e:
            logging.error(f"Failed starting ping worker: {e}")

    hotspot_supported = (app_mode == "tun")
    if hotspot_supported:
        hotspot_state = "unknown"
        hotspot_busy = False
        hotspot_last_message = ""
        hotspot_enabled_by_us = False
        threading.Thread(target=hotspot_refresh_state_worker, daemon=True).start()

    def get_bytes():
        try:
            counters = psutil.net_io_counters(pernic=True)
            return {name: (counters[name].bytes_recv, counters[name].bytes_sent) for name in monitored_adapters if name in counters}
        except Exception as e:
            logging.error(f"Failed reading net_io_counters: {e}")
            return {}

    initial_bytes = get_bytes()
    old_bytes = initial_bytes

    first_run = True
    previous_lines_to_print = 0

    try:
        while monitoring_active:
            hotkeys = poll_hotkeys()
            if hotspot_supported and 'h' in hotkeys and not hotspot_busy:
                hotspot_busy = True
                threading.Thread(target=hotspot_toggle_worker, daemon=True).start()

            time.sleep(1)
            new_bytes = get_bytes()
            total_recv_diff_kb = 0
            total_accumulated_bytes = 0

            output_lines = []
            adapter_lines = []
            tun_speed_val = "0.0 KB/s"

            for name in monitored_adapters:
                recv_diff_kb = 0
                accumulated_bytes = 0
                if name in new_bytes and name in old_bytes:
                    recv_diff_kb = (new_bytes[name][0] - old_bytes[name][0]) / 1024.0
                    if recv_diff_kb < 0:
                        recv_diff_kb = 0

                    if name in initial_bytes:
                        accumulated_bytes = (new_bytes[name][0] - initial_bytes[name][0])
                        if accumulated_bytes < 0:
                            accumulated_bytes = 0

                    if name != TUN_NAME:
                        total_recv_diff_kb += recv_diff_kb
                        total_accumulated_bytes += accumulated_bytes

                adp_speed = format_speed(recv_diff_kb)
                adp_data = format_size(accumulated_bytes)

                if name == TUN_NAME:
                    line_str = f" [*] {name:<17} | Speed: {adp_speed:<12} | Data: {adp_data}"
                    tun_speed_val = adp_speed
                else:
                    p_val = adapter_pings.get(name, "N/A")
                    name_display = (name[:12] + "..") if len(name) > 14 else name
                    label = f"[*] {name_display} ^{p_val}"
                    line_str = f" {label:<24} | Speed: {adp_speed:<12} | Data: {adp_data}"
                    adapter_lines.append(line_str)

            global_speed = format_speed(total_recv_diff_kb)
            global_data = format_size(total_accumulated_bytes)

            speed_str = f"Total Speed: {global_speed}"
            data_str = f"Total Data: {global_data}"
            tun_str = f"TUN Speed: {tun_speed_val}" if app_mode == "tun" else ""

            output_lines.append("-" * 65)

            if tun_str:
                output_lines.append(f"[GLOBAL] {speed_str:<26} |  {tun_str}")
            else:
                output_lines.append(f"[GLOBAL] {speed_str:<26}")

            if hotspot_supported:
                hotspot_display = {
                    "on": "ON", "off": "OFF", "transition": "...",
                    "unknown": "checking...", "error": "ERROR",
                }.get(hotspot_state, "unknown")
                if hotspot_busy:
                    hotspot_display += " (working...)"
                output_lines.append(f"         {data_str:<26} |  Hotspot: {hotspot_display}")
            else:
                output_lines.append(f"         {data_str:<26}")

            output_lines.append("")
            output_lines.append("-" * 65)
            output_lines.extend(adapter_lines)

            if hotspot_supported:
                output_lines.append("")
                output_lines.append("-" * 65)
                hint = "[+] Press [H] at any time to turn Windows Mobile Hotspot ON/OFF"
                output_lines.append(hint)
                if hotspot_state == "error" and hotspot_last_message:
                    for line in textwrap.wrap(f"Last error: {hotspot_last_message}", width=78):
                        output_lines.append(line)

            current_lines_to_print = len(output_lines)

            if not first_run:
                sys.stdout.write(f"\033[{previous_lines_to_print}A\033[J")
            else:
                first_run = False

            sys.stdout.write("\n".join(output_lines) + "\n")
            sys.stdout.flush()

            previous_lines_to_print = current_lines_to_print
            old_bytes = new_bytes

    except KeyboardInterrupt:
        monitoring_active = False
        raise

def start_xray_process():
    """Starts the Xray process and verifies it didn't crash immediately.
    Returns the Popen object, or None on failure (with an error message printed)."""
    if not os.path.exists(XRAY_EXECUTABLE):
        print(f"\n[!] ERROR: '{XRAY_EXECUTABLE}' not found in the script directory!")
        return None

    try:
        xray_process = subprocess.Popen(
            [XRAY_EXECUTABLE, "-c", XRAY_CONFIG_FILE],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW
        )
    except Exception as e:
        logging.error(f"Failed to launch Xray: {e}")
        print(f"\n[!] Failed to launch Xray: {e}")
        return None

    elapsed = 0.0
    step = 0.1
    while elapsed < XRAY_START_GRACE_SECONDS:
        if xray_process.poll() is not None:
            break
        time.sleep(step)
        elapsed += step

    exit_code = xray_process.poll()
    if exit_code is not None:
        stderr_output = ""
        try:
            stderr_output = xray_process.stderr.read().decode("utf-8", errors="ignore")
        except Exception:
            pass
        logging.error(f"Xray exited immediately (code {exit_code}): {stderr_output}")
        print(f"\n[!] Xray failed to start (exit code {exit_code}).")
        if stderr_output.strip():
            print("--- Xray output ---")
            print(stderr_output.strip()[:1500])
            print("-------------------")
        return None

    return xray_process

def main():
    app_data = load_data()
    global xray_process

    while True:
        clear_screen()
        print("=" * 65)
        print("    ^𝚝.𝚖𝚎 -> NETWORK LOAD BALANCER & XRAY MANAGER <- 𝚖_𝚛𝚊𝚎𝟹𝟶^  ")
        print("=" * 65)

        mode_str = app_data["settings"]['app_mode'].upper()
        if mode_str == "B-PROXY":
            mode_display = "B-PROXY (Balancing / No VPN)"
        else:
            mode_display = mode_str

        print(f"[1] Select Adapters       (Current: {app_data['settings']['selected_adapters']})")
        print(f"[2] Edit Local Port       (Current: {app_data['settings']['local_port']})")
        print(f"[3] Proxy Server Settings")
        print(f"[4] Toggle Routing Mode   (Current: {mode_display})")
        print(f"[5] START")
        print(f"[0] EXIT")
        print("=" * 65)

        try:
            choice = input("Select an option: ")
        except (KeyboardInterrupt, EOFError):
            print("\n[!] Canceled.")
            time.sleep(1)
            continue

        if choice == '1':
            adapters = sys_mgr.get_active_adapters()
            print("\nAvailable Adapters:")
            adapter_names = list(adapters.keys())
            if not adapter_names:
                print("  (No active network adapters found.)")
                try:
                    input("\nPress Enter to continue...")
                except (KeyboardInterrupt, EOFError):
                    pass
                continue

            for i, name in enumerate(adapter_names):
                print(f"  {i}. {name} ({adapters[name]})")

            try:
                sel = input("\nEnter numbers separated by comma: ")
            except (KeyboardInterrupt, EOFError):
                print("\n[!] Canceled.")
                time.sleep(1)
                continue

            try:
                indices = [int(x.strip()) for x in sel.split(',') if x.strip() != ""]
                invalid = [i for i in indices if i < 0 or i >= len(adapter_names)]
                if not indices:
                    print("\n[!] No selection made.")
                    time.sleep(1.5)
                    continue
                if invalid:
                    print(f"\n[!] Invalid index(es): {invalid}. No changes made.")
                    time.sleep(2)
                    continue
                seen = set()
                chosen = []
                for i in indices:
                    name = adapter_names[i]
                    if name not in seen:
                        seen.add(name)
                        chosen.append(name)
                app_data["settings"]['selected_adapters'] = chosen
                save_data(app_data)
                print(f"\n[+] Selected adapters: {chosen}")
                time.sleep(1.5)
            except ValueError:
                print("\n[!] Please enter valid comma-separated numbers.")
                time.sleep(2)
            except Exception as e:
                logging.error(f"Adapter selection error: {e}")
                print("\n[!] An unexpected error occurred.")
                time.sleep(2)

        elif choice == '2':
            port = safe_int_input("\nEnter new local port (1-65535): ", min_val=1, max_val=65535)
            if port is not None:
                app_data["settings"]['local_port'] = port
                save_data(app_data)
                print(f"\n[+] Local port set to {port}.")
            time.sleep(1.5)

        elif choice == '3':
            proxy_menu(app_data)

        elif choice == '4':
            modes = ["b-proxy", "v-proxy", "tun"]
            current_idx = modes.index(app_data["settings"]['app_mode']) if app_data["settings"]['app_mode'] in modes else 0
            next_idx = (current_idx + 1) % len(modes)
            app_data["settings"]['app_mode'] = modes[next_idx]
            save_data(app_data)

        elif choice == '5':
            if len(app_data["settings"]['selected_adapters']) < 1:
                print("\n[!] Please select at least 1 adapter.")
                time.sleep(2)
                continue

            kill_zombie_xray()

            adapters_dict = sys_mgr.get_active_adapters()
            success, proxy_ip = generate_xray_config(app_data, adapters_dict)
            if not success:
                time.sleep(4)
                continue

            if app_data["settings"]['app_mode'] in ["v-proxy", "b-proxy"]:
                sys_mgr.set_system_proxy(app_data["settings"]['local_port'], enable=True)

            xray_process = start_xray_process()
            if xray_process is None:
                sys_mgr.revert_all()
                delete_temp_config()
                time.sleep(3)
                continue

            tun_ready = True
            if app_data["settings"]['app_mode'] == "tun":
                print("\nWaiting for TUN adapter to initialize...")
                tun_ready = False
                attempts = int(TUN_WAIT_TIMEOUT_SECONDS / TUN_POLL_INTERVAL_SECONDS)
                for _ in range(attempts):
                    if sys_mgr.is_tun_ready():
                        sys_mgr.apply_tun_routes(proxy_ip, app_data["settings"]['selected_adapters'])
                        tun_ready = True
                        break
                    time.sleep(TUN_POLL_INTERVAL_SECONDS)

                if not tun_ready:
                    print("\n[!] TUN adapter did not come up in time. Aborting to avoid an unprotected connection.")
                    try:
                        if xray_process:
                            xray_process.kill()
                    except Exception:
                        pass
                    kill_zombie_xray()
                    sys_mgr.revert_all()
                    delete_temp_config()
                    time.sleep(3)
                    continue

            print("\n" + "=" * 65)
            print("Monitoring active... (Press Ctrl+C to STOP and return to menu)\n")
            if app_data["settings"]['app_mode'] in ["v-proxy", "tun"]:
                remark = app_data["settings"].get("proxy_remark", "Unknown")
                print(f"Connected to: {fix_persian(remark)}")

            threading.Thread(
                target=ip_monitor_worker,
                args=(app_data["settings"]['selected_adapters'], app_data, proxy_ip),
                daemon=True
            ).start()

            try:
                speed_monitor(app_data["settings"]['selected_adapters'], app_data, proxy_ip)
            except KeyboardInterrupt:
                global monitoring_active, hotspot_enabled_by_us
                monitoring_active = False

                sys.stdout.write("\n\033[0m\033[J")
                sys.stdout.flush()
                print("\n[!] Stopping Xray and restoring network settings...")
                print("    Please wait (this may take 2-4 seconds due to Windows DNS restore)...")

                if hotspot_enabled_by_us:
                    print("    Turning off Mobile Hotspot (was enabled during this session)...")
                    ok, _ = hotspot_stop()
                    if ok:
                        hotspot_enabled_by_us = False

                try:
                    if xray_process:
                        xray_process.kill()
                except Exception:
                    pass
                
                kill_zombie_xray()
                sys_mgr.revert_all()
                delete_temp_config()

                print("[+] Successfully stopped! Returning to menu...\n")
                time.sleep(1.5)
                continue

        elif choice == '0':
            kill_zombie_xray()
            sys_mgr.revert_all()
            delete_temp_config()
            break

if __name__ == "__main__":
    main()
