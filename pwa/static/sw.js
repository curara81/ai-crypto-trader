// AI 트레이더 Service Worker — stale-while-revalidate (v7.1)
// 캐시 이름은 auto_deploy.sh가 매 배포마다 commit hash로 갱신 (sed)
const CACHE = "ai-trader-v1-a31dc62";
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
      .then(() => self.clients.claim())
  );
});

// v5.3.2: 클라이언트가 SKIP_WAITING 메시지 보내면 즉시 활성화
self.addEventListener("message", e => {
  if (e.data && e.data.type === "SKIP_WAITING") {
    self.skipWaiting();
  }
});

// v7.1: cache-first → stale-while-revalidate 전환.
// cache-first는 캐시명이 갱신되지 않으면 구버전을 영원히 서빙하는 문제가 있어
// (배포 시 auto_deploy.sh를 거치지 않으면 발생) 백그라운드 갱신 방식으로 교체.
// API 요청: network-only (실시간성)
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);

  // API는 항상 네트워크
  if (url.pathname.startsWith("/api/")) {
    return;
  }

  if (e.request.method !== "GET") return;

  const isStatic = url.pathname === "/" || url.pathname.startsWith("/static/") || url.pathname === "/manifest.json";
  if (!isStatic) return;

  // stale-while-revalidate: 캐시 즉시 응답 + 백그라운드 fetch + 새 응답 캐시
  e.respondWith(
    caches.open(CACHE).then(cache =>
      cache.match(e.request).then(cached => {
        const fetchPromise = fetch(e.request).then(resp => {
          if (resp && resp.status === 200) {
            cache.put(e.request, resp.clone());
          }
          return resp;
        }).catch(() => cached);
        return cached || fetchPromise;
      })
    )
  );
});
