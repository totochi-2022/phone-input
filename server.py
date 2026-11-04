#!/usr/bin/env python3
"""スマホから Pi に文字やキーを送る入力用ページ。

起動: python3 ~/phone-input/server.py
スマホで http://192.168.0.200:8765/?t=<トークン> を開く（トークンは ~/phone-input/token）
受け取った文字は wtype で今フォーカスしているウィンドウに打ち込む。
"""
import json
import os
import secrets
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

PORT = 8765
BASE = Path(__file__).resolve().parent
TOKEN_FILE = BASE / "token"
PAGE = (BASE / "index.html").read_bytes()

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
        if urlparse(self.path).path != "/":
            return self._reply(404, b"not found")
        if not self._authed():
            return self._reply(403, "トークンが違います".encode())
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
                    wtype(["--", text])
                if data.get("enter"):
                    wtype(KEYS["Enter"])
            elif path == "/mouse":
                if "move" in data:
                    dx, dy = (int(v) for v in data["move"])
                    wlrctl(["pointer", "move", str(dx), str(dy)])
                if "scroll" in data:
                    wlrctl(["pointer", "scroll", str(int(data["scroll"])), "0"])
                if data.get("click") in ("left", "right", "middle"):
                    wlrctl(["pointer", "click", data["click"]])
            elif path == "/key":
                key = data.get("key")
                if key not in KEYS:
                    return self._reply(400, b"unknown key")
                wtype(KEYS[key])
            else:
                return self._reply(404, b"not found")
            self._reply(200, b"ok")
        except Exception as e:  # エラーはスマホの画面に出す
            self._reply(500, str(e).encode())

    def log_message(self, fmt, *args):
        pass  # ログは出さない（打った内容を残さない）


if __name__ == "__main__":
    print(f"http://192.168.0.200:{PORT}/?t={TOKEN}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
