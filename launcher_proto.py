#!/usr/bin/env python3
# Libre's TCG Online Launcher
# Copyright (C) 2026 LibreSoldier
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version. It is distributed WITHOUT ANY WARRANTY; see the LICENSE file or
# <https://www.gnu.org/licenses/> for details.
# SPDX-License-Identifier: GPL-3.0-or-later
"""Libre's TCG Online Launcher (shown as "PTCGO Launcher" once the game is found).

Server picker for the PTCGO 2.95.0.5815 client. "Connect" rewrites the three
connection lines of cake.cfg (the original is backed up once as
cake.cfg.launcher-backup) and starts the game. Network traffic: only the status
probe (TCP to hostname:39389 + HTTP to <versionURL>config.json/health/motd.json).

Settings… locates the game's cake.cfg (auto-detect or browse) and shows a
read-only access check. Runs on Linux (Wine), Windows and macOS with the
standard library only (macOS: use the python.org Python for a working Tk).
"""

import json
import os
import queue
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

APP_VERSION = "1.1"
BASE_NAME = "Libre's TCG Online Launcher"  # shown until the game is found
GAME_NAME = "PTCGO Launcher"                # shown once the user's own PTCGO copy is found
APP_NAME = BASE_NAME                        # current display name (dialog titles read it at call time)
CONFIG_NAME = "ptcgo-launcher"              # config folder name (same on every OS)
OLD_CONFIG_NAMES = ("Libre TCG Online Launcher", "PTCGO Launcher")  # earlier Windows/macOS names; migrated once
IS_WINDOWS = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"
IS_LINUX = not (IS_WINDOWS or IS_MAC)


def app_dir():
    """Per-user config folder, following each OS's convention."""
    if IS_WINDOWS:
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData/Roaming"))
    elif IS_MAC:
        base = Path.home() / "Library/Application Support"
    else:
        return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / CONFIG_NAME
    new = base / CONFIG_NAME
    for old in (base / name for name in OLD_CONFIG_NAMES):
        if old.is_dir() and not new.exists():
            try:
                old.rename(new)
            except OSError:
                return old
    return new


CONFIG_DIR = app_dir()
GAME_ICON_FILE = CONFIG_DIR / "game_icon.png"  # extracted from the user's own game, never distributed
SERVERS_FILE = CONFIG_DIR / "servers.json"
SETTINGS_FILE = CONFIG_DIR / "settings.json"
EXPORT_DIR = CONFIG_DIR / "exports"

# Client layout (2.95.0.5815)
EXPECTED_VERSION = "2.95.0.5815"
GAME_EXE = "Pokemon Trading Card Game Online.exe"
DATA_DIR = "Pokemon Trading Card Game Online_Data"
MAC_APP = "Pokemon Trading Card Game Online.app"
INSTALL_DIR = "PokemonTradingCardGameOnline"  # inside "%APPDATA%\\Pokémon Trading Card Game Online"
BACKUP_SUFFIX = ".launcher-backup"

# Placeholder used only when no real cake.cfg is available. The client's own
# keys (AppSecrets, version) are never embedded: they always come from the
# user's cake.cfg, which is edited in place.
CAKE_TEMPLATE = (
    "hostname=127.0.0.1\n"
    "versionURL=http://127.0.0.1:8000/\n"
    "assetURL=http://127.0.0.1:8000/\n"
)

CFG_KEYS = ("hostname", "versionURL", "assetURL")
GAME_TCP_PORT = 39389
PROBE_TIMEOUT = 2.0
UNNAMED = "Unnamed Server"

STATUS_STYLE = {  # status -> (color, short label, long label)
    "online": ("#2e9e44", "online", "online"),
    "starting": ("#ef6c00", "starting", "starting (server still loading content)"),
    "partial": ("#e0a800", "partial", "partial (game port 39389 closed)"),
    "not_ptcgo": ("#8e24aa", "not PTCGO", "answers, but is not a PTCGO server"),
    "offline": ("#c62828", "offline", "offline"),
    "testing": ("#5c6bc0", "testing…", "testing…"),
    None: ("#9e9e9e", "-", "not tested"),
}


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def relative_time(ts):
    if not ts:
        return "never"
    delta = max(0, int(time.time() - ts))
    for unit, secs in (("day", 86400), ("h", 3600), ("min", 60)):
        if delta >= secs:
            n = delta // secs
            return f"{n} {unit}{'s' if unit == 'day' and n > 1 else ''} ago"
    return "just now"


def normalize_url(url):
    url = url.strip()
    if url and not url.endswith("/"):
        url += "/"
    return url


def default_url(host):
    return f"http://{host}:8000/" if host else ""


def parse_cfg_text(text):
    """Reads hostname/versionURL/assetURL from pasted text or a whole cake.cfg.

    Accepts LF, CRLF or bare CR line endings; every other key is ignored."""
    found = {}
    canonical = {k.lower(): k for k in CFG_KEYS}
    for line in re.split(r"\r\n|\r|\n", text):
        key, sep, value = line.partition("=")
        key = canonical.get(key.strip().lower())
        if sep and key and key not in found:
            found[key] = value.strip()
    return found


def build_cake_preview(server, cake_path=None):
    """cake.cfg content with only the three connection keys replaced."""
    try:
        original = Path(cake_path).read_bytes().decode("utf-8", "surrogateescape")
        source = str(cake_path)
    except (OSError, TypeError):
        original, source = CAKE_TEMPLATE, "built-in template (game not configured - see Settings)"
    lines = original.splitlines(keepends=True)
    pending = dict.fromkeys(CFG_KEYS, True)
    for i, line in enumerate(lines):
        key = line.split("=", 1)[0]
        if key in pending and pending[key]:
            ending = line[len(line.rstrip("\r\n")):]
            lines[i] = f"{key}={server[key]}{ending}"
            pending[key] = False
    missing = [f"{k}={server[k]}\n" for k in CFG_KEYS if pending[k]]
    return "".join(missing + lines), source


def _fetch_json(url):
    """GET url -> parsed JSON. HTTPError means "reachable"; ValueError means "not JSON"."""
    with urllib.request.urlopen(url, timeout=PROBE_TIMEOUT) as resp:
        return json.loads(resp.read(65536).decode("utf-8", "replace"))


def _name_from_motd(motd):
    """'Welcome to CompletePTCGO!' -> 'CompletePTCGO'."""
    msg = motd.get("message") if isinstance(motd, dict) else None
    if isinstance(msg, dict):
        msg = msg.get("en_US") or msg.get("en") or next(iter(msg.values()), "")
    if not isinstance(msg, str):
        return ""
    msg = re.sub(r"^\s*welcome to\s+(the\s+)?", "", msg.strip(), flags=re.I).strip(" !.")
    return msg[:40]


