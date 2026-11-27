#!/usr/bin/env python3
"""キーボードのジェスチャー（tonzura 用）：Esc・F1・F2 を押さえたまま、タッチパッド（マウス）で描く。

- keyd で esc / f1 / f2 を layer(gesc) / layer(gf1) / layer(gf2) にしてある（押している間はキーを出さない）
- `keyd listen` で、そのレイヤーが入った・抜けたを知る（/var/run/keyd.socket は input グループで読めるようにしてある）
- 押している間は keyd の仮想ポインターを独り占め（EVIOCGRAB）して、矢印を動かさずに動きを集める
- 離したとき：動いていれば、スマホで登録したお手本（~/.local/state/phone-input/gestures.json。
  スマホの画面が /gestures で写す）と比べて、合えば phone-input の /gesture で動作を送る。
  動いていなければ（ほかのキーも押していなければ）、そのキー（Esc・F1・F2）を phone-input の仮想キーボードで打つ
- 見分け方はスマホ（index.html の gtNorm・gtVariants・gtShape・gtDist）と同じものを Python に写した
記録: ~/.local/state/phone-input/kbgesture.log
"""
import fcntl
import json
import math
import os
import select
import struct
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

LAYERS = {"gesc": 1, "gf1": 59, "gf2": 60}  # レイヤー名 → 動かなかったときに打つキー
MIN_PATH = 60          # これより短い動きはジェスチャーにしない（ポインターの単位）
STATE = Path.home() / ".local/state/phone-input"
GEST_FILE = STATE / "gestures.json"
LOG = STATE / "kbgesture.log"
TOKEN = (Path.home() / "phone-input/token").read_text().strip()
EVIOCGRAB = 0x40044590
EV = struct.Struct("llHHi")
EV_KEY, EV_REL, EV_SYN = 1, 2, 0


def log(msg):
    try:
        with LOG.open("a") as f:
            f.write(time.strftime("%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def find_dev(name):
    cur = None
    for line in open("/proc/bus/input/devices"):
        if line.startswith("N: Name="):
            cur = line.split("=", 1)[1].strip().strip('"')
        elif line.startswith("H: Handlers=") and cur == name:
            for h in line.split("=", 1)[1].split():
                if h.startswith("event"):
                    return "/dev/input/" + h
    return None


def post(path, body):
    req = urllib.request.Request("http://127.0.0.1:8765" + path, method="POST",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "X-Token": TOKEN})
    with urllib.request.urlopen(req, timeout=8) as r:
        return r.read()


# ---- 見分け方（index.html と同じ） ----
GT_N, GT_OK, SHAPE_PEN = 64, 0.18, 0.08
GT_ROT = [d * math.pi / 180 for d in (-20, -10, 0, 10, 20)]
GT_ASP = [0.7, 0.84, 1, 1.19, 1.4]
SEG_K, CORNER_DEG, STRAIGHT_DEG = 3, 45, 30


def gt_pct(d):
    return round(100 * (1 - d / (GT_OK * 2)))


def gt_norm(pts):
    L = sum(math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]) for i in range(1, len(pts)))
    if len(pts) < 2 or L < 1:
        return None
    step, out, D, p = L / (GT_N - 1), [pts[0]], 0.0, list(pts)
    i = 1
    while i < len(p):
        d = math.hypot(p[i][0] - p[i - 1][0], p[i][1] - p[i - 1][1])
        if D + d >= step and d > 0:
            t = (step - D) / d
            q = (p[i - 1][0] + t * (p[i][0] - p[i - 1][0]), p[i - 1][1] + t * (p[i][1] - p[i - 1][1]))
            out.append(q); p.insert(i, q); D = 0.0
        else:
            D += d
        i += 1
    while len(out) < GT_N:
        out.append(p[-1])
    out = out[:GT_N]
    xs, ys = [q[0] for q in out], [q[1] for q in out]
    sz = max(max(xs) - min(xs), max(ys) - min(ys)) or 1
    cx, cy = sum(xs) / GT_N, sum(ys) / GT_N
    return [((x - cx) / sz, (y - cy) / sz) for x, y in out]


