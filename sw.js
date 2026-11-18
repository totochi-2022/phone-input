// アプリ（ホーム画面に追加）として入れるための最小の Service Worker。ページは覚えず、毎回サーバーから読む
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