def inspect(server):
    """Probes a server -> (status, name). See STATUS_STYLE for the statuses.

    A PTCGO server is recognised by <versionURL>config.json containing
    "serviceID": "pokemon_tcgo" (the original client config format that
    Spirit/CompletePTCGO reproduce); then /health (CompletePTCGO only) says
    whether it finished loading and motd.json gives a display name."""
    tcp_ok = http_ok = is_ptcgo = False
    ready, name = None, ""
    try:
        with socket.create_connection((server["hostname"], GAME_TCP_PORT), timeout=PROBE_TIMEOUT):
            tcp_ok = True
    except OSError:
        pass
    base = server["versionURL"]
    try:
        config = _fetch_json(base + "config.json")
        http_ok = True
        is_ptcgo = isinstance(config, dict) and config.get("serviceID") == "pokemon_tcgo"
    except urllib.error.HTTPError:
        http_ok = True
    except ValueError:
        http_ok = True  # answered, but not with JSON
    except OSError:
        pass
    if is_ptcgo:
        try:
            ready = bool(_fetch_json(base + "health").get("ready"))
        except urllib.error.HTTPError as e:
            ready = False if e.code == 503 else None  # 503 = still loading; 404 = plain Spirit
        except (OSError, ValueError, AttributeError):
            pass
        try:
            name = _name_from_motd(_fetch_json(base + "motd.json"))
        except (OSError, ValueError):
            pass
    if not http_ok and not tcp_ok:
        return "offline", ""
    if not is_ptcgo:
        return ("not_ptcgo" if http_ok else "partial"), ""
    if ready is False:
        return "starting", name
    return ("online" if tcp_ok else "partial"), name


def probe(server):
    return inspect(server)[0]


# --------------------------------------------------------------------------- #
# Finding the game (read-only)
# --------------------------------------------------------------------------- #
def find_cake_cfg(path):
    """Resolves whatever the user picked (cake.cfg, the _Data folder, the game
    folder, the .exe or the macOS .app) to the cake.cfg file, or None."""
    if not path:
        return None
    p = Path(path).expanduser()
    if p.is_file():
        if p.name.lower() == "cake.cfg":
            return p
        return find_cake_cfg(p.parent) if p.suffix.lower() == ".exe" else None
    if not p.is_dir():
        return None
    for base in (p, p / INSTALL_DIR):
        for cand in [base / "cake.cfg", base / DATA_DIR / "cake.cfg"] + sorted(base.glob("*_Data/cake.cfg")):
            if cand.is_file():
                return cand
    if p.suffix.lower() == ".app":  # macOS bundle: Contents/Resources/Data/cake.cfg expected (unverified)
        for root, dirs, files in os.walk(p):
            if "cake.cfg" in files:
                return Path(root) / "cake.cfg"
            if len(Path(root).relative_to(p).parts) >= 4:
                dirs.clear()
    return None


def _wine_prefixes(home):
    prefixes = [home / ".wine"]
    for parent in (home / "Games", home / ".local/share/wineprefixes", home / ".local/share/lutris/prefixes"):
        if parent.is_dir():
            prefixes += sorted(d for d in parent.iterdir() if d.is_dir())
    return [p for p in prefixes if (p / "drive_c").is_dir()]


def autodetect(home=None, environ=None):
    """Known install locations that contain a cake.cfg (unique, in order)."""
    home = Path(home) if home else Path.home()
    environ = os.environ if environ is None else environ
    folders = []
    if IS_WINDOWS:
        appdata = Path(environ.get("APPDATA", home / "AppData/Roaming"))
        folders += sorted(appdata.glob("Pok*mon Trading Card Game Online"))
        for var in ("ProgramFiles", "ProgramFiles(x86)"):
            if environ.get(var):
                folders += sorted(Path(environ[var]).glob("Pok*mon Trading Card Game Online*"))
    elif IS_MAC:
        folders += [Path("/Applications") / MAC_APP, home / "Applications" / MAC_APP]
    else:
        for prefix in _wine_prefixes(home):
            drive = prefix / "drive_c"
            folders += sorted(drive.glob("users/*/AppData/Roaming/Pok*mon Trading Card Game Online"))
            folders += sorted(drive.glob("Program Files*/Pok*mon Trading Card Game Online*"))
    found = []
    for folder in folders:
        cake = find_cake_cfg(folder)
        if cake and cake.resolve() not in [f.resolve() for f in found]:
            found.append(cake)
    return found


def wine_candidates(home=None):
    """System wine plus Lutris wine runners."""
    home = Path(home) if home else Path.home()
    found = []
    system = shutil.which("wine")
    if system:
        found.append(system)
    found += [str(p) for p in sorted((home / ".local/share/lutris/runners/wine").glob("*/bin/wine"))]
    return found


def inspect_game(cake_path, servers=()):
    """Read-only report on a cake.cfg: a list of (level, label, detail) rows
    (level is 'ok', 'warn' or 'err') plus the values that were read."""
    rows, info = [], {"cake": None, "version": None, "conn": {}, "match": None,
                      "exe": None, "prefix": None, "head": ""}
    cake = find_cake_cfg(cake_path)
    if not cake:
        rows.append(("err", "cake.cfg", f"not found at {cake_path}" if cake_path else "no game location set"))
        return rows, info
    info["cake"] = cake
    try:
        text = cake.read_bytes().decode("utf-8", "replace")
    except OSError as e:
        rows.append(("err", "cake.cfg", f"found but not readable ({e.strerror})"))
        return rows, info
    rows.append(("ok", "cake.cfg", f"read OK ({len(text)} bytes): {cake}"))
    writable = os.access(cake, os.W_OK) and os.access(cake.parent, os.W_OK)
    rows.append(("ok" if writable else "err", "Writable",
                 "yes (checked permissions only, nothing written)" if writable
                 else "no - the launcher won't be able to switch servers"))
    m = re.search(r"version=([0-9.]+)", text)
    info["version"] = m.group(1) if m else None
    if info["version"] == EXPECTED_VERSION:
        rows.append(("ok", "Client version", info["version"]))
    else:
        rows.append(("warn", "Client version",
                     f"{info['version'] or 'unknown'} (servers expect {EXPECTED_VERSION})"))
    info["conn"] = parse_cfg_text(text)
    info["head"] = "\n".join(f"{k}={info['conn'].get(k, '(missing)')}" for k in CFG_KEYS)
    match = next((s["name"] for s in servers
                  if all(s.get(k) == info["conn"].get(k) for k in CFG_KEYS)), None)
    info["match"] = match
    host = info["conn"].get("hostname")
    if not host:
        rows.append(("warn", "Current server", "no hostname= line"))
    else:
        rows.append(("ok", "Current server", f"{match} ({host})" if match else f"{host} (not a saved server)"))
    app = next((p for p in cake.parents if p.suffix.lower() == ".app"), None)
    exe = app if app else cake.parent.parent / GAME_EXE
    if exe.exists():
        info["exe"] = exe
        rows.append(("ok", "Game executable", str(exe)))
    else:
        rows.append(("warn", "Game executable", f"not found next to {DATA_DIR}"))
    drive_c = next((p for p in cake.parents if p.name == "drive_c"), None)
    if drive_c:
        info["prefix"] = drive_c.parent
        rows.append(("ok", "Wine prefix", str(drive_c.parent)))
    return rows, info


