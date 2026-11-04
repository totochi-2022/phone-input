# phone-input

スマホのブラウザから、Linux（Wayland）の PC で**今フォーカスしているウィンドウ**に文字・キー・マウス操作を送るページ。
テレビにつないだ PC を、ソファーからスマホの音声入力で操作するために作った（作者は Raspberry Pi 5 + labwc で使用）。

- `server.py`：Python 標準ライブラリだけの小さな Web サーバー（ポート 8765）。`wtype` でキー入力、`wlrctl` でマウス操作
- `index.html`：スマホで開くページ
  - 音声入力などで書いた文字の送信、自動送信（まとめて入力されたら 1 秒後に送信）
  - Enter / BackSpace / Tab / Esc / 矢印 / PageUp・PageDown / Ctrl+C などのボタン
  - 直打ち欄（1 文字ずつ送信）
  - タッチパッド（マウス移動・クリック。右の帯は押している間スクロール）

## 必要なもの
- Wayland の **wlroots 系**のコンポジタ（labwc、sway など）。GNOME / KDE では wtype・wlrctl が動かない
- `python3`、`wtype`、`wlrctl`（Debian / Raspberry Pi OS なら `sudo apt install wtype wlrctl`）

## 使い方
```sh
python3 ~/phone-input/server.py
```
起動すると `http://192.168.0.200:8765/?t=<合言葉>` のような URL を表示する（`192.168.0.200` は作者の環境の IP。
自分の PC の IP に読み替える。`server.py` の最後の行で表示を変えられる）。スマホでその URL を開く。

- 合言葉は初回起動時に `token` ファイルとして自動生成される（git 管理外）
- SSH や tmux から起動しても画面に届くよう、`WAYLAND_DISPLAY`（既定 `wayland-0`）と `XDG_RUNTIME_DIR` を補っている
- `index.html` は起動時に読み込むので、変えたら server.py を再起動する
- ログイン時に自動起動するなら、labwc なら `~/.config/labwc/autostart` に
  `python3 /path/to/phone-input/server.py >/dev/null 2>&1 &` を足す
- ボタンのキーは `server.py` の `KEYS` と `index.html` で増やせる（作者の F7 / F8、Vimium 用ボタンなどが入っている）

## 安全上の注意
- **合言葉を知っている人は、その PC に文字を打ち込める＝ターミナルが手前にあればコマンドを実行できる。**
- 通信は暗号化されていない HTTP で、合言葉は URL に入る（スマホのブラウザ履歴に残る）。
- **家の LAN の中だけで使うこと。ルーターでポートを外に開けない・公衆 Wi-Fi では使わない。**
- 打った内容はログに残さない（`log_message` を無効にしている）。

## ライセンス
[MIT](LICENSE)
