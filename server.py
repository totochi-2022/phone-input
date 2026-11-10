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
import struct
import subprocess
import threading
import time
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
    # 打ち始めに少し待つ。wtype は毎回仮想キーボードを作り直すので、
    # すぐ打つと受け側の準備が間に合わず 1 文字目が落ちることがある
    wtype(["-s", "80", "--", text])


def press(key):
    if win:
        mods, vk = WIN_KEYS[key]
        return win.send(f"key {','.join(f'{m:X}' for m in mods) or '-'} {vk:X}")
    wtype(KEYS[key])


class UInputMouse:
    """/dev/uinput で仮想マウスを 1 つ作る（標準ライブラリだけ）。
    wlrctl と違ってボタンを「押す」「離す」を別々に送れるのでドラッグができる。
    /dev/uinput に書き込めないとき（udev ルール未設定）は作らず、wlrctl に戻る。
    labwc の rc.xml で、この名前のデバイスは加速なし（flat）にして wlrctl と同じ 1:1 の動きにしている"""

    NAME = b"phone-input virtual mouse"
    EV_SYN, EV_KEY, EV_REL = 0, 1, 2
    REL_X, REL_Y, REL_WHEEL, REL_WHEEL_HI_RES = 0x00, 0x01, 0x08, 0x0B
    BTN = {"left": 0x110, "right": 0x111, "middle": 0x112}

    def __init__(self):
        import fcntl
        self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_RELBIT, UI_DEV_CREATE = 0x40045564, 0x40045565, 0x40045566, 0x5501
        for ev in (self.EV_KEY, self.EV_REL):
            fcntl.ioctl(self.fd, UI_SET_EVBIT, ev)
        for code in self.BTN.values():
            fcntl.ioctl(self.fd, UI_SET_KEYBIT, code)
        for code in (self.REL_X, self.REL_Y, self.REL_WHEEL, self.REL_WHEEL_HI_RES):
            fcntl.ioctl(self.fd, UI_SET_RELBIT, code)
        # struct uinput_user_dev: name[80], input_id(bustype=USB, vendor, product, version), ff_effects_max, abs*[64]x4
        dev = struct.pack("80sHHHHi", self.NAME, 0x03, 0x1209, 0x7069, 1, 0) + bytes(4 * 64 * 4)
        os.write(self.fd, dev)
        fcntl.ioctl(self.fd, UI_DEV_CREATE)
        self.lock = threading.Lock()
        self.held = set()
        self.wheel_rest = 0  # 1 ノッチ（120）未満の端数

    def _emit(self, events):
        now = time.time()
        sec, usec = int(now), int((now % 1) * 1e6)
        data = b"".join(struct.pack("llHHi", sec, usec, t, c, v) for t, c, v in events)
        data += struct.pack("llHHi", sec, usec, self.EV_SYN, 0, 0)
        with self.lock:
            os.write(self.fd, data)

    def move(self, dx, dy):
        self._emit([(self.EV_REL, self.REL_X, dx), (self.EV_REL, self.REL_Y, dy)])

    def scroll(self, n):
        # wlrctl の scroll と同じ量にそろえる（wlrctl は 15 で 1 ノッチ、uinput の高分解能は 120 で 1 ノッチ）。
        # wlrctl は正の値で下へ、ホイールは正の値で上へなので向きを反転
        hi = -n * 8
        self.wheel_rest += hi
        notches = int(self.wheel_rest / 120)
        self.wheel_rest -= notches * 120
        ev = [(self.EV_REL, self.REL_WHEEL_HI_RES, hi)]
        if notches:
            ev.append((self.EV_REL, self.REL_WHEEL, notches))
        self._emit(ev)

    def button(self, name, down):
        self._emit([(self.EV_KEY, self.BTN[name], 1 if down else 0)])
        (self.held.add if down else self.held.discard)(name)

    def click(self, name):
        self.button(name, True)
        time.sleep(0.02)
        self.button(name, False)

    def release_all(self):
        for name in list(self.held):
            self.button(name, False)


def make_uinput():
    if IS_WSL:
        return None
    try:
        return UInputMouse()
    except OSError:
        return None  # /dev/uinput に権限がない → wlrctl（ドラッグなし）


umouse = make_uinput()