# --------------------------------------------------------------------------- #
# Game icon (read from the user's own copy at runtime)
# --------------------------------------------------------------------------- #
def _png_from_rgba(width, height, rgba_rows):
    """Minimal PNG encoder for 8-bit RGBA rows (top to bottom)."""
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + row for row in rgba_rows)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def _exe_icon_png(path):
    """Largest icon in a Windows .exe as PNG bytes (32-bit DIB or embedded PNG)."""
    d = Path(path).read_bytes()
    pe = struct.unpack_from("<I", d, 0x3C)[0]
    nsec, = struct.unpack_from("<H", d, pe + 6)
    optsz, = struct.unpack_from("<H", d, pe + 20)
    opt = pe + 24
    ddir = opt + (96 if struct.unpack_from("<H", d, opt)[0] == 0x10B else 112)
    res_rva, = struct.unpack_from("<I", d, ddir + 16)
    sections = [struct.unpack_from("<IIII", d, opt + optsz + i * 40 + 8) for i in range(nsec)]

    def off(rva):
        for vsize, va, rsize, raw in sections:
            if va <= rva < va + max(vsize, rsize):
                return rva - va + raw
        raise ValueError("bad RVA")

    base = off(res_rva)

    def entries(o):
        count = sum(struct.unpack_from("<HH", d, o + 12))
        return [struct.unpack_from("<II", d, o + 16 + i * 8) for i in range(count)]

    def leaf(ptr):
        while ptr & 0x80000000:
            ptr = entries(base + (ptr & 0x7FFFFFFF))[0][1]
        rva, size = struct.unpack_from("<II", d, base + ptr)
        return d[off(rva):off(rva) + size]

    icons, groups = {}, []
    for rtype, ptr in entries(base):
        if rtype in (3, 14):  # RT_ICON, RT_GROUP_ICON
            for name, p2 in entries(base + (ptr & 0x7FFFFFFF)):
                if rtype == 3:
                    icons[name] = leaf(p2)
                else:
                    groups.append(leaf(p2))
    best = None
    for grp in groups:
        for i in range(struct.unpack_from("<H", grp, 4)[0]):
            w, h, _, _, _, bpp, _, icon_id = struct.unpack_from("<BBBBHHIH", grp, 6 + i * 14)
            key = ((w or 256) * (h or 256), bpp)
            if icon_id in icons and (best is None or key > best[0]):
                best = (key, icons[icon_id])
    if not best:
        raise ValueError("no icon")
    img = best[1]
    if img[:8] == b"\x89PNG\r\n\x1a\n":
        return img
    hsize, w, h2, _, bpp = struct.unpack_from("<IiiHH", img, 0)
    if bpp != 32:
        raise ValueError(f"{bpp}-bit icon not supported")
    h, stride = h2 // 2, w * 4
    rows = []
    for y in range(h - 1, -1, -1):  # DIB rows are bottom-up, BGRA
        bgra = img[hsize + y * stride: hsize + (y + 1) * stride]
        row = bytearray(bgra)
        row[0::4], row[2::4] = bgra[2::4], bgra[0::4]
        rows.append(bytes(row))
    return _png_from_rgba(w, h, rows)


def _icns_png(app):
    """Largest PNG inside the .app's .icns (macOS, unverified)."""
    res = Path(app) / "Contents/Resources"
    best = b""
    for icns in sorted(res.glob("*.icns")):
        d = icns.read_bytes()
        pos = 8
        while pos + 8 <= len(d):
            size, = struct.unpack_from(">I", d, pos + 4)
            if size < 8:
                break
            data = d[pos + 8:pos + size]
            if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) > len(best):
                best = data
            pos += size
    if not best:
        raise ValueError("no PNG icon in .icns")
    return best


def extract_game_icon(exe, dest=GAME_ICON_FILE):
    """Writes the game's icon to dest (cached by source path and mtime). Returns dest or None."""
    try:
        exe = Path(exe)
        stamp = f"{exe}|{exe.stat().st_mtime_ns}"
        marker = dest.with_suffix(".source")
        if dest.is_file() and marker.is_file() and marker.read_text(encoding="utf-8") == stamp:
            return dest
        png = _icns_png(exe) if exe.suffix.lower() == ".app" else _exe_icon_png(exe)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(png)
        marker.write_text(stamp, encoding="utf-8")
        return dest
    except (OSError, ValueError, struct.error, IndexError):
        return None


def resource_path(name):
    """Files shipped next to the script (or inside a PyInstaller bundle)."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / name


DESKTOP_FILE = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "applications/ptcgo-launcher.desktop"
DESKTOP_MARKER = "X-Libre-TCG-Launcher=true"


def update_desktop_entry(name, icon):
    """Linux: keeps the launcher's own menu entry in sync (only if it carries our marker)."""
    if not IS_LINUX or not DESKTOP_FILE.is_file():
        return
    try:
        text = DESKTOP_FILE.read_text(encoding="utf-8")
        if DESKTOP_MARKER not in text:
            return
        new = re.sub(r"(?m)^Name=.*$", f"Name={name}", text)
        new = re.sub(r"(?m)^Icon=.*$", f"Icon={icon}", new)
        if new != text:
            DESKTOP_FILE.write_text(new, encoding="utf-8")
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Switching servers and starting the game
# --------------------------------------------------------------------------- #
def backup_path(cake):
    return cake.with_name(cake.name + BACKUP_SUFFIX)


def write_cake_cfg(cake, server):
    """Backs up cake.cfg once, rewrites only the connection lines atomically and
    reads the file back to confirm. Returns the backup path."""
    cake = Path(cake)
    backup = backup_path(cake)
    if not backup.exists():
        shutil.copy2(cake, backup)
    text, _ = build_cake_preview(server, cake)
    tmp = cake.with_name(cake.name + ".tmp")
    tmp.write_bytes(text.encode("utf-8", "surrogateescape"))
    os.replace(tmp, cake)
    written = parse_cfg_text(cake.read_bytes().decode("utf-8", "replace"))
    if any(written.get(k) != server[k] for k in CFG_KEYS):
        raise OSError("cake.cfg was written but reads back differently")
    return backup


def restore_cake_cfg(cake):
    cake = Path(cake)
    shutil.copy2(backup_path(cake), cake)


def parse_env(text):
    env = {}
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key and not key.startswith("#"):
            env[key.strip()] = value.strip()
    return env


