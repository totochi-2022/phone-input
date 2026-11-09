# phone-input の Windows 用入力ヘルパー（WSL の server.py から常駐させて使う）
# 標準入力から 1 行 1 命令を受け取り、SendInput で今フォーカスしているウィンドウに送る。
#   text <base64(UTF-8)>     文字を打つ（日本語も可）
#   key <修飾VK,...> <VK>    キーを押す（例: key 11 43 = Ctrl+C、key - 0D = Enter。16 進）
#   move <dx> <dy>           マウスを相対移動
#   scroll <n>               ホイール（正=下、wlrctl と同じ向き）
#   click left|right|middle  クリック
# 1 命令ごとに "ok" か "err <内容>" を 1 行返す。
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [Text.Encoding]::UTF8  # エラーメッセージ（日本語）を WSL 側で読めるように
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class PhoneInput {
    [StructLayout(LayoutKind.Sequential)] struct MOUSEINPUT { public int dx, dy, mouseData, dwFlags, time; public IntPtr extra; }
    [StructLayout(LayoutKind.Sequential)] struct KEYBDINPUT { public ushort vk, scan; public int dwFlags, time; public IntPtr extra; }
    [StructLayout(LayoutKind.Explicit)] struct UNION { [FieldOffset(0)] public MOUSEINPUT mi; [FieldOffset(0)] public KEYBDINPUT ki; }
    [StructLayout(LayoutKind.Sequential)] struct INPUT { public int type; public UNION u; }
    [DllImport("user32.dll", SetLastError = true)] static extern uint SendInput(uint n, INPUT[] inputs, int size);
    const int KEYUP = 2, UNICODE = 4;
    static void Send(INPUT[] a) {
        if (SendInput((uint)a.Length, a, Marshal.SizeOf(typeof(INPUT))) != a.Length)
            throw new Exception("SendInput failed (" + Marshal.GetLastWin32Error() + ")");
    }
    static INPUT Key(ushort vk, ushort scan, int flags) {
        var i = new INPUT { type = 1 }; i.u.ki.vk = vk; i.u.ki.scan = scan; i.u.ki.dwFlags = flags; return i;
    }
    static INPUT Mouse(int dx, int dy, int data, int flags) {
        var i = new INPUT { type = 0 }; i.u.mi.dx = dx; i.u.mi.dy = dy; i.u.mi.mouseData = data; i.u.mi.dwFlags = flags; return i;
    }
    public static void Text(string s) {
        foreach (char c in s.Replace("\r\n", "\n")) {
            if (c == '\n') { Press(new ushort[0], 0x0D); continue; }
            Send(new[] { Key(0, c, UNICODE), Key(0, c, UNICODE | KEYUP) });
        }
    }
    public static void Press(ushort[] mods, ushort vk) {
        var a = new System.Collections.Generic.List<INPUT>();
        foreach (var m in mods) a.Add(Key(m, 0, 0));
        a.Add(Key(vk, 0, 0)); a.Add(Key(vk, 0, KEYUP));
        for (int i = mods.Length - 1; i >= 0; i--) a.Add(Key(mods[i], 0, KEYUP));
        Send(a.ToArray());
    }
    public static void Move(int dx, int dy) { Send(new[] { Mouse(dx, dy, 0, 0x0001) }); }
    public static void Scroll(int n) { Send(new[] { Mouse(0, 0, -n * 8, 0x0800) }); }  // wlrctl の 15 ≒ 1 ノッチ(120)
    public static void Click(string b) {
        int down = b == "right" ? 0x0008 : b == "middle" ? 0x0020 : 0x0002;
        Send(new[] { Mouse(0, 0, 0, down), Mouse(0, 0, 0, down * 2) });
    }
}
"@
[Console]::Out.WriteLine("ready")
while ($null -ne ($line = [Console]::In.ReadLine())) {
    try {
        $p = $line.Split(" ")
        switch ($p[0]) {
            "text"   { [PhoneInput]::Text([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($p[1]))) }
            "key"    {
                $mods = if ($p[1] -eq "-") { [uint16[]]@() } else { [uint16[]]($p[1].Split(",") | ForEach-Object { [Convert]::ToUInt16($_, 16) }) }
                [PhoneInput]::Press($mods, [Convert]::ToUInt16($p[2], 16))
            }
            "move"   { [PhoneInput]::Move([int]$p[1], [int]$p[2]) }
            "scroll" { [PhoneInput]::Scroll([int]$p[1]) }
            "click"  { [PhoneInput]::Click($p[1]) }
            default  { throw "unknown command: $($p[0])" }
        }
        [Console]::Out.WriteLine("ok")
    } catch {
        [Console]::Out.WriteLine("err " + ($_.Exception.Message -replace "`r?`n", " "))
    }
}
