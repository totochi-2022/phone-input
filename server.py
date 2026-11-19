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
# HTTPS の入口（スマホでアプリとして入れるため）。証明書は mkcert で作った自分用の認証局のもの（~/.local/share/tonzura-tls、
# リポジトリには入れない）。無ければ HTTPS は開かない。スマホに入れる認証局の証明書は /ca.crt で配る
HTTPS_PORT = 8775
TLS_DIR = Path.home() / ".local/share/tonzura-tls"
BASE = Path(__file__).resolve().parent
TOKEN_FILE = BASE / "token"
EXTLOG = Path.home() / ".local/state/tv-fullscreen/ext.log"  # /extlog の書き先
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


# 文字 → (キーコード, Shift) 。JIS 配列。wtype が使えないとき（ログイン画面など、
# labwc がまだ動いていないとき）に uinput の仮想キーボードで打つ用。ASCII だけ
JIS = {" ": (57, False), "\n": (28, False), "\t": (15, False)}
for _row, _codes in (("1234567890", range(2, 12)), ("qwertyuiop", range(16, 26)),
                     ("asdfghjkl", range(30, 39)), ("zxcvbnm", range(44, 51))):
    for _c, _k in zip(_row, _codes):
        JIS[_c] = (_k, False)
        if _c.isalpha():
            JIS[_c.upper()] = (_k, True)
for _c, _k in zip('!"#$%&\'()', range(2, 11)):
    JIS[_c] = (_k, True)
for _c, _k, _sh in (("-", 12, False), ("=", 12, True), ("^", 13, False), ("~", 13, True),
                    ("|", 124, True), ("@", 26, False), ("`", 26, True), ("[", 27, False), ("{", 27, True),
                    (";", 39, False), ("+", 39, True), (":", 40, False), ("*", 40, True),
                    ("]", 43, False), ("}", 43, True), (",", 51, False), ("<", 51, True),
                    (".", 52, False), (">", 52, True), ("/", 53, False), ("?", 53, True),
                    ("\\", 89, False), ("_", 89, True)):
    JIS[_c] = (_k, _sh)


def uinput_type(text):
    """uinput の仮想キーボードで 1 文字ずつ打つ（JIS 配列・ASCII だけ）"""
    bad = sorted({c for c in text if c not in JIS})
    if bad:
        raise RuntimeError("ログイン画面では英数字・記号しか打てません: " + "".join(bad))
    for c in text:
        code, shift = JIS[c]
        ukbd.press(code, (42,) if shift else ())
        time.sleep(0.01)


def wayland_up():
    return os.path.exists(os.path.join(ENV.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"),
                                       ENV.get("WAYLAND_DISPLAY", "wayland-0")))


def type_text(text):
    if win:
        return win.send("text " + base64.b64encode(text.encode()).decode())
    if ukbd and not wayland_up():  # ログイン画面（labwc がまだ無い）
        return uinput_type(text)
    # 打ち始めに少し待つ。wtype は毎回仮想キーボードを作り直すので、
    # すぐ打つと受け側の準備が間に合わず 1 文字目が落ちることがある。
    # Brave（Chromium）はキー配置が変わった直後の 1 キーを落とすことがあるので、
    # 先にどのアプリも使わない F24 を押して、落ちるならそれが落ちるようにする
    wtype(["-s", "80", "-k", "F24", "-s", "30", "--", text])


# ボタン名 → uinput のキーコード（キー, 修飾キー）。ログイン画面用
UKEYS = {
    "Enter": (28, ()), "BackSpace": (14, ()), "Tab": (15, ()), "ShiftTab": (15, (42,)), "Esc": (1, ()),
    "Up": (103, ()), "Down": (108, ()), "Left": (105, ()), "Right": (106, ()), "Space": (57, ()),
    "F7": (65, ()), "F8": (66, ()), "CtrlC": (46, (29,)), "CtrlL": (38, (29,)),
    "PageUp": (104, ()), "PageDown": (109, ()),
}


def press(key):
    if win:
        mods, vk = WIN_KEYS[key]
        return win.send(f"key {','.join(f'{m:X}' for m in mods) or '-'} {vk:X}")
    if ukbd and not wayland_up() and key in UKEYS:  # ログイン画面
        code, mods = UKEYS[key]
        return ukbd.press(code, mods)
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