def gt_variants(n):
    out = []
    for r in GT_ROT:
        for a in GT_ASP:
            c, si, sx = math.cos(r), math.sin(r), math.sqrt(a)
            sy = 1 / sx
            v = [((x * c - y * si) * sx, (x * si + y * c) * sy) for x, y in n]
            xs, ys = [q[0] for q in v], [q[1] for q in v]
            sz = max(max(xs) - min(xs), max(ys) - min(ys)) or 1
            out.append([(x / sz, y / sz) for x, y in v])
    return out


def gt_shape(n0):
    n = [tuple(q) for q in n0]
    for _ in range(2):
        n = [q if (i < 2 or i > len(n) - 3) else
             (sum(n[j][0] for j in range(i - 2, i + 3)) / 5, sum(n[j][1] for j in range(i - 2, i + 3)) / 5)
             for i, q in enumerate(n)]
    N = len(n)
    ang = [0.0] * N
    for i in range(SEG_K, N - SEG_K):
        a1 = math.atan2(n[i][1] - n[i - SEG_K][1], n[i][0] - n[i - SEG_K][0])
        a2 = math.atan2(n[i + SEG_K][1] - n[i][1], n[i + SEG_K][0] - n[i][0])
        d = (a2 - a1) * 180 / math.pi
        while d > 180: d -= 360
        while d < -180: d += 360
        ang[i] = d
    corners = []
    for i in range(SEG_K, N - SEG_K):
        a = abs(ang[i])
        if a > CORNER_DEG and a >= abs(ang[i - 1]) and a >= abs(ang[i + 1]):
            if corners and i - corners[-1] <= SEG_K * 2:
                if a > abs(ang[corners[-1]]):
                    corners[-1] = i
            else:
                corners.append(i)
    out, cuts = [], [0, *corners, N - 1]
    for c in range(len(cuts) - 1):
        a = cuts[c] + (SEG_K if c else 0)
        b = cuts[c + 1] - (SEG_K if c < len(cuts) - 2 else 0)
        if b - a >= 3:
            turn = sum(ang[i] / SEG_K for i in range(max(a, SEG_K), min(b, N - SEG_K - 1) + 1))
            out.append("直" if abs(turn) < STRAIGHT_DEG else "曲")
        if c < len(cuts) - 2:
            out.append("角")
    return "".join(out)


DIR_MIN, DIR_FREE, DIR_FULL, DIR_PEN = 0.4, 15, 35, 0.08


def gt_dir(n):  # 始まり → 終わりの向き（度）。丸など始まりと終わりが近い形は None
    (ax, ay), (bx, by) = n[0], n[-1]
    return None if math.hypot(bx - ax, by - ay) < DIR_MIN else math.degrees(math.atan2(by - ay, bx - ax))


def dir_pen(d1, d2):
    if d1 is None or d2 is None:
        return 0.0
    x = abs(d1 - d2) % 360
    x = 360 - x if x > 180 else x
    return DIR_PEN * min(1.0, max(0.0, (x - DIR_FREE) / (DIR_FULL - DIR_FREE)))


def shape_pen(a, b):  # 1 本だけの線の「直」と「曲」は描き方でゆれるので軽く
    if a == b:
        return 0.0
    return SHAPE_PEN / 4 if {a, b} <= {"直", "曲"} else SHAPE_PEN


def gt_dist(vs, my_shape, t, my_dir=False):
    best = 9.0
    for v in vs:
        d = sum(math.hypot(v[k][0] - t[k][0], v[k][1] - t[k][1]) for k in range(GT_N)) / GT_N
        best = min(best, d)
    return best + shape_pen(gt_shape(t), my_shape) + (0 if my_dir is False else dir_pen(my_dir, gt_dir(t)))


def recognize(pts):
    n = gt_norm(pts)
    if not n:
        return None, []
    try:
        entries = json.loads(GEST_FILE.read_text())
    except (OSError, ValueError):
        entries = []
    vs, sh, dr = gt_variants(n), gt_shape(n), gt_dir(n)
    rank = sorted(({"e": e, "d": min(gt_dist(vs, sh, t, dr) for t in e["samples"])} for e in entries if e.get("samples")),
                  key=lambda r: r["d"])
    return sh, rank


# ---- 本体 ----
class Grab:
    def __init__(self):
        self.fd = None

    def open(self):
        if self.fd is None:
            path = find_dev("keyd virtual pointer")
            if path:
                self.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        return self.fd

    def grab(self, on):
        if self.fd is not None:
            try:
                fcntl.ioctl(self.fd, EVIOCGRAB, 1 if on else 0)
            except OSError as e:
                log(f"grab {on}: {e}")