def launch_game(info, settings):
    """Starts the client detached from the launcher. Returns a short description."""
    exe = info.get("exe")
    if not exe:
        raise OSError("game executable not found - check Settings")
    exe = Path(exe)
    if IS_MAC:
        subprocess.Popen(["open", str(exe)])
        return f"open {exe.name}"
    if IS_WINDOWS:
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen([str(exe)], cwd=str(exe.parent), creationflags=flags)
        return exe.name
    wine = settings["wine_binary"] or shutil.which("wine")
    if not wine:
        raise OSError("Wine not found - set the Wine executable in Settings")
    env = dict(os.environ)
    prefix = settings["wine_prefix"] or (str(info["prefix"]) if info.get("prefix") else "")
    if prefix:
        env["WINEPREFIX"] = prefix
    env.update(parse_env(settings["wine_env"]))
    subprocess.Popen([wine, str(exe)], cwd=str(exe.parent), env=env, start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return f"{Path(wine).name} {exe.name}"


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
class Store:
    def __init__(self, path):
        self.path = path
        self.fallback_enabled = False
        self.servers = []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            self.fallback_enabled = bool(data.get("fallback_enabled", False))
            self.servers = list(data.get("servers", []))
        except (OSError, ValueError):
            pass

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"fallback_enabled": self.fallback_enabled,
                                   "servers": self.servers}, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def find(self, name):
        return next((s for s in self.servers if s["name"] == name), None)


