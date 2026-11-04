# phone-input

スマホのブラウザから、Raspberry Pi（doronjo）で今フォーカス中のウィンドウに文字やキーを送るページ。
テレビの前で、スマホの音声入力を使って入力するためのもの。

- `server.py`：Python 標準ライブラリだけの小さな Web サーバー（ポート 8765）。`wtype` でキー入力、`wlrctl` でマウス操作
- `index.html`：スマホで開くページ。音声入力の文字送信、Enter/矢印/F7/F8・Vimium キーのボタン、直打ち欄、
  自動送信（まとめて入力されたら 1 秒後に送信）、タッチパッド（右の帯は押している間スクロール）
- `token`：合言葉（初回起動時に自動生成。**git 管理外**）。スマホで `http://192.168.0.200:8765/?t=<token>` を開く

## 起動
```sh
python3 ~/phone-input/server.py
```
labwc の autostart で自動起動している。`index.html` を変えたら server.py の再起動が必要（起動時に読み込むため）。

## 置き場所
実体は `~/work/phone-input`、`~/phone-input` はそこへのシンボリックリンク。

## 必要なもの
`wtype`、`wlrctl`（apt）、Wayland（labwc）