def finish(layer, pts, path_len, used):
    if used:
        log(f"{layer}: ほかのキー・クリックがあったので何もしない")
        return
    if path_len < MIN_PATH:
        post("/kbd", {"op": "press", "code": LAYERS[layer], "mods": []})
        return
    sh, rank = recognize(pts)
    if not rank:
        log(f"{layer}: 描いた（{sh}）がお手本が無い")
        return
    top = rank[0]
    e, d = top["e"], top["d"]
    thr = e.get("thr") if e.get("thr") is not None else GT_OK
    ok = d < thr
    e2 = rank[1]["e"] if len(rank) > 1 else None  # 2 番目が自分の境目に届いていない・同じ動作なら迷わない
    close = (ok and e2 is not None and rank[1]["d"] < (e2.get("thr") if e2.get("thr") is not None else GT_OK)
             and not (e2.get("api") and e2.get("label") == e.get("label")) and gt_pct(d) - gt_pct(rank[1]["d"]) < 10)
    who = f'{e.get("no")} 番「{e.get("name", "")}」 {gt_pct(d)} 点（境目 {gt_pct(thr)}）'
    if not ok or close:
        log(f"{layer}: {'？' if close else '✗'} {sh} → {who}")
        return
    if not e.get("api"):
        log(f"{layer}: ✓ {sh} → {who}：動作なし")
        return
    post("/gesture", {"api": e["api"], "body": e["body"]})
    log(f"{layer}: ✓ {sh} → {who}：{e.get('label')}")


def main():
    listen = subprocess.Popen(["keyd.rvaiya", "listen"], stdout=subprocess.PIPE, text=True, bufsize=1)
    kbd_path = find_dev("keyd virtual keyboard")
    kfd = os.open(kbd_path, os.O_RDONLY | os.O_NONBLOCK) if kbd_path else None
    ptr = Grab()
    cur = None  # 押している間：{layer, x, y, pts, len, used}
    log("起動")
    while True:
        pfd = ptr.open()
        fds = [listen.stdout] + [f for f in (kfd, pfd) if f is not None]
        r, _, _ = select.select(fds, [], [], 5)
        if listen.poll() is not None:
            log("keyd listen が終わった")
            sys.exit(1)
        if listen.stdout in r:
            line = listen.stdout.readline().strip()
            name = line[1:]
            if name in LAYERS:
                if line[0] == "+" and cur is None:
                    cur = {"layer": name, "x": 0, "y": 0, "pts": [(0, 0)], "len": 0.0, "used": False}
                    ptr.grab(True)
                elif line[0] == "-" and cur and cur["layer"] == name:
                    ptr.grab(False)
                    c, cur = cur, None
                    try:
                        finish(c["layer"], c["pts"], c["len"], c["used"])
                    except Exception as ex:  # 送れなくても止まらない
                        log(f"失敗: {ex!r}")
        if kfd is not None and kfd in r:
            try:
                data = os.read(kfd, EV.size * 64)
            except OSError:
                data = b""
            for i in range(0, len(data) - EV.size + 1, EV.size):
                _, _, typ, code, val = EV.unpack_from(data, i)
                if cur and typ == EV_KEY and val == 1:
                    cur["used"] = True
        if pfd is not None and pfd in r:
            try:
                data = os.read(pfd, EV.size * 64)
            except OSError:  # 装置が消えた（keyd の起動し直しなど）
                os.close(pfd); ptr.fd = None
                continue
            for i in range(0, len(data) - EV.size + 1, EV.size):
                _, _, typ, code, val = EV.unpack_from(data, i)
                if not cur:
                    continue
                if typ == EV_REL and code in (0, 1):
                    if code == 0: cur["x"] += val
                    else: cur["y"] += val
                elif typ == EV_SYN:
                    lx, ly = cur["pts"][-1]
                    if (cur["x"], cur["y"]) != (lx, ly):
                        cur["len"] += math.hypot(cur["x"] - lx, cur["y"] - ly)
                        cur["pts"].append((cur["x"], cur["y"]))
                elif typ == EV_KEY and val == 1:
                    cur["used"] = True


if __name__ == "__main__":
    main()