class Settings:
    DEFAULTS = {"game_path": "", "wine_binary": "", "wine_prefix": "", "wine_env": "WINEDEBUG=-all",
                "launch_game": True, "close_after_launch": False}

    def __init__(self, path):
        self.path = path
        self.exists = path.is_file()
        self.data = dict(self.DEFAULTS)
        try:
            self.data.update(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass

    def __getitem__(self, key):
        return self.data.get(key, self.DEFAULTS.get(key, ""))

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        self.exists = True


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
class LauncherApp:
    def __init__(self, root):
        self.root = root
        self.store = Store(SERVERS_FILE)
        self.settings = Settings(SETTINGS_FILE)
        self.status = {}          # server name -> status
        self.events = queue.Queue()
        self.editing = False
        self.auto_urls = ("", "")  # last auto-filled (versionURL, assetURL)

        self._icon_path = self._icon_img = None
        self.current_name = None  # saved server shown in the form (None = new server)
        root.title(APP_NAME)
        root.minsize(640, 0)
        self._build()
        self._refresh_server_lists()
        self._select(self.store.servers[0]["name"] if self.store.servers else None)
        self.root.after(100, self._drain_events)
        self.root.after(30000, self._tick)
        self._first_run_detect()
        self._refresh_game_line()
        self.test_all()

    def _first_run_detect(self):
        if self.settings.exists:
            return
        found = autodetect()
        if len(found) == 1:
            self.settings.data["game_path"] = str(found[0])
            self.settings.save()
            self._status("Game found automatically - see Settings… for details.")
        elif found:
            self._status(f"{len(found)} PTCGO installs found - pick one in Settings…")
        else:
            self._status("PTCGO not found automatically - set its location in Settings…")

    def _cake_path(self):
        return find_cake_cfg(self.settings["game_path"])

    def _apply_branding(self, info):
        """Libre's logo/name until the game is found; then the game's own icon and 'PTCGO Launcher'."""
        global APP_NAME
        icon = extract_game_icon(info["exe"]) if info.get("exe") else None
        APP_NAME = GAME_NAME if icon else BASE_NAME
        icon = icon or resource_path("logo.png")
        self.root.title(APP_NAME)
        if icon != self._icon_path and icon.is_file():
            try:
                self._icon_img = tk.PhotoImage(file=str(icon))
                self.root.iconphoto(True, self._icon_img)
                self._icon_path = icon
            except tk.TclError:
                pass
        update_desktop_entry(APP_NAME, icon)

    def _refresh_game_line(self):
        rows, info = inspect_game(self.settings["game_path"], self.store.servers)
        self._apply_branding(info)
        if not info["cake"]:
            self.game_lbl.configure(text="Game: ✗ not configured - open Settings…", foreground="#c62828")
            return
        ok = info["version"] == EXPECTED_VERSION
        current = info["match"] or info["conn"].get("hostname") or "?"
        self.game_lbl.configure(
            text=f"Game: {'✓' if ok else '⚠'} {info['version'] or 'unknown version'} · currently → {current}",
            foreground="#2e7d32" if ok else "#b26a00")

    # ---------- layout ----------
    def _build(self):
        pad = {"padx": 8, "pady": 4}
        main = ttk.Frame(self.root, padding=10)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)

        head = ttk.Frame(main)
        head.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(head, text="Close the game before connecting - it reads cake.cfg only at startup.",
                  foreground="#666").pack(side="left")
        ttk.Button(head, text="Settings…", command=self.open_settings).pack(side="right")

        # Saved servers
        top = ttk.LabelFrame(main, text="Saved Servers", padding=8)
        top.grid(row=1, column=0, sticky="ew")
        top.columnconfigure(0, weight=1)
        self.combo = ttk.Combobox(top, state="readonly")
        self.combo.grid(row=0, column=0, sticky="ew")
        self.combo.bind("<<ComboboxSelected>>", lambda e: self._select(self.combo.get()))
        ttk.Button(top, text="+ Add Server", command=self.add_server).grid(row=0, column=1, padx=(6, 0))
        self.dot = tk.Label(top, text="●", font=("TkDefaultFont", 14))
        self.dot.grid(row=0, column=2, padx=(8, 2))
        self.status_lbl = ttk.Label(top, width=24)
        self.status_lbl.grid(row=0, column=3, sticky="w")
        self.last_lbl = ttk.Label(top, text="Last Played: -")
        self.last_lbl.grid(row=1, column=0, columnspan=4, sticky="w", pady=(4, 0))
        self.game_lbl = ttk.Label(top, text="Game: -")
        self.game_lbl.grid(row=2, column=0, columnspan=4, sticky="w", pady=(2, 0))

        # Connection fields
        conn = ttk.LabelFrame(main, text="Connection", padding=8)
        conn.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        conn.columnconfigure(1, weight=1)
        self.vars = {k: tk.StringVar() for k in ("name",) + CFG_KEYS}
        labels = {"name": "Name (optional)", "hostname": "hostname",
                  "versionURL": "versionURL", "assetURL": "assetURL"}
        self.entries = {}
        for i, key in enumerate(("name",) + CFG_KEYS):
            ttk.Label(conn, text=labels[key]).grid(row=i, column=0, sticky="w", **pad)
            entry = ttk.Entry(conn, textvariable=self.vars[key])
            entry.grid(row=i, column=1, sticky="ew", **pad)
            self.entries[key] = entry
        self.vars["hostname"].trace_add("write", self._on_hostname_change)

        # Buttons
        bar = ttk.Frame(main)
        bar.grid(row=3, column=0, sticky="ew", pady=8)
        self.btn_connect = ttk.Button(bar, text="Connect", command=self.connect)
        self.btn_connect.pack(side="left")
        self.btn_save = ttk.Button(bar, text="Save", command=self.save)
        self.btn_save.pack(side="left", padx=(6, 0))
        self.btn_edit = ttk.Button(bar, text="Edit", command=self.toggle_edit)
        self.btn_edit.pack(side="left", padx=(6, 0))
        self.btn_delete = ttk.Button(bar, text="Delete", command=self.delete)
        self.btn_delete.pack(side="left", padx=(6, 0))
        ttk.Button(bar, text="Test status", command=self.test_all).pack(side="left", padx=(6, 0))

        # Sharing a server: as text (clipboard) or as a .cfg file
        share = ttk.Frame(main)
        share.grid(row=4, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(share, text="Share:").pack(side="left")
        ttk.Button(share, text="Paste Info…", command=self.paste_server).pack(side="left", padx=(6, 0))
        ttk.Button(share, text="Copy Info", command=self.copy_server).pack(side="left", padx=(6, 0))
        ttk.Separator(share, orient="vertical").pack(side="left", fill="y", padx=10)
        ttk.Button(share, text="Import File…", command=self.import_file).pack(side="left")
        ttk.Button(share, text="Export File…", command=self.export).pack(side="left", padx=(6, 0))

        # Fallback
        fb = ttk.LabelFrame(main, text="Fallback", padding=8)
        fb.grid(row=5, column=0, sticky="nsew")
        main.rowconfigure(5, weight=1)
        fb.columnconfigure(0, weight=1)
        self.fb_var = tk.BooleanVar(value=self.store.fallback_enabled)
        ttk.Checkbutton(fb, text="If the selected server is offline, use the next online server below",
                        variable=self.fb_var, command=self._on_fallback_toggle
                        ).grid(row=0, column=0, columnspan=2, sticky="w")
        self.dots = {st: self._dot_image(style[0]) for st, style in STATUS_STYLE.items()}
        cols = ("include", "name", "host", "status", "last")
        self.tree = ttk.Treeview(fb, columns=cols, show="tree headings", height=5, selectmode="browse")
        self.tree.column("#0", width=34, minwidth=34, stretch=False, anchor="center")
        for col, title, width in (("include", "FB", 40), ("name", "Name", 170), ("host", "hostname", 140),
                                  ("status", "Status", 100), ("last", "Last Played", 100)):
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, anchor="w", stretch=col in ("name", "host"))
        self.tree.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        # Click = select the server (same as Saved Servers); double-click = connect to it.
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<Double-1>", self._on_tree_double_click)
        side = ttk.Frame(fb)
        side.grid(row=1, column=1, sticky="n", padx=(6, 0), pady=(6, 0))
        ttk.Button(side, text="▲", width=3, command=lambda: self._move(-1)).pack()
        ttk.Button(side, text="▼", width=3, command=lambda: self._move(1)).pack(pady=(4, 0))
        ttk.Button(side, text="Use FB", command=self._toggle_include).pack(pady=(4, 0))
        ttk.Label(fb, text="Order = fallback priority. FB = included in fallback. Click to select, double-click to connect.",
                  foreground="#666").grid(row=2, column=0, sticky="w", pady=(4, 0))

        # width=1: long messages (e.g. file paths) are clipped instead of widening the window
        self.statusbar = ttk.Label(main, text="", foreground="#444", width=1)
        self.statusbar.grid(row=6, column=0, sticky="ew", pady=(6, 0))

    def _dot_image(self, color, size=14):
        """Filled circle image; unlike tag colors it stays visible on selected rows."""
        img = tk.PhotoImage(width=size, height=size)
        r = (size - 2) / 2
        for y in range(size):
            for x in range(size):
                if (x - r - 0.5) ** 2 + (y - r - 0.5) ** 2 <= r * r:
                    img.put(color, (x, y))
        return img

    # ---------- state helpers ----------
    def _current_saved(self):
        return self.store.find(self.current_name) if self.current_name else None

    def _refresh_server_lists(self):
        self.combo["values"] = [s["name"] for s in self.store.servers]
        self.tree.delete(*self.tree.get_children())
        for s in self.store.servers:
            st = self.status.get(s["name"])
            self.tree.insert("", "end", iid=s["name"], image=self.dots[st], values=(
                "✓" if s.get("fallback", True) else "✗", s["name"], s["hostname"],
                STATUS_STYLE[st][1], relative_time(s.get("last_played"))))
        self._sync_tree_selection()
        self._refresh_header()
        if hasattr(self, "game_lbl"):
            self._refresh_game_line()

    def _refresh_header(self):
        saved = self._current_saved()
        st = self.status.get(saved["name"]) if saved else None
        color, _, label = STATUS_STYLE[st]
        self.dot.configure(fg=color)
        self.status_lbl.configure(text=label if saved else "")
        self.last_lbl.configure(text=f"Last Played: {relative_time(saved.get('last_played'))}"
                                if saved else "Last Played: -")

    def _set_fields_editable(self, editable):
        for entry in self.entries.values():
            entry.configure(state="normal" if editable else "readonly")

    def _sync_tree_selection(self):
        """Mirrors the current server in the Fallback list (and clears it for a new server)."""
        if self.current_name and self.tree.exists(self.current_name):
            if self.tree.selection() != (self.current_name,):
                self.tree.selection_set(self.current_name)
            self.tree.see(self.current_name)
        elif self.tree.selection():
            self.tree.selection_set(())

    def _on_tree_select(self, _event=None):
        sel = self.tree.selection()
        if sel and sel[0] != self.current_name:
            self._select(sel[0])

    def _on_tree_double_click(self, event):
        row = self.tree.identify_row(event.y)
        if not row:
            return
        if row != self.current_name:
            self._select(row)
        self.connect()

    def add_server(self):
        self._select(None)
        self.entries["hostname"].focus_set()
        self._status("New server: fill in hostname (Name is optional) and Save, or Connect directly.")

    def _select(self, name):
        """Shows a saved server (name) or an empty form for a new one (None)."""
        saved = self.store.find(name) if name else None
        self.current_name = saved["name"] if saved else None
        self.combo.set(self.current_name or "")
        self._sync_tree_selection()
        self.editing = False
        self.btn_edit.configure(text="Edit")
        for key in ("name",) + CFG_KEYS:
            self.vars[key].set(saved[key] if saved else "")
        self.auto_urls = ("", "")
        self._set_fields_editable(saved is None)
        state = "normal" if saved else "disabled"
        self.btn_edit.configure(state=state)
        self.btn_delete.configure(state=state)
        self.btn_save.configure(state="disabled" if saved else "normal")
        self._refresh_header()

    def _on_hostname_change(self, *_):
        if self._current_saved() and not self.editing:
            return
        host = self.vars["hostname"].get().strip()
        host = re.sub(r"^\w+://", "", host).split("/")[0]
        new = default_url(host)
        old_v, old_a = self.auto_urls
        # Only overwrite URL fields that are empty or still hold the previous suggestion
        if self.vars["versionURL"].get() in ("", old_v):
            self.vars["versionURL"].set(new)
        if self.vars["assetURL"].get() in ("", old_a):
            self.vars["assetURL"].set(new)
        self.auto_urls = (new, new)

    def _status(self, text):
        self.statusbar.configure(text=text)

    def _copy(self, text, what):
        if not text:
            self._status(f"{what} is empty.")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self._status(f"{what} copied to clipboard.")

    def _tick(self):
        self._refresh_server_lists()
        self.root.after(30000, self._tick)

    # ---------- validation ----------
    def _fields_server(self):
        """Validated server dict from the form, or None (after showing the error)."""
        host = re.sub(r"^\w+://", "", self.vars["hostname"].get().strip()).split("/")[0]
        server = {"name": self.vars["name"].get().strip(), "hostname": host,
                  "versionURL": normalize_url(self.vars["versionURL"].get()),
                  "assetURL": normalize_url(self.vars["assetURL"].get())}
        missing = [k for k in CFG_KEYS if not server[k]]
        if missing:
            messagebox.showerror(APP_NAME, "Fill in: " + ", ".join(missing))
            return None
        if any(c.isspace() for c in host) or ":" in host:
            messagebox.showerror(APP_NAME, "hostname must be a bare IP or domain (no port, no spaces).")
            return None
        for key in ("versionURL", "assetURL"):
            if not re.match(r"^https?://[^\s/]+", server[key]):
                messagebox.showerror(APP_NAME, f"{key} must start with http:// or https://")
                return None
        for key in CFG_KEYS:
            self.vars[key].set(server[key])
        return server

    # ---------- actions ----------
    def save(self):
        server = self._fields_server()
        if not server:
            return None
        if not server["name"]:
            self._detect_name_then_save(server)
            return None
        original = self._current_saved() if self.editing else None
        existing = self.store.find(server["name"])
        if existing and existing is not original:
            if not messagebox.askyesno(APP_NAME, f"Replace the saved server '{server['name']}'?"):
                return None
            self.store.servers.remove(existing)
        if original:
            server["last_played"] = original.get("last_played")
            server["fallback"] = original.get("fallback", True)
            idx = self.store.servers.index(original)
            self.store.servers[idx] = server
            if original["name"] != server["name"]:
                self.status[server["name"]] = self.status.pop(original["name"], None)
        else:
            server["last_played"] = existing.get("last_played") if existing else None
            server["fallback"] = True
            self.store.servers.append(server)
        self.store.save()
        self._refresh_server_lists()
        self._select(server["name"])
        self._status(f"Saved '{server['name']}'.")
        self._test([server])
        return server

    def _detect_name_then_save(self, server):
        """Blank name: ask the server for its name (MOTD), then save."""
        self.btn_save.configure(state="disabled")
        self._status(f"Detecting the name of {server['hostname']}…")
        threading.Thread(target=lambda: self.events.put(("autoname", inspect(server))),
                         daemon=True).start()

    def _finish_autoname(self, result):
        status, name = result
        base = name if status in ("online", "starting", "partial") and name else UNNAMED
        taken = {s["name"] for s in self.store.servers}
        current = self._current_saved() if self.editing else None
        if current:
            taken.discard(current["name"])
        name, n = base, 2
        while name in taken:
            name, n = f"{base} ({n})", n + 1
        self.btn_save.configure(state="normal")
        self.vars["name"].set(name)
        self.save()

    def toggle_edit(self):
        saved = self._current_saved()
        if not saved:
            return
        if not self.editing:
            self.editing = True
            self._set_fields_editable(True)
            self.btn_edit.configure(text="Cancel")
            self.btn_save.configure(state="normal")
            self.btn_delete.configure(state="disabled")
        else:
            self._select(saved["name"])

    def delete(self):
        saved = self._current_saved()
        if saved and messagebox.askyesno(APP_NAME, f"Delete '{saved['name']}'?"):
            self.store.servers.remove(saved)
            self.status.pop(saved["name"], None)
            self.store.save()
            self._refresh_server_lists()
            self._select(None)
            self._status(f"Deleted '{saved['name']}'.")

    def connect(self):
        saved = self._current_saved()
        if saved and not self.editing:
            target = saved
        else:
            target = self.save() if self.vars["name"].get().strip() else self._fields_server()
            if not target:
                return
        self.btn_connect.configure(state="disabled")
        self._status(f"Checking {target['hostname']}…")
        fallback = self.fb_var.get()
        candidates = [s for s in self.store.servers
                      if s.get("fallback", True) and s.get("name") != target.get("name")]

        def work():
            results = {}
            st = probe(target)
            results[target.get("name") or None] = st
            chosen = target if st == "online" else None
            if chosen is None and fallback:
                for s in candidates:
                    s_st = probe(s)
                    results[s["name"]] = s_st
                    if s_st == "online":
                        chosen = s
                        break
            self.events.put(("connect_done", target, st, chosen, results))

        threading.Thread(target=work, daemon=True).start()

    def _finish_connect(self, target, target_status, chosen, results):
        self.btn_connect.configure(state="normal")
        for name, st in results.items():
            if name:
                self.status[name] = st
        if chosen is None:
            msg = f"'{target.get('name') or target['hostname']}' looks {target_status}."
            msg += ("\nNo online server found in the fallback list." if self.fb_var.get()
                    else "\n(Fallback is off.)")
            if not messagebox.askyesno(APP_NAME, msg + "\n\nConnect anyway?"):
                self._refresh_server_lists()
                self._status("Connection cancelled.")
                return
            chosen = target
        self._apply_and_launch(chosen, fell_back=chosen is not target)

    def _apply_and_launch(self, server, fell_back):
        label = server.get("name") or server["hostname"]
        rows, info = inspect_game(self.settings["game_path"], self.store.servers)
        if not info["cake"]:
            messagebox.showerror(APP_NAME, "The game's cake.cfg was not found.\n"
                                 "Set the game location in Settings… first.")
            self._refresh_server_lists()
            return
        try:
            backup = write_cake_cfg(info["cake"], server)
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Could not write cake.cfg:\n{e}")
            self._refresh_server_lists()
            return
        saved = self.store.find(server["name"]) if server.get("name") else None
        if saved:
            saved["last_played"] = time.time()
            self.store.save()
        self._refresh_server_lists()
        note = " (fallback - the selected server was offline)" if fell_back else ""
        if not self.settings["launch_game"]:
            self._status(f"cake.cfg now points to '{label}'{note}. Backup: {backup.name}")
            return
        try:
            how = launch_game(info, self.settings)
        except OSError as e:
            messagebox.showerror(APP_NAME, f"cake.cfg points to '{label}', but the game could not be "
                                 f"started:\n{e}")
            return
        self._status(f"Connected to '{label}'{note} - starting the game ({how}).")
        if self.settings["close_after_launch"]:
            self.root.after(1500, self.root.destroy)

    def _server_text(self):
        """(cake.cfg lines, label) for the selected server or the form, or None."""
        saved = self._current_saved()
        use_saved = saved and not self.editing
        values = saved if use_saved else {k: self.vars[k].get().strip() for k in CFG_KEYS}
        if not values.get("hostname"):
            self._status("Nothing to copy/export - fill in or select a server.")
            return None
        text = "\n".join(f"{k}={values[k]}" for k in CFG_KEYS) + "\n"
        label = (saved["name"] if use_saved else self.vars["name"].get().strip()) or values["hostname"]
        return text, label

    def copy_server(self):
        result = self._server_text()
        if result:
            self._copy(result[0], "Server (cake.cfg lines)")

    def export(self):
        result = self._server_text()
        if not result:
            return
        text, label = result
        EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        path = filedialog.asksaveasfilename(
            parent=self.root, title="Export server", initialdir=str(EXPORT_DIR),
            initialfile=re.sub(r"[^\w .()-]", "_", label) + ".cfg", defaultextension=".cfg",
            filetypes=[("Server file (cake.cfg lines)", "*.cfg"), ("All files", "*")])
        if not path:
            return
        Path(path).write_text(text, encoding="utf-8")
        messagebox.showinfo(APP_NAME, f"Server exported at {path}")
        self._status(f"Server exported at {path}")

    def _fill_from_text(self, text, name="", source="Pasted text", parent=None):
        """Loads hostname/versionURL/assetURL lines into a new-server form. Returns success."""
        found = parse_cfg_text(text)
        if "hostname" not in found:
            messagebox.showerror(APP_NAME, f"{source} has no 'hostname=' line.", parent=parent or self.root)
            return False
        self._select(None)
        self.vars["hostname"].set(found["hostname"])  # triggers URL suggestions
        for key in ("versionURL", "assetURL"):
            if found.get(key):
                self.vars[key].set(found[key])
        self.vars["name"].set(name)
        self._status(f"{source} loaded - Save it (blank Name = detect from server) or Connect directly.")
        self.entries["name"].focus_set()
        return True

    def paste_server(self):
        win = tk.Toplevel(self.root)
        win.title("Paste server info")
        win.transient(self.root)
        frame = ttk.Frame(win, padding=10)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Paste the hostname / versionURL / assetURL lines:").pack(anchor="w")
        box = tk.Text(frame, width=70, height=8, font="TkFixedFont")
        box.pack(fill="both", expand=True, pady=4)
        try:
            box.insert("1.0", self.root.clipboard_get())
        except tk.TclError:
            pass
        box.focus_set()

        def apply():
            if self._fill_from_text(box.get("1.0", "end"), parent=win):
                win.destroy()

        btns = ttk.Frame(frame)
        btns.pack(fill="x")
        ttk.Button(btns, text="OK", command=apply).pack(side="right")
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right", padx=6)

    def import_file(self):
        path = filedialog.askopenfilename(
            parent=self.root, title="Import server",
            initialdir=str(EXPORT_DIR) if EXPORT_DIR.is_dir() else str(Path.home()),
            filetypes=[("Server file / cake.cfg", "*.cfg"), ("All files", "*")])
        if not path:
            return
        stem = Path(path).stem
        self._fill_from_text(Path(path).read_bytes().decode("utf-8", "replace"),
                             name="" if stem.lower() == "cake" else stem, source=Path(path).name)

    # ---------- settings ----------
    def open_settings(self):
        SettingsDialog(self)

    # ---------- fallback list ----------
    def _on_fallback_toggle(self):
        self.store.fallback_enabled = self.fb_var.get()
        self.store.save()

    def _move(self, step):
        sel = self.tree.selection()
        if not sel:
            return
        server = self.store.find(sel[0])
        i = self.store.servers.index(server)
        j = i + step
        if 0 <= j < len(self.store.servers):
            self.store.servers[i], self.store.servers[j] = self.store.servers[j], server
            self.store.save()
            self._refresh_server_lists()

    def _toggle_include(self):
        sel = self.tree.selection()
        if sel:
            server = self.store.find(sel[0])
            server["fallback"] = not server.get("fallback", True)
            self.store.save()
            self._refresh_server_lists()

    # ---------- status probing ----------
    def test_all(self):
        self._test(list(self.store.servers))

    def _test(self, servers):
        for s in servers:
            self.status[s["name"]] = "testing"
        self._refresh_server_lists()

        def work(s):
            self.events.put(("status", s["name"], probe(s)))

        for s in servers:
            threading.Thread(target=work, args=(s,), daemon=True).start()

    def _drain_events(self):
        changed = False
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "status":
                    _, name, st = event
                    if self.store.find(name):
                        self.status[name] = st
                        changed = True
                elif event[0] == "autoname":
                    self._finish_autoname(event[1])
                elif event[0] == "connect_done":
                    self._finish_connect(*event[1:])
        except queue.Empty:
            pass
        if changed:
            self._refresh_server_lists()
        self.root.after(100, self._drain_events)


