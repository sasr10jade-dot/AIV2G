/* AI-V2G 사용자 모바일웹 서비스 워커.
 *
 * 캐시 범위는 "앱 껍데기"(HTML·매니페스트·아이콘)뿐이다. 로그인 정보와 차량 데이터가 오가는
 * /owner/*, /auth/* 같은 API 응답은 respondWith를 호출하지 않고 그대로 네트워크로 보낸다 —
 * 공용 기기에서 로그아웃한 뒤에도 개인 데이터가 캐시에 남는 일이 없게 하기 위함이다.
 * 껍데기를 바꾸면 CACHE 버전을 올려 이전 캐시를 폐기한다.
 */
const CACHE = 'ai-v2g-owner-v20';
const SHELL = [
  '/app',
  '/install',
  '/manifest.webmanifest',
  '/pwa/icon-192.png',
  '/pwa/icon-512.png',
  '/pwa/apple-touch-icon.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // 화면(HTML): 네트워크 우선, 오프라인이면 캐시된 껍데기
  if (req.mode === 'navigate' && url.pathname === '/app') {
    event.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone();
          caches.open(CACHE).then((cache) => cache.put('/app', copy));
          return res;
        })
        .catch(() => caches.match('/app'))
    );
    return;
  }

  // 정적 자원(아이콘·매니페스트): 캐시 우선
  if (SHELL.includes(url.pathname)) {
    event.respondWith(caches.match(req).then((hit) => hit || fetch(req)));
  }
  // 그 외(API 포함)는 가로채지 않는다.
});

// 웹 푸시(3단계에서 서버 발송 연결) — 알림 본문은 서버가 JSON {title, body, url}로 보낸다.
self.addEventListener('push', (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = { body: event.data && event.data.text() }; }
  event.waitUntil(
    self.registration.showNotification(data.title || 'AI-V2G', {
      body: data.body || '',
      icon: '/pwa/icon-192.png',
      badge: '/pwa/icon-192.png',
      data: { url: data.url || '/app' },
    })
  );
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || '/app';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((wins) => {
      for (const w of wins) {
        if (new URL(w.url).pathname === '/app' && 'focus' in w) return w.focus();
      }
      return self.clients.openWindow(target);
    })
  );
});