class UInputTablet:
    """画面の位置を指定してクリックする仮想マウス（絶対座標、0〜65535 が画面の端から端）。
    Brave の拡張機能 tv-fullscreen が「スキップ」などのボタンを本物のクリックで押すのに使う（/click）。
    ページが作る偽のクリック（isTrusted: false）を無視するサイトがあるため"""

    NAME = b"phone-input virtual tablet"
    EV_SYN, EV_KEY, EV_ABS = 0, 1, 3
    ABS_X, ABS_Y, MAX = 0x00, 0x01, 65535
    BTN_LEFT = 0x110

    def __init__(self):
        import fcntl
        self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_ABSBIT, UI_DEV_CREATE = 0x40045564, 0x40045565, 0x40045567, 0x5501
        for ev in (self.EV_KEY, self.EV_ABS):
            fcntl.ioctl(self.fd, UI_SET_EVBIT, ev)
        fcntl.ioctl(self.fd, UI_SET_KEYBIT, self.BTN_LEFT)
        for code in (self.ABS_X, self.ABS_Y):
            fcntl.ioctl(self.fd, UI_SET_ABSBIT, code)
        # struct uinput_user_dev: name, input_id, ff_effects_max, absmax[64], absmin[64], absfuzz[64], absflat[64]
        absmax = [0] * 64
        absmax[self.ABS_X] = absmax[self.ABS_Y] = self.MAX
        dev = struct.pack("80sHHHHi", self.NAME, 0x03, 0x1209, 0x7074, 1, 0) + \
            struct.pack("64i", *absmax) + bytes(4 * 64 * 3)
        os.write(self.fd, dev)
        fcntl.ioctl(self.fd, UI_DEV_CREATE)
        self.lock = threading.Lock()

    def _emit(self, events):
        now = time.time()
        sec, usec = int(now), int((now % 1) * 1e6)
        data = b"".join(struct.pack("llHHi", sec, usec, t, c, v) for t, c, v in events)
        data += struct.pack("llHHi", sec, usec, self.EV_SYN, 0, 0)
        os.write(self.fd, data)

    def _move(self, fx, fy):
        x = round(min(1, max(0, fx)) * self.MAX)
        y = round(min(1, max(0, fy)) * self.MAX)
        self._emit([(self.EV_ABS, self.ABS_X, x), (self.EV_ABS, self.ABS_Y, y)])

    def move(self, fx, fy):
        """矢印を動かすだけ（クリックしない）。全画面でないときに、拡張機能がページの位置のずれを測るのに使う"""
        with self.lock:
            self._move(fx, fy)

    def click(self, fx, fy):
        """fx, fy：画面の幅・高さに対する割合（0〜1）"""
        with self.lock:
            self._move(fx, fy)
            time.sleep(0.05)
            self._emit([(self.EV_KEY, self.BTN_LEFT, 1)])
            time.sleep(0.03)
            self._emit([(self.EV_KEY, self.BTN_LEFT, 0)])


def make_tablet():
    if IS_WSL:
        return None
    try:
        return UInputTablet()
    except OSError:
        return None


utablet = make_tablet()


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
        self.held = set()  # 押したままの修飾キー

    def hold(self, code, down):
        """修飾キーを押したまま／離す（Ctrl+スクロールなど、マウス操作と組み合わせるため）"""
        with self.lock:
            if down and code not in self.held:
                self._key(code, 1)
                self.held.add(code)
            elif not down and code in self.held:
                self._key(code, 0)
                self.held.discard(code)

    def release_all(self):
        for code in list(self.held):
            self.hold(code, False)

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
last_kbd = 0.0
last_mouse = time.time()  # 最後に /mouse を受けた時刻。押しっぱなしの安全装置用


def watchdog():
    """ボタンを押したまま 4 秒スマホから何も来なければ離す（指を離した通知が届かなかったとき用。
    押している間、スマホは 1 秒ごとに ping を送ってくる）"""
    while True:
        time.sleep(1)
        if umouse and umouse.held and time.time() - last_mouse > 4:
            umouse.release_all()
        if ukbd and ukbd.held and time.time() - last_kbd > 4:
            ukbd.release_all()


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


# 音声で頼む（/claude）の返事を読み上げる：常駐の Claude の会話の記録（jsonl）から返事を取り、VOICEVOX（ずんだもん）で声にする
CLAUDE_PROJ = Path.home() / ".claude/projects/-home-motoki-work-howm"
VOICEVOX = "http://127.0.0.1:50021"
ZUNDAMON = 3  # VOICEVOX の話者番号（ずんだもん ノーマル）


