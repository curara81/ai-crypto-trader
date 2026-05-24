// AI 트레이더 Service Worker — 최소 캐시 (정적 자원만)
// 캐시 이름은 auto_deploy.sh가 매 배포마다 commit hash로 갱신 (sed)
const CACHE = "ai-trader-v1-initial";
const ASSETS = [
  "/",
  "/static/style.css",
  "/static/app.js",
  "/static/manifest.json",
];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS).catch(() => {})));
  self.skipWaiting();
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
  );
  self.clients.claim();
});

// 정적 자원: cache-first. API 요청: network-only (실시간성)
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith("/api/")) {
    // API는 항상 네트워크
    return;
  }
  // 정적: 캐시 우선
  if (e.request.method === "GET" && ASSETS.some(a => url.pathname === a || url.pathname.startsWith("/static/"))) {
    e.respondWith(
      caches.match(e.request).then(r => r || fetch(e.request).then(resp => {
        const clone = resp.clone();
        caches.open(CACHE).then(c => c.put(e.request, clone));
        return resp;
      }))
    );
  }
});