class UInputKeyboard:
    """uinput の仮想キーボード（入力ページの「PC キー」用）。Linux のキーコードをそのまま送る"""

    NAME = b"phone-input virtual keyboard"
    CODES = range(1, 128)  # ESC〜F12・矢印・Home/End・かな(93)・Win(125) を含む範囲

    def __init__(self):
        import fcntl
        self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        fcntl.ioctl(self.fd, 0x40045564, 1)  # UI_SET_EVBIT EV_KEY
        for code in self.CODES:
            fcntl.ioctl(self.fd, 0x40045565, code)  # UI_SET_KEYBIT
        dev = struct.pack("80sHHHHi", self.NAME, 0x03, 0x1209, 0x706b, 1, 0) + bytes(4 * 64 * 4)
        os.write(self.fd, dev)
        fcntl.ioctl(self.fd, 0x5501)  # UI_DEV_CREATE
        self.lock = threading.Lock()

    def _key(self, code, value):
        now = time.time()
        sec, usec = int(now), int((now % 1) * 1e6)
        os.write(self.fd, struct.pack("llHHi", sec, usec, 1, code, value) +
                 struct.pack("llHHi", sec, usec, 0, 0, 0))

    def press(self, code, mods=()):
        """修飾キーを押す → キーを押して離す → 修飾キーを離す"""
        with self.lock:
            for m in mods:
                self._key(m, 1)
                time.sleep(0.01)
            self._key(code, 1)
            time.sleep(0.02)
            self._key(code, 0)
            for m in reversed(mods):
                time.sleep(0.01)
                self._key(m, 0)


def make_ukbd():
    if IS_WSL:
        return None
    try:
        return UInputKeyboard()
    except OSError:
        return None


ukbd = make_ukbd()
last_mouse = time.time()  # 最後に /mouse を受けた時刻。押しっぱなしの安全装置用


def watchdog():
    """ボタンを押したまま 4 秒スマホから何も来なければ離す（指を離した通知が届かなかったとき用。
    押している間、スマホは 1 秒ごとに ping を送ってくる）"""
    while True:
        time.sleep(1)
        if umouse and umouse.held and time.time() - last_mouse > 4:
            umouse.release_all()


def mouse_move(dx, dy):
    if win:
        return win.send(f"move {dx} {dy}")
    if umouse:
        return umouse.move(dx, dy)
    wlrctl(["pointer", "move", str(dx), str(dy)])


def mouse_scroll(n):
    if win:
        return win.send(f"scroll {n}")
    if umouse:
        return umouse.scroll(n)
    wlrctl(["pointer", "scroll", str(n), "0"])


def mouse_click(button):
    if win:
        return win.send(f"click {button}")
    if umouse:
        return umouse.click(button)
    wlrctl(["pointer", "click", button])


def mouse_button(button, down):
    """押す／離すを別々に送る。uinput がないときは「離す」でクリックだけする（ドラッグはできない）"""
    if umouse:
        return umouse.button(button, down)
    if not down:
        mouse_click(button)


def mouse_op(op):
    b = op.get("button", "left")
    if b not in ("left", "right", "middle"):
        raise ValueError("unknown button")
    kind = op.get("op")
    if kind == "down":
        mouse_button(b, True)
    elif kind == "up":
        mouse_button(b, False)
    elif kind == "click":
        mouse_click(b)
    elif kind == "dbl":  # ダブルクリック：サーバー側で間隔をそろえて 2 回
        mouse_click(b)
        time.sleep(0.06)
        mouse_click(b)
    # "ping" は何もしない（last_mouse の更新だけ）


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
                global last_mouse
                last_mouse = time.time()
                if "move" in data:
                    dx, dy = (int(v) for v in data["move"])
                    mouse_move(dx, dy)
                if "scroll" in data:
                    mouse_scroll(int(data["scroll"]))
                if data.get("click") in ("left", "right", "middle"):
                    mouse_click(data["click"])
                for op in data.get("ops", []):  # 押す・離す・ダブルクリックなど。移動のあとに順に実行
                    mouse_op(op)
            elif path == "/kbd":
                if not ukbd:
                    return self._reply(500, "仮想キーボードが使えません（/dev/uinput の権限）".encode())
                code = int(data.get("code", 0))
                mods = [int(m) for m in data.get("mods", [])]
                if not all(c in UInputKeyboard.CODES for c in [code, *mods]):
                    return self._reply(400, b"unknown key")
                ukbd.press(code, mods)
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
    print("マウス:", "uinput（ドラッグ可）" if umouse else ("Windows" if win else "wlrctl（ドラッグ不可）"))
    threading.Thread(target=watchdog, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