class SettingsDialog:
    """Game location (+ Wine options on Linux) with a read-only access check."""

    LEVEL = {"ok": ("✓", "#2e7d32"), "warn": ("⚠", "#b26a00"), "err": ("✗", "#c62828")}

    def __init__(self, app):
        self.app = app
        st = app.settings
        win = self.win = tk.Toplevel(app.root)
        win.title("Settings")
        win.transient(app.root)
        win.minsize(620, 0)
        pad = {"padx": 6, "pady": 4}
        frame = ttk.Frame(win, padding=10)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)

        loc = ttk.LabelFrame(frame, text="Game location", padding=8)
        loc.grid(row=0, column=0, sticky="ew")
        loc.columnconfigure(0, weight=1)
        self.path_var = tk.StringVar(value=st["game_path"])
        entry = ttk.Entry(loc, textvariable=self.path_var)
        entry.grid(row=0, column=0, columnspan=4, sticky="ew", **pad)
        entry.bind("<Return>", lambda e: self.check())
        hint = ("Pick the PTCGO .app" if IS_MAC else
                "Pick the game folder, its _Data folder, the .exe or cake.cfg")
        ttk.Label(loc, text=hint, foreground="#666").grid(row=1, column=0, sticky="w", **pad)
        ttk.Button(loc, text="Browse Folder…", command=self.browse_folder).grid(row=1, column=1, **pad)
        ttk.Button(loc, text="Browse File…", command=self.browse_file).grid(row=1, column=2, **pad)
        ttk.Button(loc, text="Auto-detect", command=self.auto_detect).grid(row=1, column=3, **pad)

        chk = ttk.LabelFrame(frame, text="Check (read-only)", padding=8)
        chk.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        frame.rowconfigure(1, weight=1)
        chk.columnconfigure(0, weight=1)
        self.report = tk.Text(chk, width=88, height=12, wrap="word", relief="flat",
                              background=win.cget("background"))
        self.report.grid(row=0, column=0, sticky="nsew")
        for level, (_, color) in self.LEVEL.items():
            self.report.tag_configure(level, foreground=color)
        self.report.tag_configure("mono", font="TkFixedFont")
        ttk.Button(chk, text="Check again", command=self.check).grid(row=1, column=0, sticky="e", pady=(4, 0))

        self.wine_vars = {}
        if IS_LINUX:
            wine = ttk.LabelFrame(frame, text="Wine (Linux)", padding=8)
            wine.grid(row=2, column=0, sticky="ew", pady=(8, 0))
            wine.columnconfigure(1, weight=1)
            self.wine_vars["binary"] = tk.StringVar(value=st["wine_binary"])
            self.wine_vars["prefix"] = tk.StringVar(value=st["wine_prefix"])
            ttk.Label(wine, text="Wine executable").grid(row=0, column=0, sticky="w", **pad)
            ttk.Combobox(wine, textvariable=self.wine_vars["binary"], values=wine_candidates()
                         ).grid(row=0, column=1, sticky="ew", **pad)
            ttk.Button(wine, text="Browse…", command=self.browse_wine).grid(row=0, column=2, **pad)
            ttk.Label(wine, text="Wine prefix").grid(row=1, column=0, sticky="w", **pad)
            ttk.Entry(wine, textvariable=self.wine_vars["prefix"]).grid(row=1, column=1, sticky="ew", **pad)
            ttk.Label(wine, text="(auto from location)", foreground="#666").grid(row=1, column=2, **pad)
            ttk.Label(wine, text="Environment\n(KEY=value per line)").grid(row=2, column=0, sticky="nw", **pad)
            self.env_box = tk.Text(wine, height=3, font="TkFixedFont")
            self.env_box.insert("1.0", st["wine_env"])
            self.env_box.grid(row=2, column=1, columnspan=2, sticky="ew", **pad)

        opts = ttk.LabelFrame(frame, text="On Connect", padding=8)
        opts.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        self.launch_var = tk.BooleanVar(value=bool(st["launch_game"]))
        self.close_var = tk.BooleanVar(value=bool(st["close_after_launch"]))
        ttk.Checkbutton(opts, text="Open the game on Connect", variable=self.launch_var
                        ).pack(anchor="w")
        ttk.Checkbutton(opts, text="Close the launcher after starting the game", variable=self.close_var
                        ).pack(anchor="w")

        btns = ttk.Frame(frame)
        btns.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        self.btn_restore = ttk.Button(btns, text="Restore original cake.cfg", command=self.restore)
        self.btn_restore.pack(side="left")
        ttk.Label(btns, text=f"{BASE_NAME} v{APP_VERSION}", foreground="#888").pack(side="left", padx=12)
        ttk.Button(btns, text="Save", command=self.save).pack(side="right")
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right", padx=6)
        self.check()

    def _set_path(self, path):
        self.path_var.set(str(path))
        self.check()

    def browse_folder(self):
        path = filedialog.askdirectory(parent=self.win, title="PTCGO folder",
                                       initialdir=self.path_var.get() or str(Path.home()))
        if path:
            self._set_path(path)

    def browse_file(self):
        path = filedialog.askopenfilename(parent=self.win, title="cake.cfg or the game .exe",
                                          filetypes=[("cake.cfg / game exe", "*.cfg *.exe"), ("All files", "*")])
        if path:
            self._set_path(path)

    def browse_wine(self):
        path = filedialog.askopenfilename(parent=self.win, title="Wine executable")
        if path:
            self.wine_vars["binary"].set(path)

    def auto_detect(self):
        found = autodetect()
        if not found:
            messagebox.showinfo(APP_NAME, "No PTCGO install found in the usual places.\n"
                                "Use Browse to point to it.", parent=self.win)
        elif len(found) == 1:
            self._set_path(found[0])
        else:
            self._choose(found)

    def _choose(self, found):
        pick = tk.Toplevel(self.win)
        pick.title("Several installs found")
        pick.transient(self.win)
        lb = tk.Listbox(pick, width=100, height=min(8, len(found)))
        for f in found:
            lb.insert("end", str(f))
        lb.selection_set(0)
        lb.pack(fill="both", expand=True, padx=10, pady=10)

        def use():
            if lb.curselection():
                self._set_path(found[lb.curselection()[0]])
            pick.destroy()

        lb.bind("<Double-1>", lambda e: use())
        ttk.Button(pick, text="Use selected", command=use).pack(anchor="e", padx=10, pady=(0, 10))

    def check(self):
        rows, info = inspect_game(self.path_var.get().strip(), self.app.store.servers)
        self.info = info
        box = self.report
        box.configure(state="normal")
        box.delete("1.0", "end")
        for level, label, detail in rows:
            mark, _ = self.LEVEL[level]
            box.insert("end", f"{mark} {label}: ", level)
            box.insert("end", f"{detail}\n")
        if info["head"]:
            box.insert("end", "\nFirst lines read from cake.cfg:\n")
            box.insert("end", info["head"] + "\n", "mono")
        box.configure(state="disabled")
        has_backup = bool(info["cake"]) and backup_path(info["cake"]).exists()
        self.btn_restore.configure(state="normal" if has_backup else "disabled")
        if has_backup:
            box.configure(state="normal")
            box.insert("end", f"\nBackup of the original: {backup_path(info['cake']).name}\n")
            box.configure(state="disabled")
        if IS_LINUX and info["prefix"] and not self.wine_vars["prefix"].get():
            self.wine_vars["prefix"].set(str(info["prefix"]))
        if IS_LINUX and not self.wine_vars["binary"].get() and wine_candidates():
            self.wine_vars["binary"].set(wine_candidates()[0])

    def restore(self):
        cake = self.info["cake"]
        if not cake or not messagebox.askyesno(
                APP_NAME, f"Put back the cake.cfg saved before the launcher's first change?\n\n{cake}",
                parent=self.win):
            return
        try:
            restore_cake_cfg(cake)
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Restore failed:\n{e}", parent=self.win)
            return
        self.check()
        self.app._refresh_game_line()
        self.app._status("Original cake.cfg restored.")

    def save(self):
        self.check()
        path = self.path_var.get().strip()
        if path and not self.info["cake"]:
            if not messagebox.askyesno(APP_NAME, "No cake.cfg found at that location. Save anyway?",
                                       parent=self.win):
                return
        data = self.app.settings.data
        data["game_path"] = str(self.info["cake"] or path)
        data["launch_game"] = self.launch_var.get()
        data["close_after_launch"] = self.close_var.get()
        if IS_LINUX:
            data["wine_binary"] = self.wine_vars["binary"].get().strip()
            data["wine_prefix"] = self.wine_vars["prefix"].get().strip()
            data["wine_env"] = self.env_box.get("1.0", "end").strip()
        self.app.settings.save()
        self.app._refresh_game_line()
        self.app._status("Settings saved.")
        self.win.destroy()


def main():
    root = tk.Tk(className="ptcgo-launcher")  # WM_CLASS "Ptcgo-launcher" (matches the .desktop entry)
    LauncherApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