def claude_reply(since):
    """since（UNIX 時刻）より後に Claude が答え終わった最後の返事の文字。まだなら None"""
    from datetime import datetime
    files = sorted(CLAUDE_PROJ.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    if not files:
        return None
    f = files[-1]
    with open(f, "rb") as fh:
        fh.seek(max(0, f.stat().st_size - 3_000_000))
        lines = fh.read().splitlines()
    for line in reversed(lines):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        ts = d.get("timestamp")
        if ts and datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() <= since:
            break
        m = d.get("message") or {}
        if d.get("type") == "assistant" and not d.get("isSidechain") and m.get("stop_reason") == "end_turn":
            text = "".join(c.get("text", "") for c in m.get("content", []) if c.get("type") == "text")
            if text.strip():
                return text
    return None


CHAT_CUTOFF = Path.home() / ".local/state/phone-input/chat-cutoff"  # 「会話をクリア」した時刻（ISO）。これより後だけ出す


def claude_history(n=30):
    """会話の最近のやり取り [{who: "user"|"claude", text, final, t}]（新しいものが最後）。working＝Claude が作業中か"""
    files = sorted(CLAUDE_PROJ.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    if not files:
        return {"items": [], "working": False}
    f = files[-1]
    with open(f, "rb") as fh:
        fh.seek(max(0, f.stat().st_size - 4_000_000))
        lines = fh.read().splitlines()
    items, working = [], False
    try:
        cutoff = CHAT_CUTOFF.read_text().strip()
    except OSError:
        cutoff = ""
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if d.get("isSidechain") or d.get("isMeta") or d.get("isCompactSummary"):
            continue
        m = d.get("message") or {}
        c = m.get("content")
        hidden = bool(cutoff) and (d.get("timestamp") or "") <= cutoff  # クリアより前：出さないが、作業中かどうかには数える
        if d.get("type") == "user":
            if isinstance(c, str):
                text = c
            elif isinstance(c, list):
                if any(x.get("type") == "tool_result" for x in c):
                    continue  # ツールの結果（作業の途中）
                text = "".join(x.get("text", "") for x in c if x.get("type") == "text")
            else:
                continue
            text = text.strip()
            if not text or text.startswith("<"):
                continue  # コマンドや仕組みの通知
            working = True
            if not hidden:
                items.append({"who": "user", "text": text[:2000], "t": d.get("timestamp")})
        elif d.get("type") == "assistant" and isinstance(c, list):
            text = "".join(x.get("text", "") for x in c if x.get("type") == "text").strip()
            final = m.get("stop_reason") == "end_turn"
            if final:
                working = False
            if text and not hidden:
                items.append({"who": "claude", "text": text[:6000], "final": final, "t": d.get("timestamp")})
    return {"items": items[-n:], "working": working}


GLYPH_JA = {"⏳": "作業中", "🔒": "許可待ち", "⌨": "入力待ち", "❓": "質問あり", "🔔": "終わった", "💤": "待機", "🌙": "裏で動作"}


def claude_status():
    """claude-tasks list（Board と同じ）から Claude の状態。[{dir, disp, mark, label, state, me}]（止まっているものは除く）"""
    tasks = Path.home() / ".claude_plugin/scripts/claude-tasks"
    if not tasks.exists():
        return []
    try:
        out = subprocess.run([str(tasks), "list"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    me = str(CLAUDE_PROJ.name).lstrip("-").replace("-", "/")
    rows = []
    for line in out.splitlines():
        f = line.split("\t")
        if len(f) < 5 or f[4] == "off":
            continue
        mark = f[0].replace("●", "").replace("○", "")
        label = next((v for k, v in GLYPH_JA.items() if k in mark), "動作中")
        rows.append({"dir": f[2], "disp": f[1], "mark": mark or "●", "label": label, "state": f[4],
                     "me": f[2].strip("/") == me})
    return rows


def claude_sock():
    """常駐の Claude（~/work/howm）の dtach のソケット。無ければ空"""
    tasks = Path.home() / ".claude_plugin/scripts/claude-tasks"
    if not tasks.exists():
        return ""
    sock = subprocess.run([str(tasks), "sock", str(Path.home() / "work/howm")], capture_output=True,
                          text=True).stdout.strip()
    return sock if sock and Path(sock).is_socket() else ""


def claude_pane():
    """常駐の Claude が映っている tmux のペイン（そのソケットにつないでいる dtach が中で動いているペイン）"""
    sock = claude_sock()
    if not sock:
        return ""
    try:
        panes = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{pane_id} #{pane_pid}"], capture_output=True,
                               text=True, timeout=3).stdout.split("\n")
    except (OSError, subprocess.TimeoutExpired):
        return ""
    children = {}  # 親の pid → 子の pid（/proc から）
    for st in Path("/proc").glob("[0-9]*/stat"):
        try:
            f = st.read_text().rsplit(")", 1)[1].split()
            children.setdefault(int(f[1]), []).append(int(st.parent.name))
        except (OSError, IndexError, ValueError):
            pass
    for line in panes:
        if " " not in line:
            continue
        pane, pid = line.split(" ", 1)
        todo = [int(pid)]
        while todo:
            p = todo.pop()
            try:
                if sock.encode() in Path(f"/proc/{p}/cmdline").read_bytes():
                    return pane
            except OSError:
                pass
            todo += children.get(p, [])
    return ""


def claude_prompt():
    """端末に確認（許可・質問）が出ていれば {screen, options:[{n, label}]}、無ければ None。
    画面の下の部分から「❯ 1. Yes」「  2. …」の形の行を探す"""
    pane = claude_pane()
    if not pane:
        return None
    try:
        lines = subprocess.run(["tmux", "capture-pane", "-p", "-J", "-t", pane], capture_output=True, text=True,
                               timeout=3).stdout.rstrip("\n").split("\n")
    except (OSError, subprocess.TimeoutExpired):
        return None
    import re
    lines = [re.sub(r"[\s│]+$", "", ln) for ln in lines][-60:]
    opts, first, last = [], None, None
    for i, ln in enumerate(lines):
        m = re.match(r"^[\s│]*(❯\s*)?(\d)\.\s+(\S.*)$", ln)
        if m:
            if first is None:
                first = i
            opts.append({"n": int(m.group(2)), "label": m.group(3).strip(), "sel": bool(m.group(1))})
            last = i
        elif opts and last == i - 1 and re.match(r"^\s{4,}\S", ln):  # 長い選択肢の続きの行（字下げだけ）
            opts[-1]["label"] += " " + ln.strip()
            last = i
    # 選択肢は 1 から連番で、選んでいる印（❯）が 1 つあるときだけ確認とみなす（会話の中の番号付きの箇条書きと区別）
    if len(opts) < 2 or [o["n"] for o in opts] != list(range(1, len(opts) + 1)) or sum(o["sel"] for o in opts) != 1:
        return None
    # 確認の枠：選択肢より上で最後の実線（───）から。上から 見出し → 説明 → 点線（╌）にはさまれた中身 → 注記 → 質問
    top = next((i for i in range(first - 1, -1, -1) if lines[i].strip() and set(lines[i].strip()) <= set("─━")), None)
    region = lines[top + 1:first] if top is not None else lines[max(0, first - 14):first]
    screen = "\n".join(ln for ln in region if ln.strip())
    title, desc, code, note, question, part = "", [], [], [], "", 0
    for ln in region:
        s = ln.strip()
        if not s:
            continue
        if set(s) <= set("╌┄-"):
            part += 1  # 点線：0＝見出し・説明、1＝中身、2 以上＝注記
            continue
        if part == 1:
            code.append(re.sub(r"^\s?│\s?", "", ln.rstrip()))
        elif not title and part == 0:
            title = s
        elif part == 0:
            if not s.startswith("Tip:"):
                desc.append(s)
        else:
            note.append(s)
    for bucket in (note, desc):  # 質問（最後の「〜?」の行）は別に出す
        if bucket and bucket[-1].endswith(("?", "？")):
            question = bucket.pop()
            break
    if part == 1 and not note and not question:  # 点線が 1 本だけ（閉じていない）なら中身ではない
        desc += code; code = []
    return {"screen": screen, "title": title, "desc": desc, "code": "\n".join(code), "note": note, "question": question,
            "options": [{"n": o["n"], "label": o["label"]} for o in opts]}


def claude_suggestion():
    """入力欄に薄い字で出ているおすすめ（次に頼む文の候補）。自分で打ちかけの文（普通の色）や、無ければ None。
    入力欄＝横線にはさまれた「❯ 」の行。おすすめは灰色（38;5;24x）か薄い（2m）の字で出る"""
    import re
    pane = claude_pane()
    if not pane:
        return None
    try:
        raw = subprocess.run(["tmux", "capture-pane", "-p", "-e", "-t", pane], capture_output=True, text=True,
                             timeout=3).stdout.split("\n")
    except (OSError, subprocess.TimeoutExpired):
        return None
    plain = [re.sub(r"\x1b\[[0-9;]*m", "", ln) for ln in raw]
    for i in range(len(plain) - 1, 0, -1):
        if plain[i].lstrip().startswith("❯") and set(plain[i - 1].strip()) <= set("─") and plain[i - 1].strip():
            text = plain[i].lstrip()[1:].strip()
            if not text:
                return None
            after = raw[i].split("❯", 1)[1]
            # 文字の前の最後の色の指定が灰色か薄いならおすすめ
            m = re.match(r"^((?:\x1b\[[0-9;]*m|\s)*)", after)
            codes = m.group(1) if m else ""
            if re.search(r"\x1b\[(2|38;5;24\d|90)m", codes) or "\x1b[2;" in codes:
                return text
            return None
    return None


def speakable(md):
    """Markdown を読み上げ向けに：コード・URL・表の区切り・記号を省く"""
    import re
    md = md.split("\nSources:")[0]
    md = re.sub(r"```.*?```", "（コードは省略）", md, flags=re.S)
    md = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", md)
    md = re.sub(r"https?://\S+", "", md)
    out = []
    for ln in md.splitlines():
        ln = ln.strip()
        if re.fullmatch(r"\|?[\s:|-]+\|?", ln) and "-" in ln:
            continue  # 表の区切りの行
        ln = re.sub(r"^#+\s*|^[-*]\s+|^>\s*", "", ln)
        ln = ln.strip("|").replace("|", "、")
        ln = re.sub(r"[*`_~]", "", ln)
        if ln:
            out.append(ln)
    return "\n".join(out)


ZUNDAMON_STYLES = {3, 1, 7, 22, 38, 75, 76}  # ノーマル・あまあま・ツンツン・ささやき・ヒソヒソ・ヘロヘロ・なみだめ


def tts(text, speaker=ZUNDAMON):
    """VOICEVOX でずんだもんの声の WAV を作る（speaker は ZUNDAMON_STYLES のどれか）"""
    if speaker not in ZUNDAMON_STYLES:
        speaker = ZUNDAMON
    import urllib.request
    from urllib.parse import quote
    q = json.loads(urllib.request.urlopen(urllib.request.Request(
        f"{VOICEVOX}/audio_query?speaker={speaker}&text={quote(text)}", method="POST"), timeout=30).read())
    q["speedScale"] = 1.15
    return urllib.request.urlopen(urllib.request.Request(
        f"{VOICEVOX}/synthesis?speaker={speaker}", data=json.dumps(q).encode(),
        headers={"Content-Type": "application/json"}, method="POST"), timeout=120).read()


say_gen = 0  # テレビで読む（/say）の世代。新しく読むか止めると増え、古い読み上げは途中でやめる
say_proc = None


def say_tv(parts):
    """[{text, speaker}] を順にテレビのスピーカー（PipeWire の既定の出力）で鳴らす。鳴らしている間に次を作る"""
    global say_gen, say_proc
    say_gen += 1
    gen = say_gen
    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(1) as ex:
        nxt = ex.submit(tts, parts[0]["text"], parts[0]["speaker"]) if parts else None
        for i in range(len(parts)):
            wav = nxt.result()
            nxt = ex.submit(tts, parts[i + 1]["text"], parts[i + 1]["speaker"]) if i + 1 < len(parts) else None
            if gen != say_gen:
                return
            say_proc = subprocess.Popen(["pw-play", "-"], stdin=subprocess.PIPE, env=ENV)
            say_proc.communicate(wav)


def say_stop():
    global say_gen
    say_gen += 1
    if say_proc and say_proc.poll() is None:
        say_proc.kill()


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

    STATIC = {"/sw.js": ("sw.js", "text/javascript; charset=utf-8"), "/share": ("share.html", "text/html; charset=utf-8"),
              "/icon-192.png": ("icon-192.png", "image/png"), "/icon-512.png": ("icon-512.png", "image/png")}

    def do_GET(self):
        path = urlparse(self.path).path
        if path in self.STATIC:  # 合言葉なしで出してよいもの（アプリのアイコンなど）
            name, ctype = self.STATIC[path]
            return self._reply(200, (BASE / name).read_bytes(), ctype)
        if path == "/ca.crt":  # スマホに入れる認証局の証明書（公開してよい側だけ。鍵は出さない）
            import ssl
            try:
                der = ssl.PEM_cert_to_DER_cert((TLS_DIR / "ca/rootCA.pem").read_text())
            except OSError:
                return self._reply(404, b"no CA")
            return self._reply(200, der, "application/x-x509-ca-cert")
        if path == "/manifest.webmanifest":  # 合言葉付きで読まれる（開くページにも合言葉を付けるため）
            if not self._authed():
                return self._reply(403, b"forbidden")
            return self._reply(200, json.dumps({
                "name": "テレビのリモコン", "short_name": "リモコン", "lang": "ja",
                "start_url": f"/?t={TOKEN}", "scope": "/", "display": "standalone", "orientation": "portrait",
                "background_color": "#111111", "theme_color": "#111111",
                "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                          {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"}],
                # スマホの「共有」の送り先に出る。共有された URL を /share が autotube の「テレビに送る」に渡す
                "share_target": {"action": "/share", "method": "GET",
                                 "params": {"title": "title", "text": "text", "url": "url"}},
            }, ensure_ascii=False).encode(), "application/manifest+json")
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
            elif path == "/claude":  # 音声で頼む：常駐の Claude（~/work/howm、dtach の中）の入力欄に打ち込んで送る（tonzura だけ）
                text = " ".join(str(data.get("text", "")).split())  # 改行は空白に（途中で送信されないように）
                if not text:
                    return self._reply(400, b"empty")
                if not data.get("plain"):  # plain＝おすすめをそのまま送るとき（音声ではないので印を付けない）
                    text = "🎤 " + text  # 音声入力の印（Claude が聞き間違いを音の似た言葉として読み替えられるように）
                sock = claude_sock()
                if not sock:
                    return self._reply(500, "常駐の Claude が見つかりません".encode())
                since = time.time()
                # 文字と Enter を分けて送る（一緒に送ると貼り付けの扱いになり、送信されないことがある）。操作キーは送らない
                subprocess.run(["dtach", "-p", sock], input=text.encode(), check=True)
                time.sleep(0.4)
                subprocess.run(["dtach", "-p", sock], input=b"\r", check=True)
                wf = Path.home() / ".local/bin/win-focus"  # 「送ったら端末へ」のときだけテレビを端末（ttyd）のタブへ（F7＋T と同じ）
                if data.get("jump") and wf.exists():
                    subprocess.Popen([str(wf), "ttyd"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return self._reply(200, json.dumps({"since": since}).encode(), "application/json")
            elif path == "/reply":  # /claude で頼んだあとの Claude の返事（読み上げ向けに整えたもの）。まだなら text は null
                text = claude_reply(float(data.get("since", 0)))
                return self._reply(200, json.dumps({"text": text and speakable(text)}, ensure_ascii=False).encode(),
                                   "application/json")
            elif path == "/say":  # テレビのスピーカーで読む。parts が空なら止める
                parts = [{"text": str(p.get("text", ""))[:500], "speaker": int(p.get("speaker", ZUNDAMON))}
                         for p in data.get("parts", []) if str(p.get("text", "")).strip()]
                say_stop()
                if parts:
                    threading.Thread(target=say_tv, args=(parts,), daemon=True).start()
            elif path == "/history":  # 会話の最近のやり取り（🤖 の下に出す）。clear なら今の時刻を覚えて、次からそれより後だけ
                if data.get("clear"):
                    from datetime import datetime, timezone
                    CHAT_CUTOFF.parent.mkdir(parents=True, exist_ok=True)
                    CHAT_CUTOFF.write_text(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z")
                h = claude_history(int(data.get("n", 30)))
                h["status"] = claude_status()
                h["prompt"] = claude_prompt()
                h["suggest"] = None if h["prompt"] or h["working"] else claude_suggestion()
                return self._reply(200, json.dumps(h, ensure_ascii=False).encode(), "application/json")
            elif path == "/answer":  # 端末に出ている確認（許可・質問）に答える。n＝選択肢の番号、esc＝断る
                # 送る直前に画面を見直し、リモコンに見せた選択肢がまだ出ているときだけ送る（キーは番号か Esc だけ）
                p = claude_prompt()
                if not p:
                    return self._reply(409, "確認はもう出ていません".encode())
                if [o["label"] for o in p["options"]] != list(data.get("labels", [])):
                    return self._reply(409, "確認の中身が変わりました。見直してください".encode())
                sock = claude_sock()
                if data.get("esc"):
                    key = b"\x1b"
                else:
                    n = int(data.get("n", 0))
                    if n not in [o["n"] for o in p["options"]]:
                        return self._reply(400, b"bad option")
                    key = str(n).encode()
                subprocess.run(["dtach", "-p", sock], input=key, check=True)
                return self._reply(200, b'{"ok": true}', "application/json")  # 画面の側は JSON で読む
            elif path == "/tts":  # 文字をずんだもんの声（VOICEVOX）の WAV に
                text = str(data.get("text", "")).strip()[:500]
                if not text:
                    return self._reply(400, b"empty")
                return self._reply(200, tts(text, int(data.get("speaker", ZUNDAMON))), "audio/wav")
            elif path == "/open":  # 共有（/share）から：テレビの Brave の新しいタブで開く（autotube の openurl）
                import urllib.request
                req = urllib.request.Request("http://127.0.0.1:8766/api/openurl", method="POST",
                                             data=json.dumps({"url": str(data.get("url", "")), "how": "new"}).encode(),
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=5) as r:
                    r.read()
            elif path == "/cast":  # 共有（/share）から：autotube の「テレビに送る」に渡す
                import urllib.request
                req = urllib.request.Request("http://127.0.0.1:8766/api/cast", method="POST",
                                             data=json.dumps({"url": str(data.get("url", "")), "t": data.get("t", 0)}).encode(),
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=5) as r:
                    r.read()
            elif path == "/kbd":
                if not ukbd:
                    return self._reply(500, "仮想キーボードが使えません（/dev/uinput の権限）".encode())
                global last_kbd
                last_kbd = time.time()
                op = data.get("op", "press")
                if op == "ping":  # 修飾キーを押したままの間、1 秒ごとに来る
                    return self._reply(200, b"ok")
                code = int(data.get("code", 0))
                mods = [int(m) for m in data.get("mods", [])]
                if op in ("down", "up"):
                    if code not in UInputKeyboard.CODES:
                        return self._reply(400, b"unknown key")
                    ukbd.hold(code, op == "down")
                    return self._reply(200, b"ok")
                if not all(c in UInputKeyboard.CODES for c in [code, *mods]):
                    return self._reply(400, b"unknown key")
                ukbd.press(code, mods)
            elif path == "/click":  # 画面の位置（割合）を本物のクリックで押す。拡張機能 tv-fullscreen のスキップ用
                if not utablet:
                    return self._reply(500, "仮想マウスが使えません（/dev/uinput の権限）".encode())
                if data.get("move_only"):
                    utablet.move(float(data["fx"]), float(data["fy"]))
                else:
                    utablet.click(float(data["fx"]), float(data["fy"]))
            elif path == "/extlog":  # 拡張機能 tv-fullscreen の記録（スキップなどのボタンの中身）。打った文字は来ない
                EXTLOG.parent.mkdir(exist_ok=True)
                if EXTLOG.exists() and EXTLOG.stat().st_size > 1_000_000:
                    EXTLOG.rename(EXTLOG.with_suffix(".log.1"))
                with EXTLOG.open("a") as f:
                    f.write(json.dumps({"t": time.strftime("%Y-%m-%d %H:%M:%S"), **data}, ensure_ascii=False) + "\n")
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
    if (TLS_DIR / "cert.pem").exists() and (TLS_DIR / "key.pem").exists():
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(TLS_DIR / "cert.pem", TLS_DIR / "key.pem")
        https = ThreadingHTTPServer(("0.0.0.0", HTTPS_PORT), Handler)
        https.socket = ctx.wrap_socket(https.socket, server_side=True)
        threading.Thread(target=https.serve_forever, daemon=True).start()
        print(f"HTTPS: {HTTPS_PORT}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
