#!/usr/bin/env python3
"""スマホから Pi に文字やキーを送る入力用ページ。

起動: python3 ~/phone-input/server.py
スマホで http://192.168.0.200:8765/?t=<トークン> を開く（トークンは ~/phone-input/token）
受け取った文字は wtype で今フォーカスしているウィンドウに打ち込む。
WSL で動かしたときは、Windows 側で今フォーカスしているウィンドウに送る（win_input.ps1 を常駐させる）。
"""
import base64
import json
import os
import secrets
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PORT = 8765
BASE = Path(__file__).resolve().parent
TOKEN_FILE = BASE / "token"
PAGE = (BASE / "index.html").read_bytes()
IS_WSL = bool(os.environ.get("WSL_DISTRO_NAME"))

if not TOKEN_FILE.exists():
    TOKEN_FILE.write_text(secrets.token_urlsafe(8))
    TOKEN_FILE.chmod(0o600)
TOKEN = TOKEN_FILE.read_text().strip()

# SSH や tmux から起動してもテレビの画面に届くように
ENV = dict(os.environ)
ENV.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
ENV.setdefault("WAYLAND_DISPLAY", "wayland-0")

# ボタン名 → wtype の引数
KEYS = {
    "Enter": ["-k", "Return"],
    "BackSpace": ["-k", "BackSpace"],
    "Tab": ["-k", "Tab"],
    "ShiftTab": ["-M", "shift", "-k", "Tab", "-m", "shift"],
    "Esc": ["-k", "Escape"],
    "Up": ["-k", "Up"],
    "Down": ["-k", "Down"],
    "Left": ["-k", "Left"],
    "Right": ["-k", "Right"],
    "Space": ["-k", "space"],
    "F7": ["-k", "F7"],
    "F8": ["-k", "F8"],
    "CtrlC": ["-M", "ctrl", "-k", "c", "-m", "ctrl"],
    "CtrlL": ["-M", "ctrl", "-k", "l", "-m", "ctrl"],
    "PageUp": ["-k", "Prior"],
    "PageDown": ["-k", "Next"],
}


# ボタン名 → Windows の仮想キーコード（修飾キー, キー）。16 進
WIN_KEYS = {
    "Enter": ([], 0x0D), "BackSpace": ([], 0x08), "Tab": ([], 0x09), "ShiftTab": ([0x10], 0x09),
    "Esc": ([], 0x1B), "Up": ([], 0x26), "Down": ([], 0x28), "Left": ([], 0x25), "Right": ([], 0x27),
    "Space": ([], 0x20), "F7": ([], 0x76), "F8": ([], 0x77),
    "CtrlC": ([0x11], 0x43), "CtrlL": ([0x11], 0x4C), "PageUp": ([], 0x21), "PageDown": ([], 0x22),
}


class WinInput:
    """WSL から Windows に入力を送る。PowerShell を 1 つ常駐させて 1 行 1 命令で渡す
    （毎回起動すると 1 回 0.5 秒ほどかかり、タッチパッドがカクつくため）"""

    def __init__(self):
        self.proc = None
        self.lock = threading.Lock()

    def _start(self):
        script = (BASE / "win_input.ps1").read_text(encoding="utf-8")
        enc = base64.b64encode(script.encode("utf-16-le")).decode()
        self.proc = subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-EncodedCommand", enc],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1, cwd="/mnt/c",  # WSL 側のパスのままだと「パスを変換できない」と出る
        )
        if self.proc.stdout.readline().strip() != "ready":
            raise RuntimeError("Windows 側の入力ヘルパーを起動できませんでした")

    def send(self, line):
        with self.lock:
            for attempt in range(2):  # ヘルパーが落ちていたら 1 回だけ起動し直す
                if self.proc is None or self.proc.poll() is not None:
                    self._start()
                try:
                    self.proc.stdin.write(line + "\n")
                    self.proc.stdin.flush()
                    res = self.proc.stdout.readline().strip()
                except (BrokenPipeError, OSError):
                    res = ""
                if res == "ok":
                    return
                if res.startswith("err "):
                    raise RuntimeError(res[4:])
                self.proc = None
            raise RuntimeError("Windows 側の入力ヘルパーが応答しません")


win = WinInput() if IS_WSL else None


def type_text(text):
    if win:
        return win.send("text " + base64.b64encode(text.encode()).decode())
    wtype(["--", text])


def press(key):
    if win:
        mods, vk = WIN_KEYS[key]
        return win.send(f"key {','.join(f'{m:X}' for m in mods) or '-'} {vk:X}")
    wtype(KEYS[key])


def mouse_move(dx, dy):
    if win:
        return win.send(f"move {dx} {dy}")
    wlrctl(["pointer", "move", str(dx), str(dy)])


def mouse_scroll(n):
    if win:
        return win.send(f"scroll {n}")
    wlrctl(["pointer", "scroll", str(n), "0"])


def mouse_click(button):
    if win:
        return win.send(f"click {button}")
    wlrctl(["pointer", "click", button])


def lan_url():
    """スマホから開く URL（この PC の LAN 側 IP ＋合言葉）"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # 実際には送らない。外向きに使う IP を調べるだけ
            ip = s.getsockname()[0]
    except OSError:
        ip = "127.0.0.1"
    return f"http://{ip}:{PORT}/?t={TOKEN}"


def wtype(args):
    r = subprocess.run(["wtype", *args], env=ENV, capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or f"wtype exit {r.returncode}")


def wlrctl(args):
    r = subprocess.run(["wlrctl", *args], env=ENV, capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or f"wlrctl exit {r.returncode}")


class Handler(BaseHTTPRequestHandler):
    def _authed(self):
        q = parse_qs(urlparse(self.path).query)
        return secrets.compare_digest(q.get("t", [""])[0], TOKEN) or \
            secrets.compare_digest(self.headers.get("X-Token", ""), TOKEN)

    def _reply(self, code, body=b"", ctype="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path not in ("/", "/info"):
            return self._reply(404, b"not found")
        if not self._authed():
            return self._reply(403, "トークンが違います".encode())
        if path == "/info":  # QR コード用
            return self._reply(200, json.dumps({"url": lan_url()}).encode(), "application/json")
        self._reply(200, PAGE, "text/html; charset=utf-8")

    def do_POST(self):
        if not self._authed():
            return self._reply(403, b"forbidden")
        try:
            n = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(n) or b"{}")
            path = urlparse(self.path).path
            if path == "/type":
                text = str(data.get("text", ""))
                if text:
                    type_text(text)
                if data.get("enter"):
                    press("Enter")
            elif path == "/mouse":
                if "move" in data:
                    dx, dy = (int(v) for v in data["move"])
                    mouse_move(dx, dy)
                if "scroll" in data:
                    mouse_scroll(int(data["scroll"]))
                if data.get("click") in ("left", "right", "middle"):
                    mouse_click(data["click"])
            elif path == "/key":
                key = data.get("key")
                if key not in KEYS:
                    return self._reply(400, b"unknown key")
                press(key)
            else:
                return self._reply(404, b"not found")
            self._reply(200, b"ok")
        except Exception as e:  # エラーはスマホの画面に出す
            self._reply(500, str(e).encode())

    def log_message(self, fmt, *args):
        pass  # ログは出さない（打った内容を残さない）


if __name__ == "__main__":
    print(lan_url())
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
