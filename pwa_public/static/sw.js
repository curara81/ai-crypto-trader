// AI 트레이더 (공유용) Service Worker — v5.3
// 캐시 이름은 auto_deploy.sh가 매 배포마다 commit hash로 갱신 (sed)
const CACHE = "ai-trader-public-v5.3-a31dc62";
const ASSETS = [
  "/",
  "/static/style.css",
  "/static/app.js",
  "/manifest.json",
];

self.addEventListener("install", e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(ASSETS).catch(() => {}))
  );
  // 새 SW 즉시 활성화 (구 SW 종료 안 기다림)
  self.skipWaiting();
});

self.addEventListener("activate", e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

// 클라이언트 메시지 (수동 강제 갱신용)
self.addEventListener("message", e => {
  if (e.data && e.data.type === "SKIP_WAITING") {
    self.skipWaiting();
  }
});

// 정적 자원: stale-while-revalidate (즉시 캐시 응답 + 백그라운드에서 갱신)
// API 요청: network-only (실시간성)
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);

  // API는 항상 네트워크
  if (url.pathname.startsWith("/api/")) {
    return;
  }

  // GET이 아니면 통과
  if (e.request.method !== "GET") return;

  // 정적 자원 또는 루트
  const isStatic = url.pathname === "/" || url.pathname.startsWith("/static/") || url.pathname === "/manifest.json";
  if (!isStatic) return;

  // stale-while-revalidate: 캐시 즉시 응답 + 백그라운드에서 fetch + 새 응답 캐시
  e.respondWith(
    caches.open(CACHE).then(cache =>
      cache.match(e.request).then(cached => {
        const fetchPromise = fetch(e.request).then(resp => {
          if (resp && resp.status === 200) {
            cache.put(e.request, resp.clone());
          }
          return resp;
        }).catch(() => cached);  // 네트워크 fail이면 캐시 사용
        return cached || fetchPromise;
      })
    )
  );
});
